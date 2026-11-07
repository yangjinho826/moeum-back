"""W6. 박제 중복·매매 동시성 — 같은 요청이 겹쳐도 숫자가 두 배가 되거나 보유보다 많이 팔리지 않는다.

**공개계약**
- 수동 박제는 "같은 달 또 눌러도 안전"(upsert) — `create_target_month_snapshot` docstring
- 전량 매도 시 종목 soft delete, 보유보다 많이 팔면 BAD_REQUEST — `sell`
- 박제 행은 (통장, 월) / (종목, 월) 당 활성 1개 — yearly 합계가 통장별 행을 그대로 더한다(`_build_month`)

동시 요청은 HTTP 로 각자 독립 커넥션을 탄다(Idempotency-Key 없음 — 미들웨어가 두 번째를 막지 않게).
"""
from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.core.database import async_session
from app.core.enums.data_status import DataStatus
from app.domain.account.enum import AccountType
from app.domain.account_snapshot.model import AccountSnapshot
from app.domain.account_snapshot.repository import AccountSnapshotRepository
from app.domain.market_price import service as market_price_service
from app.domain.portfolio import service as portfolio_service
from app.domain.portfolio.enum import Market, PortfolioTxType
from app.domain.portfolio.model import PortfolioItem, PortfolioTransaction
from app.domain.portfolio.repository import PortfolioItemRepository, PortfolioTransactionRepository
from app.domain.portfolio.schema import (
    PortfolioBuyRequest,
    PortfolioCreateRequest,
    PortfolioSellRequest,
    PortfolioTxUpdateRequest,
)
from app.main import app
from tests.fixtures.factory import (
    TxContext,
    account_factory,
    household_factory,
    seed_transaction_context,
    user_factory,
)

# 두 요청이 경합 지점에서 만나길 기다리는 최대 시간. lock 이 있으면 두 번째는 못 오므로 이만큼 기다리고 진행
_MEET_TIMEOUT = 1.0


def _meet_after(monkeypatch, *targets: tuple[type, str]) -> None:
    """각 owner.name 을 '원래 결과를 읽은 뒤 상대 요청을 기다리게' 감싼다(같은 barrier 공유).

    확인-후-실행 경합을 운 없이 재현한다. 두 요청이 서로 다른 지점에서 만나야 하면 target 을 둘 준다."""
    barrier = asyncio.Barrier(2)

    def wrap(original):
        async def wrapped(*args, **kwargs):
            result = await original(*args, **kwargs)
            try:
                await asyncio.wait_for(barrier.wait(), _MEET_TIMEOUT)
            except (TimeoutError, asyncio.BrokenBarrierError):
                pass  # 상대가 lock 에 막혀 못 옴 = 직렬화됨
            return result
        return wrapped

    for owner, name in targets:
        monkeypatch.setattr(owner, name, wrap(getattr(owner, name)))


async def _call(ctx, method: str, path: str, body: dict | None = None):
    """요청마다 새 client — 각자 독립 DB 커넥션. Idempotency-Key 없음."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.request(method, path, json=body, headers=ctx.auth_headers)


async def _post(ctx, path: str, body: dict | None = None):
    return await _call(ctx, "POST", path, body)


async def _count_active(model) -> int:
    async with async_session() as session:
        stmt = select(func.count()).select_from(model).where(model.data_stat_cd == DataStatus.ACTIVE)
        return (await session.execute(stmt)).scalar_one()


@pytest.fixture
def no_yahoo(monkeypatch):
    async def _skip(*args, **kwargs):
        return None

    monkeypatch.setattr(market_price_service, "backfill_yahoo_monthly", _skip)


@pytest.fixture
async def snap_ctx(db):
    ctx = await seed_transaction_context(db)  # 통장 1개 + commit
    await account_factory(db, household=ctx.household, name="비상금 통장")
    await db.commit()
    return ctx


async def _invest_ctx(db, *, bought: str | None) -> tuple[TxContext, PortfolioItem]:
    """투자 통장 + 종목(bought 주 매수) 을 커밋 — 동시 요청이 독립 커넥션에서 보게."""
    ctx = await seed_transaction_context(db)
    invest = await account_factory(
        db, household=ctx.household, name="증권", account_type=AccountType.INVESTMENT,
    )
    item = await portfolio_service.create_portfolio(
        db, ctx.household,
        PortfolioCreateRequest(
            name="삼성전자", code="005930", market=Market.KRX_KOSPI,
            currentPrice=Decimal("60000"), accountId=invest.id,
        ),
    )
    if bought:
        await portfolio_service.buy(
            db, ctx.household, item.id,
            PortfolioBuyRequest(quantity=Decimal(bought), price=Decimal("50000"), txDate=date(2026, 9, 1)),
        )
    await db.commit()
    return ctx, item


async def _load_item(item_id) -> PortfolioItem:
    async with async_session() as session:
        return await session.get(PortfolioItem, item_id)


async def _count_sells(item_id) -> int:
    async with async_session() as session:
        stmt = select(func.count()).select_from(PortfolioTransaction).where(
            PortfolioTransaction.portfolio_item_id == item_id,
            PortfolioTransaction.pt_type == PortfolioTxType.SELL,
            PortfolioTransaction.data_stat_cd == DataStatus.ACTIVE,
        )
        return (await session.execute(stmt)).scalar_one()


async def test_W6_4_같은_통장_같은_달_활성_박제는_두_개를_못_넣는다(db):
    user = await user_factory(db)
    household = await household_factory(db, owner=user)
    account = await account_factory(db, household=household)

    for _ in range(2):
        db.add(AccountSnapshot(
            account_id=account.id, snapshot_date=date(2026, 10, 1), balance=Decimal("1000"),
            data_stat_cd=DataStatus.ACTIVE,
        ))

    with pytest.raises(IntegrityError):
        await db.flush()


async def test_W6_1_수동_박제를_동시에_두_번_눌러도_통장마다_한_벌만_남는다(snap_ctx, no_yahoo, monkeypatch):
    _meet_after(monkeypatch, (AccountSnapshotRepository, "has_active_for_month"))

    a, b = await asyncio.gather(
        _post(snap_ctx, "/account-snapshot/create"),
        _post(snap_ctx, "/account-snapshot/create"),
    )

    assert (a.status_code, b.status_code) == (200, 200)
    assert await _count_active(AccountSnapshot) == 2


async def test_W6_6_수동_박제를_차례로_두_번_해도_한_벌이다(snap_ctx, no_yahoo):
    for _ in range(2):
        assert (await _post(snap_ctx, "/account-snapshot/create")).status_code == 200

    assert await _count_active(AccountSnapshot) == 2


async def test_W6_2_보유_10주를_동시에_10주씩_팔아도_한_번만_팔린다(db, monkeypatch):
    ctx, item = await _invest_ctx(db, bought="10")
    _meet_after(monkeypatch, (PortfolioTransactionRepository, "save"))
    body = {"quantity": "10", "sellPrice": "60000", "txDate": "2026-10-01"}

    a, b = await asyncio.gather(
        _post(ctx, f"/portfolio/sell/{item.id}", body),
        _post(ctx, f"/portfolio/sell/{item.id}", body),
    )

    assert sorted([a.status_code, b.status_code])[0] == 200
    assert sorted([a.status_code, b.status_code])[1] in (400, 404)
    assert await _count_sells(item.id) == 1
    assert (await _load_item(item.id)).quantity == 0


async def test_W6_5_빈_종목에_매수와_삭제가_겹쳐도_삭제된_종목에_수량이_남지_않는다(db, monkeypatch):
    ctx, item = await _invest_ctx(db, bought=None)
    # 둘 다 종목을 잠근 직후 만난다 — lock 이 빠지면 둘 다 수량 0 을 읽은 채 만나 RED
    _meet_after(monkeypatch, (PortfolioItemRepository, "find_by_id_for_update"))
    buy_body = {"quantity": "10", "price": "50000", "txDate": "2026-10-01"}

    await asyncio.gather(
        _post(ctx, f"/portfolio/buy/{item.id}", buy_body),
        _call(ctx, "DELETE", f"/portfolio/delete/{item.id}"),
    )

    after = await _load_item(item.id)
    assert not (after.data_stat_cd == DataStatus.DELETED and after.quantity > 0)


async def test_W6_3_매도_수량_수정과_앞선_날짜_매수가_겹쳐도_데드락_없이_둘_다_반영된다(db, monkeypatch):
    ctx, item = await _invest_ctx(db, bought="10")  # 9/1 10주 @50,000
    sold = await portfolio_service.sell(
        db, ctx.household, item.id,
        PortfolioSellRequest(quantity=Decimal("2"), sellPrice=Decimal("60000"), txDate=date(2026, 10, 1)),
    )
    await db.commit()
    s1 = (await portfolio_service.list_item_transactions_cursor(db, ctx.household, item.id, None, 20)).items[0]
    assert sold is not None and s1.pt_type == PortfolioTxType.SELL
    # A(PUT) 는 S1 을 읽은 직후, B(매수) 는 종목을 잠그고 거래를 넣은 뒤 만난다. 종목 lock 이 S1 쓰기보다
    # 늦으면 A 는 S1 행을, B 는 종목을 쥔 채 서로를 기다린다(B 는 평단이 바뀌어 S1 실현손익을 다시 쓴다)
    _meet_after(
        monkeypatch,
        (PortfolioTransactionRepository, "find_by_id"),
        (PortfolioTransactionRepository, "save"),
    )

    a, b = await asyncio.gather(
        _call(ctx, "PUT", f"/portfolio/transactions/{s1.id}", {"quantity": "3"}),
        _post(ctx, f"/portfolio/buy/{item.id}", {"quantity": "5", "price": "40000", "txDate": "2026-09-15"}),
    )

    assert (a.status_code, b.status_code) == (200, 200)
    assert (await _load_item(item.id)).quantity == Decimal("12")  # 10 + 5 − 3


async def test_W6_8_lock_을_기다리는_동안_바뀐_거래값으로_재계산한다(db):
    """B 가 거래를 읽은 뒤 A 가 수량을 바꿔 커밋 → B(메모만 수정)의 재계산이 옛 수량을 쓰면 안 된다.

    lock 대기 중 끼어든 커밋을 두 세션으로 결정적으로 재현한다(db = B, 새 세션 = A)."""
    ctx, item = await _invest_ctx(db, bought="10")
    buy_tx = (await portfolio_service.list_item_transactions_cursor(db, ctx.household, item.id, None, 20)).items[0]
    # B 가 먼저 읽어 둔 상태(수량 10). 변수로 붙잡아야 identity map(약한 참조)에 남는다 — 실제 요청도 tx 를 쥐고 있다
    held = await PortfolioTransactionRepository(db).find_by_id(buy_tx.id)

    async with async_session() as a:
        await portfolio_service.update_portfolio_transaction(
            a, ctx.household, buy_tx.id, PortfolioTxUpdateRequest(quantity=Decimal("5")),
        )
        await a.commit()

    await portfolio_service.update_portfolio_transaction(
        db, ctx.household, buy_tx.id, PortfolioTxUpdateRequest(memo="메모"),
    )
    await db.commit()

    assert held.quantity == Decimal("5")
    assert (await _load_item(item.id)).quantity == Decimal("5")

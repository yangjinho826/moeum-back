"""W2. OTHER(금 등) 시세 키 — 종목 단위로 저장·조회.

**공개계약**
- `market_price_history` unique = (code, market, price_date) — `market_price/model.py`
- OTHER 종목은 code 빈 문자열 허용 — `portfolio/schema.py` PortfolioCreateRequest
- `snapshot_other_prices` = 박제 시점 수동가(current_price)를 그 달 시세로 — `market_price/service.py`
- `value_holdings_at_month` = 그 달 시가로 평가, 시가 없으면 원가 — 같은 파일

code 를 키로 쓰면 code 없는 OTHER 종목들이 한 키("")를 공유해 ① 한 INSERT 안에서 충돌로
잡 전체가 롤백되고 ② 남의 가계부 수동가로 평가된다. 키를 종목(item_id)으로 바꾼 것을 고정한다.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select

from app.domain.market_price import service as market_price_service
from app.domain.market_price.model import MarketPriceHistory
from app.domain.market_price.repository import MarketPriceHistoryRepository
from app.domain.portfolio import service as portfolio_service
from app.domain.portfolio.enum import Market
from app.domain.portfolio.schema import PortfolioBuyRequest, PortfolioCreateRequest
from tests.fixtures.factory import PortfolioContext, seed_portfolio_context

NOV = date(2026, 11, 1)
OCT = date(2026, 10, 1)


async def _other_item(db, ctx: PortfolioContext, *, name: str, code: str, price: str) -> UUID:
    """OTHER 종목 등록 + 1단위 매수(원가 100,000) — as-of 보유를 만든다."""
    item = await portfolio_service.create_portfolio(
        db,
        ctx.household,
        PortfolioCreateRequest(
            name=name,
            code=code,
            market=Market.OTHER,
            currentPrice=Decimal(price),
            accountId=ctx.account.id,
        ),
    )
    await portfolio_service.buy(
        db, ctx.household, item.id,
        PortfolioBuyRequest(quantity=Decimal("1"), price=Decimal("100000"), txDate=date(2026, 9, 1)),
    )
    return item.id


async def _history_rows(db, month: date) -> dict[str, Decimal]:
    rows = (
        await db.execute(
            select(MarketPriceHistory.code, MarketPriceHistory.price).where(
                MarketPriceHistory.market == Market.OTHER.value,
                MarketPriceHistory.price_date == month,
            )
        )
    ).all()
    return {code: Decimal(price) for code, price in rows}


async def _holdings(db, ctx: PortfolioContext, as_of: date) -> list[dict]:
    from app.domain.portfolio.repository import PortfolioTransactionRepository

    return await PortfolioTransactionRepository(db).asof_holdings_by_account(ctx.account.id, as_of)


async def test_W2_1_가계부가_달라도_code_없는_OTHER_시세가_충돌하지_않는다(db):
    # given: 두 가계부에 code "" OTHER 종목 하나씩
    a = await seed_portfolio_context(db)
    b = await seed_portfolio_context(db)
    gold = await _other_item(db, a, name="금", code="", price="145000")
    silver = await _other_item(db, b, name="은", code="", price="1200")

    # when: 월 1일 잡처럼 가계부 전체 박제
    await market_price_service.snapshot_other_prices(db, NOV)

    # then: 종목마다 한 행 — 키는 item_id
    assert await _history_rows(db, NOV) == {
        str(gold): Decimal("145000.00"),
        str(silver): Decimal("1200.00"),
    }


async def test_W2_5_한_가계부에_code_없는_OTHER_가_둘이어도_수동_박제가_된다(db):
    ctx = await seed_portfolio_context(db)
    gold = await _other_item(db, ctx, name="금", code="", price="145000")
    silver = await _other_item(db, ctx, name="은", code="", price="1200")

    await market_price_service.snapshot_other_prices(db, NOV, ctx.household.id)

    assert await _history_rows(db, NOV) == {
        str(gold): Decimal("145000.00"),
        str(silver): Decimal("1200.00"),
    }


async def test_W2_2_각_가계부는_자기_종목_수동가로_평가된다(db):
    a = await seed_portfolio_context(db)
    b = await seed_portfolio_context(db)
    gold = await _other_item(db, a, name="금", code="", price="145000")
    silver = await _other_item(db, b, name="은", code="", price="1200")
    await market_price_service.snapshot_other_prices(db, NOV)

    valued_a = await market_price_service.value_holdings_at_month(
        db, await _holdings(db, a, date(2026, 11, 30)), NOV,
    )
    valued_b = await market_price_service.value_holdings_at_month(
        db, await _holdings(db, b, date(2026, 11, 30)), NOV,
    )

    assert valued_a[gold]["valuation"] == Decimal("145000")
    assert valued_b[silver]["valuation"] == Decimal("1200")


async def test_W2_3_code_가_있어도_옛_시세는_가계부_공유_키라_안_읽는다(db):
    # given: 옛 키 ("GOLD", OTHER, 10월) 에 다른 가계부 값 540,000 이 남아 있음
    ctx = await seed_portfolio_context(db)
    gold = await _other_item(db, ctx, name="금", code="GOLD", price="160000")
    await MarketPriceHistoryRepository(db).upsert_prices(
        [{"code": "GOLD", "market": Market.OTHER.value, "price_date": OCT, "price": Decimal("540000")}]
    )

    valued = await market_price_service.value_holdings_at_month(
        db, await _holdings(db, ctx, date(2026, 10, 31)), OCT,
    )

    # then: 원가(1단위 × 100,000) — 과거 달 기록값은 backfill 리비전이 새 키로 옮긴다
    assert valued[gold]["valuation"] == Decimal("100000.00")


async def test_W2_6_code_없는_옛_시세는_공유_키라_무시하고_원가로_평가한다(db):
    # given: 데모 리셋이 덮어쓴 ("", OTHER, 10월) = 145,000
    ctx = await seed_portfolio_context(db)
    gold = await _other_item(db, ctx, name="금", code="", price="160000")
    await MarketPriceHistoryRepository(db).upsert_prices(
        [{"code": "", "market": Market.OTHER.value, "price_date": OCT, "price": Decimal("145000")}]
    )

    valued = await market_price_service.value_holdings_at_month(
        db, await _holdings(db, ctx, date(2026, 10, 31)), OCT,
    )

    # then: 원가(1단위 × 100,000)
    assert valued[gold]["valuation"] == Decimal("100000.00")


async def test_W2_4_야후_종목은_code_키_그대로_평가한다(db):
    ctx = await seed_portfolio_context(db)
    item = await portfolio_service.create_portfolio(
        db,
        ctx.household,
        PortfolioCreateRequest(
            name="삼성전자", code="005930", market=Market.KRX_KOSPI,
            currentPrice=Decimal("70000"), accountId=ctx.account.id,
        ),
    )
    await portfolio_service.buy(
        db, ctx.household, item.id,
        PortfolioBuyRequest(quantity=Decimal("2"), price=Decimal("60000"), txDate=date(2026, 9, 1)),
    )
    await MarketPriceHistoryRepository(db).upsert_prices(
        [{"code": "005930", "market": Market.KRX_KOSPI.value, "price_date": OCT, "price": Decimal("65000")}]
    )

    valued = await market_price_service.value_holdings_at_month(
        db, await _holdings(db, ctx, date(2026, 10, 31)), OCT,
    )

    assert valued[item.id]["valuation"] == Decimal("130000")


async def test_W2_7_데모_리셋을_반복해도_OTHER_시세_행이_쌓이지_않는다(db, monkeypatch):
    from app.core.demo import seed
    from app.domain.portfolio.model import PortfolioItem

    async def _no_yahoo(*_args, **_kwargs):  # 외부 호출 차단 — 원가 fallback 으로 시딩
        return 0

    monkeypatch.setattr(market_price_service, "backfill_yahoo_monthly", _no_yahoo)

    # when: 05:00 리셋 두 번
    await seed.seed_demo(db)
    await seed.seed_demo(db)

    # then: OTHER 시세 행 = 지금 살아 있는 데모 OTHER 종목 수(지난 리셋 종목 행은 지워짐)
    other_items = (
        await db.execute(
            select(PortfolioItem.id).where(PortfolioItem.market == Market.OTHER.value)
        )
    ).scalars().all()
    rows = (
        await db.execute(
            select(MarketPriceHistory.code).where(MarketPriceHistory.market == Market.OTHER.value)
        )
    ).scalars().all()
    assert other_items
    assert sorted(rows) == sorted(str(i) for i in other_items)

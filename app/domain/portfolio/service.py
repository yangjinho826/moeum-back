"""Portfolio 도메인 CRUD 비즈니스 로직.

책임:
- 종목(PortfolioItem) 등록/조회/수정/소프트 삭제
- 매수/매도 액션 — qty 누적 + avg_price 재계산 + PortfolioTransaction 이력
- 야후 파이낸스 종목 조회 (저장 전 폼 자동 채움)
- PNL 계산 (cost / valuation / profit_loss / profit_loss_rate)

월별 박제(PortfolioValueHistory) 는 별도 모듈 — `snapshot_service.py` 참조.
"""

import logging
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dates import today_kst
from app.core.enums.data_status import DataStatus
from app.core.exceptions import CustomException, ErrorCode
from app.domain.account.enum import AccountType
from app.domain.account.repository import AccountRepository
from app.domain.exchange_rate.enum import CurrencyCode
from app.domain.exchange_rate.repository import CurrencyRateRepository
from app.domain.household.model import Household
from app.domain.portfolio.enum import Market, PortfolioTxType
from app.domain.portfolio.model import (
    PortfolioItem,
    PortfolioTransaction,
    PortfolioValueHistory,
)
from app.domain.portfolio.repository import (
    PortfolioItemRepository,
    PortfolioTransactionRepository,
    PortfolioValueHistoryRepository,
)
from app.domain.account.schema import AccountResponse
from app.domain.portfolio.schema import (
    AccountOverviewResponse,
    InvestmentAccountWithPortfolios,
    PortfolioBuyRequest,
    PortfolioCreateRequest,
    PortfolioFormOptionsResponse,
    PortfolioLookupResponse,
    PortfolioOverviewResponse,
    PortfolioOverviewSummary,
    PortfolioResponse,
    PortfolioSellRequest,
    PortfolioTxPage,
    PortfolioTxResponse,
    PortfolioTxUpdateRequest,
    PortfolioUpdateRequest,
    PortfolioValueHistoryByItem,
    PortfolioValueHistoryPoint,
    RealizedPnlResponse,
    RealizedPnlRow,
    RealizedPnlSummary,
)
from app.domain.portfolio.yahoo import lookup as yahoo_lookup

logger = logging.getLogger(__name__)

# USD 시장 — lookup 시 환율 적용해 KRW 환산 (market_price/service.py 와 동일 사상)
_USD_MARKETS = frozenset({Market.NASDAQ, Market.NYSE})

async def lookup_stock(
    db: AsyncSession, market: Market, code: str,
) -> PortfolioLookupResponse:
    """야후 파이낸스로 종목명 + 현재가 조회 — 폼 자동 채움용.
    USD 시장(NASDAQ/NYSE) 은 환율 적용해 KRW 환산 — DB current_price 는 항상 KRW.
    환율 미존재 시 lookup 실패 (평일 09:00 환율 갱신 잡 선행 필요).
    OTHER 는 야후 미지원 — frontend 에서 조회 버튼 숨기지만 방어용으로 거부."""
    if market == Market.OTHER:
        raise CustomException(ErrorCode.STOCK_LOOKUP_FAILED)
    name, price, yahoo_symbol = await yahoo_lookup(market, code)

    if market in _USD_MARKETS:
        fx = await CurrencyRateRepository(db).find_by_pair(
            CurrencyCode.USD, CurrencyCode.KRW,
        )
        if fx is None:
            logger.error(
                "환율 없음 — USD 종목 lookup 실패 (market=%s, code=%s)", market, code,
            )
            raise CustomException(ErrorCode.STOCK_LOOKUP_FAILED)
        price = (price * fx.rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    return PortfolioLookupResponse(
        market=market,
        code=code.strip(),
        name=name,
        current_price=price,
        yahoo_symbol=yahoo_symbol,
    )


async def create_portfolio(
    db: AsyncSession, household: Household, req: PortfolioCreateRequest,
) -> PortfolioResponse:
    """종목 등록 — 메타만 (qty=0, avg_price=0). 매수는 buy() 로"""
    await _validate_investment_account(db, household.id, req.account_id)

    item = PortfolioItem(
        household_id=household.id,
        account_id=req.account_id,
        name=req.name.strip(),
        code=req.code.strip(),
        market=req.market.value,
        quantity=Decimal("0.0000"),
        avg_price=Decimal("0.00"),
        current_price=req.current_price,
        is_archived=False,
        data_stat_cd=DataStatus.ACTIVE,
    )
    await PortfolioItemRepository(db).save(item)
    logger.info(
        "종목 등록 (account_id=%s, market=%s, code=%s, name=%s, current_price=%s)",
        req.account_id, item.market, item.code, item.name, req.current_price,
    )

    accounts = await AccountRepository(db).find_by_ids([item.account_id])
    return _build_response(item, {a.id: a for a in accounts})


async def buy(
    db: AsyncSession, household: Household, item_id: UUID, req: PortfolioBuyRequest,
) -> PortfolioResponse:
    """매수 액션 — 이력 기록 후 거래 replay 로 수량·평단 재계산"""
    item_repo = PortfolioItemRepository(db)
    pt_repo = PortfolioTransactionRepository(db)

    # 같은 종목 매매는 줄 세움 — 동시 매도가 둘 다 보유 검사를 통과하지 않게
    item = await item_repo.find_by_id_for_update(item_id)
    if not item or item.household_id != household.id:
        raise CustomException(ErrorCode.NOT_FOUND)

    # 1. 매수 이력 기록
    pt_tx = PortfolioTransaction(
        household_id=household.id,
        account_id=item.account_id,
        portfolio_item_id=item.id,
        name=item.name,
        code=item.code,
        market=item.market,
        pt_type=PortfolioTxType.BUY,
        quantity=req.quantity,
        price=req.price,
        fee=req.fee,
        tx_date=req.tx_date or today_kst(),
        memo=req.memo,
        data_stat_cd=DataStatus.ACTIVE,
    )
    await pt_repo.save(pt_tx)

    # 2. 거래 전체 replay 로 수량·평단 재계산.
    # 입력 순서 기준 누적계산(incremental)이면 과거 날짜 매수를 뒤늦게 넣었을 때
    # 저장값과 replay 값이 갈린다 — 진실 원천을 replay 하나로 통일한다.
    await _recalc_item_from_transactions(db, item)

    logger.info(
        "매수 (item_id=%s, qty=%s, price=%s, total_qty=%s, avg=%s)",
        item.id, req.quantity, req.price, item.quantity, item.avg_price,
    )

    accounts = await AccountRepository(db).find_by_ids([item.account_id])
    return _build_response(item, {a.id: a for a in accounts})


async def update_portfolio(
    db: AsyncSession, household: Household, item_id: UUID, req: PortfolioUpdateRequest,
) -> PortfolioResponse:
    """평가액/메타 수정 (transaction 무관)"""
    repo = PortfolioItemRepository(db)
    # 보관의 수량 검사가 동시 매수와 엇갈리지 않게 잠근다
    item = await repo.find_by_id_for_update(item_id)
    if not item or item.household_id != household.id:
        raise CustomException(ErrorCode.NOT_FOUND)

    if req.current_price is not None:
        item.current_price = req.current_price
    if req.name is not None:
        item.name = req.name.strip()
    if req.code is not None:
        item.code = req.code.strip()
    if req.market is not None:
        item.market = req.market.value
    if req.is_archived is not None:
        # 프론트 "삭제"는 보관(archive)으로 동작 — 보유수량>0 이면 차단(전량 매도 후 가능).
        if req.is_archived and item.quantity > 0:
            raise CustomException(ErrorCode.PORTFOLIO_HAS_HOLDINGS)
        item.is_archived = req.is_archived

    await db.flush()

    accounts = await AccountRepository(db).find_by_ids([item.account_id])
    return _build_response(item, {a.id: a for a in accounts})


async def sell(
    db: AsyncSession, household: Household, item_id: UUID, req: PortfolioSellRequest,
) -> PortfolioResponse | None:
    """매도 (부분/전량). 전량 시 portfolio_items soft delete, 응답 None"""
    item_repo = PortfolioItemRepository(db)
    pt_repo = PortfolioTransactionRepository(db)

    # 같은 종목 매매는 줄 세움 — 동시 매도가 둘 다 보유 검사를 통과하지 않게
    item = await item_repo.find_by_id_for_update(item_id)
    if not item or item.household_id != household.id:
        raise CustomException(ErrorCode.NOT_FOUND)

    if req.quantity > item.quantity:
        raise CustomException(ErrorCode.BAD_REQUEST)

    # 매도 이력 기록 — 실현손익은 아래 replay 가 매도시점 평단으로 박제한다.
    # 매수/매도 단가 모두 입력값(원화) 그대로 저장 — buy() 와 동일 기준이라 환산 불필요.
    pt_tx = PortfolioTransaction(
        household_id=household.id,
        account_id=item.account_id,
        portfolio_item_id=item.id,
        name=item.name,
        code=item.code,
        market=item.market,
        pt_type=PortfolioTxType.SELL,
        quantity=req.quantity,
        price=req.sell_price,
        fee=req.fee,
        tx_date=req.tx_date or today_kst(),
        memo=req.memo,
        data_stat_cd=DataStatus.ACTIVE,
    )
    await pt_repo.save(pt_tx)

    # 거래 전체 replay — 수량·평단 갱신 + 이 매도를 포함한 모든 SELL 실현손익 재박제.
    # 수량이 0 이 되면 replay 안에서 soft delete 된다(전량 매도).
    await _recalc_item_from_transactions(db, item)

    if item.quantity == 0:
        logger.info("전량 매도 (item_id=%s, qty=%s)", item.id, req.quantity)
        return None

    logger.info(
        "부분 매도 (item_id=%s, sold=%s, remaining=%s)",
        item.id, req.quantity, item.quantity,
    )

    accounts = await AccountRepository(db).find_by_ids([item.account_id])
    return _build_response(item, {a.id: a for a in accounts})


async def update_portfolio_transaction(
    db: AsyncSession, household: Household, tx_id: UUID, req: PortfolioTxUpdateRequest,
) -> PortfolioTxResponse:
    """거래 내역 수정 — 수정 후 해당 종목 자동 재계산"""
    pt_repo = PortfolioTransactionRepository(db)
    tx = await pt_repo.find_by_id(tx_id)
    if not tx or tx.household_id != household.id:
        raise CustomException(ErrorCode.NOT_FOUND)
    item = await _lock_item_of(db, tx)  # 거래 행을 쓰기 전에 — 순서가 반대면 매매와 서로 기다린다
    await db.refresh(tx)  # lock 을 기다리는 사이 커밋된 수정·삭제를 반영 — 옛 값으로 재계산하지 않게
    if tx.data_stat_cd != DataStatus.ACTIVE:
        raise CustomException(ErrorCode.NOT_FOUND)

    if req.quantity is not None:
        tx.quantity = req.quantity
    if req.price is not None:
        tx.price = req.price
    if req.fee is not None:
        tx.fee = req.fee
    if req.tx_date is not None:
        tx.tx_date = req.tx_date
    if req.memo is not None:
        tx.memo = req.memo
    await db.flush()

    # 종목 재계산 (tx 는 BUY/SELL 모두 portfolio_item_id 보유)
    if item:
        await _recalc_item_from_transactions(db, item)

    logger.info("거래 수정 (tx_id=%s)", tx_id)

    accounts = await AccountRepository(db).find_by_ids([tx.account_id])
    return _build_tx_response(tx, {a.id: a for a in accounts})


async def delete_portfolio_transaction(
    db: AsyncSession, household: Household, tx_id: UUID,
) -> None:
    """거래 내역 soft delete — 삭제 후 해당 종목 자동 재계산"""
    pt_repo = PortfolioTransactionRepository(db)
    tx = await pt_repo.find_by_id(tx_id)
    if not tx or tx.household_id != household.id:
        raise CustomException(ErrorCode.NOT_FOUND)
    item = await _lock_item_of(db, tx)  # 거래 행을 쓰기 전에 — 순서가 반대면 매매와 서로 기다린다
    await db.refresh(tx)  # lock 을 기다리는 사이 커밋된 수정·삭제를 반영 — 옛 값으로 재계산하지 않게
    if tx.data_stat_cd != DataStatus.ACTIVE:
        raise CustomException(ErrorCode.NOT_FOUND)

    tx.data_stat_cd = DataStatus.DELETED
    await db.flush()

    if item:
        await _recalc_item_from_transactions(db, item)

    logger.info("거래 삭제 (tx_id=%s)", tx_id)


async def delete_portfolio(
    db: AsyncSession, household: Household, item_id: UUID,
) -> None:
    """종목 soft delete (data_stat_cd='99'). value_history row 는 보존.

    보유수량>0 이면 차단 — 먼저 전량 매도해야 삭제 가능(전량 매도는 qty=0 라 통과).
    """
    repo = PortfolioItemRepository(db)
    # 수량 검사가 동시 매수와 엇갈려 "삭제됐는데 수량 남음" 이 되지 않게 잠근다
    item = await repo.find_by_id_for_update(item_id)
    if not item or item.household_id != household.id:
        raise CustomException(ErrorCode.NOT_FOUND)
    if item.quantity > 0:
        raise CustomException(ErrorCode.PORTFOLIO_HAS_HOLDINGS)
    item.data_stat_cd = DataStatus.DELETED
    await db.flush()
    logger.info("종목 삭제 (item_id=%s)", item_id)


# 수동 시세 갱신 대상 시장 — OTHER(야후 미지원) 제외.
_REFRESH_MARKETS = [
    Market.KRX_KOSPI,
    Market.KRX_KOSDAQ,
    Market.NASDAQ,
    Market.NYSE,
]


async def refresh_prices_for_household(
    db: AsyncSession, household: Household,
) -> "RefreshResult":
    """이 가계부가 보유한 종목들의 current_price 를 야후로 즉시 갱신(수동 새로고침 버튼)."""
    from app.domain.market_price import service as market_price_service

    return await market_price_service.refresh(
        db, _REFRESH_MARKETS, household_id=household.id,
    )


async def get_portfolio_detail(
    db: AsyncSession, household: Household, item_id: UUID,
) -> PortfolioResponse:
    """종목 단건 조회 — PNL 포함"""
    repo = PortfolioItemRepository(db)
    item = await repo.find_by_id(item_id)
    if not item or item.household_id != household.id or item.data_stat_cd != DataStatus.ACTIVE:
        raise CustomException(ErrorCode.NOT_FOUND)
    accounts = await AccountRepository(db).find_by_ids([item.account_id])
    return _build_response(item, {a.id: a for a in accounts})


async def get_realized_pnl_by_item(
    db: AsyncSession,
    household: Household,
    item_id: UUID,
    from_date: date | None = None,
    to_date: date | None = None,
) -> RealizedPnlResponse:
    """종목 매매손익 — 기간 내 매도 건별 실현손익 + 요약.

    증권사 '매매손익' 화면과 동일 구성. 기간 미지정 시 최근 12개월.
    구버전 SELL(realized=NULL) 은 0 으로 간주해 합계 왜곡 방지.
    전량 매도로 DELETED 된 종목도 매도 이력은 남으니 상태 무관 조회.
    """
    item = await PortfolioItemRepository(db).find_by_id_any_status(item_id)
    if not item or item.household_id != household.id:
        raise CustomException(ErrorCode.NOT_FOUND)

    tx_repo = PortfolioTransactionRepository(db)
    earliest = (
        await tx_repo.earliest_sell_date_by_item(item_id)
        if from_date is None
        else None
    )
    from_date, to_date = _default_day_range(from_date, to_date, earliest)
    sells = await tx_repo.find_sell_txs_by_item(item_id, from_date, to_date)

    rows, summary = _build_realized_pnl(sells, with_name=False)
    return RealizedPnlResponse(
        summary=summary,
        rows=rows,
        effective_from=from_date,
        effective_to=to_date,
    )


def _build_realized_pnl(
    sells: list[PortfolioTransaction], *, with_name: bool,
) -> tuple[list[RealizedPnlRow], RealizedPnlSummary]:
    """매도 거래 목록 → 건별 행 + 요약. 종목/계좌 단위가 공유하는 순수 계산.

    with_name 은 계좌 단위에서만 True — 여러 종목이 섞이므로 행에 종목명이 필요하다.
    """
    rows: list[RealizedPnlRow] = []
    total_realized = Decimal("0.00")
    total_cost = Decimal("0.00")
    total_sell = Decimal("0.00")
    total_fee = Decimal("0.00")
    for tx in sells:
        pnl = tx.realized_pnl or Decimal("0.00")
        cost = tx.realized_cost_basis or Decimal("0.00")
        rate = (pnl / cost * 100) if cost > 0 else Decimal("0.00")
        amount = tx.quantity * tx.price
        rows.append(
            RealizedPnlRow(
                tx_id=tx.id,
                tx_date=tx.tx_date,
                portfolio_item_id=tx.portfolio_item_id,
                # 종목 단위 응답은 화면 헤더에 이미 종목명이 있어 행에서는 생략한다.
                name=tx.name if with_name else None,
                code=tx.code,
                market=Market(tx.market),
                quantity=tx.quantity,
                sell_price=tx.price,
                amount=amount,
                fee=tx.fee,
                settlement=amount - tx.fee,
                memo=tx.memo,
                realized_pnl=pnl,
                realized_rate=rate.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            )
        )
        total_realized += pnl
        total_cost += cost
        total_sell += amount
        total_fee += tx.fee

    total_rate = (
        (total_realized / total_cost * 100) if total_cost > 0 else Decimal("0.00")
    )
    summary = RealizedPnlSummary(
        total_realized=total_realized,
        total_rate=total_rate.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        sell_amount=total_sell,
        buy_amount=total_cost,
        total_fee=total_fee,
    )
    return rows, summary


async def get_realized_pnl_by_account(
    db: AsyncSession,
    household: Household,
    account_id: UUID,
    from_date: date | None = None,
    to_date: date | None = None,
) -> RealizedPnlResponse:
    """계좌 누적 매매손익 — 기간 내 계좌의 모든 종목 매도 건별 실현손익 + 요약.

    전량매도로 종목이 사라져도 매도 거래는 남아 집계에 포함된다(조회 사각지대 해소).
    여러 종목이 섞이므로 row 에 종목명(name)을 채운다. 기간 미지정 시 최근 12개월.
    구버전 SELL(realized=NULL) 은 0 으로 간주해 합계 왜곡 방지.
    """
    tx_repo = PortfolioTransactionRepository(db)
    earliest = (
        await tx_repo.earliest_sell_date_by_account(account_id, household.id)
        if from_date is None
        else None
    )
    from_date, to_date = _default_day_range(from_date, to_date, earliest)
    sells = await tx_repo.find_sell_txs_by_account(
        account_id, household.id, from_date, to_date,
    )

    rows, summary = _build_realized_pnl(sells, with_name=True)
    return RealizedPnlResponse(
        summary=summary,
        rows=rows,
        effective_from=from_date,
        effective_to=to_date,
    )


async def get_value_history_by_account(
    db: AsyncSession,
    household: Household,
    account_id: UUID,
    from_date: date | None,
    to_date: date | None,
) -> list[PortfolioValueHistoryByItem]:
    """통장 단위 — 종목별 그루핑된 월별 평가액 추이"""
    accounts = await AccountRepository(db).find_by_ids([account_id])
    if not accounts or accounts[0].household_id != household.id:
        raise CustomException(ErrorCode.NOT_FOUND)

    from_date, to_date = _default_month_range(from_date, to_date)

    history_repo = PortfolioValueHistoryRepository(db)
    rows = await history_repo.find_by_account_and_range(account_id, from_date, to_date)

    if not rows:
        return []

    # 삭제된 종목도 ticker 표시 위해 직접 fetch
    item_ids = list({r.portfolio_item_id for r in rows})
    items = await PortfolioItemRepository(db).find_by_ids_including_deleted(item_ids)
    item_map = {i.id: i for i in items}

    grouped: dict[UUID, list[PortfolioValueHistory]] = {}
    for r in rows:
        grouped.setdefault(r.portfolio_item_id, []).append(r)

    return [
        PortfolioValueHistoryByItem(
            portfolio_item_id=item_id,
            account_id=account_id,
            name=item_map[item_id].name if item_id in item_map else "(삭제됨)",
            code=item_map[item_id].code if item_id in item_map else "",
            market=Market(item_map[item_id].market) if item_id in item_map else Market.KRX_KOSPI,
            history=[_to_history_point(p) for p in points],
        )
        for item_id, points in grouped.items()
    ]


async def get_value_history_by_item(
    db: AsyncSession,
    household: Household,
    item_id: UUID,
    from_date: date | None,
    to_date: date | None,
) -> PortfolioValueHistoryByItem:
    """특정 종목 — 월별 평가액 추이 (삭제된 종목도 조회 가능)"""
    items = await PortfolioItemRepository(db).find_by_ids_including_deleted([item_id])
    if not items or items[0].household_id != household.id:
        raise CustomException(ErrorCode.NOT_FOUND)
    item = items[0]

    from_date, to_date = _default_month_range(from_date, to_date)

    history_repo = PortfolioValueHistoryRepository(db)
    rows = await history_repo.find_by_item_and_range(item_id, from_date, to_date)

    return PortfolioValueHistoryByItem(
        portfolio_item_id=item.id,
        account_id=item.account_id,
        name=item.name,
        code=item.code,
        market=Market(item.market),
        history=[_to_history_point(r) for r in rows],
    )


async def get_portfolio_overview(
    db: AsyncSession, household: Household,
) -> PortfolioOverviewResponse:
    """포트폴리오 메인 진입 응답 — INVESTMENT 계좌 + 그 종목 합쳐서 한번에"""
    # 순환 import 방지 — 함수 안에서 import
    from app.domain.account import service as account_service

    accounts = await account_service.list_accounts(db, household)
    investment_accounts = [a for a in accounts if a.account_type == AccountType.INVESTMENT]

    if not investment_accounts:
        return PortfolioOverviewResponse(
            summary=_zero_summary(), investment_accounts=[],
        )

    items = await PortfolioItemRepository(db).find_active_by_household_id(household.id)
    account_map = {
        a.id: a
        for a in await AccountRepository(db).find_by_ids(
            [a.id for a in investment_accounts]
        )
    }
    portfolios_by_account = _group_portfolios_by_account(items, account_map)

    return PortfolioOverviewResponse(
        # 보관 계좌는 목록엔 두고 합계에서만 뺀다 — 홈 레일 "투자"가 총자산과 같은 모집단
        summary=_summarize_investment_accounts(
            [a for a in investment_accounts if account_service.counts_in_totals(a)]
        ),
        investment_accounts=[
            InvestmentAccountWithPortfolios(
                account=a,
                portfolios=portfolios_by_account.get(a.id, []),
            )
            for a in investment_accounts
        ],
    )


async def get_account_overview(
    db: AsyncSession, household: Household, account_id: UUID,
) -> AccountOverviewResponse:
    """계좌 상세 페이지 진입 응답.

    INVESTMENT 통장이면 보유 종목까지 함께. 그 외 타입은 portfolios=[].
    """
    # 순환 import 방지
    from app.domain.account import service as account_service

    account = await account_service.get_account_detail(db, household, account_id)
    if account.account_type != AccountType.INVESTMENT:
        return AccountOverviewResponse(account=account, portfolios=[])

    pi_repo = PortfolioItemRepository(db)
    items = await pi_repo.find_active_by_account_id(account_id)
    # find_active_by_account_id 는 household 필터 없어서 명시적으로 다시 거름
    items = [i for i in items if i.household_id == household.id]

    accounts = await AccountRepository(db).find_by_ids([account_id])
    account_map = {a.id: a for a in accounts}

    return AccountOverviewResponse(
        account=account,
        portfolios=[_build_response(i, account_map) for i in items],
    )


async def list_item_transactions_cursor(
    db: AsyncSession,
    household: Household,
    item_id: UUID,
    cursor: str | None,
    limit: int,
) -> PortfolioTxPage:
    """종목 단건 거래 내역 — 무한 스크롤. transaction 패턴 그대로."""
    # 종목 소유권 검증 — 전량 매도로 DELETED 된 종목도 거래 내역은 남아 상태 무관 조회
    item = await PortfolioItemRepository(db).find_by_id_any_status(item_id)
    if not item or item.household_id != household.id:
        raise CustomException(ErrorCode.NOT_FOUND)

    pt_repo = PortfolioTransactionRepository(db)
    rows = await pt_repo.list_active_by_item_id_cursor(item_id, cursor, limit)
    has_next = len(rows) > limit
    rows = rows[:limit]

    accounts = await AccountRepository(db).find_by_ids(
        list({r.account_id for r in rows}),
    )
    account_map = {a.id: a for a in accounts}
    items = [_build_tx_response(r, account_map) for r in rows]

    next_cursor: str | None = None
    if has_next and rows:
        last = rows[-1]
        next_cursor = f"{last.tx_date.isoformat()}|{last.id}"

    return PortfolioTxPage(
        items=items,
        next_cursor=next_cursor,
        has_next=has_next,
        total_count=None,  # 무한 스크롤 — 카운트 미사용
    )


async def get_portfolio_form_options(
    db: AsyncSession, household: Household,
) -> PortfolioFormOptionsResponse:
    """종목 등록/수정 폼 옵션 — INVESTMENT 계좌만"""
    # 순환 import 방지
    from app.domain.account import service as account_service

    accounts = await account_service.list_accounts(
        db, household, account_type=AccountType.INVESTMENT.value, is_archived=False,
    )
    return PortfolioFormOptionsResponse(investment_accounts=accounts)


def _build_response(item: PortfolioItem, account_map: dict) -> PortfolioResponse:
    account = account_map.get(item.account_id)
    cost = item.quantity * item.avg_price
    valuation = item.quantity * item.current_price
    profit_loss = valuation - cost
    profit_loss_rate = (profit_loss / cost * Decimal("100")) if cost > 0 else Decimal("0.00")

    return PortfolioResponse(
        id=item.id,
        account_id=item.account_id,
        account_name=account.name if account else "(삭제됨)",
        name=item.name,
        code=item.code,
        market=Market(item.market),
        quantity=item.quantity,
        avg_price=item.avg_price,
        current_price=item.current_price,
        cost=cost,
        valuation=valuation,
        profit_loss=profit_loss,
        profit_loss_rate=profit_loss_rate,
        is_archived=item.is_archived,
    )


def _build_tx_response(tx: PortfolioTransaction, account_map: dict) -> PortfolioTxResponse:
    account = account_map.get(tx.account_id)
    return PortfolioTxResponse(
        id=tx.id,
        account_id=tx.account_id,
        account_name=account.name if account else "(삭제됨)",
        name=tx.name,
        code=tx.code,
        market=Market(tx.market),
        pt_type=tx.pt_type,
        quantity=tx.quantity,
        price=tx.price,
        total=tx.quantity * tx.price,
        fee=tx.fee,
        settlement_amount=_settlement_amount(tx),
        tx_date=tx.tx_date,
        memo=tx.memo,
        realized_pnl=tx.realized_pnl,
    )


def _settlement_amount(tx: PortfolioTransaction) -> Decimal:
    """정산금액 — 실제 계좌에서 나가고 들어온 돈.

    매수는 거래금액에 수수료를 더해 출금되고, 매도는 빼고 입금된다.
    """
    gross = tx.quantity * tx.price
    if tx.pt_type == PortfolioTxType.BUY:
        return gross + tx.fee
    return gross - tx.fee


async def _validate_investment_account(
    db: AsyncSession, household_id: UUID, account_id: UUID,
):
    """INVESTMENT 통장이고 같은 가계부 소속인지 검증"""
    accounts = await AccountRepository(db).find_by_ids([account_id])
    if not accounts:
        raise CustomException(ErrorCode.NOT_FOUND)
    a = accounts[0]
    if a.household_id != household_id or a.data_stat_cd != DataStatus.ACTIVE:
        raise CustomException(ErrorCode.NOT_FOUND)
    if a.account_type != AccountType.INVESTMENT:
        raise CustomException(ErrorCode.BAD_REQUEST)
    return a


def _recompute_realized_pnl(
    txs: list[PortfolioTransaction],
) -> tuple[Decimal, Decimal]:
    """거래를 시간순 replay 하며 각 SELL 의 실현손익을 그 시점 평단으로 재박제.

    거래 수정/삭제로 평단이 바뀌면 과거 SELL 의 박제값이 틀어지므로,
    매도시점 누적 이동평균(running_avg)으로 다시 계산해 SELL row 를 갱신한다.
    txs 의 price 는 이미 KRW 박제값이라 환율 변환 불필요.

    수수료(fee) — 증권사 계산과 동일:
    - 매수 수수료는 원가에 가산돼 평단을 올린다. 따라서 `realized_cost_basis`
      (= 매도시점 평단 × 수량) 에도 매수수수료가 자동으로 포함되고,
      `realized_pnl / realized_cost_basis` 는 '매수원가 대비 순손익률'이 된다.
    - 매도 수수료는 실현손익에서만 차감한다. 원가에 넣으면 이미 판 수량의
      비용이 남은 보유분 평단까지 올린다.

    반환: replay 종료 시점의 (남은 수량, 남은 원가) — 호출부의 평단 재계산용.
    """
    running_qty = Decimal("0")
    running_cost = Decimal("0")
    for t in txs:
        if t.pt_type == PortfolioTxType.BUY:
            running_qty += t.quantity
            running_cost += t.quantity * t.price + t.fee
        elif t.pt_type == PortfolioTxType.SELL:
            # 그 시점 보유보다 많이 팔 수는 없다. 최종 수량만 검사하면 매도가 매수보다
            # 앞선 순서를 뒤따르는 매수가 가려주고, 그 매도는 running_avg=0 으로
            # 계산돼 매도금액 전액이 이익으로 박제된다(원가 0).
            if t.quantity > running_qty:
                raise CustomException(ErrorCode.BAD_REQUEST)
            running_avg = running_cost / running_qty if running_qty > 0 else Decimal("0")
            t.realized_cost_basis = (running_avg * t.quantity).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP,
            )
            t.realized_pnl = (
                (t.price - running_avg) * t.quantity - t.fee
            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            # 매도는 평단 불변 — 수량/원가를 평단 비율로 차감 (running_avg 유지)
            running_cost -= running_avg * t.quantity
            running_qty -= t.quantity
    return running_qty, running_cost


async def _lock_item_of(db: AsyncSession, tx: PortfolioTransaction) -> PortfolioItem | None:
    """거래가 속한 종목을 잠가 가져온다 — 전량매도로 죽은 종목도 재계산 대상이라 상태 무관.

    매매 경로는 모두 "종목 lock → 거래 행 쓰기" 순서다. 거래 행을 먼저 잡으면, 그 종목을 잡고
    실현손익 재박제로 이 행을 고치려는 매매와 서로 기다린다(데드락)."""
    if not tx.portfolio_item_id:
        return None
    return await PortfolioItemRepository(db).find_by_id_for_update(
        tx.portfolio_item_id, any_status=True,
    )


async def _recalc_item_from_transactions(
    db: AsyncSession, item: PortfolioItem,
) -> None:
    """종목의 활성 거래 시간순 replay → quantity / avg_price 재계산.

    이동평균: 매도 시점 평단으로 원가를 차감하므로 매도 후 재매수도 정확히
    반영된다(단순 전체매수합/전체매수량은 평단을 왜곡). 매도 실현손익 재박제와
    같은 replay 를 공유한다. 매도가 매수보다 많으면 BAD_REQUEST.
    """
    pt_repo = PortfolioTransactionRepository(db)
    txs = await pt_repo.find_active_by_item_id(item.id)

    remaining_qty, remaining_cost = _recompute_realized_pnl(txs)
    if remaining_qty < 0:
        raise CustomException(ErrorCode.BAD_REQUEST)

    item.quantity = remaining_qty
    item.avg_price = (
        remaining_cost / remaining_qty if remaining_qty > 0 else Decimal("0.00")
    )
    # 거래 수정/삭제로 수량이 0→양수면 종목 부활, 양수→0이면 소멸(전량매도와 동일).
    item.data_stat_cd = DataStatus.ACTIVE if remaining_qty > 0 else DataStatus.DELETED
    await db.flush()


def _default_month_range(
    from_date: date | None, to_date: date | None,
) -> tuple[date, date]:
    """월별 스냅샷 추이 전용 — from/to 를 그달 1일로 정규화. 미지정 시 최근 12개월.

    account-snapshot/yearly 와 동일 컨벤션. 일 단위 필터(매매손익)는 월 정규화하면
    안 되므로 _default_day_range 를 쓴다.
    """
    today = today_kst().replace(day=1)
    if not to_date:
        to_date = today
    else:
        to_date = to_date.replace(day=1)
    if not from_date:
        total = to_date.year * 12 + (to_date.month - 1) - 11
        y, m = divmod(total, 12)
        from_date = date(y, m + 1, 1)
    else:
        from_date = from_date.replace(day=1)
    return from_date, to_date


def _default_day_range(
    from_date: date | None,
    to_date: date | None,
    earliest_sell: date | None = None,
) -> tuple[date, date]:
    """매매손익 전용 — 일 단위 자유 필터. 월 정규화 없음. 미지정 시 최근 12개월.

    from 미지정 + earliest_sell 이 기본 시작일(1년 전)보다 이르면 거기까지 확장한다
    (기본 '최근 1년' 이 첫 매도를 놓치는 경계 케이스 방지). from 명시 시는 존중.
    """
    if not to_date:
        to_date = today_kst()
    if not from_date:
        try:
            from_date = to_date.replace(year=to_date.year - 1)
        except ValueError:  # 2/29 → 전년 2/28
            from_date = to_date.replace(year=to_date.year - 1, day=28)
        if earliest_sell is not None and earliest_sell < from_date:
            from_date = earliest_sell
    return from_date, to_date


def _to_history_point(row: PortfolioValueHistory) -> PortfolioValueHistoryPoint:
    return PortfolioValueHistoryPoint(
        snapshot_date=row.snapshot_date,
        quantity=row.quantity,
        avg_price=row.avg_price,
        current_price=row.current_price,
        cost=row.cost,
        valuation=row.valuation,
    )


# =========================================================
# Page-level overview / form-options (페이지 진입 시 1호출)
# =========================================================


def _zero_summary() -> PortfolioOverviewSummary:
    z = Decimal("0")
    return PortfolioOverviewSummary(
        total_balance=z, total_cash=z, total_valuation=z,
        total_cost=z, total_profit=z, total_rate=z,
    )


def _summarize_investment_accounts(
    accounts: list[AccountResponse],
) -> PortfolioOverviewSummary:
    """INVESTMENT 계좌들의 총합 — balance/cash/valuation/cost/profit + 손익률. 순수 계산."""
    total_balance = sum((a.balance for a in accounts), Decimal("0"))
    total_cash = sum((a.cash or Decimal("0") for a in accounts), Decimal("0"))
    total_valuation = sum(
        (a.portfolio_valuation or Decimal("0") for a in accounts), Decimal("0"),
    )
    total_cost = sum(
        (a.portfolio_cost or Decimal("0") for a in accounts), Decimal("0"),
    )
    total_profit = sum(
        (a.portfolio_profit_loss or Decimal("0") for a in accounts), Decimal("0"),
    )
    total_rate = (
        (total_profit / total_cost * Decimal("100")) if total_cost > 0 else Decimal("0")
    )
    return PortfolioOverviewSummary(
        total_balance=total_balance,
        total_cash=total_cash,
        total_valuation=total_valuation,
        total_cost=total_cost,
        total_profit=total_profit,
        total_rate=total_rate,
    )


def _group_portfolios_by_account(
    items: list, account_map: dict[UUID, Account],
) -> dict[UUID, list[PortfolioResponse]]:
    """보유 종목을 계좌별 종목 응답 리스트로 그룹화 (account_map 에 없는 계좌는 제외)."""
    grouped: dict[UUID, list[PortfolioResponse]] = {}
    for it in items:
        if it.account_id not in account_map:
            continue
        grouped.setdefault(it.account_id, []).append(_build_response(it, account_map))
    return grouped

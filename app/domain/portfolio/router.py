from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_response import ApiResponse
from app.core.database import DbDepends
from app.domain.household.deps import CurrentHousehold
from app.domain.portfolio import service
from app.domain.portfolio.enum import Market
from app.domain.portfolio.schema import (
    AccountOverviewResponse,
    PortfolioBuyRequest,
    PortfolioCreateRequest,
    PortfolioFormOptionsResponse,
    PortfolioLookupResponse,
    PortfolioOverviewResponse,
    PortfolioRefreshResponse,
    PortfolioResponse,
    PortfolioSellRequest,
    PortfolioTxPage,
    PortfolioTxResponse,
    PortfolioTxUpdateRequest,
    PortfolioUpdateRequest,
    PortfolioValueHistoryByAccountQuery,
    PortfolioValueHistoryByItem,
    PortfolioValueHistoryByItemQuery,
    RealizedPnlResponse,
)

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


# =========================================================
# Page-level entry endpoints (페이지 1호출)
# =========================================================


@router.get("/overview")
async def get_overview(
    household: CurrentHousehold,
    db: AsyncSession = DbDepends,
) -> ApiResponse[PortfolioOverviewResponse]:
    """포트폴리오 메인 페이지 진입 — INVESTMENT 계좌 + 종목 묶음 + 요약"""
    response = await service.get_portfolio_overview(db, household)
    return ApiResponse.ok(data=response)


@router.get("/accounts/{account_id}/overview")
async def get_account_overview(
    account_id: UUID,
    household: CurrentHousehold,
    db: AsyncSession = DbDepends,
) -> ApiResponse[AccountOverviewResponse]:
    """계좌 상세 페이지 진입 — 통장 + (INVESTMENT 면) 보유 종목"""
    response = await service.get_account_overview(db, household, account_id)
    return ApiResponse.ok(data=response)


@router.get("/accounts/{account_id}/realized-pnl")
async def get_account_realized_pnl(
    account_id: UUID,
    household: CurrentHousehold,
    from_date: date | None = Query(None, alias="fromDate"),
    to_date: date | None = Query(None, alias="toDate"),
    db: AsyncSession = DbDepends,
) -> ApiResponse[RealizedPnlResponse]:
    """계좌 누적 매매손익 — 기간 내 계좌 전체 매도 건별 실현손익 + 요약. 기본 최근 12개월.

    전량매도로 사라진 종목의 매도도 포함(조회 사각지대 해소).
    """
    response = await service.get_realized_pnl_by_account(
        db, household, account_id, from_date, to_date,
    )
    return ApiResponse.ok(data=response)


@router.get("/form-options")
async def get_form_options(
    household: CurrentHousehold,
    db: AsyncSession = DbDepends,
) -> ApiResponse[PortfolioFormOptionsResponse]:
    """종목 등록/수정 폼 옵션 — INVESTMENT 계좌만"""
    response = await service.get_portfolio_form_options(db, household)
    return ApiResponse.ok(data=response)


# =========================================================
# Item-level (단건 + 그 거래 내역)
# =========================================================


@router.get("/items/{item_id}")
async def get_item(
    item_id: UUID,
    household: CurrentHousehold,
    db: AsyncSession = DbDepends,
) -> ApiResponse[PortfolioResponse]:
    """종목 단건 조회 — PNL 포함"""
    response = await service.get_portfolio_detail(db, household, item_id)
    return ApiResponse.ok(data=response)


@router.get("/items/{item_id}/transactions")
async def list_item_transactions(
    item_id: UUID,
    household: CurrentHousehold,
    cursor: str | None = Query(None),
    limit: int = Query(30, ge=1, le=200),
    db: AsyncSession = DbDepends,
) -> ApiResponse[PortfolioTxPage]:
    """종목 단건 거래 내역 — 무한 스크롤 (cursor + limit)"""
    response = await service.list_item_transactions_cursor(
        db, household, item_id, cursor, limit,
    )
    return ApiResponse.ok(data=response)


@router.get("/items/{item_id}/realized-pnl")
async def get_item_realized_pnl(
    item_id: UUID,
    household: CurrentHousehold,
    from_date: date | None = Query(None, alias="fromDate"),
    to_date: date | None = Query(None, alias="toDate"),
    db: AsyncSession = DbDepends,
) -> ApiResponse[RealizedPnlResponse]:
    """종목 매매손익 — 기간 내 매도 건별 실현손익 + 요약. 기본 최근 12개월."""
    response = await service.get_realized_pnl_by_item(
        db, household, item_id, from_date, to_date,
    )
    return ApiResponse.ok(data=response)


# =========================================================
# Mutations + utility (action endpoints)
# =========================================================


@router.get("/lookup")
async def lookup_stock(
    household: CurrentHousehold,  # 인증만 — 결과는 가계부와 무관한 공개 정보
    market: Market = Query(...),
    code: str = Query(..., min_length=1, max_length=50),
    db: AsyncSession = DbDepends,
) -> ApiResponse[PortfolioLookupResponse]:
    """야후 파이낸스로 종목명 + 현재가 조회 — 폼 자동 채움용 (저장 X).
    USD 시장은 KRW 로 환산해 응답."""
    response = await service.lookup_stock(db, market, code)
    return ApiResponse.ok(data=response)


@router.post("/refresh-prices")
async def refresh_prices(
    household: CurrentHousehold,
    db: AsyncSession = DbDepends,
) -> ApiResponse[PortfolioRefreshResponse]:
    """이 가계부 보유 종목들의 현재가를 야후로 즉시 갱신(수동 새로고침)."""
    result = await service.refresh_prices_for_household(db, household)
    return ApiResponse.ok(
        data=PortfolioRefreshResponse(
            fetched=result.fetched,
            skipped=result.skipped,
            updated_rows=result.updated_rows,
        )
    )


@router.post("/create")
async def create_portfolio(
    req: PortfolioCreateRequest,
    household: CurrentHousehold,
    db: AsyncSession = DbDepends,
) -> ApiResponse[PortfolioResponse]:
    """종목 등록 — 메타만 (qty=0 시작). 매수는 /buy/{id}"""
    response = await service.create_portfolio(db, household, req)
    return ApiResponse.ok(data=response)


@router.post("/buy/{item_id}")
async def buy_portfolio(
    item_id: UUID,
    req: PortfolioBuyRequest,
    household: CurrentHousehold,
    db: AsyncSession = DbDepends,
) -> ApiResponse[PortfolioResponse]:
    """매수 — qty 누적 + avg_price 재계산 + 이력 기록"""
    response = await service.buy(db, household, item_id, req)
    return ApiResponse.ok(data=response)


@router.put("/update/{item_id}")
async def update_portfolio(
    item_id: UUID,
    req: PortfolioUpdateRequest,
    household: CurrentHousehold,
    db: AsyncSession = DbDepends,
) -> ApiResponse[PortfolioResponse]:
    """평가액/메타 수정 (transaction 무관)"""
    response = await service.update_portfolio(db, household, item_id, req)
    return ApiResponse.ok(data=response)


@router.post("/sell/{item_id}")
async def sell_portfolio(
    item_id: UUID,
    req: PortfolioSellRequest,
    household: CurrentHousehold,
    db: AsyncSession = DbDepends,
) -> ApiResponse[PortfolioResponse | None]:
    """매도 (부분/전량). 전량 시 응답 data=null"""
    response = await service.sell(db, household, item_id, req)
    return ApiResponse.ok(data=response)


@router.put("/transactions/{tx_id}")
async def update_portfolio_transaction(
    tx_id: UUID,
    req: PortfolioTxUpdateRequest,
    household: CurrentHousehold,
    db: AsyncSession = DbDepends,
) -> ApiResponse[PortfolioTxResponse]:
    """거래 내역 수정 — 해당 종목 quantity / avg_price 자동 재계산"""
    response = await service.update_portfolio_transaction(db, household, tx_id, req)
    return ApiResponse.ok(data=response)


@router.delete("/transactions/{tx_id}")
async def delete_portfolio_transaction(
    tx_id: UUID,
    household: CurrentHousehold,
    db: AsyncSession = DbDepends,
) -> ApiResponse[None]:
    """거래 내역 soft delete — 해당 종목 quantity / avg_price 자동 재계산"""
    await service.delete_portfolio_transaction(db, household, tx_id)
    return ApiResponse.ok()


@router.delete("/delete/{item_id}")
async def delete_portfolio(
    item_id: UUID,
    household: CurrentHousehold,
    db: AsyncSession = DbDepends,
) -> ApiResponse[None]:
    """종목 soft delete (data_stat_cd='99'). value-history 는 보존"""
    await service.delete_portfolio(db, household, item_id)
    return ApiResponse.ok()


# =========================================================
# Value history (차트)
# =========================================================


@router.get("/value-history")
async def get_portfolio_value_history_by_account(
    household: CurrentHousehold,
    q: Annotated[PortfolioValueHistoryByAccountQuery, Query()],
    db: AsyncSession = DbDepends,
) -> ApiResponse[list[PortfolioValueHistoryByItem]]:
    """통장 단위 종목별 월별 평가액 추이 (차트용). 기본: 최근 12개월"""
    response = await service.get_value_history_by_account(
        db, household, q.account_id, q.from_date, q.to_date,
    )
    return ApiResponse.ok(data=response)


@router.get("/items/{item_id}/value-history")
async def get_portfolio_value_history_by_item(
    item_id: UUID,
    household: CurrentHousehold,
    q: Annotated[PortfolioValueHistoryByItemQuery, Query()],
    db: AsyncSession = DbDepends,
) -> ApiResponse[PortfolioValueHistoryByItem]:
    """특정 종목 월별 평가액 추이 (차트용)"""
    response = await service.get_value_history_by_item(
        db, household, item_id, q.from_date, q.to_date,
    )
    return ApiResponse.ok(data=response)

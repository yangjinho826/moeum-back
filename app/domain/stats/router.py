from uuid import UUID

from fastapi import APIRouter, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_response import ApiResponse
from app.core.database import DbDepends
from app.domain.household.deps import CurrentHousehold
from app.domain.stats import service
from app.domain.stats.schema import MonthlyStatsResponse

router = APIRouter(prefix="/stats", tags=["stats"])


@router.get("/monthly")
async def get_monthly_stats(
    household: CurrentHousehold,
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    account_id: UUID | None = Query(None, alias="accountId"),
    db: AsyncSession = DbDepends,
) -> ApiResponse[MonthlyStatsResponse]:
    """월간 카테고리별 지출/수입 집계 — 이번 달 지출 카드/차트용.

    accountId 를 주면 그 통장이 낀 거래만 집계(거래 탭 통장 필터).
    """
    response = await service.get_monthly_stats(
        db, household, year, month, account_id,
    )
    return ApiResponse.ok(data=response)

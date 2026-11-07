from fastapi import APIRouter, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_response import ApiResponse
from app.core.database import DbDepends
from app.domain.home import service
from app.domain.home.schema import HomeOverviewResponse
from app.domain.household.deps import CurrentHousehold

router = APIRouter(prefix="/home", tags=["home"])


@router.get("/overview")
async def get_overview(
    household: CurrentHousehold,
    year: int | None = Query(None, ge=2000, le=2100),
    month: int | None = Query(None, ge=1, le=12),
    db: AsyncSession = DbDepends,
) -> ApiResponse[HomeOverviewResponse]:
    """홈 페이지 1호출 — 총자산 + 통장 + 최근 거래 + 월간 stats"""
    response = await service.get_home_overview(db, household, year, month)
    return ApiResponse.ok(data=response)

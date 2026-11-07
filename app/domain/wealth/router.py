from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_response import ApiResponse
from app.core.database import DbDepends
from app.domain.account_snapshot.schema import SnapshotYearlyQuery
from app.domain.household.deps import CurrentHousehold
from app.domain.wealth import service
from app.domain.wealth.schema import WealthOverviewResponse

router = APIRouter(prefix="/wealth", tags=["wealth"])


@router.get("/overview")
async def get_overview(
    household: CurrentHousehold,
    q: Annotated[SnapshotYearlyQuery, Query()],
    db: AsyncSession = DbDepends,
) -> ApiResponse[WealthOverviewResponse]:
    """자산 페이지 1호출 — 통장 + 연간 자산 추이"""
    response = await service.get_wealth_overview(db, household, q.from_date, q.to_date)
    return ApiResponse.ok(data=response)

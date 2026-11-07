from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_response import ApiResponse
from app.core.database import DbDepends
from app.domain.account_snapshot import service
from app.domain.account_snapshot.schema import (
    SnapshotMonth,
    SnapshotYearlyQuery,
    SnapshotYearlyResponse,
)
from app.domain.household.deps import CurrentHousehold

router = APIRouter(prefix="/account-snapshot", tags=["account-snapshot"])


@router.post("/create")
async def create_snapshot(
    household: CurrentHousehold,
    db: AsyncSession = DbDepends,
) -> ApiResponse[SnapshotMonth]:
    """수동 박제 — 지난달 박제 (upsert). 6/1~6/말에 누르면 5월 박제, 또 눌러도 안전."""
    response = await service.create_target_month_snapshot(db, household)
    return ApiResponse.ok(data=response)


@router.get("/yearly")
async def yearly_snapshots(
    household: CurrentHousehold,
    q: Annotated[SnapshotYearlyQuery, Query()],
    db: AsyncSession = DbDepends,
) -> ApiResponse[SnapshotYearlyResponse]:
    """월별 자산 추이 (기본 최근 12개월)"""
    response = await service.get_yearly_snapshots(db, household, q.from_date, q.to_date)
    return ApiResponse.ok(data=response)

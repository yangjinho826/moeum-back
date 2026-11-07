from datetime import datetime
from typing import Annotated
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_response import ApiResponse
from app.core.database import DbDepends
from app.core.exceptions import CustomException, ErrorCode
from app.core.pagination import CursorPage
from app.domain.fixed import service
from app.domain.fixed.schema import (
    FixedCreateRequest,
    FixedListQuery,
    FixedMonthlySummaryResponse,
    FixedResponse,
    FixedUpdateRequest,
)
from app.domain.household.deps import CurrentHousehold

router = APIRouter(prefix="/fixed", tags=["fixed"])


@router.get("/list")
async def list_fixed_expenses(
    household: CurrentHousehold,
    q: Annotated[FixedListQuery, Query()],
    db: AsyncSession = DbDepends,
) -> ApiResponse[CursorPage[FixedResponse]]:
    """고정지출 목록 (cursor 무한 스크롤) — searchTerm/isArchived 필터"""
    response = await service.list_fixed_expenses_cursor(
        db, household,
        search_term=q.search_term,
        is_archived=q.is_archived,
        cursor=q.cursor,
        limit=q.limit,
    )
    return ApiResponse.ok(data=response)


@router.post("/create")
async def create_fixed_expense(
    req: FixedCreateRequest,
    household: CurrentHousehold,
    db: AsyncSession = DbDepends,
) -> ApiResponse[FixedResponse]:
    """고정지출 생성"""
    response = await service.create_fixed_expense(db, household, req)
    return ApiResponse.ok(data=response)


@router.get("/detail/{fixed_id}")
async def get_fixed_detail(
    fixed_id: UUID,
    household: CurrentHousehold,
    db: AsyncSession = DbDepends,
) -> ApiResponse[FixedResponse]:
    """고정지출 단건 조회"""
    response = await service.get_fixed_detail(db, household, fixed_id)
    return ApiResponse.ok(data=response)


@router.put("/update/{fixed_id}")
async def update_fixed_expense(
    fixed_id: UUID,
    req: FixedUpdateRequest,
    household: CurrentHousehold,
    db: AsyncSession = DbDepends,
) -> ApiResponse[FixedResponse]:
    """고정지출 수정"""
    response = await service.update_fixed_expense(db, household, fixed_id, req)
    return ApiResponse.ok(data=response)


@router.delete("/delete/{fixed_id}")
async def delete_fixed_expense(
    fixed_id: UUID,
    household: CurrentHousehold,
    db: AsyncSession = DbDepends,
) -> ApiResponse[None]:
    """고정지출 삭제 (soft)"""
    await service.delete_fixed_expense(db, household, fixed_id)
    return ApiResponse.ok()


@router.get("/monthly-summary")
async def get_monthly_summary(
    household: CurrentHousehold,
    month: str | None = Query(None, description="YYYY-MM 형식. 생략 시 KST 이번달"),
    db: AsyncSession = DbDepends,
) -> ApiResponse[FixedMonthlySummaryResponse]:
    """고정지출별 해당 월 누적 사용액"""
    if month:
        try:
            year_str, month_str = month.split("-")
            year, month_int = int(year_str), int(month_str)
            if not (1 <= month_int <= 12):
                raise ValueError
        except (ValueError, AttributeError) as e:
            raise CustomException(ErrorCode.BAD_REQUEST) from e
    else:
        today = datetime.now(ZoneInfo("Asia/Seoul")).date()
        year, month_int = today.year, today.month

    response = await service.get_monthly_summary(db, household, year, month_int)
    return ApiResponse.ok(data=response)

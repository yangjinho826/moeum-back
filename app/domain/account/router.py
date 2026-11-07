from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_response import ApiResponse
from app.core.database import DbDepends
from app.core.pagination import CursorPage
from app.domain.account import service
from app.domain.account.schema import (
    AccountCreateRequest,
    AccountListQuery,
    AccountReportResponse,
    AccountResponse,
    AccountUpdateRequest,
)
from app.domain.household.deps import CurrentHousehold

router = APIRouter(prefix="/account", tags=["account"])


@router.get("/list")
async def list_accounts(
    household: CurrentHousehold,
    q: Annotated[AccountListQuery, Query()],
    db: AsyncSession = DbDepends,
) -> ApiResponse[CursorPage[AccountResponse]]:
    """통장 목록 (cursor 무한 스크롤) — searchTerm/accountType/isArchived 필터"""
    response = await service.list_accounts_cursor(
        db, household,
        search_term=q.search_term,
        account_type=q.account_type.value if q.account_type else None,
        is_archived=q.is_archived,
        cursor=q.cursor,
        limit=q.limit,
    )
    return ApiResponse.ok(data=response)


@router.post("/create")
async def create_account(
    req: AccountCreateRequest,
    household: CurrentHousehold,
    db: AsyncSession = DbDepends,
) -> ApiResponse[AccountResponse]:
    """통장 생성"""
    response = await service.create_account(db, household, req)
    return ApiResponse.ok(data=response)


@router.get("/detail/{account_id}")
async def get_account_detail(
    account_id: UUID,
    household: CurrentHousehold,
    db: AsyncSession = DbDepends,
) -> ApiResponse[AccountResponse]:
    """통장 단건 조회"""
    response = await service.get_account_detail(db, household, account_id)
    return ApiResponse.ok(data=response)


@router.get("/report/{account_id}")
async def get_account_report(
    account_id: UUID,
    household: CurrentHousehold,
    db: AsyncSession = DbDepends,
) -> ApiResponse[AccountReportResponse]:
    """계좌별 리포트 — 현재 잔액 + 월별 수입/지출 추이 (최근 12개월)"""
    response = await service.get_account_report(db, household, account_id)
    return ApiResponse.ok(data=response)


@router.put("/update/{account_id}")
async def update_account(
    account_id: UUID,
    req: AccountUpdateRequest,
    household: CurrentHousehold,
    db: AsyncSession = DbDepends,
) -> ApiResponse[AccountResponse]:
    """통장 수정"""
    response = await service.update_account(db, household, account_id, req)
    return ApiResponse.ok(data=response)


@router.delete("/delete/{account_id}")
async def delete_account(
    account_id: UUID,
    household: CurrentHousehold,
    db: AsyncSession = DbDepends,
) -> ApiResponse[None]:
    """통장 삭제 (soft)"""
    await service.delete_account(db, household, account_id)
    return ApiResponse.ok()

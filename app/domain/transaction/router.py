from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_response import ApiResponse
from app.core.auth.deps import CurrentUser
from app.core.database import get_db
from app.domain.household.deps import CurrentHousehold
from app.domain.transaction import service
from app.domain.transaction.repository import TransactionFilter
from app.domain.transaction.schema import (
    AccountLedgerPage,
    CalendarFullResponse,
    TransactionCreateRequest,
    TransactionFormOptionsResponse,
    TransactionListQuery,
    TransactionListResponse,
    TransactionResponse,
    TransactionUpdateRequest,
)

router = APIRouter(prefix="/transaction", tags=["transaction"])


@router.get("/list")
async def list_transactions(
    household: CurrentHousehold,
    q: Annotated[TransactionListQuery, Query()],
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[TransactionListResponse]:
    """거래 목록 (커서 기반 페이징)"""
    filter_data = q.model_dump(exclude={"cursor", "limit"}, exclude_none=True)
    f = TransactionFilter(**filter_data)
    response = await service.list_transactions(db, household, f, q.cursor, q.limit)
    return ApiResponse.ok(data=response)


@router.post("/create")
async def create_transaction(
    req: TransactionCreateRequest,
    household: CurrentHousehold,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[TransactionResponse]:
    """거래 생성"""
    response = await service.create_transaction(db, household, req, current_user)
    return ApiResponse.ok(data=response)


@router.put("/update/{tx_id}")
async def update_transaction(
    tx_id: UUID,
    req: TransactionUpdateRequest,
    household: CurrentHousehold,
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[TransactionResponse]:
    """거래 수정"""
    response = await service.update_transaction(db, household, tx_id, req)
    return ApiResponse.ok(data=response)


@router.delete("/delete/{tx_id}")
async def delete_transaction(
    tx_id: UUID,
    household: CurrentHousehold,
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[None]:
    """거래 삭제 (soft)"""
    await service.delete_transaction(db, household, tx_id)
    return ApiResponse.ok()


@router.get("/detail/{tx_id}")
async def get_transaction_detail(
    tx_id: UUID,
    household: CurrentHousehold,
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[TransactionResponse]:
    """거래 단건 조회"""
    response = await service.get_transaction_detail(db, household, tx_id)
    return ApiResponse.ok(data=response)


@router.get("/account/{account_id}/ledger")
async def get_account_ledger(
    account_id: UUID,
    household: CurrentHousehold,
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=500),
    year: int | None = Query(None, ge=2000, le=2100),
    month: int | None = Query(None, ge=1, le=12),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[AccountLedgerPage]:
    """계좌별 거래 이력 (커서 페이징) — 각 행에 running balance(거래 후 잔액).

    거래계좌(LIVING/SAVINGS/OTHER) 전용. 이체는 그 계좌 관점 부호로.
    year+month 를 주면 그 달 거래만(거래 탭), 없으면 전체(계좌 상세).
    """
    response = await service.list_account_ledger(
        db, household, account_id, cursor, limit, year, month,
    )
    return ApiResponse.ok(data=response)


@router.get("/calendar/{year}/{month}/full")
async def get_calendar_full(
    household: CurrentHousehold,
    year: int = Path(..., ge=2000, le=2100),
    month: int = Path(..., ge=1, le=12),
    account_id: UUID | None = Query(None, alias="accountId"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[CalendarFullResponse]:
    """달력 페이지 1호출 — calendar + stats(카테고리) + 그달 거래 전부.

    accountId 를 주면 셋 다 그 통장이 낀 거래만(거래 탭 통장 필터).
    """
    response = await service.get_calendar_full(
        db, household, year, month, account_id,
    )
    return ApiResponse.ok(data=response)


@router.get("/form-options")
async def get_form_options(
    household: CurrentHousehold,
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[TransactionFormOptionsResponse]:
    """거래 등록/수정 폼 옵션 — 통장 + 카테고리 + 활성 고정지출"""
    response = await service.get_form_options(db, household)
    return ApiResponse.ok(data=response)

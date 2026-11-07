from uuid import UUID

from fastapi import APIRouter
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_response import ApiResponse
from app.core.auth.deps import CurrentUser
from app.core.database import DbDepends
from app.domain.household import service
from app.domain.household.schema import (
    HouseholdCreateRequest,
    HouseholdListResponse,
    HouseholdMemberCreateRequest,
    HouseholdMemberListResponse,
    HouseholdMemberResponse,
    HouseholdResponse,
    HouseholdUpdateRequest,
)

router = APIRouter(prefix="/household", tags=["household"])


@router.get("/list")
async def list_households(
    current_user: CurrentUser,
    db: AsyncSession = DbDepends,
) -> ApiResponse[HouseholdListResponse]:
    """현재 user 가 멤버인 가계부 목록 (CursorPage 봉투 통일)"""
    response = await service.list_households(db, current_user)
    return ApiResponse.ok(data=response)


@router.post("/create")
async def create_household(
    req: HouseholdCreateRequest,
    current_user: CurrentUser,
    db: AsyncSession = DbDepends,
) -> ApiResponse[HouseholdResponse]:
    """가계부 생성 (생성자가 owner 로 자동 등록)"""
    response = await service.create_household(db, req, current_user)
    return ApiResponse.ok(data=response)


@router.get("/detail/{household_id}")
async def get_household_detail(
    household_id: UUID,
    current_user: CurrentUser,
    db: AsyncSession = DbDepends,
) -> ApiResponse[HouseholdResponse]:
    """가계부 단건 조회 — 멤버만 접근 가능"""
    response = await service.get_household_detail(db, household_id, current_user)
    return ApiResponse.ok(data=response)


@router.put("/update/{household_id}")
async def update_household(
    household_id: UUID,
    req: HouseholdUpdateRequest,
    current_user: CurrentUser,
    db: AsyncSession = DbDepends,
) -> ApiResponse[HouseholdResponse]:
    """가계부 수정 (owner 만)"""
    response = await service.update_household(db, household_id, req, current_user)
    return ApiResponse.ok(data=response)


@router.delete("/delete/{household_id}")
async def delete_household(
    household_id: UUID,
    current_user: CurrentUser,
    db: AsyncSession = DbDepends,
) -> ApiResponse[None]:
    """가계부 삭제 (soft, owner 만)"""
    await service.delete_household(db, household_id, current_user)
    return ApiResponse.ok()


@router.get("/{household_id}/members")
async def list_household_members(
    household_id: UUID,
    current_user: CurrentUser,
    db: AsyncSession = DbDepends,
) -> ApiResponse[HouseholdMemberListResponse]:
    """가계부 멤버 목록 — 본인이 멤버일 때만 (CursorPage 봉투 통일)"""
    response = await service.list_household_members(db, household_id, current_user)
    return ApiResponse.ok(data=response)


@router.post("/{household_id}/members")
async def add_household_member(
    household_id: UUID,
    req: HouseholdMemberCreateRequest,
    current_user: CurrentUser,
    db: AsyncSession = DbDepends,
) -> ApiResponse[HouseholdMemberResponse]:
    """가계부 멤버 추가 (owner 만)"""
    response = await service.add_household_member(db, household_id, req, current_user)
    return ApiResponse.ok(data=response)


@router.delete("/{household_id}/members/{member_id}")
async def remove_household_member(
    household_id: UUID,
    member_id: UUID,
    current_user: CurrentUser,
    db: AsyncSession = DbDepends,
) -> ApiResponse[None]:
    """가계부 멤버 제거 (owner 만, owner 본인은 제거 불가)"""
    await service.remove_household_member(db, household_id, member_id, current_user)
    return ApiResponse.ok()

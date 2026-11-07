from uuid import UUID

from fastapi import APIRouter, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_response import ApiResponse
from app.core.auth.deps import CurrentUser
from app.core.database import DbDepends
from app.domain.user import service
from app.domain.user.schema import UserCreateRequest, UserResponse, UserUpdateRequest

router = APIRouter(prefix="/user", tags=["user"])


@router.post("")
async def create(
    req: UserCreateRequest,
    db: AsyncSession = DbDepends,
) -> ApiResponse[UserResponse]:
    """회원 가입"""
    user = await service.create_user(db, req)
    return ApiResponse.ok(data=UserResponse.model_validate(user))


@router.get("/me")
async def me(current_user: CurrentUser) -> ApiResponse[UserResponse]:
    """현재 로그인한 사용자 정보 — 페이지 새로고침/SSR hydrate 용"""
    return ApiResponse.ok(data=UserResponse.model_validate(current_user))


@router.get("/search")
async def search(
    current_user: CurrentUser,  # noqa: ARG001 — 인증 가드용
    email: str = Query(..., min_length=3, max_length=255),
    db: AsyncSession = DbDepends,
) -> ApiResponse[UserResponse]:
    """이메일 정확 매칭 검색 — 가계부 멤버 초대 시 user_id 조회용. 인증 필요"""
    response = await service.search_by_email(db, email)
    return ApiResponse.ok(data=response)


@router.get("/{user_id}")
async def detail(
    user_id: UUID,
    current_user: CurrentUser,  # noqa: ARG001 — 인증 가드용 (이메일 노출 방지)
    db: AsyncSession = DbDepends,
) -> ApiResponse[UserResponse]:
    """사용자 상세 조회 — 인증 필요"""
    response = await service.detail_user(db, user_id)
    return ApiResponse.ok(data=response)


@router.put("/{user_id}")
async def update(
    user_id: UUID,
    req: UserUpdateRequest,
    current_user: CurrentUser,
    db: AsyncSession = DbDepends,
) -> ApiResponse[UserResponse]:
    """사용자 정보 수정 (본인만 가능)"""
    response = await service.update_user(db, user_id, req, current_user)
    return ApiResponse.ok(data=response)

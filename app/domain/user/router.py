from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_response import ApiResponse
from app.core.auth.deps import CurrentUser
from app.core.database import DbDepends
from app.core.rate_limit import (
    SlidingWindowLimiter,
    get_password_limiter,
    get_search_limiter,
)
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
    current_user: CurrentUser,
    email: str = Query(..., min_length=3, max_length=255),
    db: AsyncSession = DbDepends,
    limiter: SlidingWindowLimiter = Depends(get_search_limiter),
) -> ApiResponse[UserResponse]:
    """이메일 정확 매칭 검색 — 가계부 멤버 초대 시 user_id 조회용. 가계부 owner 만, 분당 횟수 제한"""
    response = await service.search_by_email(db, email, current_user, limiter)
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
    refresh_token: str | None = Cookie(None, alias="refresh_token"),
    password_limiter: SlidingWindowLimiter = Depends(get_password_limiter),
) -> ApiResponse[UserResponse]:
    """사용자 정보 수정 (본인만 가능). 비번 변경은 현재 비번 필요 + 다른 기기 세션 폐기"""
    # 쿠키의 refresh = 지금 요청한 기기 — 비번을 바꿔도 이 기기 세션은 남긴다
    response = await service.update_user(
        db, user_id, req, current_user, password_limiter, keep_refresh=refresh_token,
    )
    return ApiResponse.ok(data=response)

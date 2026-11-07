import logging

from fastapi import APIRouter, Cookie, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_response import ApiResponse
from app.core.config import settings
from app.core.database import DbDepends
from app.core.exceptions import CustomException, ErrorCode
from app.domain.auth import service
from app.domain.auth.schema import LoginRequest, RefreshResponse, TokenResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

_REFRESH_COOKIE_KEY = "refresh_token"
_REFRESH_COOKIE_PATH = "/"


def _set_refresh_cookie(response: Response, refresh_token: str) -> None:
    """Refresh Token을 HttpOnly 쿠키로 설정"""
    response.set_cookie(
        key=_REFRESH_COOKIE_KEY,
        value=refresh_token,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite="lax",
        path=_REFRESH_COOKIE_PATH,
        max_age=settings.JWT_REFRESH_EXPIRATION,
    )


def _delete_refresh_cookie(response: Response) -> None:
    response.delete_cookie(key=_REFRESH_COOKIE_KEY, path=_REFRESH_COOKIE_PATH)


def _refresh_failure_response(error: ErrorCode) -> JSONResponse:
    """refresh 실패 응답 — 쿠키 삭제 + 에러 JSON. 프론트의 무한 polling 차단"""
    resp = JSONResponse(
        status_code=error.status,
        content=ApiResponse.fail(
            status=error.status, code=error.code, message=error.message,
        ).model_dump(),
    )
    resp.delete_cookie(key=_REFRESH_COOKIE_KEY, path=_REFRESH_COOKIE_PATH)
    return resp


@router.post("/login")
async def login(
    req: LoginRequest, response: Response, db: AsyncSession = DbDepends,
) -> ApiResponse[TokenResponse]:
    """로그인 후 JWT 토큰 발급"""
    token_response, refresh_token = await service.login(db, req)
    _set_refresh_cookie(response, refresh_token)
    return ApiResponse.ok(data=token_response)


@router.post("/refresh")
async def refresh(
    db: AsyncSession = DbDepends,
    refresh_token: str | None = Cookie(None, alias=_REFRESH_COOKIE_KEY),
) -> ApiResponse[RefreshResponse]:
    """Refresh Token으로 새 Access Token 발급.

    실패 시 refresh_token 쿠키 삭제 — 클라가 redirect 후 다시 protected 페이지로
    들어가도 guard 가 cookie 없음을 확인하고 /login 으로 가도록 보장.
    """
    if not refresh_token:
        logger.warning("리프레시 토큰 쿠키 없이 갱신 요청")
        return _refresh_failure_response(ErrorCode.INVALID_REFRESH_TOKEN)
    try:
        result = await service.refresh(db, refresh_token)
    except CustomException as e:
        return _refresh_failure_response(e.error_code)
    return ApiResponse.ok(data=result)


@router.post("/logout")
async def logout(
    response: Response,
    db: AsyncSession = DbDepends,
    refresh_token: str | None = Cookie(None, alias=_REFRESH_COOKIE_KEY),
) -> ApiResponse[None]:
    """로그아웃 (refresh token DB 폐기 + 쿠키 삭제)"""
    if refresh_token:
        await service.logout(db, refresh_token)
    _delete_refresh_cookie(response)
    return ApiResponse.ok()

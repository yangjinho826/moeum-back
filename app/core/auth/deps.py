from typing import Annotated
from uuid import UUID

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import ExpiredSignatureError, JWTError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.enum import TokenType
from app.core.auth.jwt import decode_token
from app.core.database import DbDepends
from app.core.enums.data_status import DataStatus
from app.core.exceptions import CustomException, ErrorCode
from app.domain.user.model import User
from app.domain.user.repository import UserRepository

bearer_scheme = HTTPBearer()


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer_scheme)],
    db: AsyncSession = DbDepends,
) -> User:
    """Bearer 토큰에서 현재 로그인 유저를 조회"""
    try:
        payload = decode_token(credentials.credentials)
    except ExpiredSignatureError:
        raise CustomException(ErrorCode.EXPIRED_TOKEN)
    except JWTError:
        raise CustomException(ErrorCode.INVALID_TOKEN)

    if payload.get("type") != TokenType.ACCESS:
        raise CustomException(ErrorCode.INVALID_TOKEN)

    sub = payload.get("sub")
    if not sub:
        raise CustomException(ErrorCode.INVALID_TOKEN)

    user = await UserRepository(db).find_by_id(UUID(sub))
    if not user:
        raise CustomException(ErrorCode.UNAUTHORIZED)
    return user


async def get_current_active_user(
    current_user: Annotated[User, Depends(get_current_user)],
) -> User:
    """활성 상태 유저인지 확인"""
    if current_user.data_stat_cd != DataStatus.ACTIVE:
        raise CustomException(ErrorCode.FORBIDDEN)
    return current_user


CurrentUser = Annotated[User, Depends(get_current_active_user)]

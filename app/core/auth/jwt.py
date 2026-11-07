from datetime import datetime, timedelta, timezone
from uuid import uuid4

from jose import jwt

from app.core.auth.enum import TokenType
from app.core.config import settings


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    """Access Token 생성

    Args:
        data: 페이로드에 담을 데이터 (예: {"sub": "1", "role": "ADMIN"})
        expires_delta: 만료 시간 (미지정 시 settings.JWT_EXPIRATION 사용)

    Returns:
        JWT 인코딩된 Access Token 문자열
    """
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta if expires_delta
        else timedelta(seconds=settings.JWT_EXPIRATION)
    )
    to_encode.update({"exp": expire, "type": TokenType.ACCESS})
    return jwt.encode(to_encode, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def create_refresh_token(data: dict, expires_delta: timedelta | None = None) -> str:
    """Refresh Token 생성

    Args:
        data: 페이로드에 담을 데이터 (예: {"sub": "1"})
        expires_delta: 만료 시간 (미지정 시 settings.JWT_REFRESH_EXPIRATION 사용)

    Returns:
        JWT 인코딩된 Refresh Token 문자열
    """
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta if expires_delta
        else timedelta(seconds=settings.JWT_REFRESH_EXPIRATION)
    )
    # jti: 같은 초에 발급해도 문자열이 달라야 DB 조회·폐기가 기기 하나만 가리킨다
    to_encode.update({"exp": expire, "type": TokenType.REFRESH, "jti": uuid4().hex})
    return jwt.encode(to_encode, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    """토큰 검증 + 페이로드 반환

    Args:
        token: JWT 토큰 문자열

    Returns:
        디코딩된 페이로드 딕셔너리

    Raises:
        ExpiredSignatureError: 토큰 만료
        JWTError: 유효하지 않은 토큰
    """
    return jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])

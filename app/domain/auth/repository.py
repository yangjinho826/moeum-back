from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums.data_status import DataStatus
from app.domain.auth.model import RefreshToken


class RefreshTokenRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def find_active_by_user_id(self, user_id: UUID) -> list[RefreshToken]:
        result = await self.db.execute(
            select(RefreshToken).where(
                and_(
                    RefreshToken.user_id == user_id,
                    RefreshToken.data_stat_cd == DataStatus.ACTIVE,
                )
            )
        )
        return list(result.scalars().all())

    async def find_active_by_token(self, token: str) -> RefreshToken | None:
        result = await self.db.execute(
            select(RefreshToken).where(
                and_(
                    RefreshToken.token == token,
                    RefreshToken.data_stat_cd == DataStatus.ACTIVE,
                )
            )
        )
        # jti 도입 전 같은 초 로그인은 같은 문자열 — 그런 행이 둘 남아 있어도 500 이 아니게
        return result.scalars().first()

    async def find_by_token(self, token: str) -> RefreshToken | None:
        """상태 무관하게 토큰 조회 (디버깅용)"""
        result = await self.db.execute(
            select(RefreshToken).where(RefreshToken.token == token)
        )
        return result.scalars().first()

    async def revoke_all_for_user(self, user_id: UUID, *, except_token: str | None = None) -> int:
        """그 사용자의 활성 refresh 전부 폐기 — except_token(지금 요청한 기기)만 남긴다."""
        conditions = [
            RefreshToken.user_id == user_id,
            RefreshToken.data_stat_cd == DataStatus.ACTIVE,
        ]
        if except_token:
            conditions.append(RefreshToken.token != except_token)
        result = await self.db.execute(
            update(RefreshToken)
            .where(and_(*conditions))
            .values(data_stat_cd=DataStatus.DELETED, revoked_at=datetime.now())
            .execution_options(synchronize_session=False)
        )
        return result.rowcount or 0

    async def save(self, token: RefreshToken) -> None:
        self.db.add(token)
        await self.db.flush()

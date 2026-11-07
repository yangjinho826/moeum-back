from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums.data_status import DataStatus
from app.domain.fixed.model import FixedExpense


class FixedRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def find_by_id(self, fixed_id: UUID) -> FixedExpense | None:
        result = await self.db.execute(
            select(FixedExpense).where(
                and_(
                    FixedExpense.id == fixed_id,
                    FixedExpense.data_stat_cd == DataStatus.ACTIVE,
                )
            )
        )
        return result.scalar_one_or_none()

    async def has_active_for_category(self, category_id: UUID) -> bool:
        """이 카테고리를 쓰는 활성 고정지출이 있나 — 카테고리 kind 변경 가드용."""
        result = await self.db.execute(
            select(FixedExpense.id)
            .where(
                and_(
                    FixedExpense.category_id == category_id,
                    FixedExpense.data_stat_cd == DataStatus.ACTIVE,
                )
            )
            .limit(1)
        )
        return result.first() is not None

    async def find_by_ids(self, ids: list[UUID]) -> list[FixedExpense]:
        """거래 응답에 고정지출명을 붙이기 위한 batch 조회 — 상태 필터 없음.

        보관/삭제된 고정지출도 과거 거래에는 그대로 물려 있다. ACTIVE 로 거르면
        그 거래들이 이름 없이 표시되므로, 여기서는 id 로만 찾는다.
        """
        if not ids:
            return []
        result = await self.db.execute(
            select(FixedExpense).where(FixedExpense.id.in_(ids))
        )
        return list(result.scalars().all())

    async def find_active_by_household_id(self, household_id: UUID) -> list[FixedExpense]:
        result = await self.db.execute(
            select(FixedExpense)
            .where(
                and_(
                    FixedExpense.household_id == household_id,
                    FixedExpense.data_stat_cd == DataStatus.ACTIVE,
                )
            )
            .order_by(FixedExpense.sort_order.asc(), FixedExpense.frst_reg_dt.asc())
        )
        return list(result.scalars().all())

    async def search_by_household_id(
        self,
        household_id: UUID,
        *,
        search_term: str | None = None,
        is_archived: bool | None = None,
    ) -> list[FixedExpense]:
        conditions = [
            FixedExpense.household_id == household_id,
            FixedExpense.data_stat_cd == DataStatus.ACTIVE,
        ]
        if search_term:
            conditions.append(FixedExpense.name.ilike(f"%{search_term.strip()}%"))
        if is_archived is not None:
            conditions.append(FixedExpense.is_archived == is_archived)

        result = await self.db.execute(
            select(FixedExpense)
            .where(and_(*conditions))
            .order_by(FixedExpense.sort_order.asc(), FixedExpense.frst_reg_dt.asc())
        )
        return list(result.scalars().all())

    def _build_search_conditions(
        self,
        household_id: UUID,
        *,
        search_term: str | None,
        is_archived: bool | None,
    ):
        conditions = [
            FixedExpense.household_id == household_id,
            FixedExpense.data_stat_cd == DataStatus.ACTIVE,
        ]
        if search_term:
            conditions.append(FixedExpense.name.ilike(f"%{search_term.strip()}%"))
        if is_archived is not None:
            conditions.append(FixedExpense.is_archived == is_archived)
        return conditions

    @staticmethod
    def _cursor_after(cursor: str | None):
        """frst_reg_dt DESC, id DESC 기준 cursor — `{datetime_iso}|{uuid}` 평문."""
        if not cursor:
            return None
        try:
            dt_str, id_str = cursor.split("|", 1)
            cur_dt = datetime.fromisoformat(dt_str)
            cur_id = UUID(id_str)
        except (ValueError, AttributeError):
            return None
        return or_(
            FixedExpense.frst_reg_dt < cur_dt,
            and_(FixedExpense.frst_reg_dt == cur_dt, FixedExpense.id < cur_id),
        )

    async def list_by_cursor(
        self,
        household_id: UUID,
        *,
        search_term: str | None = None,
        is_archived: bool | None = None,
        cursor: str | None = None,
        limit: int = 30,
    ) -> list[FixedExpense]:
        conds = self._build_search_conditions(
            household_id,
            search_term=search_term,
            is_archived=is_archived,
        )
        cursor_cond = self._cursor_after(cursor)
        if cursor_cond is not None:
            conds.append(cursor_cond)
        result = await self.db.execute(
            select(FixedExpense)
            .where(and_(*conds))
            .order_by(FixedExpense.frst_reg_dt.desc(), FixedExpense.id.desc())
            .limit(limit + 1)
        )
        return list(result.scalars().all())

    async def count_search(
        self,
        household_id: UUID,
        *,
        search_term: str | None = None,
        is_archived: bool | None = None,
    ) -> int:
        conds = self._build_search_conditions(
            household_id,
            search_term=search_term,
            is_archived=is_archived,
        )
        result = await self.db.execute(
            select(func.count(FixedExpense.id)).where(and_(*conds))
        )
        return result.scalar() or 0

    async def max_sort_order(self, household_id: UUID) -> int:
        result = await self.db.execute(
            select(func.max(FixedExpense.sort_order)).where(
                and_(
                    FixedExpense.household_id == household_id,
                    FixedExpense.data_stat_cd == DataStatus.ACTIVE,
                )
            )
        )
        return result.scalar() or 0

    async def save(self, fixed: FixedExpense) -> None:
        self.db.add(fixed)
        await self.db.flush()

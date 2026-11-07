from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums.data_status import DataStatus
from app.domain.household.model import Household, HouseholdMember


class HouseholdRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def find_by_id(self, household_id: UUID) -> Household | None:
        result = await self.db.execute(
            select(Household).where(
                and_(
                    Household.id == household_id,
                    Household.data_stat_cd == DataStatus.ACTIVE,
                )
            )
        )
        return result.scalar_one_or_none()

    async def has_active_owned_by(self, user_id: UUID) -> bool:
        """이 사용자가 owner 인 활성 가계부가 있나 — 멤버 초대(이메일 검색) 자격."""
        result = await self.db.execute(
            select(Household.id)
            .where(
                and_(
                    Household.owner_id == user_id,
                    Household.data_stat_cd == DataStatus.ACTIVE,
                )
            )
            .limit(1)
        )
        return result.first() is not None

    async def find_active_by_user_id(self, user_id: UUID) -> list[Household]:
        """현재 user 가 멤버인 활성 가계부 목록"""
        stmt = (
            select(Household)
            .join(HouseholdMember, HouseholdMember.household_id == Household.id)
            .where(
                and_(
                    HouseholdMember.user_id == user_id,
                    HouseholdMember.data_stat_cd == DataStatus.ACTIVE,
                    Household.data_stat_cd == DataStatus.ACTIVE,
                )
            )
            .order_by(Household.frst_reg_dt.asc())
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def find_all_active(self) -> list[Household]:
        """모든 활성 가계부 — 월간 자동 스냅샷 배치용."""
        result = await self.db.execute(
            select(Household).where(Household.data_stat_cd == DataStatus.ACTIVE)
        )
        return list(result.scalars().all())

    async def save(self, household: Household) -> None:
        self.db.add(household)
        await self.db.flush()


class HouseholdMemberRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def find_by_household_and_user(
        self, household_id: UUID, user_id: UUID
    ) -> HouseholdMember | None:
        result = await self.db.execute(
            select(HouseholdMember).where(
                and_(
                    HouseholdMember.household_id == household_id,
                    HouseholdMember.user_id == user_id,
                    HouseholdMember.data_stat_cd == DataStatus.ACTIVE,
                )
            )
        )
        return result.scalar_one_or_none()

    async def find_by_id(self, member_id: UUID) -> HouseholdMember | None:
        result = await self.db.execute(
            select(HouseholdMember).where(
                and_(
                    HouseholdMember.id == member_id,
                    HouseholdMember.data_stat_cd == DataStatus.ACTIVE,
                )
            )
        )
        return result.scalar_one_or_none()

    async def find_active_by_household_id(
        self, household_id: UUID,
    ) -> list[HouseholdMember]:
        stmt = (
            select(HouseholdMember)
            .where(
                and_(
                    HouseholdMember.household_id == household_id,
                    HouseholdMember.data_stat_cd == DataStatus.ACTIVE,
                )
            )
            .order_by(HouseholdMember.joined_at.asc())
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def save(self, member: HouseholdMember) -> None:
        self.db.add(member)
        await self.db.flush()

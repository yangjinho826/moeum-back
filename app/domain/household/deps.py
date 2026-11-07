from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.deps import get_current_active_user
from app.core.database import DbDepends
from app.core.exceptions import CustomException, ErrorCode
from app.domain.household.model import Household
from app.domain.household.repository import (
    HouseholdMemberRepository,
    HouseholdRepository,
)
from app.domain.user.model import User


async def get_current_household(
    current_user: Annotated[User, Depends(get_current_active_user)],
    db: Annotated[AsyncSession, DbDepends],
    x_household_id: Annotated[UUID, Header(alias="X-Household-Id")],
) -> Household:
    """X-Household-Id 헤더 → 멤버십 검증 → Household 반환"""
    member = await HouseholdMemberRepository(db).find_by_household_and_user(
        x_household_id, current_user.id,
    )
    if not member:
        raise CustomException(ErrorCode.HOUSEHOLD_NOT_MEMBER)

    household = await HouseholdRepository(db).find_by_id(x_household_id)
    if not household:
        raise CustomException(ErrorCode.HOUSEHOLD_NOT_FOUND)
    return household


CurrentHousehold = Annotated[Household, Depends(get_current_household)]

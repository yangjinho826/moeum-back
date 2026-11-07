import logging
from datetime import datetime, date
from uuid import UUID

from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums.data_status import DataStatus
from app.core.demo.guard import is_demo_user
from app.core.exceptions import CustomException, ErrorCode
from app.core.pagination import CursorPage
from app.domain.account.model import Account
from app.domain.account_snapshot.model import AccountSnapshot
from app.domain.category.model import Category
from app.domain.fixed.model import FixedExpense
from app.domain.household.enum import HouseholdRole
from app.domain.household.model import Household, HouseholdMember
from app.domain.household.repository import (
    HouseholdMemberRepository,
    HouseholdRepository,
)
from app.domain.portfolio.model import (
    PortfolioItem,
    PortfolioTransaction,
    PortfolioValueHistory,
)
from app.domain.transaction.model import Transaction
from app.domain.household.schema import (
    HouseholdCreateRequest,
    HouseholdListResponse,
    HouseholdMemberCreateRequest,
    HouseholdMemberListResponse,
    HouseholdMemberResponse,
    HouseholdResponse,
    HouseholdUpdateRequest,
)
from app.domain.user.model import User
from app.domain.user.repository import UserRepository

logger = logging.getLogger(__name__)


async def list_households(
    db: AsyncSession, current_user: User,
) -> HouseholdListResponse:
    """현재 user 가 멤버인 가계부 목록.

    한 user 의 가입 household 가 보통 1~5개라 cursor 페이징은 의미 없음.
    CursorPage 형식만 통일 (next_cursor=None, has_next=False, total_count=len).
    """
    repo = HouseholdRepository(db)
    households = await repo.find_active_by_user_id(current_user.id)
    items = [
        _build_response(
            h,
            HouseholdRole.OWNER if h.owner_id == current_user.id else HouseholdRole.MEMBER,
        )
        for h in households
    ]
    return CursorPage(
        items=items, next_cursor=None, has_next=False, total_count=len(items),
    )


async def create_household(
    db: AsyncSession, req: HouseholdCreateRequest, current_user: User,
) -> HouseholdResponse:
    """가계부 생성 + owner 멤버 row 자동 등록"""
    household_repo = HouseholdRepository(db)
    member_repo = HouseholdMemberRepository(db)

    household = Household(
        name=req.name.strip(),
        description=req.description,
        owner_id=current_user.id,
        currency=req.currency.upper(),
        started_at=req.started_at or date.today(),
        data_stat_cd=DataStatus.ACTIVE,
    )
    await household_repo.save(household)

    owner_member = HouseholdMember(
        household_id=household.id,
        user_id=current_user.id,
        role=HouseholdRole.OWNER,
        joined_at=datetime.now(),
        data_stat_cd=DataStatus.ACTIVE,
    )
    await member_repo.save(owner_member)

    logger.info("가계부 생성 (household_id=%s, owner_id=%s)", household.id, current_user.id)
    return _build_response(household, HouseholdRole.OWNER)


async def update_household(
    db: AsyncSession,
    household_id: UUID,
    req: HouseholdUpdateRequest,
    current_user: User,
) -> HouseholdResponse:
    """가계부 수정 (owner 만)"""
    repo = HouseholdRepository(db)
    household = await repo.find_by_id(household_id)
    if not household:
        raise CustomException(ErrorCode.HOUSEHOLD_NOT_FOUND)
    if household.owner_id != current_user.id:
        raise CustomException(ErrorCode.HOUSEHOLD_NOT_OWNER)

    if req.name is not None:
        household.name = req.name.strip()
    if req.description is not None:
        household.description = req.description
    if req.currency is not None:
        household.currency = req.currency.upper()
    if req.started_at is not None:
        household.started_at = req.started_at

    await db.flush()
    return _build_response(household, HouseholdRole.OWNER)


async def delete_household(
    db: AsyncSession, household_id: UUID, current_user: User,
) -> None:
    """가계부 soft delete (owner 만) — 모든 자식도 함께 cascade soft-delete."""
    repo = HouseholdRepository(db)
    household = await repo.find_by_id(household_id)
    if not household:
        raise CustomException(ErrorCode.HOUSEHOLD_NOT_FOUND)
    if household.owner_id != current_user.id:
        raise CustomException(ErrorCode.HOUSEHOLD_NOT_OWNER)
    if is_demo_user(current_user):  # 지우면 다음 리셋(05:00)까지 체험 화면이 빈다
        raise CustomException(ErrorCode.DEMO_READ_ONLY)

    await _cascade_soft_delete_children(db, household_id)
    household.data_stat_cd = DataStatus.DELETED
    await db.flush()
    logger.info("가계부 cascade 삭제 (household_id=%s)", household_id)


async def list_household_members(
    db: AsyncSession, household_id: UUID, current_user: User,
) -> HouseholdMemberListResponse:
    """가계부 멤버 목록 — 본인이 멤버일 때만 조회 가능.

    한 가계부 멤버 수가 보통 1~10 이라 cursor 페이징은 의미 없음.
    CursorPage 형식만 통일.
    """
    await _require_membership(db, household_id, current_user.id)

    member_repo = HouseholdMemberRepository(db)
    members = await member_repo.find_active_by_household_id(household_id)

    user_ids = [m.user_id for m in members]
    users = await UserRepository(db).find_by_ids(user_ids) if user_ids else []
    user_map = {u.id: u for u in users}

    items = [_build_member_response(m, user_map.get(m.user_id)) for m in members]
    return CursorPage(
        items=items, next_cursor=None, has_next=False, total_count=len(items),
    )


async def add_household_member(
    db: AsyncSession,
    household_id: UUID,
    req: HouseholdMemberCreateRequest,
    current_user: User,
) -> HouseholdMemberResponse:
    """가계부 멤버 추가 (owner 만)"""
    await _require_owner(db, household_id, current_user.id)

    user_repo = UserRepository(db)
    target = await user_repo.find_by_id(req.user_id)
    if not target:
        raise CustomException(ErrorCode.NOT_FOUND)

    member_repo = HouseholdMemberRepository(db)
    existing = await member_repo.find_by_household_and_user(household_id, req.user_id)
    if existing:
        raise CustomException(ErrorCode.HOUSEHOLD_MEMBER_ALREADY)

    member = HouseholdMember(
        household_id=household_id,
        user_id=req.user_id,
        role=req.role,
        joined_at=datetime.now(),
        data_stat_cd=DataStatus.ACTIVE,
    )
    await member_repo.save(member)
    logger.info(
        "가계부 멤버 추가 (household_id=%s, user_id=%s, role=%s)",
        household_id, req.user_id, req.role,
    )
    return _build_member_response(member, target)


async def remove_household_member(
    db: AsyncSession,
    household_id: UUID,
    member_id: UUID,
    current_user: User,
) -> None:
    """가계부 멤버 제거 (owner 만, owner 본인은 제거 불가)"""
    household = await _require_owner(db, household_id, current_user.id)

    member_repo = HouseholdMemberRepository(db)
    member = await member_repo.find_by_id(member_id)
    if not member or member.household_id != household_id:
        raise CustomException(ErrorCode.HOUSEHOLD_MEMBER_NOT_FOUND)
    if member.user_id == household.owner_id:
        raise CustomException(ErrorCode.HOUSEHOLD_OWNER_CANNOT_LEAVE)

    member.data_stat_cd = DataStatus.DELETED
    await db.flush()
    logger.info("가계부 멤버 제거 (member_id=%s)", member_id)


async def get_household_detail(
    db: AsyncSession, household_id: UUID, current_user: User,
) -> HouseholdResponse:
    """가계부 단건 조회 — 멤버만 접근 가능"""
    repo = HouseholdRepository(db)
    member_repo = HouseholdMemberRepository(db)
    household = await repo.find_by_id(household_id)
    if not household or household.data_stat_cd != DataStatus.ACTIVE:
        raise CustomException(ErrorCode.HOUSEHOLD_NOT_FOUND)

    membership = await member_repo.find_by_household_and_user(
        household_id, current_user.id,
    )
    if not membership:
        raise CustomException(ErrorCode.HOUSEHOLD_NOT_FOUND)

    role = (
        HouseholdRole.OWNER
        if household.owner_id == current_user.id
        else HouseholdRole(membership.role)
    )
    return _build_response(household, role)


def _build_response(household: Household, role: HouseholdRole) -> HouseholdResponse:
    return HouseholdResponse(
        id=household.id,
        name=household.name,
        description=household.description,
        owner_id=household.owner_id,
        currency=household.currency,
        started_at=household.started_at,
        role=role,
    )


# 가계부 삭제 시 함께 soft-delete 할 자식 (모두 household_id 보유).
# account_snapshots 는 household_id 가 없어 account_id 기반으로 별도 처리.
_CHILD_MODELS_WITH_HOUSEHOLD_ID = (
    Account,
    Category,
    Transaction,
    PortfolioItem,
    PortfolioTransaction,
    PortfolioValueHistory,
    FixedExpense,
    HouseholdMember,
)


async def _cascade_soft_delete_children(db: AsyncSession, household_id: UUID) -> None:
    """가계부의 모든 활성 자식을 soft-delete. soft-delete 라 순서 무관(FK cascade 아님)."""
    for model in _CHILD_MODELS_WITH_HOUSEHOLD_ID:
        await db.execute(
            update(model)
            .where(
                and_(
                    model.household_id == household_id,
                    model.data_stat_cd == DataStatus.ACTIVE,
                )
            )
            .values(data_stat_cd=DataStatus.DELETED)
        )
    # account_snapshots — household 의 통장 id 들로 (통장은 위에서 이미 soft-delete 됐지만
    # 행은 남아있어 household_id 매칭 select 는 그대로 동작)
    await db.execute(
        update(AccountSnapshot)
        .where(
            and_(
                AccountSnapshot.account_id.in_(
                    select(Account.id).where(Account.household_id == household_id)
                ),
                AccountSnapshot.data_stat_cd == DataStatus.ACTIVE,
            )
        )
        .values(data_stat_cd=DataStatus.DELETED)
    )


def _build_member_response(
    member: HouseholdMember, user: User | None,
) -> HouseholdMemberResponse:
    return HouseholdMemberResponse(
        id=member.id,
        household_id=member.household_id,
        user_id=member.user_id,
        user_name=user.name if user else None,
        user_email=user.email if user else None,
        role=HouseholdRole(member.role),
        joined_at=member.joined_at,
    )


async def _require_membership(
    db: AsyncSession, household_id: UUID, user_id: UUID,
) -> HouseholdMember:
    """현재 user 가 해당 household 멤버인지 검증, 멤버 반환"""
    member_repo = HouseholdMemberRepository(db)
    membership = await member_repo.find_by_household_and_user(household_id, user_id)
    if not membership:
        raise CustomException(ErrorCode.HOUSEHOLD_NOT_MEMBER)
    return membership


async def _require_owner(
    db: AsyncSession, household_id: UUID, user_id: UUID,
) -> Household:
    """owner 권한 검증, household 반환"""
    repo = HouseholdRepository(db)
    household = await repo.find_by_id(household_id)
    if not household:
        raise CustomException(ErrorCode.HOUSEHOLD_NOT_FOUND)
    if household.owner_id != user_id:
        raise CustomException(ErrorCode.HOUSEHOLD_NOT_OWNER)
    return household

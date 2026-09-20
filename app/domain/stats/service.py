import logging
from decimal import Decimal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.category.enum import CategoryKind
from app.domain.category.repository import CategoryRepository
from app.domain.household.model import Household
from app.domain.stats.schema import CategoryStatsItem, MonthlyStatsResponse
from app.domain.transaction.repository import TransactionRepository

logger = logging.getLogger(__name__)


async def get_monthly_stats(
    db: AsyncSession, household: Household, year: int, month: int,
    account_id: UUID | None = None,
) -> MonthlyStatsResponse:
    """월간 카테고리별 지출/수입 집계 — 이번 달 지출 카드 + 차트.

    account_id 를 주면 그 통장이 낀 거래만(거래 탭 통장 필터). 없으면 가구 전체.
    """
    tx_repo = TransactionRepository(db)
    cat_repo = CategoryRepository(db)

    type_sums = await tx_repo.sum_by_type_for_month(
        household.id, year, month, account_id,
    )
    category_sums = await tx_repo.sum_by_category_for_month(
        household.id, year, month, account_id,
    )

    # 거래에 등장한 카테고리만 id 로 조회 — find_by_ids 는 필터가 없어 삭제된
    # 카테고리도 포함된다. 삭제된 카테고리의 기존 거래가 by_category 에서 누락되지 않도록.
    cat_ids = [cat_id for cat_id, _ in category_sums]
    categories = await cat_repo.find_by_ids(cat_ids)
    cat_map = {c.id: c for c in categories}

    # 같은 kind 안에서 max 잡기 (ratio 계산용)
    kind_max: dict[bool, Decimal] = {True: Decimal("0"), False: Decimal("0")}
    valid_rows: list[tuple] = []
    for cat_id, amount in category_sums:
        if amount <= 0 or cat_id not in cat_map:
            continue
        cat = cat_map[cat_id]
        is_income = cat.kind == CategoryKind.INCOME
        valid_rows.append((cat, amount, is_income))
        if amount > kind_max[is_income]:
            kind_max[is_income] = amount

    items = [
        CategoryStatsItem(
            category_id=cat.id,
            name=cat.name,
            icon=cat.icon,
            color=cat.color,
            is_income=is_income,
            amount=amount,
            ratio=(amount / kind_max[is_income]) if kind_max[is_income] > 0 else Decimal("0.00"),
        )
        for cat, amount, is_income in valid_rows
    ]
    items.sort(key=lambda x: x.amount, reverse=True)

    return MonthlyStatsResponse(
        year=year,
        month=month,
        monthly_income=type_sums["income"],
        monthly_expense=type_sums["expense"],
        monthly_transfer=type_sums["transfer"],
        by_category=items,
    )

"""자산 페이지 1호출 overview.

account.list_accounts + account_snapshot.get_yearly_snapshots 위임 + 자산군 배분 집계(보관 통장은 합계 밖).
종목 단건/통장 단건 상세는 별도 endpoint 유지 (`/portfolio/accounts/{id}/overview`).
"""

import uuid
from collections import defaultdict
from datetime import date
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.account import service as account_service
from app.domain.account.enum import AccountType
from app.domain.account.model import Account
from app.domain.account.repository import AccountRepository
from app.domain.account.schema import AccountResponse
from app.domain.account_snapshot import service as account_snapshot_service
from app.domain.account_snapshot.model import AccountSnapshot
from app.domain.account_snapshot.repository import AccountSnapshotRepository
from app.domain.household.model import Household
from app.domain.portfolio.enum import AssetClass
from app.domain.portfolio.model import PortfolioValueHistory
from app.domain.portfolio.repository import PortfolioValueHistoryRepository
from app.domain.wealth.schema import (
    AllocationResponse,
    AllocationTrendPoint,
    AssetClassSlice,
    WealthOverviewResponse,
)


# 수동자산/저축 전용계좌 → 자산군 슬라이스 매핑.
# INVESTMENT(현금 분리)·일반계좌(전액 현금)는 별도 처리하므로 여기 없음.
_ASSET_CLASS_BY_TYPE = {
    AccountType.REAL_ESTATE: AssetClass.REAL_ESTATE,
    AccountType.PENSION: AssetClass.PENSION,
    AccountType.COMMODITY: AssetClass.COMMODITY,
    AccountType.SAVINGS_ASSET: AssetClass.SAVINGS,
}


async def get_wealth_overview(
    db: AsyncSession,
    household: Household,
    from_date: date | None,
    to_date: date | None,
) -> WealthOverviewResponse:
    accounts = await account_service.list_accounts(db, household)
    # 보관 통장은 목록엔 두고 합계·배분에서만 뺀다(월 박제와 같은 모집단)
    in_totals = [a for a in accounts if account_service.counts_in_totals(a)]
    total_balance = sum((a.balance for a in in_totals), Decimal("0"))
    current_allocation = _build_allocation(in_totals)

    # 월별 배분추이 — 박제된 스냅샷 재구성 (yearly_snapshots 와 동일 범위)
    from_resolved, to_resolved = account_snapshot_service.resolve_snapshot_range(
        from_date, to_date,
    )
    account_snapshots = await AccountSnapshotRepository(db).find_by_household_and_range(
        household.id, from_resolved, to_resolved,
    )
    portfolio_histories = await PortfolioValueHistoryRepository(
        db
    ).find_by_household_and_range(household.id, from_resolved, to_resolved)
    snapshot_account_ids = list({s.account_id for s in account_snapshots})
    snapshot_accounts = await AccountRepository(db).find_by_ids(snapshot_account_ids)
    allocation = AllocationResponse(
        current_allocation=current_allocation,
        allocation_trend=build_allocation_trend(
            account_snapshots, portfolio_histories, snapshot_accounts,
        ),
    )

    yearly = await account_snapshot_service.get_yearly_snapshots(
        db, household, from_date, to_date,
    )

    return WealthOverviewResponse(
        total_balance=total_balance,
        accounts=accounts,
        yearly_snapshots=yearly,
        allocation=allocation,
    )


def build_allocation_trend(
    account_snapshots: list[AccountSnapshot],
    portfolio_histories: list[PortfolioValueHistory],
    accounts: list[Account],
) -> list[AllocationTrendPoint]:
    """월별 자산군 배분추이 — 박제된 AccountSnapshot + PortfolioValueHistory 재구성.

    종목 PVH valuation 은 전부 INVESTMENT 슬라이스로 합산하고, AccountSnapshot 은
    계좌타입별로 분류(INVESTMENT 는 cash 역산). _add_snapshot_to_month 참고.
    """
    account_type_map = {a.id: a.account_type for a in accounts}

    # (월, 계좌)별 종목 평가액 합 — INVESTMENT cash 역산용
    pvh_by_account: dict[tuple[date, uuid.UUID], Decimal] = defaultdict(
        lambda: Decimal("0")
    )
    trend: dict[date, dict[str, Decimal]] = defaultdict(
        lambda: defaultdict(lambda: Decimal("0"))
    )

    for h in portfolio_histories:
        pvh_by_account[(h.snapshot_date, h.account_id)] += h.valuation
        trend[h.snapshot_date][AssetClass.INVESTMENT.value] += h.valuation

    for s in account_snapshots:
        _add_snapshot_to_month(
            trend[s.snapshot_date],
            account_type_map.get(s.account_id),
            s.balance,
            pvh_by_account[(s.snapshot_date, s.account_id)],
        )

    return [
        AllocationTrendPoint(snapshot_date=d, slices=_slices_to_list(slices))
        for d, slices in sorted(trend.items(), key=lambda kv: kv[0])
    ]


def _slices_to_list(slices: dict[str, Decimal]) -> list[AssetClassSlice]:
    """자산군별 누적액 dict → ratio 채운 슬라이스 리스트. 0값 제외, valuation desc."""
    total = sum(slices.values(), Decimal("0"))
    result = [
        AssetClassSlice(
            asset_class=AssetClass(asset_class),
            valuation=valuation,
            ratio=(valuation / total * Decimal("100")) if total > 0 else Decimal("0"),
        )
        for asset_class, valuation in slices.items()
        if valuation != 0
    ]
    result.sort(key=lambda s: s.valuation, reverse=True)
    return result


def _build_allocation(accounts: list[AccountResponse]) -> list[AssetClassSlice]:
    """현재 시점 자산군별 배분 — 호출자가 집계 대상 통장만 넘긴다.

    - INVESTMENT 통장은 현금은 CASH, 종목 평가액(portfolio_valuation = qty * current_price,
      잔액 계산과 같은 값)은 INVESTMENT 슬라이스. 그 외 통장은 balance 전체가 현금.
    - REAL_ESTATE/PENSION/COMMODITY 전용계좌는 balance 가 해당 슬라이스(수동자산 roll-up).
    """
    slices: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))

    for a in accounts:
        if a.account_type == AccountType.INVESTMENT:
            slices[AssetClass.CASH.value] += a.cash or Decimal("0")
            slices[AssetClass.INVESTMENT.value] += a.portfolio_valuation or Decimal("0")
        elif a.account_type in _ASSET_CLASS_BY_TYPE:
            slices[_ASSET_CLASS_BY_TYPE[a.account_type].value] += a.balance
        else:
            slices[AssetClass.CASH.value] += a.balance

    return _slices_to_list(slices)


def _add_snapshot_to_month(
    month: dict[str, Decimal],
    account_type: AccountType | None,
    balance: Decimal,
    pvh_valuation: Decimal,
) -> None:
    """박제 스냅샷 balance 를 계좌타입별 자산군 슬라이스에 누적.

    INVESTMENT 는 cash = balance − 그달 종목평가 역산(나머지는 종목 슬라이스에서 별도 합산).
    수동자산 전용계좌는 매핑대로, 그 외 일반계좌는 전액 CASH.
    """
    if account_type == AccountType.INVESTMENT:
        month[AssetClass.CASH.value] += balance - pvh_valuation
    elif account_type in _ASSET_CLASS_BY_TYPE:
        month[_ASSET_CLASS_BY_TYPE[account_type].value] += balance
    else:
        month[AssetClass.CASH.value] += balance

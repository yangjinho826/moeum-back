"""Portfolio 월별 박제(PortfolioValueHistory) 전용 로직.

책임:
- 자산 스냅샷 시점의 모든 INVESTMENT 통장 종목 상태를 1행씩 기록
- 가격/수량/평가액을 그 시점 그대로 보존 (차트의 라인 1개)

호출처: `account_snapshot/service.py` 의 `_build_and_save_snapshot` — 매월 박제 시 같이 실행.
"""

import logging
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums.data_status import DataStatus
from app.domain.account.enum import AccountType
from app.domain.account.repository import AccountRepository
from app.domain.account.service import counts_in_totals
from app.domain.household.model import Household
from app.domain.market_price import service as market_price_service
from app.domain.portfolio.model import PortfolioValueHistory
from app.domain.portfolio.repository import (
    PortfolioTransactionRepository,
    PortfolioValueHistoryRepository,
)

logger = logging.getLogger(__name__)


async def snapshot_household_portfolio(
    db: AsyncSession,
    household: Household,
    snapshot_date: date,
    as_of: date,
    *,
    replace: bool = False,
) -> list[PortfolioValueHistory]:
    """가계부의 모든 INVESTMENT 통장 종목을 as_of(그 달 말일) 시점 보유로 박제.

    현재 보유가 아니라 as_of 까지의 거래로 재구성한 보유수량을 원가로 평가한다
    (과거 시가 이력이 없어 원가 기준). 이렇게 해야 as_of 현금과 짝이 맞아
    account_snapshots.balance = cash + Σvaluation 의 이중계상이 사라진다.
    replace=True 면 그달 기존 박제를 먼저 지우고 다시 만듦 (upsert).
    호출처: account_snapshot/service.py 의 _build_and_save_snapshot
    """
    account_repo = AccountRepository(db)
    tx_repo = PortfolioTransactionRepository(db)
    history_repo = PortfolioValueHistoryRepository(db)

    if replace:
        await history_repo.delete_for_household_month(household.id, snapshot_date)

    accounts = await account_repo.find_active_by_household_id(household.id)
    investment_accounts = [
        a for a in accounts
        if a.account_type == AccountType.INVESTMENT and counts_in_totals(a)
    ]

    # 계좌별 holdings 모아 전체를 그 달 시가로 평가 (item 현재 code/market 기준, N+1 방지).
    # 시가 없으면 원가 fallback — value_holdings_at_month 가 처리.
    per_account: list[tuple] = []
    all_holdings: list[dict] = []
    for a in investment_accounts:
        holdings = await tx_repo.asof_holdings_by_account(a.id, as_of)
        per_account.append((a, holdings))
        all_holdings.extend(holdings)

    valued = await market_price_service.value_holdings_at_month(
        db, all_holdings, snapshot_date,
    )

    histories: list[PortfolioValueHistory] = []
    for a, holdings in per_account:
        for h in holdings:
            v = valued[h["item_id"]]
            histories.append(
                PortfolioValueHistory(
                    household_id=household.id,
                    account_id=a.id,
                    portfolio_item_id=h["item_id"],
                    snapshot_date=snapshot_date,
                    quantity=h["quantity"],
                    avg_price=h["avg_cost"],
                    current_price=v["current_price"],
                    cost=h["cost"],
                    valuation=v["valuation"],
                    data_stat_cd=DataStatus.ACTIVE,
                )
            )

    await history_repo.save_all(histories)
    logger.info(
        "종목 박제 완료 (household_id=%s, date=%s, as_of=%s, items=%d)",
        household.id, snapshot_date, as_of, len(histories),
    )
    return histories

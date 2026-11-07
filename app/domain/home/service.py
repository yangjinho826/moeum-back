"""홈 페이지 진입 1호출 overview.

기존 도메인 service (account / transaction / stats) 를 합치는 얇은 집계 레이어.
재구현하지 않고 호출 위임만 한다.
"""

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.account import service as account_service
from app.domain.home.schema import HomeOverviewResponse
from app.domain.household.model import Household
from app.domain.stats import service as stats_service
from app.domain.transaction import service as transaction_service
from app.domain.transaction.repository import TransactionFilter

KST = ZoneInfo("Asia/Seoul")

# 홈 페이지 최근 거래 노출 건수 — 토스 스타일 카드
RECENT_TX_LIMIT = 10


async def get_home_overview(
    db: AsyncSession,
    household: Household,
    year: int | None,
    month: int | None,
) -> HomeOverviewResponse:
    """통장 목록 + 최근 거래 + 월간 stats 묶음.

    year/month 미지정 시 KST 현재.
    """
    if year is None or month is None:
        year, month = _today_kst()

    accounts = await account_service.list_accounts(db, household)
    # 보관 통장은 목록엔 두고 합계에서만 뺀다 — 월 박제와 같은 모집단이어야 hero 증감이 맞는다
    total_balance = sum(
        (a.balance for a in accounts if account_service.counts_in_totals(a)), Decimal("0"),
    )

    tx_page = await transaction_service.list_transactions(
        db, household, TransactionFilter(), cursor=None, limit=RECENT_TX_LIMIT,
    )

    stats = await stats_service.get_monthly_stats(db, household, year, month)

    return HomeOverviewResponse(
        total_balance=total_balance,
        accounts=accounts,
        recent_transactions=tx_page.items,
        stats=stats,
        year=year,
        month=month,
    )


def _today_kst() -> tuple[int, int]:
    now = datetime.now(KST)
    return now.year, now.month

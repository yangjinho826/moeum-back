import logging
from calendar import monthrange
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums.data_status import DataStatus
from app.domain.account.model import Account
from app.domain.account.repository import AccountRepository
from app.domain.account.service import _calc_balance, counts_in_totals
from app.domain.account_snapshot.model import AccountSnapshot
from app.domain.account_snapshot.repository import AccountSnapshotRepository
from app.domain.account_snapshot.schema import (
    SnapshotMonth,
    SnapshotMonthBalance,
    SnapshotYearlyResponse,
)
from app.domain.household.model import Household
from app.domain.household.repository import HouseholdRepository
from app.domain.market_price import service as market_price_service
from app.domain.portfolio.snapshot_service import snapshot_household_portfolio
from app.domain.transaction.repository import TransactionRepository

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")

# catch-up 으로 거슬러 올라가 빠진 달을 채울 최근 개월 수
SNAPSHOT_CATCHUP_MONTHS = 12

# 박제 advisory lock 의 키 공간 — 2인자 형식이라 잡 lock(hashtext(잡 이름), 1인자)과 겹치지 않는다
SNAPSHOT_LOCK_NS = 1001


async def _lock_household_snapshot(db: AsyncSession, household_id: UUID) -> None:
    """가계부 박제 직렬화 — 대기형 트랜잭션 lock, commit/rollback 에 자동 해제.

    "그 달 있나 → (지우고) 넣기" 가 겹치면 통장마다 2벌이 된다(yearly 합계 2배)."""
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:ns, hashtext(:hid))"),
        {"ns": SNAPSHOT_LOCK_NS, "hid": str(household_id)},
    )


async def create_monthly_snapshots_for_all(db: AsyncSession) -> int:
    """매월 자동 박제 — catch-up + upsert.

    최근 SNAPSHOT_CATCHUP_MONTHS 개월을 훑어:
    - 최근 2개월(지난달·그전달): 항상 재계산해 덮어씀 (upsert) — 늦게 입력된 거래 정정 반영
    - 그 이전: 빠진 달만 채움 (catch-up), 이미 있으면 보존

    1일에 서버가 안 떠 누락된 달도 다음 실행 때 자동으로 메워진다.
    스케줄러(매월 1일)에서 호출. 반환값은 신규 박제 + 갱신된 (가계부×월) 수.
    """
    target_date = _target_month()  # 박제 기준 = 지난달
    months = [_shift_months(target_date, -i) for i in range(SNAPSHOT_CATCHUP_MONTHS)]
    upsert_months = {target_date, _shift_months(target_date, -1)}  # 최근 2개월

    # 박제 전 시세 확보 (시장 공통 — 전 가계부 1회). 야후 종목은 과거 월봉까지 채워
    # catch-up 달도 시가 평가되고, OTHER(금 등)는 지난달에 현재가를 박는다('현재부터').
    await market_price_service.backfill_yahoo_monthly(db, range_="2y")
    await market_price_service.snapshot_other_prices(db, target_date)

    repo = AccountSnapshotRepository(db)
    households = await HouseholdRepository(db).find_all_active()

    saved = 0
    for h in households:
        await _lock_household_snapshot(db, h.id)  # 그 가계부 수동 박제와 직렬화 — 잡 커밋까지 유지
        # catch-up 하한 = 기존 최古 박제월. 박제 이력 없으면 지난달만(첫 박제).
        oldest = await repo.oldest_active_month(h.id)
        catchup_floor = oldest if oldest else target_date
        for month in months:
            if month < catchup_floor:
                continue  # 데이터 시작 전 — 채우지 않음
            exists = await repo.has_active_for_month(h.id, month)
            if exists and month not in upsert_months:
                continue  # 과거 박제는 고정 — 건드리지 않음
            result = await _build_and_save_snapshot(db, h, month, replace=exists)
            if result.accounts:  # 계좌 없는 빈 가계부는 카운트 제외
                saved += 1

    logger.info(
        "월간 자동 박제 완료 (households=%d, saved=%d, target=%s, catchup=%d개월)",
        len(households), saved, target_date, SNAPSHOT_CATCHUP_MONTHS,
    )
    return saved


async def create_target_month_snapshot(
    db: AsyncSession, household: Household,
) -> SnapshotMonth:
    """수동 박제 — 지난달 박제. 이미 있으면 덮어씀 (upsert).

    실수로 누락했거나(자동이 안 돈 경우) 잘못 박제된 달을 사용자가 직접 다시
    찍을 때. 6/1~6/말에 누르면 5월 박제. 같은 달 또 눌러도 안전.
    """
    repo = AccountSnapshotRepository(db)
    target_date = _target_month()
    # 수동 박제도 시가 반영 — 그 가계부 종목 시세 먼저 확보.
    await market_price_service.backfill_yahoo_monthly(db, household.id, range_="2y")
    await market_price_service.snapshot_other_prices(db, target_date, household.id)
    # 시세 upsert 뒤에 잡는다 — 잡은 시세 행 lock 을 쥔 채 가계부 lock 을 차례로 잡으므로,
    # 먼저 가계부 lock 을 쥐고 시세 행을 기다리면 서로 기다리는 데드락이 된다
    await _lock_household_snapshot(db, household.id)
    exists = await repo.has_active_for_month(household.id, target_date)
    result = await _build_and_save_snapshot(db, household, target_date, replace=exists)
    logger.info(
        "수동 박제 (household_id=%s, date=%s, upsert=%s)",
        household.id, target_date, exists,
    )
    return result


def resolve_snapshot_range(
    from_date: date | None, to_date: date | None,
) -> tuple[date, date]:
    """조회 범위 정규화 — to=지난달(미지정 시), from=to−11개월. 모두 그달 1일.

    배분추이(wealth)와 연간추이(snapshot)가 같은 축을 쓰도록 공통 사용.
    """
    target_month = _shift_months(_normalize_to_month_first(_today_kst()), -1)
    to_resolved = _normalize_to_month_first(to_date) if to_date else target_month
    from_resolved = (
        _normalize_to_month_first(from_date)
        if from_date
        else _shift_months(to_resolved, -11)
    )
    return from_resolved, to_resolved


async def get_yearly_snapshots(
    db: AsyncSession,
    household: Household,
    from_date: date | None = None,
    to_date: date | None = None,
) -> SnapshotYearlyResponse:
    repo = AccountSnapshotRepository(db)
    target_month = _shift_months(_normalize_to_month_first(_today_kst()), -1)
    from_date, to_date = resolve_snapshot_range(from_date, to_date)

    rows = await repo.find_by_household_and_range(household.id, from_date, to_date)

    account_ids = list({s.account_id for s in rows})
    accounts: list[Account] = await AccountRepository(db).find_by_ids(account_ids)
    account_map = {a.id: a for a in accounts}

    months_grouped: dict[date, list[AccountSnapshot]] = {}
    for s in rows:
        months_grouped.setdefault(s.snapshot_date, []).append(s)

    months = [
        _build_month(d, snaps, account_map)
        for d, snaps in sorted(months_grouped.items(), key=lambda kv: kv[0])
    ]

    saved = await repo.has_active_for_month(household.id, target_month)

    return SnapshotYearlyResponse(
        months=months,
        target_month_saved=saved,
        target_month_date=target_month,
    )


def _today_kst() -> date:
    """한국 시각 기준 오늘 — 운영 서버 UTC 여도 KST 자정 경계 보장"""
    return datetime.now(KST).date()


def _normalize_to_month_first(d: date) -> date:
    return d.replace(day=1)


def _shift_months(d: date, delta_months: int) -> date:
    """delta_months 만큼 이동한 그 달 1일"""
    total = d.year * 12 + (d.month - 1) + delta_months
    y, m = divmod(total, 12)
    return date(y, m + 1, 1)


def _month_end(d: date) -> date:
    """그 달 마지막 날짜 — 시점 잔액 박제의 as-of 기준."""
    return date(d.year, d.month, monthrange(d.year, d.month)[1])


def _build_month(
    snapshot_date: date,
    snapshots: list[AccountSnapshot],
    account_map: dict,
) -> SnapshotMonth:
    items = []
    total_balance = Decimal("0.00")
    total_income = Decimal("0.00")
    total_expense = Decimal("0.00")
    total_fixed_expense = Decimal("0.00")
    for s in snapshots:
        a = account_map.get(s.account_id)
        items.append(
            SnapshotMonthBalance(
                account_id=s.account_id,
                account_name=a.name if a else "(삭제됨)",
                balance=s.balance,
                monthly_income=s.monthly_income,
                monthly_expense=s.monthly_expense,
                monthly_fixed_expense=s.monthly_fixed_expense,
            )
        )
        total_balance += s.balance
        total_income += s.monthly_income
        total_expense += s.monthly_expense
        total_fixed_expense += s.monthly_fixed_expense
    return SnapshotMonth(
        snapshot_date=snapshot_date,
        total_balance=total_balance,
        total_income=total_income,
        total_expense=total_expense,
        total_fixed_expense=total_fixed_expense,
        accounts=items,
    )


def _target_month() -> date:
    """박제 대상 월 = 지난달 1일. _shift_months 가 연도 넘김 처리."""
    return _shift_months(_normalize_to_month_first(_today_kst()), -1)


async def _build_and_save_snapshot(
    db: AsyncSession, household: Household, target_date: date, *, replace: bool = False,
) -> SnapshotMonth:
    """실제 박제 실행 — 중복 체크 없음 (호출자가 책임).

    계좌별 잔액 + 그달 수입/지출/고정지출 박제 + 종목 평가액 박제.
    replace=True 면 그달 기존 박제를 먼저 지우고 다시 만듦 (upsert).
    """
    if replace:
        await AccountSnapshotRepository(db).delete_for_household_month(
            household.id, target_date,
        )

    as_of = _month_end(target_date)  # 그 달 말일 시점 잔액으로 박제
    accounts = [
        a for a in await AccountRepository(db).find_active_by_household_id(household.id)
        if counts_in_totals(a)
    ]
    if not accounts:
        # 계좌 없는 가계부 — 박제할 게 없음. replace 면 종목 박제도 정리.
        if replace:
            await snapshot_household_portfolio(
                db, household, target_date, as_of, replace=True,
            )
        return _build_month(target_date, [], {})

    tx_repo = TransactionRepository(db)
    snapshots: list[AccountSnapshot] = []
    for a in accounts:
        summary = await _calc_balance(tx_repo, a, db, as_of=as_of)
        monthly = await tx_repo.sum_by_account_for_month(
            a.id, target_date.year, target_date.month,
        )
        snapshots.append(
            AccountSnapshot(
                account_id=a.id,
                snapshot_date=target_date,
                balance=summary.balance,
                monthly_income=monthly["income"],
                monthly_expense=monthly["expense"],
                monthly_fixed_expense=monthly["fixed_expense"],
                data_stat_cd=DataStatus.ACTIVE,
            )
        )

    await AccountSnapshotRepository(db).save_all(snapshots)

    # 종목 박제 — portfolio 도메인이 자기 책임 (replace 면 그달 것 갈아끼움)
    await snapshot_household_portfolio(db, household, target_date, as_of, replace=replace)

    account_map = {a.id: a for a in accounts}
    return _build_month(target_date, snapshots, account_map)

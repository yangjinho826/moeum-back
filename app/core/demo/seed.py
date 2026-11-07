"""데모 가계부 시딩 — 이력서에 공개하는 체험 계정의 데이터를 만든다.

멱등하다: 실행할 때마다 데모 가계부를 통째로 지우고 다시 만든다(= 리셋).
체험자가 거래를 고치거나 지워도 다음 실행이 원상복구한다. "추가분만 삭제"로는
시드 데이터의 수정·삭제를 되돌리지 못해 통째 재생성을 택했다.

users 행만은 예외로 절대 지우지 않는다 —
  1) 로그인 정보가 이력서에 인쇄돼 나가므로 고정이어야 하고
  2) infra/backup/restore-drill.sh 의 UTF8 검증이 users.name 에 한글이 최소 1행
     있다는 전제로 돌기 때문이다.

집계(월별 잔액 박제·종목 평가액)는 직접 INSERT 하지 않고 화면이 쓰는 서비스 함수를
그대로 호출한다 — 손으로 계산하면 잔액 공식이 갈라져 화면과 어긋난다.
"""
import logging
import random
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.security import hash_password
from app.core.config import settings
from app.core.enums.data_status import DataStatus
from app.domain.account.enum import AccountType
from app.domain.account.model import Account
from app.domain.account_snapshot.model import AccountSnapshot
from app.domain.account_snapshot.service import (
    _build_and_save_snapshot,
    _month_end,
    _shift_months,
)
from app.domain.category.enum import CategoryKind
from app.domain.category.model import Category
from app.domain.fixed.model import FixedExpense
from app.domain.household.enum import HouseholdRole
from app.domain.household.model import Household, HouseholdMember
from app.domain.market_price import service as market_price_service
from app.domain.market_price.model import MarketPriceHistory
from app.domain.portfolio.enum import Market, PortfolioTxType
from app.domain.portfolio.model import (
    PortfolioItem,
    PortfolioTransaction,
    PortfolioValueHistory,
)
from app.domain.transaction.enum import TxType, ValuationDirection
from app.domain.transaction.model import Transaction
from app.domain.user.model import User

logger = logging.getLogger(__name__)

DEMO_HOUSEHOLD_NAME = "모음 데모 가계부"
DEMO_USER_NAME = "김모음"
SNAPSHOT_MONTHS = 12          # 박제할 개월 수 (지난달부터 역순)
MONTHLY_VARIABLE_COUNT = 30   # 월별 변동지출 건수

# 계좌 — (이름, 타입, 기초잔액, 색, 아이콘)
_ACCOUNTS: list[tuple[str, AccountType, int, str, str]] = [
    ("생활비 통장", AccountType.LIVING, 3_000_000, "#3B82F6", "wallet"),
    ("비상금 통장", AccountType.SAVINGS, 5_000_000, "#06B6D4", "shield-check"),
    ("증권 계좌", AccountType.INVESTMENT, 0, "#EF4444", "chart-line"),
    ("청약저축", AccountType.SAVINGS_ASSET, 2_000_000, "#10B981", "wallet"),
    ("연금저축", AccountType.PENSION, 4_000_000, "#EC4899", "pig-money"),
]

# 지출 카테고리 — (이름, 색, 아이콘, 1건당 금액 범위, 월 발생 비중)
_EXPENSE_CATEGORIES: list[tuple[str, str, str, tuple[int, int], int]] = [
    ("식비", "#F97316", "tools-kitchen-2", (8_000, 45_000), 9),
    ("카페·간식", "#A855F7", "coffee", (3_500, 12_000), 5),
    ("교통", "#0EA5E9", "bus", (1_500, 30_000), 4),
    ("생활용품", "#22C55E", "shopping-cart", (5_000, 60_000), 3),
    ("의료·건강", "#EF4444", "stethoscope", (6_000, 90_000), 1),
    ("문화·여가", "#8B5CF6", "movie", (12_000, 70_000), 2),
    ("의류·미용", "#EC4899", "shirt", (20_000, 150_000), 2),
    ("경조사", "#F59E0B", "gift", (50_000, 200_000), 1),
    ("여행", "#14B8A6", "plane", (80_000, 400_000), 1),
    ("기타", "#6B7280", "dots", (5_000, 40_000), 2),
]

_INCOME_CATEGORIES: list[tuple[str, str, str]] = [
    ("급여", "#2563EB", "businessplan"),
    ("상여", "#7C3AED", "confetti"),
    ("기타수입", "#64748B", "coin"),
]

# 고정지출 — (이름, 결제일, 금액, 색, 아이콘, 연결 지출카테고리)
_FIXED_EXPENSES: list[tuple[str, int, int, str, str, str]] = [
    ("월세", 1, 700_000, "#8B5CF6", "home", "기타"),
    ("통신비", 5, 66_000, "#0EA5E9", "device-mobile", "기타"),
    ("인터넷·TV", 5, 33_000, "#06B6D4", "wifi", "기타"),
    ("실손보험", 10, 120_000, "#EF4444", "shield-check", "의료·건강"),
    ("넷플릭스", 15, 17_000, "#DC2626", "movie", "문화·여가"),
    ("유튜브 프리미엄", 15, 14_900, "#F43F5E", "brand-youtube", "문화·여가"),
    ("헬스장", 20, 55_000, "#22C55E", "barbell", "의료·건강"),
    ("정수기 렌탈", 25, 19_900, "#3B82F6", "droplet", "기타"),
]

# 보유 종목 — (이름, 코드, 시장, 1주 단가(KRW), 월 매수 수량)
# DB current_price 는 시장 불문 항상 KRW (portfolio/service.py:71) — USD 종목도 환산가로 둔다.
# 실제 티커라 야후 백필이 과거 월봉을, 기존 시세 갱신 잡이 현재가를 계속 덮어쓴다.
_HOLDINGS: list[tuple[str, str, Market, int, Decimal]] = [
    ("삼성전자", "005930", Market.KRX_KOSPI, 75_000, Decimal("2")),
    ("TIGER 미국S&P500", "360750", Market.KRX_KOSPI, 19_500, Decimal("6")),
    ("Apple", "AAPL", Market.NASDAQ, 350_000, Decimal("0.3")),
    ("NVIDIA", "NVDA", Market.NASDAQ, 260_000, Decimal("0.4")),
    ("금 (실물)", "", Market.OTHER, 145_000, Decimal("0.5")),
]

_SALARY = 3_800_000        # 매월 25일 급여
_BONUS = 2_500_000         # 상여 (연 2회)

# 생활비 통장 → 각 계좌 월 이체 (이름, 금액, 일자).
# 합계를 급여-고정-변동 잉여(약 163만)에 가깝게 잡아 생활비 통장 잔액이 완만하게 간다 —
# 잉여를 안 흘려보내면 생활비 통장에만 돈이 고여 비현실적으로 보인다.
# 증권 이체는 월 매수액(약 55만)보다 커야 매수 현금이 마이너스로 빠지지 않는다.
_TRANSFER_PLAN = [
    ("증권 계좌", 600_000, 26),
    ("연금저축", 300_000, 26),
    ("청약저축", 200_000, 26),
    ("비상금 통장", 450_000, 27),
]


async def seed_demo(session: AsyncSession) -> None:
    """데모 가계부를 지우고 다시 만든다. 호출자가 트랜잭션을 소유한다(여기선 commit 안 함)."""
    today = date.today()
    # 그날 안에서는 재현 가능하고 날짜가 바뀌면 자연스럽게 달라지도록 날짜를 시드로 고정
    rng = random.Random(today.toordinal())

    user = await _ensure_demo_user(session)
    await _purge_demo_data(session, user.id)

    first_month = _shift_months(today.replace(day=1), -SNAPSHOT_MONTHS)
    household = await _create_household(session, user.id, first_month)
    accounts = await _create_accounts(session, household.id)
    categories = await _create_categories(session, household.id)
    fixed_expenses = await _create_fixed_expenses(
        session, household.id, categories,
    )

    tx_count = await _create_transactions(
        session, household.id, user.id, accounts, categories,
        fixed_expenses, first_month, today, rng,
    )
    pt_count = await _create_portfolio(
        session, household.id, accounts["증권 계좌"].id, first_month, today, rng,
    )
    await session.flush()

    await _build_snapshots(session, household, today)

    logger.info(
        "데모 시딩 완료 (household_id=%s, 거래=%d, 자산거래=%d, 기간=%s~%s)",
        household.id, tx_count, pt_count, first_month, today,
    )


async def _ensure_demo_user(session: AsyncSession) -> User:
    """데모 유저 확보 — 없으면 생성. 절대 삭제하지 않는다 (모듈 docstring 참조)."""
    existing = await session.scalar(
        select(User).where(User.email == settings.DEMO_EMAIL),
    )
    if existing:
        # 비밀번호·이름·상태는 매번 되돌린다 — 체험자가 PUT /user/{id} 로 바꿔도 복구
        existing.name = DEMO_USER_NAME
        existing.password_hash = await hash_password(settings.DEMO_PASSWORD)
        existing.data_stat_cd = DataStatus.ACTIVE
        return existing

    user = User(
        email=settings.DEMO_EMAIL,
        name=DEMO_USER_NAME,
        password_hash=await hash_password(settings.DEMO_PASSWORD),
        language="ko",
        data_stat_cd=DataStatus.ACTIVE,
    )
    session.add(user)
    await session.flush()
    return user


async def _purge_demo_data(session: AsyncSession, user_id) -> None:
    """데모 유저가 owner 인 가계부의 하위 데이터를 전부 hard delete.

    "가계부 1개"가 아니라 "owner 인 전부"가 기준 — 체험자가 POST /household 로
    새 가계부를 만들 수 있어서 그것까지 회수해야 다음 체험자가 같은 화면을 본다.
    market_price_history 의 야후 시세는 시장 공통이라 두고, OTHER(수동가)는 종목 id 가 키라
    지우는 데모 종목 것만 같이 지운다 — 리셋마다 새 종목 id 행이 쌓이지 않게.
    """
    household_ids = list(
        (
            await session.scalars(
                select(Household.id).where(Household.owner_id == user_id),
            )
        ).all()
    )
    if not household_ids:
        return

    account_ids = list(
        (
            await session.scalars(
                select(Account.id).where(Account.household_id.in_(household_ids)),
            )
        ).all()
    )
    if account_ids:
        await session.execute(
            delete(AccountSnapshot).where(AccountSnapshot.account_id.in_(account_ids)),
        )

    other_item_ids = (
        await session.scalars(
            select(PortfolioItem.id).where(
                PortfolioItem.household_id.in_(household_ids),
                PortfolioItem.market == Market.OTHER.value,
            ),
        )
    ).all()
    if other_item_ids:
        await session.execute(
            delete(MarketPriceHistory).where(
                MarketPriceHistory.market == Market.OTHER.value,
                MarketPriceHistory.code.in_(
                    [market_price_service.other_price_code(i) for i in other_item_ids]
                ),
            ),
        )

    # transactions 가 fixed_expenses 를 FK 로 물고 있어(ondelete=SET NULL) 먼저 지운다
    for model in (
        Transaction,
        PortfolioValueHistory,
        PortfolioTransaction,
        PortfolioItem,
        FixedExpense,
        Category,
        Account,
        HouseholdMember,
    ):
        await session.execute(
            delete(model).where(model.household_id.in_(household_ids)),
        )
    await session.execute(delete(Household).where(Household.id.in_(household_ids)))
    await session.flush()
    logger.info("데모 데이터 삭제 (households=%d)", len(household_ids))


async def _create_household(
    session: AsyncSession, user_id, started_at: date,
) -> Household:
    household = Household(
        name=DEMO_HOUSEHOLD_NAME,
        description="이력서 공개용 체험 가계부입니다. 매일 05:00(KST)에 초기화됩니다.",
        owner_id=user_id,
        currency="KRW",
        started_at=started_at,
        data_stat_cd=DataStatus.ACTIVE,
    )
    session.add(household)
    await session.flush()

    session.add(
        HouseholdMember(
            household_id=household.id,
            user_id=user_id,
            role=HouseholdRole.OWNER,
            joined_at=datetime.now(),
            data_stat_cd=DataStatus.ACTIVE,
        )
    )
    await session.flush()
    return household


async def _create_accounts(
    session: AsyncSession, household_id,
) -> dict[str, Account]:
    accounts: dict[str, Account] = {}
    for order, (name, acc_type, start, color, icon) in enumerate(_ACCOUNTS, start=1):
        account = Account(
            household_id=household_id,
            name=name,
            account_type=acc_type,
            start_balance=Decimal(start),
            color=color,
            icon=icon,
            sort_order=order,
            is_archived=False,
            data_stat_cd=DataStatus.ACTIVE,
        )
        session.add(account)
        accounts[name] = account
    await session.flush()
    return accounts


async def _create_categories(
    session: AsyncSession, household_id,
) -> dict[str, Category]:
    categories: dict[str, Category] = {}
    order = 0
    for name, color, icon, _range, _weight in _EXPENSE_CATEGORIES:
        order += 1
        category = Category(
            household_id=household_id,
            kind=CategoryKind.EXPENSE,
            name=name,
            color=color,
            icon=icon,
            sort_order=order,
            is_archived=False,
            data_stat_cd=DataStatus.ACTIVE,
        )
        session.add(category)
        categories[name] = category

    for name, color, icon in _INCOME_CATEGORIES:
        order += 1
        category = Category(
            household_id=household_id,
            kind=CategoryKind.INCOME,
            name=name,
            color=color,
            icon=icon,
            sort_order=order,
            is_archived=False,
            data_stat_cd=DataStatus.ACTIVE,
        )
        session.add(category)
        categories[name] = category

    await session.flush()
    return categories


async def _create_fixed_expenses(
    session: AsyncSession, household_id, categories: dict[str, Category],
) -> dict[str, FixedExpense]:
    fixed: dict[str, FixedExpense] = {}
    for order, (name, day, _amount, color, icon, cat_name) in enumerate(
        _FIXED_EXPENSES, start=1,
    ):
        entity = FixedExpense(
            household_id=household_id,
            name=name,
            day_of_month=day,
            category_id=categories[cat_name].id,
            color=color,
            icon=icon,
            sort_order=order,
            is_archived=False,
            data_stat_cd=DataStatus.ACTIVE,
        )
        session.add(entity)
        fixed[name] = entity
    await session.flush()
    return fixed


def _safe_date(year: int, month: int, day: int) -> date:
    """말일이 짧은 달(2월 등)에서 day 를 그 달 마지막 날로 클램프."""
    last_day = _month_end(date(year, month, 1)).day
    return date(year, month, min(day, last_day))


def _months_between(first: date, last: date) -> list[date]:
    """first 달 1일부터 last 달 1일까지 각 달의 1일 목록."""
    months = []
    cursor = first.replace(day=1)
    while cursor <= last.replace(day=1):
        months.append(cursor)
        cursor = _shift_months(cursor, 1)
    return months


async def _create_transactions(  # noqa: PLR0913 — 시드 조립부라 인자가 많다
    session: AsyncSession,
    household_id,
    user_id,
    accounts: dict[str, Account],
    categories: dict[str, Category],
    fixed_expenses: dict[str, FixedExpense],
    first_month: date,
    today: date,
    rng: random.Random,
) -> int:
    """월별 루프로 수입·고정지출·변동지출·이체·평가조정을 만든다.

    이번 달은 오늘까지만 — 미래 날짜 거래가 있으면 홈 화면의 '이번달' 집계가 어긋난다.
    """
    living = accounts["생활비 통장"]
    pension = accounts["연금저축"]
    variable_pool = [
        (categories[name], amount_range)
        for name, _c, _i, amount_range, weight in _EXPENSE_CATEGORIES
        for _ in range(weight)
    ]
    fixed_amounts = {name: amount for name, _d, amount, _c, _i, _cat in _FIXED_EXPENSES}
    fixed_days = {name: day for name, day, _a, _c, _i, _cat in _FIXED_EXPENSES}

    count = 0
    for month in _months_between(first_month, today):
        # 급여
        salary_date = _safe_date(month.year, month.month, 25)
        if salary_date <= today:
            session.add(
                Transaction(
                    household_id=household_id, tx_type=TxType.INCOME,
                    amount=Decimal(_SALARY), tx_date=salary_date,
                    account_id=living.id, category_id=categories["급여"].id,
                    paid_by_user_id=user_id, memo="월급",
                    data_stat_cd=DataStatus.ACTIVE,
                )
            )
            count += 1

        # 상여 — 6월·12월
        if month.month in (6, 12):
            bonus_date = _safe_date(month.year, month.month, 20)
            if bonus_date <= today:
                session.add(
                    Transaction(
                        household_id=household_id, tx_type=TxType.INCOME,
                        amount=Decimal(_BONUS), tx_date=bonus_date,
                        account_id=living.id, category_id=categories["상여"].id,
                        paid_by_user_id=user_id, memo="정기 상여",
                        data_stat_cd=DataStatus.ACTIVE,
                    )
                )
                count += 1

        # 고정지출 — FIXED_EXPENSE 타입 + fixed_expense_id 매핑이라야 고정지출 집계에 잡힌다
        for name, entity in fixed_expenses.items():
            pay_date = _safe_date(month.year, month.month, fixed_days[name])
            if pay_date > today:
                continue
            session.add(
                Transaction(
                    household_id=household_id, tx_type=TxType.FIXED_EXPENSE,
                    amount=Decimal(fixed_amounts[name]), tx_date=pay_date,
                    account_id=living.id, category_id=entity.category_id,
                    fixed_expense_id=entity.id, paid_by_user_id=user_id,
                    memo=name, data_stat_cd=DataStatus.ACTIVE,
                )
            )
            count += 1

        # 변동지출
        last_day = _month_end(month).day
        for _ in range(MONTHLY_VARIABLE_COUNT):
            category, (low, high) = rng.choice(variable_pool)
            spend_date = _safe_date(month.year, month.month, rng.randint(1, last_day))
            if spend_date > today:
                continue
            amount = rng.randrange(low, high, 500)
            session.add(
                Transaction(
                    household_id=household_id, tx_type=TxType.EXPENSE,
                    amount=Decimal(amount), tx_date=spend_date,
                    account_id=living.id, category_id=category.id,
                    paid_by_user_id=user_id, data_stat_cd=DataStatus.ACTIVE,
                )
            )
            count += 1

        # 저축·투자 이체
        for target_name, amount, day in _TRANSFER_PLAN:
            transfer_date = _safe_date(month.year, month.month, day)
            if transfer_date > today:
                continue
            session.add(
                Transaction(
                    household_id=household_id, tx_type=TxType.TRANSFER,
                    amount=Decimal(amount), tx_date=transfer_date,
                    account_id=living.id, to_account_id=accounts[target_name].id,
                    paid_by_user_id=user_id, memo=f"{target_name} 자동이체",
                    data_stat_cd=DataStatus.ACTIVE,
                )
            )
            count += 1

        # 연금 평가조정 — 분기말에 수익 반영 (현금 유입 없는 가치 증감)
        if month.month % 3 == 0:
            valuation_date = _safe_date(month.year, month.month, 28)
            if valuation_date <= today:
                session.add(
                    Transaction(
                        household_id=household_id, tx_type=TxType.VALUATION,
                        amount=Decimal(rng.randrange(80_000, 260_000, 1_000)),
                        tx_date=valuation_date, account_id=pension.id,
                        valuation_direction=ValuationDirection.INCREASE,
                        memo="분기 평가손익 반영", data_stat_cd=DataStatus.ACTIVE,
                    )
                )
                count += 1

    await session.flush()
    return count


async def _create_portfolio(  # noqa: PLR0913 — 시드 조립부라 인자가 많다
    session: AsyncSession,
    household_id,
    account_id,
    first_month: date,
    today: date,
    rng: random.Random,
) -> int:
    """적립식 매수 이력 + 보유 종목. 평단은 이동평균으로 누적 계산한다.

    매도 2건에는 realized_pnl / realized_cost_basis 를 채운다 — 매도 시점 평단
    기준 건별 박제라 나중에 복원할 수 없는 값이다.
    """
    months = _months_between(first_month, today)
    count = 0

    for name, code, market, base_price, monthly_qty in _HOLDINGS:
        quantity = Decimal("0")
        cost = Decimal("0")
        item = PortfolioItem(
            household_id=household_id, account_id=account_id,
            name=name, code=code, market=market,
            quantity=Decimal("0"), avg_price=Decimal("0"),
            current_price=Decimal(base_price), is_archived=False,
            data_stat_cd=DataStatus.ACTIVE,
        )
        session.add(item)
        await session.flush()

        for index, month in enumerate(months):
            buy_date = _safe_date(month.year, month.month, 27)
            if buy_date > today:
                continue
            # 매수 단가를 달마다 ±12% 흔들어 평단과 현재가가 갈리게 한다
            price = (Decimal(base_price) * Decimal(rng.randint(88, 112)) / 100).quantize(
                Decimal("0.01"),
            )
            quantity += monthly_qty
            cost += price * monthly_qty
            session.add(
                PortfolioTransaction(
                    household_id=household_id, account_id=account_id,
                    portfolio_item_id=item.id, name=name, code=code, market=market,
                    pt_type=PortfolioTxType.BUY, quantity=monthly_qty, price=price,
                    tx_date=buy_date, memo="적립식 매수",
                    data_stat_cd=DataStatus.ACTIVE,
                )
            )
            count += 1

            # 삼성전자만 중간에 일부 매도 — 실현손익이 있는 이력을 남긴다
            if code == "005930" and index in (4, 9) and quantity > monthly_qty:
                avg_price = (cost / quantity).quantize(Decimal("0.01"))
                sell_qty = monthly_qty
                sell_price = (price * Decimal("1.08")).quantize(Decimal("0.01"))
                sell_date = _safe_date(month.year, month.month, 28)
                if sell_date <= today:
                    basis = (avg_price * sell_qty).quantize(Decimal("0.01"))
                    session.add(
                        PortfolioTransaction(
                            household_id=household_id, account_id=account_id,
                            portfolio_item_id=item.id, name=name, code=code,
                            market=market, pt_type=PortfolioTxType.SELL,
                            quantity=sell_qty, price=sell_price, tx_date=sell_date,
                            memo="일부 차익 실현",
                            realized_pnl=(sell_price * sell_qty - basis).quantize(
                                Decimal("0.01"),
                            ),
                            realized_cost_basis=basis,
                            data_stat_cd=DataStatus.ACTIVE,
                        )
                    )
                    quantity -= sell_qty
                    cost -= basis      # 이동평균 — 매도는 평단을 바꾸지 않는다
                    count += 1

        item.quantity = quantity
        item.avg_price = (cost / quantity).quantize(Decimal("0.01")) if quantity else Decimal("0")

    await session.flush()
    return count


async def _build_snapshots(
    session: AsyncSession, household: Household, today: date,
) -> None:
    """월별 잔액·평가액 박제 — 화면이 쓰는 서비스 함수를 그대로 호출한다.

    시세를 먼저 확보해야 과거 달이 원가가 아닌 그달 시가로 평가된다.
    야후 호출이 실패해도 원가 fallback 이라 화면이 깨지진 않는다.
    """
    last_month = _shift_months(today.replace(day=1), -1)
    await market_price_service.backfill_yahoo_monthly(
        session, household.id, range_="2y",
    )
    await market_price_service.snapshot_other_prices(
        session, last_month, household.id,
    )

    for offset in range(SNAPSHOT_MONTHS):
        month = _shift_months(last_month, -offset)
        await _build_and_save_snapshot(session, household, month, replace=True)
    logger.info("데모 월별 박제 완료 (%d개월, ~%s)", SNAPSHOT_MONTHS, last_month)


async def bootstrap_if_empty(session: AsyncSession) -> None:
    """데모 가계부가 하나도 없을 때만 시딩 — 앱 기동 시 호출.

    새 서버·DB 복구 직후처럼 데이터가 없는 환경을 자동으로 채우되, 재배포에는
    아무 일도 하지 않는다(체험 중인 사람의 화면이 리셋되면 안 된다).
    """
    user = await session.scalar(
        select(User).where(User.email == settings.DEMO_EMAIL),
    )
    if user is not None:
        exists = await session.scalar(
            select(Household.id).where(Household.owner_id == user.id).limit(1),
        )
        if exists is not None:
            logger.info("데모 가계부 이미 존재 — 부트스트랩 skip")
            return

    logger.info("데모 가계부 없음 — 부트스트랩 시딩 시작")
    await seed_demo(session)

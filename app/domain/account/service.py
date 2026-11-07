import logging
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums.data_status import DataStatus
from app.core.exceptions import CustomException, ErrorCode
from app.core.pagination import CursorPage
from app.domain.account.enum import (
    MANUAL_ASSET_ACCOUNT_TYPES,
    MANUAL_ASSET_DEFAULT_META,
    AccountType,
)
from app.domain.account.model import Account
from app.domain.account.repository import AccountRepository
from app.domain.account.schema import (
    AccountCreateRequest,
    AccountMonthlyFlow,
    AccountReportResponse,
    AccountResponse,
    AccountUpdateRequest,
)
from app.domain.account_snapshot.repository import AccountSnapshotRepository
from app.domain.household.model import Household
from app.domain.market_price import service as market_price_service
from app.domain.portfolio.repository import (
    PortfolioItemRepository,
    PortfolioTransactionRepository,
    PortfolioValueHistoryRepository,
)
from app.domain.transaction.repository import TransactionRepository

logger = logging.getLogger(__name__)


@dataclass
class BalanceSummary:
    """통장별 잔액 + (INVESTMENT 한정) portfolio 요약"""

    balance: Decimal
    cash: Decimal | None = None
    portfolio_cost: Decimal | None = None
    portfolio_valuation: Decimal | None = None
    portfolio_profit_loss: Decimal | None = None
    portfolio_profit_loss_rate: Decimal | None = None


async def list_accounts(
    db: AsyncSession,
    household: Household,
    *,
    search_term: str | None = None,
    account_type: str | None = None,
    is_archived: bool | None = None,
) -> list[AccountResponse]:
    """내부용 — sort_order 정렬 유지 (portfolio overview / snapshot 에서 호출)."""
    repo = AccountRepository(db)
    accounts = await repo.search_by_household_id(
        household.id,
        search_term=search_term,
        account_type=account_type,
        is_archived=is_archived,
    )
    sources = await _load_balance_sources(db, accounts)
    return [_build_response(a, _build_balance(a, *sources)) for a in accounts]


async def list_accounts_cursor(
    db: AsyncSession,
    household: Household,
    *,
    search_term: str | None = None,
    account_type: str | None = None,
    is_archived: bool | None = None,
    cursor: str | None = None,
    limit: int = 30,
) -> "CursorPage[AccountResponse]":
    """관리 페이지용 — frst_reg_dt DESC 정렬, cursor 무한 스크롤."""
    repo = AccountRepository(db)
    rows = await repo.list_by_cursor(
        household.id,
        search_term=search_term,
        account_type=account_type,
        is_archived=is_archived,
        cursor=cursor,
        limit=limit,
    )
    has_next = len(rows) > limit
    rows = rows[:limit]

    sources = await _load_balance_sources(db, rows)
    items = [_build_response(a, _build_balance(a, *sources)) for a in rows]

    total_count = await repo.count_search(
        household.id,
        search_term=search_term,
        account_type=account_type,
        is_archived=is_archived,
    )

    next_cursor: str | None = None
    if has_next and rows:
        last = rows[-1]
        next_cursor = f"{last.frst_reg_dt.isoformat()}|{last.id}"

    return CursorPage(
        items=items,
        next_cursor=next_cursor,
        has_next=has_next,
        total_count=total_count,
    )


async def create_account(
    db: AsyncSession, household: Household, req: AccountCreateRequest,
) -> AccountResponse:
    repo = AccountRepository(db)
    sort_order = req.sort_order if req.sort_order is not None else (await repo.max_sort_order(household.id)) + 1

    # 수동자산 통장은 color/icon 미지정 시 type별 기본값 부여 (프론트가 부담하지 않게).
    color, icon = req.color, req.icon
    if req.account_type in MANUAL_ASSET_DEFAULT_META:
        default_color, default_icon = MANUAL_ASSET_DEFAULT_META[req.account_type]
        color = color or default_color
        icon = icon or default_icon

    account = Account(
        household_id=household.id,
        name=req.name.strip(),
        account_type=req.account_type,
        start_balance=req.start_balance,
        color=color,
        icon=icon,
        sort_order=sort_order,
        is_archived=False,
        data_stat_cd=DataStatus.ACTIVE,
    )
    await repo.save(account)
    logger.info("통장 생성 (account_id=%s, household_id=%s)", account.id, household.id)
    # 갓 생성: 거래/portfolio 0건이라 balance == start_balance
    summary = BalanceSummary(balance=account.start_balance)
    if account.account_type == AccountType.INVESTMENT:
        zero = Decimal("0.00")
        summary = BalanceSummary(
            balance=account.start_balance,
            cash=account.start_balance,
            portfolio_cost=zero,
            portfolio_valuation=zero,
            portfolio_profit_loss=zero,
            portfolio_profit_loss_rate=zero,
        )
    return _build_response(account, summary)


async def update_account(
    db: AsyncSession, household: Household, account_id: UUID, req: AccountUpdateRequest,
) -> AccountResponse:
    repo = AccountRepository(db)
    account = await repo.find_by_id(account_id)
    if not account or account.household_id != household.id:
        raise CustomException(ErrorCode.NOT_FOUND)

    if req.name is not None:
        account.name = req.name.strip()
    if req.account_type is not None:
        account.account_type = req.account_type
    if req.start_balance is not None:
        account.start_balance = req.start_balance
    if req.color is not None:
        account.color = req.color
    if req.icon is not None:
        account.icon = req.icon
    if req.sort_order is not None:
        account.sort_order = req.sort_order
    if req.is_archived is not None:
        account.is_archived = req.is_archived

    await db.flush()
    tx_repo = TransactionRepository(db)
    summary = await _calc_balance(tx_repo, account, db)
    return _build_response(account, summary)


async def delete_account(
    db: AsyncSession, household: Household, account_id: UUID,
) -> None:
    repo = AccountRepository(db)
    account = await repo.find_by_id(account_id)
    if not account or account.household_id != household.id:
        raise CustomException(ErrorCode.NOT_FOUND)

    # 보유종목이 연결돼 있으면 차단 — 투자 데이터 보호(어떤 변경보다 먼저 검사).
    if await PortfolioItemRepository(db).count_active_by_account_id(account_id) > 0:
        raise CustomException(ErrorCode.ACCOUNT_HAS_DEPENDENTS)

    # 자식 cascade soft-delete (단일 트랜잭션). 이체는 상대 통장이 살아있으면
    # 행을 유지해 상대 잔액·통계를 보존하고(D안), 양쪽 다 죽은 이체만 삭제한다.
    # 이체 처리는 통장 본체를 죽이기 전에 해야 자기 자신을 상대로 오판하지 않는다.
    tx_repo = TransactionRepository(db)
    await tx_repo.soft_delete_solo_by_account_id(account_id)
    await tx_repo.soft_delete_transfers_with_dead_counterparty(account_id)
    await PortfolioTransactionRepository(db).soft_delete_by_account_id(account_id)
    await PortfolioValueHistoryRepository(db).soft_delete_by_account_id(account_id)
    await AccountSnapshotRepository(db).soft_delete_by_account_id(account_id)

    account.data_stat_cd = DataStatus.DELETED
    await db.flush()
    logger.info("통장 cascade 삭제 (account_id=%s)", account_id)


async def get_account_detail(
    db: AsyncSession, household: Household, account_id: UUID,
) -> AccountResponse:
    """통장 단건 조회 — 잔액 + PNL 포함"""
    repo = AccountRepository(db)
    account = await repo.find_by_id(account_id)
    if not account or account.household_id != household.id or account.data_stat_cd != DataStatus.ACTIVE:
        raise CustomException(ErrorCode.NOT_FOUND)
    tx_repo = TransactionRepository(db)
    summary = await _calc_balance(tx_repo, account, db)
    return _build_response(account, summary)


async def get_account_report(
    db: AsyncSession,
    household: Household,
    account_id: UUID,
    from_date: date | None = None,
    to_date: date | None = None,
) -> AccountReportResponse:
    """계좌별 리포트 — 현재 잔액 + 월별 수입/지출 추이.

    박제된 과거 월 + 아직 박제 전인 이번달(실시간 집계)을 합쳐 내려준다.
    기본 기간은 최근 12개월(이번달 포함).
    """
    account = await AccountRepository(db).find_by_id(account_id)
    if not account or account.household_id != household.id or account.data_stat_cd != DataStatus.ACTIVE:
        raise CustomException(ErrorCode.NOT_FOUND)

    today = _today_kst()
    this_month_first = today.replace(day=1)
    from_date, to_date = _report_range(from_date, to_date, this_month_first)

    snaps = await AccountSnapshotRepository(db).find_by_account_and_range(
        account_id, from_date, to_date,
    )
    flows = [_snapshot_to_flow(s) for s in snaps]

    tx_repo = TransactionRepository(db)
    summary = await _calc_balance(tx_repo, account, db)

    if to_date >= this_month_first and not any(
        s.snapshot_date == this_month_first for s in snaps
    ):
        flows.append(
            await _current_month_flow(tx_repo, account_id, today, summary.balance)
        )

    return AccountReportResponse(
        account_id=account.id,
        account_name=account.name,
        account_type=account.account_type,
        balance=summary.balance,
        monthly_flows=flows,
    )


async def _calc_balance(
    tx_repo: TransactionRepository,
    account: Account,
    db: AsyncSession,
    as_of: date | None = None,
) -> BalanceSummary:
    """통장 balance 계산 — 계좌 타입별 전략으로 위임.

    수동자산(부동산·연금·금·적금)도 일반계좌와 동일 공식: start_balance + 거래합(평가조정 포함).

    as_of 를 주면 그 날짜까지의 거래만 반영한 '시점 잔액'. 미지정 시 현재 잔액(전체).
    월별 스냅샷 박제가 그 달 말일 시점 잔액을 찍을 때 사용한다. 투자 평가액은
    현재 보유종목 기준이라 시점 박제 순간(=현재)에만 정확하다.
    """
    if account.account_type != AccountType.INVESTMENT:
        return await _calc_cash_balance(tx_repo, account, as_of)
    return await _calc_investment_balance(tx_repo, account, db, as_of)


def counts_in_totals(account: Account | AccountResponse) -> bool:
    """총자산·배분·투자 요약·월 박제에 들어가는 통장인가 — 보관 통장은 집계 밖(2026-11-07 정책).

    목록(관리·표시)에는 남긴다 — 다시 열어 고치거나 보관을 풀 수 있게.
    """
    return not account.is_archived


def _cash_flow(start_balance: Decimal, sums: dict[str, Decimal]) -> Decimal:
    """거래 합계로 잔액 산출 — 수입·이체입금 +, 지출·이체출금 -, 평가조정은 방향대로(valuation_net)."""
    return (
        start_balance
        + sums["income"]
        - sums["expense"]
        - sums["transfer_out"]
        + sums["transfer_in"]
        + sums["valuation_net"]
    )


def _summarize_holdings(items: list) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """보유 종목 평가 — (매입원가, 평가액, 평가손익, 손익률). 순수 계산."""
    cost = sum((i.quantity * i.avg_price for i in items), Decimal("0.00"))
    valuation = sum((i.quantity * i.current_price for i in items), Decimal("0.00"))
    profit_loss = valuation - cost
    rate = (profit_loss / cost * Decimal("100")) if cost > 0 else Decimal("0.00")
    return cost, valuation, profit_loss, rate


async def _calc_cash_balance(
    tx_repo: TransactionRepository, account: Account, as_of: date | None = None,
) -> BalanceSummary:
    """일반 거래계좌 — 현금흐름 잔액. as_of 면 그 날짜까지 시점 잔액."""
    sums = await tx_repo.sum_for_account(account.id, to_date=as_of)
    return BalanceSummary(balance=_cash_flow(account.start_balance, sums))


async def _calc_investment_balance(
    tx_repo: TransactionRepository,
    account: Account,
    db: AsyncSession,
    as_of: date | None = None,
) -> BalanceSummary:
    """INVESTMENT 통장 — 현금흐름 + 매매현금 증감 + 보유종목 평가.

    as_of 면 거래·매매현금을 그 날짜까지 시점 컷한다. 평가액은 현재 보유종목 기준
    (과거 가격 이력이 없어 소급 불가 — 박제 순간=현재일 때만 정확).
    """
    pt_repo = PortfolioTransactionRepository(db)
    sums = await tx_repo.sum_for_account(account.id, to_date=as_of)
    cash = _cash_flow(account.start_balance, sums)

    pt_sums = await pt_repo.sum_for_account(account.id, to_date=as_of)
    cash = cash - pt_sums["buy"] + pt_sums["sell"]

    if as_of is not None:
        # 시점 박제 — 그 시점 보유수량을 '그 달 시가'(market_price_history)로 평가.
        # 시가 이력 없는 종목(금 과거·수집 실패 등)은 원가 fallback → 회귀 없음.
        holdings = await pt_repo.asof_holdings_by_account(account.id, as_of)
        valued = await market_price_service.value_holdings_at_month(
            db, holdings, as_of.replace(day=1),
        )
        cost = sum((h["cost"] for h in holdings), Decimal("0.00"))
        valuation = sum(
            (valued[h["item_id"]]["valuation"] for h in holdings), Decimal("0.00"),
        )
        profit_loss = valuation - cost
        rate = (profit_loss / cost * Decimal("100")) if cost > 0 else Decimal("0.00")
    else:
        items = await PortfolioItemRepository(db).find_active_by_account_id(account.id)
        cost, valuation, profit_loss, rate = _summarize_holdings(items)

    return BalanceSummary(
        balance=cash + valuation,
        cash=cash,
        portfolio_cost=cost,
        portfolio_valuation=valuation,
        portfolio_profit_loss=profit_loss,
        portfolio_profit_loss_rate=rate,
    )


async def _load_balance_sources(
    db: AsyncSession, accounts: list[Account],
) -> tuple[dict, dict, dict]:
    """계좌 목록 잔액 계산에 필요한 데이터를 배치로 로드 — 계좌 수 무관 3쿼리(N+1 제거)."""
    ids = [a.id for a in accounts]
    inv_ids = [a.id for a in accounts if a.account_type == AccountType.INVESTMENT]
    tx_sums = await TransactionRepository(db).sum_for_accounts(ids)
    pt_sums = await PortfolioTransactionRepository(db).sum_for_accounts(inv_ids)
    items_map = await PortfolioItemRepository(db).find_active_by_account_ids(inv_ids)
    return tx_sums, pt_sums, items_map


def _build_balance(
    account: Account,
    tx_sums: dict,
    pt_sums: dict,
    items_map: dict,
) -> BalanceSummary:
    """배치 로드된 데이터로 잔액 조립 — _calc_balance 와 동일 로직의 N+1 없는 버전."""
    s = tx_sums[account.id]
    if account.account_type != AccountType.INVESTMENT:
        return BalanceSummary(balance=_cash_flow(account.start_balance, s))
    cash = _cash_flow(account.start_balance, s)
    pt = pt_sums.get(account.id, {"buy": Decimal("0"), "sell": Decimal("0")})
    cash = cash - pt["buy"] + pt["sell"]
    items = items_map.get(account.id, [])
    cost, valuation, profit_loss, rate = _summarize_holdings(items)
    return BalanceSummary(
        balance=cash + valuation,
        cash=cash,
        portfolio_cost=cost,
        portfolio_valuation=valuation,
        portfolio_profit_loss=profit_loss,
        portfolio_profit_loss_rate=rate,
    )


def _build_response(account: Account, summary: BalanceSummary) -> AccountResponse:
    return AccountResponse(
        id=account.id,
        household_id=account.household_id,
        name=account.name,
        account_type=account.account_type,
        start_balance=account.start_balance,
        balance=summary.balance,
        color=account.color,
        icon=account.icon,
        sort_order=account.sort_order,
        is_archived=account.is_archived,
        is_manual_asset=account.account_type in MANUAL_ASSET_ACCOUNT_TYPES,
        cash=summary.cash,
        portfolio_cost=summary.portfolio_cost,
        portfolio_valuation=summary.portfolio_valuation,
        portfolio_profit_loss=summary.portfolio_profit_loss,
        portfolio_profit_loss_rate=summary.portfolio_profit_loss_rate,
    )


def _today_kst() -> date:
    """KST 기준 오늘 — 리포트 이번달 판정용."""
    return datetime.now(ZoneInfo("Asia/Seoul")).date()


def _shift_months(d: date, delta: int) -> date:
    """d 기준 delta 개월 이동한 달의 1일."""
    total = d.year * 12 + (d.month - 1) + delta
    return date(total // 12, total % 12 + 1, 1)


def _report_range(
    from_date: date | None, to_date: date | None, this_month_first: date,
) -> tuple[date, date]:
    """리포트 기간 정규화 — to 미지정 시 이번달, from 미지정 시 to−11개월."""
    to_resolved = to_date or this_month_first
    from_resolved = from_date or _shift_months(to_resolved, -11)
    return from_resolved, to_resolved


def _snapshot_to_flow(s) -> AccountMonthlyFlow:
    """박제된 월별 스냅샷 → 월간 유량 DTO."""
    return AccountMonthlyFlow(
        month_date=s.snapshot_date,
        income=s.monthly_income,
        expense=s.monthly_expense,
        fixed_expense=s.monthly_fixed_expense,
        balance=s.balance,
    )


async def _current_month_flow(
    tx_repo: TransactionRepository, account_id: UUID, today: date, balance: Decimal,
) -> AccountMonthlyFlow:
    """아직 박제 전인 이번달 — 거래 실시간 집계 (잔액은 현재값)."""
    m = await tx_repo.sum_by_account_for_month(account_id, today.year, today.month)
    return AccountMonthlyFlow(
        month_date=today.replace(day=1),
        income=m["income"],
        expense=m["expense"],
        fixed_expense=m["fixed_expense"],
        balance=balance,
    )

import logging
from calendar import monthrange
from datetime import date
from decimal import Decimal, InvalidOperation
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums.data_status import DataStatus
from app.core.exceptions import CustomException, ErrorCode
from app.domain.account.enum import MANUAL_ASSET_ACCOUNT_TYPES
from app.domain.account.model import Account
from app.domain.account.repository import AccountRepository
from app.domain.category.enum import CategoryKind
from app.domain.category.model import Category
from app.domain.category.repository import CategoryRepository
from app.domain.fixed.repository import FixedRepository
from app.domain.household.model import Household
from app.domain.transaction.enum import TxType, ValuationDirection
from app.domain.transaction.model import Transaction
from app.domain.transaction.repository import (
    TransactionFilter,
    TransactionRepository,
)
from app.domain.transaction.schema import (
    AccountLedgerItem,
    AccountLedgerPage,
    CalendarDay,
    CalendarFullResponse,
    CalendarResponse,
    TransactionCreateRequest,
    TransactionFormOptionsResponse,
    TransactionListResponse,
    TransactionResponse,
    TransactionUpdateRequest,
)
from app.domain.user.model import User

logger = logging.getLogger(__name__)


async def list_transactions(
    db: AsyncSession,
    household: Household,
    f: TransactionFilter,
    cursor: str | None,
    limit: int,
) -> TransactionListResponse:
    repo = TransactionRepository(db)
    rows = await repo.list_by_cursor(household.id, f, cursor, limit)
    total = await repo.count(household.id, f)

    has_next = len(rows) > limit
    items_rows = rows[:limit]

    # account/category/고정지출 일괄 조회
    account_ids = {r.account_id for r in items_rows}
    account_ids.update({r.to_account_id for r in items_rows if r.to_account_id})
    category_ids = {r.category_id for r in items_rows if r.category_id}
    fixed_ids = {r.fixed_expense_id for r in items_rows if r.fixed_expense_id}

    accounts = await AccountRepository(db).find_by_ids(list(account_ids))
    categories = await CategoryRepository(db).find_by_ids(list(category_ids))
    fixed_expenses = await FixedRepository(db).find_by_ids(list(fixed_ids))
    account_map = {a.id: a for a in accounts}
    category_map = {c.id: c for c in categories}
    fixed_map = {f.id: f for f in fixed_expenses}

    items = [
        _build_response(r, account_map, category_map, fixed_map) for r in items_rows
    ]

    next_cursor = None
    if has_next:
        last = items_rows[-1]
        next_cursor = (
            f"{last.tx_date.isoformat()}|{last.frst_reg_dt.isoformat()}|{last.id}"
        )

    return TransactionListResponse(
        items=items,
        next_cursor=next_cursor,
        has_next=has_next,
        total_count=total,
    )


async def list_account_ledger(
    db: AsyncSession,
    household: Household,
    account_id: UUID,
    cursor: str | None,
    limit: int,
    year: int | None = None,
    month: int | None = None,
) -> AccountLedgerPage:
    """계좌별 거래 이력 — 각 행에 running balance(거래 후 잔액).

    잔액은 "기준 잔액에서 desc 로 역산". 첫 페이지 첫 행의 잔액 = 그 시점까지의
    누적 현금흐름 잔액, 한 칸 옛 거래로 내려갈 때마다 위 행의 signed_amount 를
    빼서 그 아래 잔액을 만든다. 페이지 경계는 carry 를 커서에 실어 이어붙인다.

    year+month 를 주면 그 달 거래만(거래 탭). 기준점은 "그 달 말까지의 누적 잔액"
    이라 미래 달 거래와 무관하게 그 달 안에서 잔액이 맞는다. 없으면 전체(계좌 상세).
    """
    repo = TransactionRepository(db)
    account = await AccountRepository(db).find_by_id(account_id)
    if account is None or account.household_id != household.id:
        raise CustomException(ErrorCode.NOT_FOUND)
    # 모든 통장 타입 허용 — 현금흐름/수동자산은 running balance 정확, INVESTMENT 는
    # 매매현금이 거래 밖이라 잔액은 부정확(프론트에서 잔액 표시를 숨긴다).

    tx_cursor, carry = _split_ledger_cursor(cursor)
    f, balance_to_date = _ledger_filter(account_id, year, month)
    rows = await repo.list_by_cursor(household.id, f, tx_cursor, limit)
    total = await repo.count(household.id, f)

    has_next = len(rows) > limit
    items_rows = rows[:limit]

    start_balance = await _ledger_start_balance(repo, account, carry, balance_to_date)
    account_map, category_map, fixed_map = await _load_ledger_maps(db, items_rows)
    items, running = _build_ledger_items(
        items_rows, account_id, account_map, category_map, fixed_map, start_balance,
    )

    next_cursor = None
    if has_next:
        last = items_rows[-1]
        next_cursor = (
            f"{last.tx_date.isoformat()}|{last.frst_reg_dt.isoformat()}"
            f"|{last.id}|{running}"
        )

    return AccountLedgerPage(
        items=items,
        next_cursor=next_cursor,
        has_next=has_next,
        total_count=total,
    )


async def create_transaction(
    db: AsyncSession,
    household: Household,
    req: TransactionCreateRequest,
    current_user: User,
) -> TransactionResponse:
    account_ids = [req.account_id]
    if req.to_account_id is not None:
        account_ids.append(req.to_account_id)
    category_ids = [req.category_id] if req.category_id else []
    await _validate_fk_belong_to_household(
        db, household.id, account_ids, category_ids, tx_type=req.tx_type,
    )
    await _validate_fixed_belongs(db, household.id, req.fixed_expense_id)

    tx = Transaction(
        household_id=household.id,
        tx_type=req.tx_type,
        amount=req.amount,
        tx_date=req.tx_date,
        account_id=req.account_id,
        to_account_id=req.to_account_id if req.tx_type == TxType.TRANSFER else None,
        category_id=req.category_id,
        paid_by_user_id=req.paid_by_user_id or current_user.id,
        fixed_expense_id=req.fixed_expense_id,
        memo=req.memo,
        valuation_direction=(
            req.valuation_direction.value
            if req.tx_type == TxType.VALUATION and req.valuation_direction
            else None
        ),
        data_stat_cd=DataStatus.ACTIVE,
    )
    await TransactionRepository(db).save(tx)
    logger.info("거래 생성 (tx_id=%s, type=%s, amount=%s)", tx.id, req.tx_type, req.amount)
    return await _single_response(db, tx)


async def update_transaction(
    db: AsyncSession,
    household: Household,
    tx_id: UUID,
    req: TransactionUpdateRequest,
) -> TransactionResponse:
    repo = TransactionRepository(db)
    tx = await repo.find_by_id(tx_id)
    if not tx or tx.household_id != household.id:
        raise CustomException(ErrorCode.NOT_FOUND)

    changed = _changed_fields(tx, req)  # 병합 전 기준 — 안 바뀐 FK·옛 행은 다시 막지 않는다
    await _validate_update_fks(db, household, tx, req, changed)
    _apply_partial_update(tx, req)
    _normalize_to_account(tx)
    _normalize_category(tx)
    _normalize_valuation(tx)
    _normalize_fixed(tx)
    _validate_transfer_consistency(tx)
    _validate_type_requirements(tx, changed)

    await db.flush()
    return await _single_response(db, tx)


async def delete_transaction(
    db: AsyncSession, household: Household, tx_id: UUID,
) -> None:
    repo = TransactionRepository(db)
    tx = await repo.find_by_id(tx_id)
    if not tx or tx.household_id != household.id:
        raise CustomException(ErrorCode.NOT_FOUND)

    tx.data_stat_cd = DataStatus.DELETED
    await db.flush()
    logger.info("거래 삭제 (tx_id=%s)", tx_id)


async def get_transaction_detail(
    db: AsyncSession, household: Household, tx_id: UUID,
) -> TransactionResponse:
    """거래 단건 조회 — account/category 조인 필드 포함"""
    repo = TransactionRepository(db)
    tx = await repo.find_by_id(tx_id)
    if not tx or tx.household_id != household.id or tx.data_stat_cd != DataStatus.ACTIVE:
        raise CustomException(ErrorCode.NOT_FOUND)
    return await _single_response(db, tx)


async def get_calendar_full(
    db: AsyncSession, household: Household, year: int, month: int,
    account_id: UUID | None = None,
) -> CalendarFullResponse:
    """달력 페이지 1호출 — calendar(일별 합계) + stats(카테고리별) + 그달 거래 전부.

    account_id 를 주면 세 묶음 모두 그 통장이 낀 거래만. 거래 탭에서 좌(목록)·우(요약·
    달력·Top5)가 같은 응답을 보도록 하는 게 목적이라 셋이 같은 필터를 타야 한다.
    """
    # 순환 import 방지 — 함수 안에서 import
    from app.domain.stats import service as stats_service

    calendar = await get_calendar(db, household, year, month, account_id)
    stats = await stats_service.get_monthly_stats(
        db, household, year, month, account_id,
    )

    f = TransactionFilter(year=year, month=month, account_id=account_id)
    # 한 달치 거래 — 가계부 평균 100 미만이라 cursor 페이징 불필요, 큰 limit 로 통째.
    tx_page = await list_transactions(db, household, f, cursor=None, limit=500)

    return CalendarFullResponse(
        year=calendar.year,
        month=calendar.month,
        monthly_income=calendar.monthly_income,
        monthly_expense=calendar.monthly_expense,
        monthly_transfer=calendar.monthly_transfer,
        days=calendar.days,
        by_category=stats.by_category,
        transactions=tx_page.items,
    )


async def get_form_options(
    db: AsyncSession, household: Household,
) -> TransactionFormOptionsResponse:
    """거래 등록/수정 폼 옵션 — 통장 + 카테고리 + 활성 고정지출 한 번에."""
    # 순환 import 방지
    from app.domain.account import service as account_service
    from app.domain.category import service as category_service
    from app.domain.fixed import service as fixed_service

    accounts = await account_service.list_accounts(db, household)
    categories = await category_service.list_categories(db, household)
    fixed_expenses = await fixed_service.list_fixed_expenses(
        db, household, is_archived=False,
    )

    return TransactionFormOptionsResponse(
        accounts=accounts,
        categories=categories,
        fixed_expenses=fixed_expenses,
    )


async def get_calendar(
    db: AsyncSession, household: Household, year: int, month: int,
    account_id: UUID | None = None,
) -> CalendarResponse:
    """달력 뷰 — 일별 income/expense/transfer 합계 + 월간 합계"""
    repo = TransactionRepository(db)
    rows = await repo.daily_sums_for_month(household.id, year, month, account_id)

    by_date: dict[date, dict] = {}
    monthly_income = Decimal("0.00")
    monthly_expense = Decimal("0.00")
    monthly_transfer = Decimal("0.00")

    for tx_date, tx_type, total, cnt in rows:
        d = by_date.setdefault(
            tx_date,
            {
                "income": Decimal("0.00"),
                "expense": Decimal("0.00"),
                "transfer": Decimal("0.00"),
                "count": 0,
            },
        )
        if tx_type == TxType.INCOME:
            d["income"] += total
            monthly_income += total
        elif tx_type in (TxType.EXPENSE, TxType.FIXED_EXPENSE):
            d["expense"] += total
            monthly_expense += total
        elif tx_type == TxType.TRANSFER:
            d["transfer"] += total
            monthly_transfer += total
        d["count"] += cnt

    days = [
        CalendarDay(
            date=d,
            income=v["income"],
            expense=v["expense"],
            transfer=v["transfer"],
            count=v["count"],
        )
        for d, v in sorted(by_date.items())
    ]

    return CalendarResponse(
        year=year,
        month=month,
        monthly_income=monthly_income,
        monthly_expense=monthly_expense,
        monthly_transfer=monthly_transfer,
        days=days,
    )


# 부분 업데이트(PATCH) 대상 필드 — req·tx 동일 이름. 추가 시 여기만 늘리면 된다.
_TX_UPDATABLE_FIELDS = (
    "tx_type", "amount", "tx_date", "account_id", "to_account_id",
    "category_id", "paid_by_user_id", "fixed_expense_id", "memo",
    "valuation_direction",
)


def _category_kind_matches(tx_type: TxType, kind: str) -> bool:
    """지출계열 거래엔 EXPENSE 카테고리, 수입 거래엔 INCOME 카테고리만 허용.
    kind 불일치 시 월합계(타입 기준)와 카테고리차트(kind 기준)가 어긋난다."""
    if tx_type in (TxType.EXPENSE, TxType.FIXED_EXPENSE):
        return kind == CategoryKind.EXPENSE
    if tx_type == TxType.INCOME:
        return kind == CategoryKind.INCOME
    return True  # TRANSFER 는 category 없음


async def _validate_fixed_belongs(
    db: AsyncSession, household_id: UUID, fixed_expense_id: UUID | None,
) -> None:
    """고정지출 매핑 ID 가 같은 household 소속인지 검증 (타 가계부 ID 차단)."""
    if fixed_expense_id is None:
        return
    fixed = await FixedRepository(db).find_by_id(fixed_expense_id)
    if not fixed or fixed.household_id != household_id:
        raise CustomException(ErrorCode.NOT_FOUND)


async def _validate_fk_belong_to_household(
    db: AsyncSession,
    household_id: UUID,
    account_ids: list[UUID],
    category_ids: list[UUID],
    tx_type: TxType | None = None,
) -> None:
    """account/category 가 모두 같은 household 소속인지 검증.

    tx_type 이 주어지면 수동자산 통장(부동산·연금·금)은 이체(TRANSFER)만 허용 —
    지출/수입이 수동자산 통장에 걸리면 BAD_REQUEST.
    """
    if account_ids:
        accounts = await AccountRepository(db).find_by_ids(account_ids)
        if len(accounts) != len(set(account_ids)):
            raise CustomException(ErrorCode.NOT_FOUND)
        for a in accounts:
            if a.household_id != household_id or a.data_stat_cd != DataStatus.ACTIVE:
                raise CustomException(ErrorCode.NOT_FOUND)
        if tx_type is not None:
            _check_account_types(accounts, tx_type)
    if category_ids:
        categories = await CategoryRepository(db).find_by_ids(category_ids)
        if len(categories) != len(set(category_ids)):
            raise CustomException(ErrorCode.NOT_FOUND)
        for c in categories:
            if c.household_id != household_id or c.data_stat_cd != DataStatus.ACTIVE:
                raise CustomException(ErrorCode.NOT_FOUND)
        if tx_type is not None:
            _check_category_kinds(categories, tx_type)


def _check_account_types(accounts: list[Account], tx_type: TxType) -> None:
    """평가조정은 수동자산 통장에만, 지출/수입은 수동자산 통장엔 안 된다(이체는 어디든)."""
    if tx_type == TxType.VALUATION:
        if any(a.account_type not in MANUAL_ASSET_ACCOUNT_TYPES for a in accounts):
            raise CustomException(ErrorCode.BAD_REQUEST)
    elif tx_type != TxType.TRANSFER:
        if any(a.account_type in MANUAL_ASSET_ACCOUNT_TYPES for a in accounts):
            raise CustomException(ErrorCode.BAD_REQUEST)


def _check_category_kinds(categories: list[Category], tx_type: TxType) -> None:
    if any(not _category_kind_matches(tx_type, c.kind) for c in categories):
        raise CustomException(ErrorCode.BAD_REQUEST)


def _build_response(
    tx: Transaction,
    account_map: dict,
    category_map: dict,
    fixed_map: dict,
) -> TransactionResponse:
    account = account_map.get(tx.account_id)
    to_account = account_map.get(tx.to_account_id) if tx.to_account_id else None
    category = category_map.get(tx.category_id) if tx.category_id else None
    fixed = fixed_map.get(tx.fixed_expense_id) if tx.fixed_expense_id else None
    return TransactionResponse(
        id=tx.id,
        household_id=tx.household_id,
        tx_type=tx.tx_type,
        amount=tx.amount,
        tx_date=tx.tx_date,
        account_id=tx.account_id,
        account_name=account.name if account else None,
        to_account_id=tx.to_account_id,
        to_account_name=to_account.name if to_account else None,
        category_id=tx.category_id,
        category_name=category.name if category else None,
        category_color=category.color if category else None,
        category_icon=category.icon if category else None,
        paid_by_user_id=tx.paid_by_user_id,
        fixed_expense_id=tx.fixed_expense_id,
        fixed_expense_name=fixed.name if fixed else None,
        valuation_direction=(
            ValuationDirection(tx.valuation_direction)
            if tx.valuation_direction
            else None
        ),
        memo=tx.memo,
    )


def _signed_amount(tx: Transaction, account_id: UUID) -> Decimal:
    """이 계좌 관점의 부호 금액. 이체는 출금계좌 −, 입금계좌 +."""
    if tx.tx_type == TxType.TRANSFER and tx.to_account_id == account_id:
        return tx.amount  # 이체 입금
    if tx.tx_type == TxType.INCOME:
        return tx.amount
    if tx.tx_type == TxType.VALUATION:
        return (
            tx.amount
            if tx.valuation_direction == ValuationDirection.INCREASE
            else -tx.amount
        )
    return -tx.amount  # EXPENSE/FIXED_EXPENSE/이체 출금


def _split_ledger_cursor(cursor: str | None) -> tuple[str | None, Decimal | None]:
    """ledger 커서 = "{tx_date}|{frst_reg_dt}|{id}|{carry_balance}".

    carry_balance 는 다음 페이지 첫 행의 시작 잔액(이전 페이지 끝에서 이어받음).
    list_by_cursor 가 쓰는 정렬 커서는 앞 3-tuple 뿐이라 carry 를 떼어 분리한다.
    """
    if not cursor:
        return None, None
    base, _, carry_str = cursor.rpartition("|")
    if not base:
        return cursor, None
    try:
        return base, Decimal(carry_str)
    except (InvalidOperation, ValueError):
        return cursor, None


def _ledger_filter(
    account_id: UUID, year: int | None, month: int | None,
) -> tuple[TransactionFilter, date | None]:
    """거래 탭(year+month)이면 그 달 + 기준일=말일, 아니면 전체(기준일 없음)."""
    if year is not None and month is not None:
        return (
            TransactionFilter(account_id=account_id, year=year, month=month),
            date(year, month, monthrange(year, month)[1]),
        )
    return TransactionFilter(account_id=account_id), None


async def _ledger_start_balance(
    repo: TransactionRepository,
    account: Account,
    carry: Decimal | None,
    balance_to_date: date | None,
) -> Decimal:
    """페이지 시작 잔액 — 이어지는 페이지면 carry, 첫 페이지면 기준일까지 누적 현금흐름."""
    if carry is not None:
        return carry
    sums = await repo.sum_for_account(account.id, to_date=balance_to_date)
    return (
        account.start_balance
        + sums["income"]
        - sums["expense"]
        - sums["transfer_out"]
        + sums["transfer_in"]
        + sums["valuation_net"]  # 수동자산 평가조정 누적 — 현금흐름 통장은 0.
    )


async def _load_ledger_maps(
    db: AsyncSession, rows: list[Transaction],
) -> tuple[dict, dict, dict]:
    """행에 등장한 계좌(이체 상대 포함)·카테고리·고정지출 batch 로드."""
    account_ids = {r.account_id for r in rows}
    account_ids.update({r.to_account_id for r in rows if r.to_account_id})
    category_ids = {r.category_id for r in rows if r.category_id}
    fixed_ids = {r.fixed_expense_id for r in rows if r.fixed_expense_id}
    accounts = await AccountRepository(db).find_by_ids(list(account_ids))
    categories = await CategoryRepository(db).find_by_ids(list(category_ids))
    fixed_expenses = await FixedRepository(db).find_by_ids(list(fixed_ids))
    return (
        {a.id: a for a in accounts},
        {c.id: c for c in categories},
        {f.id: f for f in fixed_expenses},
    )


def _build_ledger_items(
    rows: list[Transaction],
    account_id: UUID,
    account_map: dict,
    category_map: dict,
    fixed_map: dict,
    start_balance: Decimal,
) -> tuple[list[AccountLedgerItem], Decimal]:
    """각 행에 running balance(거래 후 잔액)를 부여하며 역산. 끝 잔액(다음 페이지 carry) 반환.

    desc 정렬이므로 첫 행이 start_balance, 한 칸 옛 거래로 내려갈 때마다 위 행의
    signed_amount 를 빼서 아래 잔액을 만든다. 순수 계산.
    """
    items: list[AccountLedgerItem] = []
    running = start_balance
    for r in rows:
        signed = _signed_amount(r, account_id)
        base = _build_response(r, account_map, category_map, fixed_map)
        items.append(
            AccountLedgerItem(
                **base.model_dump(),
                signed_amount=signed,
                balance_after=running,
            )
        )
        running -= signed
    return items, running


# NOT NULL 컬럼 — 요청의 None 은 "안 바꿈". 나머지(선택 필드)는 요청에 명시되면 None 도 값(해제)
_TX_REQUIRED_FIELDS = ("tx_type", "amount", "tx_date", "account_id")


def _requested(req: TransactionUpdateRequest, tx: Transaction, field: str):
    """병합 뒤 값. 프론트는 상세값 전체를 보내므로 사용자가 지운 칸은 None 으로 온다."""
    value = getattr(req, field)
    if field in _TX_REQUIRED_FIELDS:
        return value if value is not None else getattr(tx, field)
    return value if field in req.model_fields_set else getattr(tx, field)


def _changed_fields(tx: Transaction, req: TransactionUpdateRequest) -> set[str]:
    return {f for f in _TX_UPDATABLE_FIELDS if _requested(req, tx, f) != getattr(tx, f)}


async def _validate_update_fks(
    db: AsyncSession,
    household: Household,
    tx: Transaction,
    req: TransactionUpdateRequest,
    changed: set[str],
) -> None:
    """**바뀌는** FK(계좌·이체상대·카테고리·고정지출)만 소속·ACTIVE 검증.

    안 바꾼 값까지 보면 삭제된 고정지출·카테고리·상대 통장이 걸린 거래는 메모 하나도 못 고친다.
    계좌 규칙(수동자산엔 이체·평가조정만)과 카테고리 kind 는 타입이 바뀔 때 남은 값에도 다시 본다
    (삭제 여부는 안 따짐).
    """
    new = {f: _requested(req, tx, f) for f in _TX_UPDATABLE_FIELDS}
    new_type = TxType(new["tx_type"])

    if changed & {"tx_type", "account_id", "to_account_id"}:
        ids = [new["account_id"]]
        if new_type == TxType.TRANSFER and new["to_account_id"] is not None:
            ids.append(new["to_account_id"])
        # 소속·ACTIVE 는 새로 고른 통장만 — 이미 삭제된 상대 통장이 남아 있어도 출발 통장은 바꿀 수 있게
        fresh = [i for f, i in zip(("account_id", "to_account_id"), ids) if f in changed]
        # 유형이 바뀌면 출발 통장에 새로 거는 셈 — 삭제된 통장에 단독 거래가 생기지 않게
        if "tx_type" in changed and "account_id" not in changed:
            fresh.append(new["account_id"])
        await _validate_fk_belong_to_household(db, household.id, fresh, [])
        _check_account_types(await AccountRepository(db).find_by_ids(ids), new_type)
    # 이체·평가조정은 카테고리를 정규화로 지우므로 검증할 필요가 없다
    if (
        changed & {"tx_type", "category_id"}
        and new["category_id"] is not None
        and new_type not in (TxType.TRANSFER, TxType.VALUATION)
    ):
        if "category_id" in changed:
            await _validate_fk_belong_to_household(
                db, household.id, [], [new["category_id"]], tx_type=new_type,
            )
        else:  # 안 바꾼 카테고리(삭제됐을 수 있음)는 kind 만
            categories = await CategoryRepository(db).find_by_ids([new["category_id"]])
            _check_category_kinds(categories, new_type)
    if "fixed_expense_id" in changed:
        await _validate_fixed_belongs(db, household.id, new["fixed_expense_id"])


def _apply_partial_update(tx: Transaction, req: TransactionUpdateRequest) -> None:
    """요청 병합 — 필수 필드는 None 이면 유지, 선택 필드는 명시된 None 으로 해제."""
    for field in _TX_UPDATABLE_FIELDS:
        setattr(tx, field, _requested(req, tx, field))


def _normalize_to_account(tx: Transaction) -> None:
    """TRANSFER 가 아니면 도착계좌는 무의미 — 타입 전환(이체→지출 등) 시 잔존
    to_account_id 를 제거해야 상대 계좌 원장에 가짜 거래가 잡히지 않는다."""
    if tx.tx_type != TxType.TRANSFER:
        tx.to_account_id = None


def _normalize_category(tx: Transaction) -> None:
    """TRANSFER 엔 카테고리가 없다(생성은 schema 가 막지만 타입 전환은 여기서).

    남아 있으면 카테고리 집계가 출금·입금 통장 양쪽에서 같은 금액을 더해
    "통장별 합 > 가계부 전체" 가 된다 — _normalize_to_account 와 같은 이유다."""
    if tx.tx_type == TxType.TRANSFER:
        tx.category_id = None


def _normalize_valuation(tx: Transaction) -> None:
    """평가조정엔 카테고리가 없고, 그 외 타입엔 방향이 없다 — 생성 schema 와 같은 규칙을
    타입 전환에도. 남은 카테고리는 카테고리 차트엔 잡히고 월 지출엔 안 잡혀 둘이 어긋난다."""
    if tx.tx_type == TxType.VALUATION:
        tx.category_id = None
    else:
        tx.valuation_direction = None


def _normalize_fixed(tx: Transaction) -> None:
    """고정지출 연결은 FIXED_EXPENSE 만 — 지출로 바꿨는데 남으면 고정지출 사용액에 계속 잡힌다."""
    if tx.tx_type != TxType.FIXED_EXPENSE:
        tx.fixed_expense_id = None


def _validate_type_requirements(tx: Transaction, changed: set[str]) -> None:
    """타입별 필수값 — 전환 뒤에도 생성 schema 와 같게.

    방향 없는 평가조정은 잔액 계산에서 감소로 처리돼 금액이 틀린다(sum_for_account).
    관련 칸을 안 건드린 수정은 통과 — 옛 규칙으로 저장된 행도 메모는 고칠 수 있게.
    """
    if not changed & {"tx_type", "valuation_direction", "fixed_expense_id"}:
        return
    if tx.tx_type == TxType.VALUATION and tx.valuation_direction is None:
        raise CustomException(ErrorCode.BAD_REQUEST)
    if tx.tx_type == TxType.FIXED_EXPENSE and tx.fixed_expense_id is None:
        raise CustomException(ErrorCode.BAD_REQUEST)


def _validate_transfer_consistency(tx: Transaction) -> None:
    """TRANSFER 는 도착 계좌가 있어야 하고 출발=도착이면 안 된다."""
    if tx.tx_type == TxType.TRANSFER:
        if tx.to_account_id is None or tx.to_account_id == tx.account_id:
            raise CustomException(ErrorCode.BAD_REQUEST)


async def _single_response(db: AsyncSession, tx: Transaction) -> TransactionResponse:
    """단일 거래 응답 — account/category/고정지출 JOIN"""
    account_ids = [tx.account_id]
    if tx.to_account_id:
        account_ids.append(tx.to_account_id)
    category_ids = [tx.category_id] if tx.category_id else []
    fixed_ids = [tx.fixed_expense_id] if tx.fixed_expense_id else []

    accounts = await AccountRepository(db).find_by_ids(account_ids)
    categories = await CategoryRepository(db).find_by_ids(category_ids)
    fixed_expenses = await FixedRepository(db).find_by_ids(fixed_ids)
    return _build_response(
        tx,
        {a.id: a for a in accounts},
        {c.id: c for c in categories},
        {f.id: f for f in fixed_expenses},
    )

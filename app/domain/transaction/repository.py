from calendar import monthrange
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import and_, case, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.enums.data_status import DataStatus
from app.domain.account.model import Account
from app.domain.transaction.enum import TxType, ValuationDirection
from app.domain.transaction.model import Transaction


def _account_involved(account_id: UUID):
    """그 통장이 낀 거래 — 출금처 또는 입금처(이체 상대). _build_conditions 와 동일 규칙."""
    return or_(
        Transaction.account_id == account_id,
        Transaction.to_account_id == account_id,
    )


class TransactionFilter:
    def __init__(
        self,
        tx_type: TxType | None = None,
        account_id: UUID | None = None,
        category_id: UUID | None = None,
        year: int | None = None,
        month: int | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
    ) -> None:
        self.tx_type = tx_type
        self.account_id = account_id
        self.category_id = category_id
        # year + month → from/to 자동 변환
        if year is not None and month is not None:
            self.from_date = date(year, month, 1)
            self.to_date = date(year, month, monthrange(year, month)[1])
        else:
            self.from_date = from_date
            self.to_date = to_date


class TransactionRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def find_by_id(self, tx_id: UUID) -> Transaction | None:
        result = await self.db.execute(
            select(Transaction).where(
                and_(
                    Transaction.id == tx_id,
                    Transaction.data_stat_cd == DataStatus.ACTIVE,
                )
            )
        )
        return result.scalar_one_or_none()

    def _build_conditions(self, household_id: UUID, f: TransactionFilter):
        conds = [
            Transaction.household_id == household_id,
            Transaction.data_stat_cd == DataStatus.ACTIVE,
        ]
        if f.tx_type is not None:
            conds.append(Transaction.tx_type == f.tx_type)
        if f.account_id is not None:
            conds.append(_account_involved(f.account_id))
        if f.category_id is not None:
            conds.append(Transaction.category_id == f.category_id)
        if f.from_date is not None:
            conds.append(Transaction.tx_date >= f.from_date)
        if f.to_date is not None:
            conds.append(Transaction.tx_date <= f.to_date)
        return conds

    @staticmethod
    def _cursor_after(cursor: str | None):
        if not cursor:
            return None
        # 정렬 키(tx_date desc, frst_reg_dt desc, id desc)와 동일한 3-tuple 비교.
        # id 가 uuid4(랜덤)라 tie-breaker 일 뿐, 같은 날 순서는 frst_reg_dt 가 결정.
        try:
            date_str, reg_str, id_str = cursor.split("|", 2)
            cur_date = date.fromisoformat(date_str)
            cur_reg = datetime.fromisoformat(reg_str)
            cur_id = UUID(id_str)
        except (ValueError, AttributeError):
            return None
        return or_(
            Transaction.tx_date < cur_date,
            and_(Transaction.tx_date == cur_date, Transaction.frst_reg_dt < cur_reg),
            and_(
                Transaction.tx_date == cur_date,
                Transaction.frst_reg_dt == cur_reg,
                Transaction.id < cur_id,
            ),
        )

    async def list_by_cursor(
        self,
        household_id: UUID,
        f: TransactionFilter,
        cursor: str | None,
        limit: int,
    ) -> list[Transaction]:
        conds = self._build_conditions(household_id, f)
        cursor_cond = self._cursor_after(cursor)
        if cursor_cond is not None:
            conds.append(cursor_cond)
        result = await self.db.execute(
            select(Transaction)
            .where(and_(*conds))
            .order_by(
                Transaction.tx_date.desc(),
                Transaction.frst_reg_dt.desc(),
                Transaction.id.desc(),
            )
            .limit(limit + 1)
        )
        return list(result.scalars().all())

    async def count(self, household_id: UUID, f: TransactionFilter) -> int:
        conds = self._build_conditions(household_id, f)
        result = await self.db.execute(
            select(func.count(Transaction.id)).where(and_(*conds))
        )
        return result.scalar() or 0

    async def soft_delete_solo_by_account_id(self, account_id: UUID) -> int:
        """이 통장의 단독거래(수입/지출/고정비, 이체 아님) soft-delete — 통장 cascade용.
        이체는 상대 통장과 공유라 여기서 제외(to_account_id IS NULL 로 단독만)."""
        stmt = (
            update(Transaction)
            .where(
                and_(
                    Transaction.account_id == account_id,
                    Transaction.to_account_id.is_(None),
                    Transaction.tx_type.in_(
                        [TxType.INCOME, TxType.EXPENSE, TxType.FIXED_EXPENSE]
                    ),
                    Transaction.data_stat_cd == DataStatus.ACTIVE,
                )
            )
            .values(data_stat_cd=DataStatus.DELETED)
            .execution_options(synchronize_session=False)
        )
        result = await self.db.execute(stmt)
        return result.rowcount or 0

    async def soft_delete_transfers_with_dead_counterparty(
        self, account_id: UUID,
    ) -> int:
        """이 통장이 낀 이체 중 '상대 통장도 이미 DELETED'인 것만 soft-delete.
        상대가 ACTIVE면 행 유지 — 살아있는 상대 통장 잔액·통계 보존(D안).
        통장 본체를 죽이기 전에 호출해야 자기 자신을 상대로 오판하지 않음."""
        dead = aliased(Account)
        counterparty = case(
            (Transaction.account_id == account_id, Transaction.to_account_id),
            else_=Transaction.account_id,
        )
        stmt = (
            update(Transaction)
            .where(
                and_(
                    Transaction.data_stat_cd == DataStatus.ACTIVE,
                    Transaction.tx_type == TxType.TRANSFER,
                    or_(
                        Transaction.account_id == account_id,
                        Transaction.to_account_id == account_id,
                    ),
                    exists().where(
                        and_(
                            dead.id == counterparty,
                            dead.data_stat_cd == DataStatus.DELETED,
                        )
                    ),
                )
            )
            .values(data_stat_cd=DataStatus.DELETED)
            .execution_options(synchronize_session=False)
        )
        result = await self.db.execute(stmt)
        return result.rowcount or 0

    async def sum_for_account(
        self, account_id: UUID, to_date: date | None = None,
    ) -> dict[str, Decimal]:
        """통장별 거래 합계 — balance 계산용.

        to_date 를 주면 그 날짜까지의 누적(running balance 기준점용). 없으면 전체.
        """
        conds = [
            Transaction.data_stat_cd == DataStatus.ACTIVE,
            or_(
                Transaction.account_id == account_id,
                Transaction.to_account_id == account_id,
            ),
        ]
        if to_date is not None:
            conds.append(Transaction.tx_date <= to_date)
        result = await self.db.execute(
            select(
                Transaction.tx_type,
                Transaction.account_id,
                Transaction.to_account_id,
                Transaction.valuation_direction,
                func.sum(Transaction.amount).label("total"),
            )
            .where(and_(*conds))
            .group_by(
                Transaction.tx_type,
                Transaction.account_id,
                Transaction.to_account_id,
                Transaction.valuation_direction,
            )
        )

        sums = {
            "income": Decimal("0"),
            "expense": Decimal("0"),
            "transfer_out": Decimal("0"),
            "transfer_in": Decimal("0"),
            "valuation_net": Decimal("0"),
        }
        for tx_type, acc_id, to_acc_id, direction, total in result.all():
            if tx_type == TxType.INCOME and acc_id == account_id:
                sums["income"] += total
            elif (
                tx_type in (TxType.EXPENSE, TxType.FIXED_EXPENSE)
                and acc_id == account_id
            ):
                sums["expense"] += total
            elif tx_type == TxType.TRANSFER:
                if acc_id == account_id:
                    sums["transfer_out"] += total
                if to_acc_id == account_id:
                    sums["transfer_in"] += total
            elif tx_type == TxType.VALUATION and acc_id == account_id:
                # 평가조정 — 현금 유입 없는 가치 증감. 방향대로 잔액에만 반영.
                if direction == ValuationDirection.INCREASE:
                    sums["valuation_net"] += total
                else:
                    sums["valuation_net"] -= total
        return sums

    async def sum_for_accounts(
        self, account_ids: list[UUID],
    ) -> dict[UUID, dict[str, Decimal]]:
        """여러 통장 거래합 배치 — sum_for_account 의 N+1 제거 버전(현재잔액용).
        단건 sum_for_account 와 동일 분배 로직(이체는 출금·입금 양쪽 반영)."""
        base = {
            aid: {
                "income": Decimal("0"),
                "expense": Decimal("0"),
                "transfer_out": Decimal("0"),
                "transfer_in": Decimal("0"),
                "valuation_net": Decimal("0"),
            }
            for aid in account_ids
        }
        if not account_ids:
            return base
        id_set = set(account_ids)
        result = await self.db.execute(
            select(
                Transaction.tx_type,
                Transaction.account_id,
                Transaction.to_account_id,
                Transaction.valuation_direction,
                func.sum(Transaction.amount).label("total"),
            )
            .where(
                and_(
                    Transaction.data_stat_cd == DataStatus.ACTIVE,
                    or_(
                        Transaction.account_id.in_(account_ids),
                        Transaction.to_account_id.in_(account_ids),
                    ),
                )
            )
            .group_by(
                Transaction.tx_type,
                Transaction.account_id,
                Transaction.to_account_id,
                Transaction.valuation_direction,
            )
        )
        for tx_type, acc_id, to_acc_id, direction, total in result.all():
            if acc_id in id_set:
                if tx_type == TxType.INCOME:
                    base[acc_id]["income"] += total
                elif tx_type in (TxType.EXPENSE, TxType.FIXED_EXPENSE):
                    base[acc_id]["expense"] += total
                elif tx_type == TxType.TRANSFER:
                    base[acc_id]["transfer_out"] += total
                elif tx_type == TxType.VALUATION:
                    if direction == ValuationDirection.INCREASE:
                        base[acc_id]["valuation_net"] += total
                    else:
                        base[acc_id]["valuation_net"] -= total
            if to_acc_id in id_set and tx_type == TxType.TRANSFER:
                base[to_acc_id]["transfer_in"] += total
        return base

    async def sum_by_account_for_month(
        self, account_id: UUID, year: int, month: int,
    ) -> dict[str, Decimal]:
        """account 단위 그 달 INCOME/EXPENSE/FIXED_EXPENSE 합계. TRANSFER 제외.

        account_snapshots 의 monthly_* 캐시 컬럼 박제용.
        """
        stmt = (
            select(Transaction.tx_type, func.sum(Transaction.amount).label("total"))
            .where(
                and_(
                    Transaction.account_id == account_id,
                    Transaction.data_stat_cd == DataStatus.ACTIVE,
                    Transaction.tx_type.in_(
                        [TxType.INCOME, TxType.EXPENSE, TxType.FIXED_EXPENSE]
                    ),
                    func.extract("year", Transaction.tx_date) == year,
                    func.extract("month", Transaction.tx_date) == month,
                )
            )
            .group_by(Transaction.tx_type)
        )
        result = await self.db.execute(stmt)

        sums: dict[str, Decimal] = {
            "income": Decimal("0"),
            "expense": Decimal("0"),
            "fixed_expense": Decimal("0"),
        }
        key_map = {
            TxType.INCOME: "income",
            TxType.EXPENSE: "expense",
            TxType.FIXED_EXPENSE: "fixed_expense",
        }
        for tx_type, total in result.all():
            sums[key_map[tx_type]] += total
        return sums

    async def sum_by_category_for_month(
        self, household_id: UUID, year: int, month: int,
        account_id: UUID | None = None,
    ) -> list[tuple[UUID, Decimal]]:
        """category_id 별 EXPENSE/INCOME 합계 (TRANSFER 는 category_id 없음)"""
        conds = [
            Transaction.household_id == household_id,
            func.extract("year", Transaction.tx_date) == year,
            func.extract("month", Transaction.tx_date) == month,
            Transaction.category_id.is_not(None),
            Transaction.data_stat_cd == DataStatus.ACTIVE,
        ]
        if account_id is not None:
            conds.append(_account_involved(account_id))
        stmt = (
            select(
                Transaction.category_id,
                func.coalesce(func.sum(Transaction.amount), 0).label("total"),
            )
            .where(and_(*conds))
            .group_by(Transaction.category_id)
        )
        result = await self.db.execute(stmt)
        return [(row[0], Decimal(row[1])) for row in result.all()]

    async def sum_by_fixed_for_month(
        self, household_id: UUID, year: int, month: int,
    ) -> list[tuple[UUID, Decimal]]:
        """fixed_expense_id 별 해당 월 지출 합계 — 고정지출별 누적 사용액.

        EXPENSE 와 FIXED_EXPENSE 둘 다 포함 (기존 EXPENSE+fixed_expense_id 호환).
        """
        stmt = (
            select(
                Transaction.fixed_expense_id,
                func.coalesce(func.sum(Transaction.amount), 0).label("total"),
            )
            .where(
                and_(
                    Transaction.household_id == household_id,
                    func.extract("year", Transaction.tx_date) == year,
                    func.extract("month", Transaction.tx_date) == month,
                    Transaction.fixed_expense_id.is_not(None),
                    Transaction.tx_type.in_(
                        [TxType.EXPENSE, TxType.FIXED_EXPENSE]
                    ),
                    Transaction.data_stat_cd == DataStatus.ACTIVE,
                )
            )
            .group_by(Transaction.fixed_expense_id)
        )
        result = await self.db.execute(stmt)
        return [(row[0], Decimal(row[1])) for row in result.all()]

    async def sum_by_type_for_month(
        self, household_id: UUID, year: int, month: int,
        account_id: UUID | None = None,
    ) -> dict[str, Decimal]:
        """tx_type 별 월 합계 (income/expense/transfer)"""
        conds = [
            Transaction.household_id == household_id,
            func.extract("year", Transaction.tx_date) == year,
            func.extract("month", Transaction.tx_date) == month,
            Transaction.data_stat_cd == DataStatus.ACTIVE,
        ]
        if account_id is not None:
            conds.append(_account_involved(account_id))
        stmt = (
            select(
                Transaction.tx_type,
                func.coalesce(func.sum(Transaction.amount), 0).label("total"),
            )
            .where(and_(*conds))
            .group_by(Transaction.tx_type)
        )
        result = await self.db.execute(stmt)
        sums = {
            "income": Decimal("0.00"),
            "expense": Decimal("0.00"),
            "transfer": Decimal("0.00"),
        }
        for tx_type, total in result.all():
            if tx_type == TxType.INCOME:
                sums["income"] = Decimal(total)
            elif tx_type in (TxType.EXPENSE, TxType.FIXED_EXPENSE):
                sums["expense"] += Decimal(total)
            elif tx_type == TxType.TRANSFER:
                sums["transfer"] = Decimal(total)
        return sums

    async def save(self, tx: Transaction) -> None:
        self.db.add(tx)
        await self.db.flush()

    async def daily_sums_for_month(
        self, household_id: UUID, year: int, month: int,
        account_id: UUID | None = None,
    ) -> list[tuple[date, str, Decimal, int]]:
        """일별 (tx_date, tx_type) 별 SUM(amount) + COUNT — 달력 뷰용"""
        first_day = date(year, month, 1)
        last_day = date(year, month, monthrange(year, month)[1])
        conds = [
            Transaction.household_id == household_id,
            Transaction.data_stat_cd == DataStatus.ACTIVE,
            Transaction.tx_date >= first_day,
            Transaction.tx_date <= last_day,
            # 평가조정은 수입/지출/이체가 아니라 달력 일별 집계·카운트에서 제외.
            Transaction.tx_type != TxType.VALUATION,
        ]
        if account_id is not None:
            conds.append(_account_involved(account_id))
        result = await self.db.execute(
            select(
                Transaction.tx_date,
                Transaction.tx_type,
                func.coalesce(func.sum(Transaction.amount), 0).label("total"),
                func.count(Transaction.id).label("cnt"),
            )
            .where(and_(*conds))
            .group_by(Transaction.tx_date, Transaction.tx_type)
            .order_by(Transaction.tx_date.asc())
        )
        return [
            (row.tx_date, row.tx_type, Decimal(row.total), int(row.cnt))
            for row in result.all()
        ]

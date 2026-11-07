from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import and_, case, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums.data_status import DataStatus
from app.domain.portfolio.enum import Market, PortfolioTxType
from app.domain.portfolio.model import (
    PortfolioItem,
    PortfolioTransaction,
    PortfolioValueHistory,
)


def _settlement_expr():
    """정산금액 SQL 식 — 매수는 거래금액+수수료, 매도는 거래금액−수수료.

    계좌를 실제로 드나든 금액이라 매매현금 합산의 기준이 된다.
    """
    gross = PortfolioTransaction.quantity * PortfolioTransaction.price
    return case(
        (PortfolioTransaction.pt_type == PortfolioTxType.BUY,
         gross + PortfolioTransaction.fee),
        else_=gross - PortfolioTransaction.fee,
    )


class PortfolioItemRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def find_by_id(self, item_id: UUID) -> PortfolioItem | None:
        result = await self.db.execute(
            select(PortfolioItem).where(
                and_(
                    PortfolioItem.id == item_id,
                    PortfolioItem.data_stat_cd == DataStatus.ACTIVE,
                )
            )
        )
        return result.scalar_one_or_none()

    async def find_by_id_for_update(
        self, item_id: UUID, *, any_status: bool = False,
    ) -> PortfolioItem | None:
        """매매 경로의 직렬화 지점 — SELECT … FOR UPDATE.

        같은 종목 요청은 여기서 줄을 서고, 뒤 요청은 앞 요청이 커밋한 수량·상태를 본다.
        populate_existing: 세션에 먼저 읽힌 옛 값이 있어도 잠근 시점 값으로 덮는다.
        autoflush 를 끈 채(no_autoflush) 종목을 고치고 부르면 그 변경이 덮이니, 호출은 첫 쓰기 전에."""
        stmt = select(PortfolioItem).where(PortfolioItem.id == item_id)
        if not any_status:
            stmt = stmt.where(PortfolioItem.data_stat_cd == DataStatus.ACTIVE)
        result = await self.db.execute(
            stmt.with_for_update().execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def find_by_id_any_status(self, item_id: UUID) -> PortfolioItem | None:
        """삭제 상태 무관 조회 — 전량매도로 죽은 종목의 거래 수정/삭제 재계산용."""
        result = await self.db.execute(
            select(PortfolioItem).where(PortfolioItem.id == item_id)
        )
        return result.scalar_one_or_none()

    async def find_active_by_account_ids(
        self, account_ids: list[UUID],
    ) -> dict[UUID, list[PortfolioItem]]:
        """여러 통장 보유종목 배치 — 계좌목록 잔액 N+1 제거용."""
        base: dict[UUID, list[PortfolioItem]] = {aid: [] for aid in account_ids}
        if not account_ids:
            return base
        result = await self.db.execute(
            select(PortfolioItem).where(
                and_(
                    PortfolioItem.account_id.in_(account_ids),
                    PortfolioItem.data_stat_cd == DataStatus.ACTIVE,
                )
            )
        )
        for item in result.scalars().all():
            base[item.account_id].append(item)
        return base

    async def find_active_by_household_id(self, household_id: UUID) -> list[PortfolioItem]:
        result = await self.db.execute(
            select(PortfolioItem)
            .where(
                and_(
                    PortfolioItem.household_id == household_id,
                    PortfolioItem.data_stat_cd == DataStatus.ACTIVE,
                )
            )
            .order_by(PortfolioItem.frst_reg_dt.asc())
        )
        return list(result.scalars().all())

    async def count_active_by_household_id(self, household_id: UUID) -> int:
        """settings overview 종목 카운트용"""
        result = await self.db.execute(
            select(func.count(PortfolioItem.id)).where(
                and_(
                    PortfolioItem.household_id == household_id,
                    PortfolioItem.data_stat_cd == DataStatus.ACTIVE,
                )
            )
        )
        return result.scalar() or 0

    async def count_active_by_account_id(self, account_id: UUID) -> int:
        """통장 삭제 가드용 — 이 통장에 연결된 활성 종목 수"""
        result = await self.db.execute(
            select(func.count(PortfolioItem.id)).where(
                and_(
                    PortfolioItem.account_id == account_id,
                    PortfolioItem.data_stat_cd == DataStatus.ACTIVE,
                )
            )
        )
        return result.scalar() or 0

    async def find_active_by_account_id(self, account_id: UUID) -> list[PortfolioItem]:
        result = await self.db.execute(
            select(PortfolioItem)
            .where(
                and_(
                    PortfolioItem.account_id == account_id,
                    PortfolioItem.data_stat_cd == DataStatus.ACTIVE,
                )
            )
            .order_by(PortfolioItem.frst_reg_dt.asc())
        )
        return list(result.scalars().all())

    async def find_active_by_account_market_code(
        self, account_id: UUID, market: str, code: str,
    ) -> PortfolioItem | None:
        """누적 매수 시 같은 종목 있는지 확인 — (account, market, code) 기준"""
        result = await self.db.execute(
            select(PortfolioItem).where(
                and_(
                    PortfolioItem.account_id == account_id,
                    PortfolioItem.market == market,
                    PortfolioItem.code == code,
                    PortfolioItem.data_stat_cd == DataStatus.ACTIVE,
                )
            )
        )
        return result.scalar_one_or_none()

    async def find_by_ids_including_deleted(self, item_ids: list[UUID]) -> list[PortfolioItem]:
        """삭제된 종목까지 포함 fetch (value-history 응답에 ticker 표시용)"""
        if not item_ids:
            return []
        result = await self.db.execute(
            select(PortfolioItem).where(PortfolioItem.id.in_(item_ids))
        )
        return list(result.scalars().all())

    async def sum_valuation_by_account(self, account_id: UUID) -> Decimal:
        """qty * current_price 합산 (_calc_balance 용)"""
        result = await self.db.execute(
            select(func.coalesce(func.sum(PortfolioItem.quantity * PortfolioItem.current_price), 0)).where(
                and_(
                    PortfolioItem.account_id == account_id,
                    PortfolioItem.data_stat_cd == DataStatus.ACTIVE,
                )
            )
        )
        return Decimal(result.scalar() or 0)

    async def save(self, item: PortfolioItem) -> None:
        self.db.add(item)
        await self.db.flush()

    async def find_active_distinct_code_market_by_markets(
        self, markets: list[Market],
    ) -> list[tuple[str, Market]]:
        """전 가계부 통틀어 (code, market) DISTINCT 추출 — 시세 갱신 야후 호출 단위.
        is_archived 종목은 제외 (보관된 종목 시세 갱신 불필요)."""
        result = await self.db.execute(
            select(PortfolioItem.code, PortfolioItem.market)
            .where(
                and_(
                    PortfolioItem.market.in_([m.value for m in markets]),
                    PortfolioItem.data_stat_cd == DataStatus.ACTIVE,
                    PortfolioItem.is_archived.is_(False),
                )
            )
            .distinct()
        )
        return [(code, Market(market)) for code, market in result.all()]

    async def find_active_distinct_code_market_by_household_and_markets(
        self, household_id: UUID, markets: list[Market],
    ) -> list[tuple[str, Market]]:
        """특정 가계부가 보유한 (code, market) DISTINCT — 수동 시세 갱신 야후 호출 단위.
        가격 자체는 시장 공통이라 bulk update 는 전 가계부에 적용되지만, fetch 대상은
        이 가계부 보유 종목으로 한정해 야후 호출을 최소화한다."""
        result = await self.db.execute(
            select(PortfolioItem.code, PortfolioItem.market)
            .where(
                and_(
                    PortfolioItem.household_id == household_id,
                    PortfolioItem.market.in_([m.value for m in markets]),
                    PortfolioItem.data_stat_cd == DataStatus.ACTIVE,
                    PortfolioItem.is_archived.is_(False),
                )
            )
            .distinct()
        )
        return [(code, Market(market)) for code, market in result.all()]

    async def find_active_other_prices(
        self, household_id: UUID | None = None,
    ) -> list[dict]:
        """OTHER 시장(금 등) 활성 종목의 (item_id, current_price) — 시세이력 박제용.

        야후 미지원이라 backfill 불가 → 박제 시점 수동가를 그 달 시세로 남기는 데 쓴다.
        수동가는 가계부·종목마다 달라 code 가 아니라 종목 id 로 돌려준다(code 는 비어 있기도 함).
        household_id 주면 그 가계부만, 없으면 전체(시장 공통 수집).
        """
        conds = [
            PortfolioItem.market == Market.OTHER.value,
            PortfolioItem.data_stat_cd == DataStatus.ACTIVE,
            PortfolioItem.is_archived.is_(False),
        ]
        if household_id is not None:
            conds.append(PortfolioItem.household_id == household_id)
        result = await self.db.execute(
            select(PortfolioItem.id, PortfolioItem.current_price).where(and_(*conds))
        )
        return [
            {"item_id": item_id, "current_price": price} for item_id, price in result.all()
        ]

    async def bulk_update_current_price_by_code_market(
        self, prices: dict[tuple[str, Market], Decimal],
    ) -> int:
        """매치되는 모든 active row 의 current_price 일괄 update.
        반환: 총 영향받은 row 수."""
        if not prices:
            return 0
        total = 0
        for (code, market), price in prices.items():
            result = await self.db.execute(
                update(PortfolioItem)
                .where(
                    and_(
                        PortfolioItem.code == code,
                        PortfolioItem.market == market.value,
                        PortfolioItem.data_stat_cd == DataStatus.ACTIVE,
                    )
                )
                .values(current_price=price)
            )
            total += result.rowcount or 0
        return total


class PortfolioTransactionRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def has_active_for_account(self, account_id: UUID) -> bool:
        """이 통장에 활성 매매내역이 있나 — 통장 타입 변경 가드용(전량매도 종목의 이력 포함)."""
        result = await self.db.execute(
            select(PortfolioTransaction.id)
            .where(
                and_(
                    PortfolioTransaction.account_id == account_id,
                    PortfolioTransaction.data_stat_cd == DataStatus.ACTIVE,
                )
            )
            .limit(1)
        )
        return result.first() is not None

    async def find_by_id(self, tx_id: UUID) -> PortfolioTransaction | None:
        result = await self.db.execute(
            select(PortfolioTransaction).where(
                and_(
                    PortfolioTransaction.id == tx_id,
                    PortfolioTransaction.data_stat_cd == DataStatus.ACTIVE,
                )
            )
        )
        return result.scalar_one_or_none()

    async def find_active_by_item_id(self, item_id: UUID) -> list[PortfolioTransaction]:
        """종목의 모든 활성 거래 — 재계산용"""
        result = await self.db.execute(
            select(PortfolioTransaction)
            .where(
                and_(
                    PortfolioTransaction.portfolio_item_id == item_id,
                    PortfolioTransaction.data_stat_cd == DataStatus.ACTIVE,
                )
            )
            .order_by(PortfolioTransaction.tx_date.asc(), PortfolioTransaction.frst_reg_dt.asc())
        )
        return list(result.scalars().all())

    async def find_active_by_household_id(self, household_id: UUID) -> list[PortfolioTransaction]:
        result = await self.db.execute(
            select(PortfolioTransaction)
            .where(
                and_(
                    PortfolioTransaction.household_id == household_id,
                    PortfolioTransaction.data_stat_cd == DataStatus.ACTIVE,
                )
            )
            .order_by(PortfolioTransaction.tx_date.desc(), PortfolioTransaction.frst_reg_dt.desc())
        )
        return list(result.scalars().all())

    async def find_active_by_account_id(self, account_id: UUID) -> list[PortfolioTransaction]:
        result = await self.db.execute(
            select(PortfolioTransaction)
            .where(
                and_(
                    PortfolioTransaction.account_id == account_id,
                    PortfolioTransaction.data_stat_cd == DataStatus.ACTIVE,
                )
            )
            .order_by(PortfolioTransaction.tx_date.desc(), PortfolioTransaction.frst_reg_dt.desc())
        )
        return list(result.scalars().all())

    async def sum_for_account(
        self, account_id: UUID, to_date: date | None = None,
    ) -> dict[str, Decimal]:
        """통장별 BUY/SELL 정산금액 합산 (_calc_balance 용).

        실제 계좌를 드나든 금액이라 수수료가 포함된다 — 매수는 금액+수수료 출금,
        매도는 금액−수수료 입금. 호출부(`account/service.py`)의
        `cash = cash - buy + sell` 공식은 그대로 성립한다.

        to_date 를 주면 그 날짜까지의 누적(스냅샷 as-of 잔액용). 없으면 전체.
        """
        conds = [
            PortfolioTransaction.account_id == account_id,
            PortfolioTransaction.data_stat_cd == DataStatus.ACTIVE,
        ]
        if to_date is not None:
            conds.append(PortfolioTransaction.tx_date <= to_date)
        result = await self.db.execute(
            select(
                PortfolioTransaction.pt_type,
                func.coalesce(func.sum(_settlement_expr()), 0).label("total"),
            )
            .where(and_(*conds))
            .group_by(PortfolioTransaction.pt_type)
        )
        sums = {"buy": Decimal("0"), "sell": Decimal("0")}
        for pt_type, total in result.all():
            if pt_type == PortfolioTxType.BUY:
                sums["buy"] = Decimal(total)
            elif pt_type == PortfolioTxType.SELL:
                sums["sell"] = Decimal(total)
        return sums

    async def sum_for_accounts(
        self, account_ids: list[UUID],
    ) -> dict[UUID, dict[str, Decimal]]:
        """여러 통장 BUY/SELL 정산금액 합산 배치 — 계좌목록 잔액 N+1 제거용."""
        base = {
            aid: {"buy": Decimal("0"), "sell": Decimal("0")} for aid in account_ids
        }
        if not account_ids:
            return base
        result = await self.db.execute(
            select(
                PortfolioTransaction.account_id,
                PortfolioTransaction.pt_type,
                func.coalesce(func.sum(_settlement_expr()), 0).label("total"),
            )
            .where(
                and_(
                    PortfolioTransaction.account_id.in_(account_ids),
                    PortfolioTransaction.data_stat_cd == DataStatus.ACTIVE,
                )
            )
            .group_by(
                PortfolioTransaction.account_id, PortfolioTransaction.pt_type,
            )
        )
        for account_id, pt_type, total in result.all():
            if pt_type == PortfolioTxType.BUY:
                base[account_id]["buy"] = Decimal(total)
            elif pt_type == PortfolioTxType.SELL:
                base[account_id]["sell"] = Decimal(total)
        return base

    async def save(self, tx: PortfolioTransaction) -> None:
        self.db.add(tx)
        await self.db.flush()

    async def soft_delete_by_account_id(self, account_id: UUID) -> int:
        """이 통장의 종목 매매이력 soft-delete — 통장 cascade용(전량매도 후 잔존분)."""
        stmt = (
            update(PortfolioTransaction)
            .where(
                and_(
                    PortfolioTransaction.account_id == account_id,
                    PortfolioTransaction.data_stat_cd == DataStatus.ACTIVE,
                )
            )
            .values(data_stat_cd=DataStatus.DELETED)
            .execution_options(synchronize_session=False)
        )
        result = await self.db.execute(stmt)
        return result.rowcount or 0

    async def find_sell_txs_by_item(
        self, item_id: UUID, from_date: date, to_date: date,
    ) -> list[PortfolioTransaction]:
        """종목의 기간 내 매도 거래 — 매매손익 집계용. 최신순."""
        result = await self.db.execute(
            select(PortfolioTransaction)
            .where(
                and_(
                    PortfolioTransaction.portfolio_item_id == item_id,
                    PortfolioTransaction.pt_type == PortfolioTxType.SELL,
                    PortfolioTransaction.data_stat_cd == DataStatus.ACTIVE,
                    PortfolioTransaction.tx_date >= from_date,
                    PortfolioTransaction.tx_date <= to_date,
                )
            )
            .order_by(
                PortfolioTransaction.tx_date.desc(),
                PortfolioTransaction.frst_reg_dt.desc(),
            )
        )
        return list(result.scalars().all())

    async def find_sell_txs_by_account(
        self, account_id: UUID, household_id: UUID, from_date: date, to_date: date,
    ) -> list[PortfolioTransaction]:
        """계좌의 기간 내 매도 거래 — 계좌 누적 매매손익 집계용. 최신순.

        전량매도로 종목(item)이 soft delete 돼도 매도 거래는 ACTIVE 로 남아 포함된다.
        household_id 를 함께 걸어 소유권을 보장(남의 계좌 id 면 빈 결과).
        """
        result = await self.db.execute(
            select(PortfolioTransaction)
            .where(
                and_(
                    PortfolioTransaction.account_id == account_id,
                    PortfolioTransaction.household_id == household_id,
                    PortfolioTransaction.pt_type == PortfolioTxType.SELL,
                    PortfolioTransaction.data_stat_cd == DataStatus.ACTIVE,
                    PortfolioTransaction.tx_date >= from_date,
                    PortfolioTransaction.tx_date <= to_date,
                )
            )
            .order_by(
                PortfolioTransaction.tx_date.desc(),
                PortfolioTransaction.frst_reg_dt.desc(),
            )
        )
        return list(result.scalars().all())

    async def earliest_sell_date_by_account(
        self, account_id: UUID, household_id: UUID,
    ) -> date | None:
        """계좌의 가장 이른 매도일 — 매매손익 기본 조회기간 clamp 용.

        매도가 없으면 None. 기본 '최근 1년' 이 첫 매도를 놓치지 않게,
        from 미지정 시 이 날짜까지 범위를 확장한다.
        """
        result = await self.db.execute(
            select(func.min(PortfolioTransaction.tx_date)).where(
                and_(
                    PortfolioTransaction.account_id == account_id,
                    PortfolioTransaction.household_id == household_id,
                    PortfolioTransaction.pt_type == PortfolioTxType.SELL,
                    PortfolioTransaction.data_stat_cd == DataStatus.ACTIVE,
                )
            )
        )
        return result.scalar_one_or_none()

    async def earliest_sell_date_by_item(self, item_id: UUID) -> date | None:
        """종목의 가장 이른 매도일 — 매매손익 기본 조회기간 clamp 용. 없으면 None."""
        result = await self.db.execute(
            select(func.min(PortfolioTransaction.tx_date)).where(
                and_(
                    PortfolioTransaction.portfolio_item_id == item_id,
                    PortfolioTransaction.pt_type == PortfolioTxType.SELL,
                    PortfolioTransaction.data_stat_cd == DataStatus.ACTIVE,
                )
            )
        )
        return result.scalar_one_or_none()

    async def asof_holdings_by_account(
        self, account_id: UUID, as_of: date,
    ) -> list[dict]:
        """as_of(그 달 말일) 시점 보유 종목 재구성 — 스냅샷 시점 평가용.

        종목별로 as_of 까지의 BUY-SELL 순수량과 매수 평단(원가)을 집계한다.
        과거 시가 이력이 없어 평가액은 원가(수량×평단) 기준. 순수량 0 이하(전량매도)는 제외.
        반환: [{item_id, name, code, market, quantity, avg_cost, cost}] (cost = 수량×평단).
        """
        qty_signed = func.sum(
            case(
                (PortfolioTransaction.pt_type == PortfolioTxType.BUY,
                 PortfolioTransaction.quantity),
                (PortfolioTransaction.pt_type == PortfolioTxType.SELL,
                 -PortfolioTransaction.quantity),
                else_=0,
            )
        )
        buy_qty = func.sum(
            case(
                (PortfolioTransaction.pt_type == PortfolioTxType.BUY,
                 PortfolioTransaction.quantity),
                else_=0,
            )
        )
        # 매수원가에 수수료 포함 — replay(_recompute_realized_pnl)가 평단에 fee 를
        # 가산하므로 여기서 빼면 박제 평단과 종목 평단이 갈린다.
        buy_amt = func.sum(
            case(
                (PortfolioTransaction.pt_type == PortfolioTxType.BUY,
                 PortfolioTransaction.quantity * PortfolioTransaction.price
                 + PortfolioTransaction.fee),
                else_=0,
            )
        )
        result = await self.db.execute(
            select(
                PortfolioTransaction.portfolio_item_id.label("item_id"),
                func.max(PortfolioTransaction.name).label("name"),
                func.max(PortfolioTransaction.code).label("code"),
                func.max(PortfolioTransaction.market).label("market"),
                qty_signed.label("qty"),
                buy_qty.label("buy_qty"),
                buy_amt.label("buy_amt"),
            )
            .where(
                and_(
                    PortfolioTransaction.account_id == account_id,
                    PortfolioTransaction.data_stat_cd == DataStatus.ACTIVE,
                    PortfolioTransaction.tx_date <= as_of,
                    PortfolioTransaction.portfolio_item_id.isnot(None),
                )
            )
            .group_by(PortfolioTransaction.portfolio_item_id)
        )

        holdings: list[dict] = []
        for row in result.all():
            qty = Decimal(row.qty or 0)
            if qty <= 0:
                continue  # 전량매도 — 그 시점 보유 아님
            bq = Decimal(row.buy_qty or 0)
            ba = Decimal(row.buy_amt or 0)
            avg_cost = (ba / bq) if bq > 0 else Decimal("0")
            holdings.append({
                "item_id": row.item_id,
                "name": row.name,
                "code": row.code,
                "market": row.market,
                "quantity": qty,
                "avg_cost": avg_cost.quantize(Decimal("0.01")),
                "cost": (qty * avg_cost).quantize(Decimal("0.01")),
            })
        return holdings

    @staticmethod
    def _cursor_after(cursor: str | None):
        """tx_date DESC, id DESC 정렬 기준 cursor 조건 — transaction 패턴 그대로"""
        if not cursor:
            return None
        try:
            date_str, id_str = cursor.split("|", 1)
            cur_date = date.fromisoformat(date_str)
            cur_id = UUID(id_str)
        except (ValueError, AttributeError):
            return None
        return or_(
            PortfolioTransaction.tx_date < cur_date,
            and_(
                PortfolioTransaction.tx_date == cur_date,
                PortfolioTransaction.id < cur_id,
            ),
        )

    async def list_active_by_item_id_cursor(
        self, item_id: UUID, cursor: str | None, limit: int,
    ) -> list[PortfolioTransaction]:
        """종목 단건 거래 내역 — 무한 스크롤. limit+1 로 has_next 판정."""
        conds = [
            PortfolioTransaction.portfolio_item_id == item_id,
            PortfolioTransaction.data_stat_cd == DataStatus.ACTIVE,
        ]
        cursor_cond = self._cursor_after(cursor)
        if cursor_cond is not None:
            conds.append(cursor_cond)
        result = await self.db.execute(
            select(PortfolioTransaction)
            .where(and_(*conds))
            .order_by(
                PortfolioTransaction.tx_date.desc(),
                PortfolioTransaction.id.desc(),
            )
            .limit(limit + 1)
        )
        return list(result.scalars().all())


class PortfolioValueHistoryRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def save_all(self, histories: list[PortfolioValueHistory]) -> None:
        if not histories:
            return
        self.db.add_all(histories)
        await self.db.flush()

    async def soft_delete_by_account_id(self, account_id: UUID) -> int:
        """이 통장의 종목 가치 박제 soft-delete — 통장 cascade용."""
        stmt = (
            update(PortfolioValueHistory)
            .where(
                and_(
                    PortfolioValueHistory.account_id == account_id,
                    PortfolioValueHistory.data_stat_cd == DataStatus.ACTIVE,
                )
            )
            .values(data_stat_cd=DataStatus.DELETED)
            .execution_options(synchronize_session=False)
        )
        result = await self.db.execute(stmt)
        return result.rowcount or 0

    async def delete_for_household_month(
        self, household_id: UUID, snapshot_date: date,
    ) -> None:
        """그 가계부의 해당 월 종목 박제 hard delete (upsert 재생성용)."""
        stmt = delete(PortfolioValueHistory).where(
            and_(
                PortfolioValueHistory.household_id == household_id,
                PortfolioValueHistory.snapshot_date == snapshot_date,
            )
        )
        await self.db.execute(stmt)

    async def has_active_for_month(self, household_id: UUID, month_date: date) -> bool:
        """이번 달에 종목 박제됐는지 (account_snapshot 과 동일 패턴)"""
        result = await self.db.execute(
            select(func.count(PortfolioValueHistory.id)).where(
                and_(
                    PortfolioValueHistory.household_id == household_id,
                    PortfolioValueHistory.snapshot_date == month_date,
                    PortfolioValueHistory.data_stat_cd == DataStatus.ACTIVE,
                )
            )
        )
        return (result.scalar_one() or 0) > 0

    async def find_by_account_and_range(
        self, account_id: UUID, from_date: date, to_date: date,
    ) -> list[PortfolioValueHistory]:
        result = await self.db.execute(
            select(PortfolioValueHistory)
            .where(
                and_(
                    PortfolioValueHistory.account_id == account_id,
                    PortfolioValueHistory.snapshot_date >= from_date,
                    PortfolioValueHistory.snapshot_date <= to_date,
                    PortfolioValueHistory.data_stat_cd == DataStatus.ACTIVE,
                )
            )
            .order_by(
                PortfolioValueHistory.portfolio_item_id.asc(),
                PortfolioValueHistory.snapshot_date.asc(),
            )
        )
        return list(result.scalars().all())

    async def find_by_household_and_range(
        self, household_id: UUID, from_date: date, to_date: date,
    ) -> list[PortfolioValueHistory]:
        """가계부 전체 종목 박제 — 월별 자산군 배분추이 집계용."""
        result = await self.db.execute(
            select(PortfolioValueHistory)
            .where(
                and_(
                    PortfolioValueHistory.household_id == household_id,
                    PortfolioValueHistory.snapshot_date >= from_date,
                    PortfolioValueHistory.snapshot_date <= to_date,
                    PortfolioValueHistory.data_stat_cd == DataStatus.ACTIVE,
                )
            )
            .order_by(PortfolioValueHistory.snapshot_date.asc())
        )
        return list(result.scalars().all())

    async def find_by_item_and_range(
        self, item_id: UUID, from_date: date, to_date: date,
    ) -> list[PortfolioValueHistory]:
        result = await self.db.execute(
            select(PortfolioValueHistory)
            .where(
                and_(
                    PortfolioValueHistory.portfolio_item_id == item_id,
                    PortfolioValueHistory.snapshot_date >= from_date,
                    PortfolioValueHistory.snapshot_date <= to_date,
                    PortfolioValueHistory.data_stat_cd == DataStatus.ACTIVE,
                )
            )
            .order_by(PortfolioValueHistory.snapshot_date.asc())
        )
        return list(result.scalars().all())

from datetime import date, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import and_, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums.data_status import DataStatus
from app.domain.market_price.model import MarketPriceHistory


class MarketPriceHistoryRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def upsert_prices(self, rows: list[dict]) -> int:
        """(code, market, price_date) upsert — 충돌 시 price 갱신.

        rows: [{"code", "market", "price_date", "price"}]. 시세 재수집이 반복돼도
        같은 달은 최신값으로 덮인다. core insert 라 BaseEntity 기본값(id/시각/상태)을
        여기서 직접 채운다.
        """
        if not rows:
            return 0
        now = datetime.now()
        values = [
            {
                "id": uuid4(),
                "code": r["code"],
                "market": r["market"],
                "price_date": r["price_date"],
                "price": r["price"],
                "frst_reg_dt": now,
                "last_mdfcn_dt": now,
                "data_stat_cd": DataStatus.ACTIVE,
            }
            # 행 lock 을 늘 같은 순서로 잡게 정렬 — 월 잡과 수동 박제가 반대 순서로 잡으면 데드락
            for r in sorted(rows, key=lambda r: (r["market"], r["code"], r["price_date"]))
        ]
        stmt = pg_insert(MarketPriceHistory).values(values)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_market_price_code_market_date",
            set_={"price": stmt.excluded.price, "last_mdfcn_dt": now},
        )
        result = await self.db.execute(stmt)
        return result.rowcount or 0

    async def find_prices_for_month(
        self, pairs: list[tuple[str, str]], month: date,
    ) -> dict[tuple[str, str], Decimal]:
        """(code, market) 목록의 그 달(price_date) 시가 배치 조회 — 박제 N+1 방지.

        시가 이력 없는 종목은 결과 dict 에 키 자체가 없다(호출자가 원가 fallback).
        """
        if not pairs:
            return {}
        result = await self.db.execute(
            select(
                MarketPriceHistory.code,
                MarketPriceHistory.market,
                MarketPriceHistory.price,
            ).where(
                and_(
                    tuple_(MarketPriceHistory.code, MarketPriceHistory.market).in_(
                        list(pairs)
                    ),
                    MarketPriceHistory.price_date == month,
                    MarketPriceHistory.data_stat_cd == DataStatus.ACTIVE,
                )
            )
        )
        return {
            (code, market): Decimal(price) for code, market, price in result.all()
        }

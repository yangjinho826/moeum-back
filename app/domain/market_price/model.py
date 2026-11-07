import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import Date, Index, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.model import BaseEntity


class MarketPriceHistory(BaseEntity):
    """종목 월별 시세 이력 — market_price_history 테이블.

    자산 스냅샷 박제 시 그 달 시가로 평가하기 위한 시세 캐시.
    (code, market, price_date) 유일 — price_date 는 월 1일 정규화(snapshot_date 컨벤션).
    야후 종목은 월봉 종가, OTHER(금 등) 는 박제 시점 current_price 를 그 달로 박는다.
    """

    __tablename__ = "market_price_history"
    __table_args__ = (
        UniqueConstraint(
            "code", "market", "price_date", name="uq_market_price_code_market_date",
        ),
        Index("ix_market_price_lookup", "code", "market", "price_date"),
    )

    # 야후 종목은 티커, OTHER 는 종목 id(UUID 36자) — 가계부마다 수동가가 달라 종목 단위 키.
    # 길이는 portfolio_items.code 와 같게(긴 티커도 upsert 에서 잘리지 않게)
    code: Mapped[str] = mapped_column(String(50), nullable=False)
    market: Mapped[str] = mapped_column(String(20), nullable=False)
    price_date: Mapped[date] = mapped_column(Date, nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)

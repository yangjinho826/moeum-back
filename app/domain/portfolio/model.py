import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import Boolean, Date, Index, Numeric, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.model import BaseEntity


class PortfolioItem(BaseEntity):
    """보유 종목 — portfolio_items 테이블"""

    __tablename__ = "portfolio_items"
    __table_args__ = (
        Index("idx_portfolio_household", "household_id"),
        Index("idx_portfolio_account", "account_id"),
    )

    household_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    code: Mapped[str] = mapped_column(String(50), nullable=False)
    market: Mapped[str] = mapped_column(String(20), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(15, 4), nullable=False)
    avg_price: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    current_price: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    is_archived: Mapped[bool] = mapped_column(Boolean, nullable=False)


class PortfolioTransaction(BaseEntity):
    """자산 거래 이력 — portfolio_transactions 테이블"""

    __tablename__ = "portfolio_transactions"
    __table_args__ = (
        Index("idx_pt_household_date", "household_id", text("tx_date DESC")),
        Index("idx_pt_account", "account_id"),
        Index("idx_pt_item", "portfolio_item_id"),
    )

    household_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    portfolio_item_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    code: Mapped[str] = mapped_column(String(50), nullable=False)
    market: Mapped[str] = mapped_column(String(20), nullable=False)
    pt_type: Mapped[str] = mapped_column(String(10), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(15, 4), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    # 매매 수수료 — 매수는 원가(평단)에 가산, 매도는 실현손익에서 차감.
    # 기존 거래는 0 이라 도입 전 계산과 동일하다.
    fee: Mapped[Decimal] = mapped_column(
        Numeric(15, 2), nullable=False, server_default=text("0"),
    )
    tx_date: Mapped[date] = mapped_column(Date, nullable=False)
    memo: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 매도 실현손익 — SELL 만 채워짐. 매도시점 이동평균 평단 기준 건별 박제.
    # BUY 또는 R2 이전 SELL 은 NULL (매도시점 평단 복원 불가).
    realized_pnl: Mapped[Decimal | None] = mapped_column(Numeric(15, 2), nullable=True)
    realized_cost_basis: Mapped[Decimal | None] = mapped_column(
        Numeric(15, 2), nullable=True,
    )


class PortfolioValueHistory(BaseEntity):
    """종목별 월별 평가액 박제 — portfolio_value_history 테이블"""

    __tablename__ = "portfolio_value_history"
    __table_args__ = (
        Index("idx_pvh_household", "household_id"),
        Index("idx_pvh_account", "account_id"),
        Index("idx_pvh_item_date", "portfolio_item_id", text("snapshot_date DESC")),
        Index(
            "uq_pvh_item_date_active", "portfolio_item_id", "snapshot_date",
            unique=True, postgresql_where=text("data_stat_cd = '50'"),
        ),
    )

    household_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    portfolio_item_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(15, 4), nullable=False)
    avg_price: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    current_price: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    cost: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    valuation: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)

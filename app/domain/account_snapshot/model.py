import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import Date, Index, Numeric, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.model import BaseEntity


class AccountSnapshot(BaseEntity):
    """월말 통장 잔액 스냅샷 — account_snapshots 테이블"""

    __tablename__ = "account_snapshots"
    __table_args__ = (
        Index("idx_snapshots_account_date", "account_id", text("snapshot_date DESC")),
        Index("idx_snapshots_date", text("snapshot_date DESC")),
        # 활성 (통장, 월) 당 1개 — 가계부 lock 을 우회한 중복 박제의 안전망(yearly 합계 2배 방지)
        Index(
            "uq_snapshots_account_date_active", "account_id", "snapshot_date",
            unique=True, postgresql_where=text("data_stat_cd = '50'"),
        ),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    balance: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    # 그 달 흐름 캐시 — 매번 transactions 합산 안 하려고 박제 시점에 같이 박음.
    monthly_income: Mapped[Decimal] = mapped_column(
        Numeric(15, 2), nullable=False, server_default=text("0"),
    )
    monthly_expense: Mapped[Decimal] = mapped_column(
        Numeric(15, 2), nullable=False, server_default=text("0"),
    )
    monthly_fixed_expense: Mapped[Decimal] = mapped_column(
        Numeric(15, 2), nullable=False, server_default=text("0"),
    )

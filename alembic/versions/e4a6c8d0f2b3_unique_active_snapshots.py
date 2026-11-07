"""박제 행 부분 unique — 활성 (통장, 월)·(종목, 월) 당 1개

Revision ID: e4a6c8d0f2b3
Revises: d3f5b7c9e1a2
Create Date: 2026-11-07 22:31:00.000000

앱은 가계부 advisory lock 으로 박제를 직렬화하고, 이 인덱스는 그걸 우회한 중복을 DB 에서 막는
안전망이다. soft delete 된 행(통장 삭제 cascade)은 같은 달에 남아 있을 수 있어 활성만 건다.
additive DDL — 직전 이미지는 중복을 만들지 않는 한 그대로 돈다.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e4a6c8d0f2b3"
down_revision: Union[str, Sequence[str], None] = "d3f5b7c9e1a2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ACTIVE = "50"


def upgrade() -> None:
    op.create_index(
        "uq_snapshots_account_date_active", "account_snapshots",
        ["account_id", "snapshot_date"], unique=True,
        postgresql_where=sa.text(f"data_stat_cd = '{ACTIVE}'"),
    )
    op.create_index(
        "uq_pvh_item_date_active", "portfolio_value_history",
        ["portfolio_item_id", "snapshot_date"], unique=True,
        postgresql_where=sa.text(f"data_stat_cd = '{ACTIVE}'"),
    )


def downgrade() -> None:
    op.drop_index("uq_pvh_item_date_active", table_name="portfolio_value_history")
    op.drop_index("uq_snapshots_account_date_active", table_name="account_snapshots")

"""market_price_history.code VARCHAR(20) → VARCHAR(50)

Revision ID: b7d1e3f5a9c2
Revises: 4851000e13bd
Create Date: 2026-11-07 21:20:00.000000

OTHER(금 등) 종목 시세를 code 대신 종목 id(UUID 36자)로 저장한다. code 는 빈 문자열을
허용해서, code 키로는 여러 가계부의 수동가가 한 행을 다퉈 월 박제 upsert 가 통째로 실패했다.
길이는 portfolio_items.code(50)와 맞춘다. 확장(widen)만이라 구 이미지와 호환된다
(docs/migration-playbook.md 2장). 과거 달 값 이관은 다음 리비전(backfill)으로 분리.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b7d1e3f5a9c2"
down_revision: Union[str, Sequence[str], None] = "4851000e13bd"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "market_price_history",
        "code",
        existing_type=sa.String(length=20),
        type_=sa.String(length=50),
        existing_nullable=False,
    )


def downgrade() -> None:
    # 20자를 넘는 행(종목 id 키 OTHER 시세)은 축소 전에 지운다 — 남겨 두면 ALTER 가 실패한다.
    # 시세 캐시라 다음 박제 때 OTHER 는 원가 fallback, 야후 종목은 backfill 로 다시 채워진다.
    op.execute("DELETE FROM market_price_history WHERE length(code) > 20")
    op.alter_column(
        "market_price_history",
        "code",
        existing_type=sa.String(length=50),
        type_=sa.String(length=20),
        existing_nullable=False,
    )

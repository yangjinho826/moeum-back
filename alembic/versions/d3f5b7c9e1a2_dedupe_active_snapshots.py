"""중복 활성 박제 행 정리 — (통장, 월)·(종목, 월) 당 최신 1개만 남김

Revision ID: d3f5b7c9e1a2
Revises: c2e4a6b8d0f1
Create Date: 2026-11-07 22:30:00.000000

수동 박제가 lock 없이 "그 달 있나 → 넣기" 라 더블클릭·월 잡과 겹치면 같은 달이 통장마다 2벌
박제될 수 있었다(yearly 합계 2배). 다음 리비전의 부분 unique 인덱스가 생성되려면 기존 중복이
먼저 없어야 해서 DDL 과 분리해 둔다(docs/migration-playbook.md 2장 backfill 규칙).

박제는 거래에서 다시 계산되는 파생 데이터라 기존 replace 경로와 같이 hard delete 한다.
남기는 행 = 가장 최근 수정(last_mdfcn_dt), 동률이면 id 큰 쪽. downgrade 는 지운 행을 되살릴 수
없어 no-op.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "d3f5b7c9e1a2"
down_revision: Union[str, Sequence[str], None] = "c2e4a6b8d0f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ACTIVE = "50"  # DataStatus.ACTIVE — 마이그레이션은 앱 코드를 import 하지 않는다


def _dedupe(table: str, owner_col: str) -> None:
    op.execute(
        f"""
        DELETE FROM {table}
        WHERE id IN (
            SELECT id FROM (
                SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY {owner_col}, snapshot_date
                    ORDER BY last_mdfcn_dt DESC, id DESC
                ) AS rn
                FROM {table}
                WHERE data_stat_cd = '{ACTIVE}'
            ) ranked
            WHERE ranked.rn > 1
        )
        """
    )


def upgrade() -> None:
    _dedupe("account_snapshots", "account_id")
    _dedupe("portfolio_value_history", "portfolio_item_id")


def downgrade() -> None:
    pass

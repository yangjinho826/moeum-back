"""OTHER 시세를 종목 id 키로 backfill — 과거 박제에 실제로 쓴 가격

Revision ID: c2e4a6b8d0f1
Revises: b7d1e3f5a9c2
Create Date: 2026-11-07 21:40:00.000000

앞 리비전부터 OTHER(금 등) 시세는 (종목 id, OTHER, 월) 로만 읽는다. 옛 (code, OTHER) 행은
가계부끼리 공유돼 남의 수동가일 수 있어 읽지 않는데, 그러면 월 잡이 지난달을 다시 박제할 때
과거 값이 원가로 바뀐다. 그래서 각 종목이 그 달 박제에 실제로 쓴 가격
(portfolio_value_history.current_price)을 새 키로 옮겨 재박제가 기록값을 그대로 재현하게 한다.
시세가 없어 원가로 박제된 달은 current_price=평단이라 재박제 결과도 같다.

DDL 과 분리(docs/migration-playbook.md 2장 backfill 규칙). 이미 새 키가 있으면 건드리지 않는다.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "c2e4a6b8d0f1"
down_revision: Union[str, Sequence[str], None] = "b7d1e3f5a9c2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ACTIVE = "50"  # DataStatus.ACTIVE — 마이그레이션은 앱 코드를 import 하지 않는다


def upgrade() -> None:
    op.execute(
        f"""
        INSERT INTO market_price_history
            (id, code, market, price_date, price, frst_reg_dt, last_mdfcn_dt, data_stat_cd)
        SELECT DISTINCT ON (pvh.portfolio_item_id, pvh.snapshot_date)
               gen_random_uuid(), pvh.portfolio_item_id::text, 'OTHER',
               pvh.snapshot_date, pvh.current_price, now(), now(), '{ACTIVE}'
        FROM portfolio_value_history pvh
        JOIN portfolio_items pi ON pi.id = pvh.portfolio_item_id
        WHERE pi.market = 'OTHER' AND pvh.data_stat_cd = '{ACTIVE}'
        ORDER BY pvh.portfolio_item_id, pvh.snapshot_date, pvh.last_mdfcn_dt DESC
        ON CONFLICT ON CONSTRAINT uq_market_price_code_market_date DO NOTHING
        """
    )


def downgrade() -> None:
    # 넣은 행은 전부 36자 키라 앞 리비전 downgrade(length>20 삭제)가 함께 지운다.
    pass

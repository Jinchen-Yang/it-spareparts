"""fix WBDD-20251204-0011 wrong order_date (2025-10-27 -> 2025-12-04)

北京博瑞兴云 LEGACY-0104 对账（2026-08-24）发现：单号日期段（1204）与
领用单 issue_date（2025-12-04）都证明该单真实日期是 2025-12-04，导入时
order_date 被错录为 2025-10-27。错日期会扰动领用取价层"同 PN 取最新一单"
的挑选和 ±7 天采购窗口。带旧值守卫，只修这一行。

Revision ID: b3f8e1d6c4a2
Revises: a9e2f7c4d1b8
"""

from collections.abc import Sequence

from alembic import op


revision: str = "b3f8e1d6c4a2"
down_revision: str | None = "a9e2f7c4d1b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        UPDATE f_maintenance_order
        SET order_date = DATE '2025-12-04'
        WHERE order_no = 'WBDD-20251204-0011'
          AND order_date = DATE '2025-10-27'
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        UPDATE f_maintenance_order
        SET order_date = DATE '2025-10-27'
        WHERE order_no = 'WBDD-20251204-0011'
          AND order_date = DATE '2025-12-04'
        """
    )

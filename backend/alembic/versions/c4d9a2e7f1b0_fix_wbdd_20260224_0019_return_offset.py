"""fix WBDD-20260224-0019 return mis-offset (bad-part return treated as demand return)

客户 2026-08-24 微信确认（截图留档）：WBDD-20260224-0019（北京博瑞兴云
LEGACY-0104，ST8000NM0075 ×2）退回的 2 块是换下来的坏件——使用是真实的，
按领用走。氚云行上的"已返数量=2"是旧件返还，被导入映射当成"退货数量"冲抵
了需求净量与成本（单头"已返货数量"=0、同日确认领用 2、坏件返还模块无记录，
三项旁证与客户确认一致）。

修复：清掉该行退货数量并按单价重算金额（未税 1592.92×2、含税 1800.00×2）。
带旧值守卫；原始值保留在氚云源文件（sys_raw_file batch 171）可追溯。

Revision ID: c4d9a2e7f1b0
Revises: b3f8e1d6c4a2
"""

from collections.abc import Sequence

from alembic import op


revision: str = "c4d9a2e7f1b0"
down_revision: str | None = "b3f8e1d6c4a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        UPDATE f_maintenance_line
        SET return_qty = NULL,
            cost_amount_ex_tax = ROUND(qty * unit_cost_ex_tax, 2),
            cost_amount_inc_tax = ROUND(qty * unit_cost_inc_tax, 2),
            cost_amount = ROUND(qty * COALESCE(unit_cost, unit_cost_ex_tax), 2)
        WHERE raw_line_id = '6c508d87-2153-4a57-925b-9ec39fa60f47'
          AND qty = 2
          AND return_qty = 2
          AND unit_cost_ex_tax IS NOT NULL
          AND unit_cost_inc_tax IS NOT NULL
          AND cost_amount_ex_tax = 0
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        UPDATE f_maintenance_line
        SET return_qty = 2,
            cost_amount_ex_tax = 0,
            cost_amount_inc_tax = 0,
            cost_amount = 0
        WHERE raw_line_id = '6c508d87-2153-4a57-925b-9ec39fa60f47'
          AND qty = 2
          AND return_qty IS NULL
        """
    )

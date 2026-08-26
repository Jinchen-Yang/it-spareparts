"""合同总额含税化：回填 amount_inc_tax（客户 2026-08-26 拍板）

生产现状：531 条合同关系中 497 条只有未税 contract_amount（台账导入取
销售单 amount_ex_tax），amount_inc_tax 为空——展示层"含税总额"实际显示
未税数。回填口径：优先用销售单（contract_no=order_no）的
未税额×(1+税率)（税率 NULL 按 0，与看板 XSDD 回退同口径）；销售单
查不到的按全局 13% 归一（CONTEXT.md 双税口径）。contract_amount
（未税对账额）保持不动。

Revision ID: f7a3d2c8e6b1
Revises: e2f6a9c4b1d8
"""

from collections.abc import Sequence

from alembic import op


revision: str = "f7a3d2c8e6b1"
down_revision: str | None = "e2f6a9c4b1d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # 1) 销售单可查：未税 × (1+税率)，税率 NULL 按 0
    op.execute(
        """
        WITH rate AS (
            SELECT DISTINCT ON (order_no) order_no,
                   coalesce(tax_rate, 0) AS r,
                   amount_ex_tax
            FROM f_sales_order
            WHERE order_no IN (
                SELECT contract_no FROM maintenance_project_contract
                WHERE amount_inc_tax IS NULL AND contract_amount IS NOT NULL
            )
            ORDER BY order_no, id DESC
        )
        UPDATE maintenance_project_contract c
        SET amount_inc_tax = round(
                -- 优先用合同行的未税额（台账对账值），缺则用销售单未税额
                coalesce(c.contract_amount, rate.amount_ex_tax)
                * (1 + rate.r), 2)
        FROM rate
        WHERE rate.order_no = c.contract_no
          AND c.amount_inc_tax IS NULL
          AND coalesce(c.contract_amount, rate.amount_ex_tax) IS NOT NULL
        """
    )
    # 2) 销售单查不到：全局 13% 归一
    op.execute(
        """
        UPDATE maintenance_project_contract
        SET amount_inc_tax = round(contract_amount * 1.13, 2),
            updated_at = now()
        WHERE amount_inc_tax IS NULL
          AND contract_amount IS NOT NULL
        """
    )


def downgrade() -> None:
    # 回退会把本迁移写入的含税额清空；无法区分导入与回填来源，
    # 故 downgrade 不自动还原（展示层回退口径已独立于本列）。
    pass

"""领用成本新增需求单价格来源（2026-08-23）：cost_source CHECK 加 maint_demand。

Revision ID: c7d2e9a4b1f6
Revises: b1d4f6a8c2e7
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7d2e9a4b1f6"
down_revision: str | None = "b1d4f6a8c2e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint(
        "ck_maintenance_site_issue_line_cost_source",
        "maintenance_site_issue_line",
        type_="check",
    )
    op.create_check_constraint(
        "ck_maintenance_site_issue_line_cost_source",
        "maintenance_site_issue_line",
        "cost_source IS NULL OR cost_source IN "
        "('direct_purchase', 'purchase_window', 'sales_window', 'manual', 'maint_demand')",
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # 回收前先把 maint_demand 归 NULL（该来源只在 v2 算法下产生）
    op.execute(
        sa.text("UPDATE maintenance_site_issue_line SET cost_source = NULL, "
                "unit_cost = NULL, cost_amount = NULL, unit_cost_ex_tax = NULL, "
                "unit_cost_inc_tax = NULL, cost_amount_ex_tax = NULL, "
                "cost_amount_inc_tax = NULL WHERE cost_source = 'maint_demand'")
    )
    op.drop_constraint(
        "ck_maintenance_site_issue_line_cost_source",
        "maintenance_site_issue_line",
        type_="check",
    )
    op.create_check_constraint(
        "ck_maintenance_site_issue_line_cost_source",
        "maintenance_site_issue_line",
        "cost_source IS NULL OR cost_source IN "
        "('direct_purchase', 'purchase_window', 'sales_window', 'manual')",
    )

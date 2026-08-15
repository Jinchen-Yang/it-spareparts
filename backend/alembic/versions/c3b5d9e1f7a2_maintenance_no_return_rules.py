"""maintenance no-return rules: project default + line override + obligation exemption source

Revision ID: c3b5d9e1f7a2
Revises: b1e3f7d9c2a5
Create Date: 2026-08-15
"""
from alembic import op
import sqlalchemy as sa

revision = "c3b5d9e1f7a2"
down_revision = "b1e3f7d9c2a5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- 项目级不返还默认值 ----
    op.add_column(
        "maintenance_project",
        sa.Column(
            "no_return_default",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )

    # ---- 领用行级不返还覆盖（NULL = 继承项目默认） ----
    op.add_column(
        "maintenance_site_issue_line",
        sa.Column("no_return", sa.Boolean(), nullable=True),
    )

    # ---- 返还义务：豁免来源（审计） ----
    op.add_column(
        "maintenance_return_obligation",
        sa.Column("exemption_source", sa.String(length=32), nullable=True),
    )
    # 回填存量：required → none；exempt（历史硬盘规则）→ category_disk
    op.execute(
        """
        UPDATE maintenance_return_obligation
        SET exemption_source = CASE
            WHEN classification = 'required' THEN 'none'
            WHEN classification = 'exempt' THEN 'category_disk'
            ELSE NULL
        END
        """
    )
    op.alter_column(
        "maintenance_return_obligation",
        "exemption_source",
        nullable=True,
    )
    op.drop_constraint(
        "ck_maintenance_return_obligation_rule_result",
        "maintenance_return_obligation",
        type_="check",
    )
    op.create_check_constraint(
        "ck_maintenance_return_obligation_rule_result",
        "maintenance_return_obligation",
        "(classification = 'required' AND category_id_snapshot IS NOT NULL "
        "AND required_quantity = source_quantity AND exemption_source = 'none') OR "
        "(classification = 'exempt' AND required_quantity = 0 AND ("
        "(category_id_snapshot IS NOT NULL AND category_major_snapshot = '硬盘' "
        "AND exemption_source = 'category_disk') OR "
        "(exemption_source IN ('line_no_return', 'project_default_no_return')"
        "))) OR "
        "(classification = 'pending_category' AND category_id_snapshot IS NULL "
        "AND category_major_snapshot IS NULL AND category_minor_snapshot IS NULL "
        "AND required_quantity = 0 AND exemption_source IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_maintenance_return_obligation_rule_result",
        "maintenance_return_obligation",
        type_="check",
    )
    op.create_check_constraint(
        "ck_maintenance_return_obligation_rule_result",
        "maintenance_return_obligation",
        "(classification = 'required' AND category_id_snapshot IS NOT NULL "
        "AND required_quantity = source_quantity) OR "
        "(classification = 'exempt' AND category_id_snapshot IS NOT NULL "
        "AND category_major_snapshot = '硬盘' AND required_quantity = 0) OR "
        "(classification = 'pending_category' AND category_id_snapshot IS NULL "
        "AND category_major_snapshot IS NULL AND category_minor_snapshot IS NULL "
        "AND required_quantity = 0)",
    )
    op.drop_column("maintenance_return_obligation", "exemption_source")
    op.drop_column("maintenance_site_issue_line", "no_return")
    op.drop_column("maintenance_project", "no_return_default")

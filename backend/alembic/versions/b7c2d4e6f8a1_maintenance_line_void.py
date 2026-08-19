"""维保备件需求单行作废 + 工作簿手工来源（REQUIREMENTS #11/#18/#55）。

V2 项目总表全字段可编辑：03_备件明细支持「删除行=作废」、改数量、新增行。
作废为软删除（不计入计算、不再导出、需求单内关联记录级联作废），不物理删除
氚云事实（#18：消失的单只进差异清单不删不隐藏/作废保留展示）。

纯加法（expand 迁移）：
- ``f_maintenance_line``: ``is_active`` / ``voided_at`` / ``voided_by`` /
  ``void_reason`` / ``edited_source`` 五列；两个索引。
- ``maintenance_site_issue_line``: ``is_active`` 列 + 索引，用于 06 级联作废过滤。

旧应用不引用新列，向前兼容；downgrade 仅供迁移测试往返。氚云 loader 的 upsert
白名单（``_MAINT_LINE_UPD``）不含这些列，重传不会复活被作废的行。

Revision ID: b7c2d4e6f8a1
Revises: d1f3a5c7e2b4
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b7c2d4e6f8a1"
down_revision = "d1f3a5c7e2b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "f_maintenance_line",
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.add_column(
        "f_maintenance_line",
        sa.Column("voided_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "f_maintenance_line",
        sa.Column("voided_by", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "f_maintenance_line",
        sa.Column("void_reason", sa.Text(), nullable=True),
    )
    op.add_column(
        "f_maintenance_line",
        sa.Column(
            "edited_source",
            sa.String(length=16),
            nullable=False,
            server_default="wbdd",
        ),
    )
    op.create_index(
        "ix_ml_active", "f_maintenance_line", ["is_active"], unique=False
    )
    op.create_index(
        "ix_ml_order_active",
        "f_maintenance_line",
        ["order_id", "is_active"],
        unique=False,
    )

    op.add_column(
        "maintenance_site_issue_line",
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.create_index(
        "ix_msil_active",
        "maintenance_site_issue_line",
        ["is_active"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_msil_active", table_name="maintenance_site_issue_line")
    op.drop_column("maintenance_site_issue_line", "is_active")
    op.drop_index("ix_ml_order_active", table_name="f_maintenance_line")
    op.drop_index("ix_ml_active", table_name="f_maintenance_line")
    op.drop_column("f_maintenance_line", "edited_source")
    op.drop_column("f_maintenance_line", "void_reason")
    op.drop_column("f_maintenance_line", "voided_by")
    op.drop_column("f_maintenance_line", "voided_at")
    op.drop_column("f_maintenance_line", "is_active")

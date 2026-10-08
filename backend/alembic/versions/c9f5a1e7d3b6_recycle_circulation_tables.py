"""备件循环台账第一批：回收批次 / 回收明细聚合行 / SN 台账（板块 D，D-1/D-4）。

Revision ID: c9f5a1e7d3b6
Revises: e7c1f9a3b5d2
Create Date: 2026-10-07

additive：只建新表，不动既有表。口径依据 docs/decisions/0002（D-18~D-24，
2026-10-07 甲方微信确认）：SN 挂 PN 下一 SN 一物；送修环节不建流程不记成本。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c9f5a1e7d3b6"
down_revision: Union[str, Sequence[str], None] = "e7c1f9a3b5d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "recycle_batch",
        sa.Column("batch_id", sa.String(36), primary_key=True),
        sa.Column("source_filename", sa.String(255), nullable=False),
        sa.Column("file_sha256", sa.String(64), nullable=False),
        sa.Column("batch_label", sa.String(64)),
        sa.Column("source_doc_no", sa.String(64)),
        sa.Column("imported_by", sa.String(64), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("row_count", sa.Integer, nullable=False),
        sa.Column("line_count", sa.Integer, nullable=False),
        sa.Column("warning_count", sa.Integer, nullable=False),
        sa.Column("total_amount", sa.Numeric(14, 2), nullable=False),
        sa.UniqueConstraint("file_sha256", name="uq_recycle_batch_sha256"),
    )
    op.create_table(
        "recycle_line",
        sa.Column("line_id", sa.String(36), primary_key=True),
        sa.Column(
            "batch_id", sa.String(36),
            sa.ForeignKey("recycle_batch.batch_id"), nullable=False,
        ),
        sa.Column("part_id", sa.Integer, sa.ForeignKey("dim_part.id"), nullable=True),
        sa.Column("pn_raw", sa.String(128), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("category_label", sa.String(64)),
        sa.Column("warehouse_code", sa.String(32), nullable=False),
        sa.Column("bin_code", sa.String(64), nullable=False),
        sa.Column("condition", sa.String(8), nullable=False),
        sa.Column("qty", sa.Numeric(14, 3), nullable=False),
        sa.Column("unit_price", sa.Numeric(14, 2), nullable=False),
        sa.Column("total_price", sa.Numeric(14, 2), nullable=False),
        sa.Column("company_entity", sa.String(128)),
        sa.Column("needs_review", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("price_mismatch", sa.Boolean, nullable=False, server_default="false"),
        sa.CheckConstraint("condition IN ('好件', '坏件')", name="ck_recycle_line_condition"),
        sa.CheckConstraint("qty > 0", name="ck_recycle_line_qty_positive"),
        sa.CheckConstraint("unit_price >= 0", name="ck_recycle_line_price_nonneg"),
        sa.UniqueConstraint(
            "batch_id", "pn_raw", "warehouse_code", "bin_code", "condition",
            name="uq_recycle_line_aggregate",
        ),
    )
    op.create_index("ix_recycle_line_batch", "recycle_line", ["batch_id"])
    op.create_index("ix_recycle_line_part", "recycle_line", ["part_id"])
    op.create_table(
        "circulation_sn_item",
        sa.Column("sn", sa.String(128), primary_key=True),
        sa.Column("part_id", sa.Integer, sa.ForeignKey("dim_part.id"), nullable=True),
        sa.Column("pn_std", sa.String(128), nullable=False),
        sa.Column("lifecycle_status", sa.String(32), nullable=False, server_default="pending_detection"),
        sa.Column("source_batch_id", sa.String(36), sa.ForeignKey("recycle_batch.batch_id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "lifecycle_status IN ('pending_detection', 'in_stock', 'retired')",
            name="ck_sn_lifecycle",
        ),
    )
    op.create_index("ix_circulation_sn_item_part", "circulation_sn_item", ["part_id"])
    op.create_index("ix_circulation_sn_item_pn_std", "circulation_sn_item", ["pn_std"])


def downgrade() -> None:
    op.drop_index("ix_circulation_sn_item_pn_std", table_name="circulation_sn_item")
    op.drop_index("ix_circulation_sn_item_part", table_name="circulation_sn_item")
    op.drop_table("circulation_sn_item")
    op.drop_index("ix_recycle_line_part", table_name="recycle_line")
    op.drop_index("ix_recycle_line_batch", table_name="recycle_line")
    op.drop_table("recycle_line")
    op.drop_table("recycle_batch")

"""检测单表 + SN 台账扩展（板块 D，D-2/D-23）。

Revision ID: e2b7d9c4a6f8
Revises: c9f5a1e7d3b6
Create Date: 2026-10-08

additive：新建 detection_sheet / detection_item；circulation_sn_item 加
detection_item_id 关联列并放宽生命周期 CHECK（新增 bad_stock 坏件在库）。
口径依据 docs/decisions/0002 D-23（检测环节为 SN 采集点、实物 PN 以实物为准）。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e2b7d9c4a6f8"
down_revision: Union[str, Sequence[str], None] = "c9f5a1e7d3b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "detection_sheet",
        sa.Column("sheet_id", sa.String(36), primary_key=True),
        sa.Column("batch_id", sa.String(36), sa.ForeignKey("recycle_batch.batch_id"), nullable=False),
        sa.Column("inspector", sa.String(64), nullable=False),
        sa.Column("note", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_detection_sheet_batch_id", "detection_sheet", ["batch_id"])
    op.create_table(
        "detection_item",
        sa.Column("item_id", sa.String(36), primary_key=True),
        sa.Column("sheet_id", sa.String(36), sa.ForeignKey("detection_sheet.sheet_id"), nullable=False),
        sa.Column("line_id", sa.String(36), sa.ForeignKey("recycle_line.line_id"), nullable=False),
        sa.Column("nominal_pn_raw", sa.String(128), nullable=False),
        sa.Column("actual_pn_raw", sa.String(128)),
        sa.Column("part_id", sa.Integer, sa.ForeignKey("dim_part.id"), nullable=True),
        sa.Column("received_qty", sa.Numeric(14, 3), nullable=False),
        sa.Column("actual_condition", sa.String(8), nullable=False),
        sa.Column("handling", sa.String(32), nullable=False),
        sa.Column("sn_count", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("actual_condition IN ('好件', '坏件')", name="ck_detection_item_condition"),
        sa.CheckConstraint("received_qty > 0", name="ck_detection_item_qty_positive"),
    )
    op.create_index("ix_detection_item_sheet_id", "detection_item", ["sheet_id"])
    op.create_index("ix_detection_item_line_id", "detection_item", ["line_id"])
    op.create_index("ix_detection_item_part_id", "detection_item", ["part_id"])
    # SN 台账：加检测关联列
    op.add_column(
        "circulation_sn_item",
        sa.Column("detection_item_id", sa.String(36),
                  sa.ForeignKey("detection_item.item_id"), nullable=True),
    )
    op.create_index("ix_circulation_sn_item_detection_item_id", "circulation_sn_item", ["detection_item_id"])
    # 生命周期 CHECK 放宽：新增 bad_stock（坏件在库）
    op.drop_constraint("ck_sn_lifecycle", "circulation_sn_item", type_="check")
    op.create_check_constraint(
        "ck_sn_lifecycle", "circulation_sn_item",
        "lifecycle_status IN ('pending_detection', 'in_stock', 'bad_stock', 'retired')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_sn_lifecycle", "circulation_sn_item", type_="check")
    op.create_check_constraint(
        "ck_sn_lifecycle", "circulation_sn_item",
        "lifecycle_status IN ('pending_detection', 'in_stock', 'retired')",
    )
    op.drop_index("ix_circulation_sn_item_detection_item_id", table_name="circulation_sn_item")
    op.drop_column("circulation_sn_item", "detection_item_id")
    op.drop_index("ix_detection_item_part_id", table_name="detection_item")
    op.drop_index("ix_detection_item_line_id", table_name="detection_item")
    op.drop_index("ix_detection_item_sheet_id", table_name="detection_item")
    op.drop_table("detection_item")
    op.drop_index("ix_detection_sheet_batch_id", table_name="detection_sheet")
    op.drop_table("detection_sheet")

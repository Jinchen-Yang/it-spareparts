"""WBDD 展示补全列（plan v1.3 §3：头 34 + 明细 28，全 nullable）+ 专用上传回执表

纯加法：不改既有列、不加索引到事实表、无 backfill。
成本回填列不受影响（loader upsert 白名单仍排除、recompute 独占）。

Revision ID: b4c8d2e6f1a3
Revises: f1b3d5e7a9c2
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "b4c8d2e6f1a3"
down_revision: str | None = "f1b3d5e7a9c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Qty 与模型 _types.Qty 一致（Numeric(14,3)）
_QTY = sa.Numeric(14, 3)

# (列名, 类型) —— 与 app/models/maintenance.py / plan v1.3 §3.1 逐列一致
_ORDER_COLUMNS: list[tuple[str, sa.types.TypeEngine]] = [
    ("head_demand_qty", _QTY),
    ("head_purchase_qty", _QTY),
    ("head_shipped_qty", _QTY),
    ("head_returned_qty", _QTY),
    ("maintainer_raw", sa.String(64)),
    ("work_order_no", sa.String(64)),
    ("created_by_raw", sa.String(64)),
    ("purchaser_raw", sa.String(64)),
    ("purchaser2_raw", sa.String(64)),
    ("project_manager_raw", sa.String(64)),
    ("project_manager_staff_raw", sa.String(64)),
    ("co_salesperson_raw", sa.String(64)),
    ("partner_raw", sa.String(64)),
    ("sales_dept_raw", sa.String(64)),
    ("warehouse_keeper_raw", sa.String(64)),
    ("storage_center", sa.String(64)),
    ("warehouse_raw", sa.String(64)),
    ("change_warehouse_flag", sa.Boolean()),
    ("change_warehouse", sa.String(64)),
    ("change_warehouse_handler", sa.String(64)),
    ("warehouse_handler", sa.String(64)),
    ("supply_deadline", sa.Date()),
    ("delivery_address_option", sa.String(128)),
    ("receiver", sa.String(64)),
    ("receiver_phone", sa.String(32)),
    ("receiver_address", sa.Text()),
    ("express_no", sa.String(128)),
    ("express_no2", sa.String(128)),
    ("image_urls", sa.Text()),
    ("attachments", sa.Text()),
    ("whole_machine_check", sa.String(16)),
    ("accept_generic_flag", sa.Boolean()),
    ("created_at_raw", sa.String(32)),
    ("modified_at_raw", sa.String(32)),
]

# plan v1.3 §3.2：前 14 为流转状态列（只展示），后 14 为展示/排查列
_LINE_COLUMNS: list[tuple[str, sa.types.TypeEngine]] = [
    ("purchase_qty", _QTY),
    ("change_warehouse_purchase_qty", _QTY),
    ("purchased_qty", _QTY),
    ("pending_purchase_qty", _QTY),
    ("direct_ship_qty", _QTY),
    ("warehouse_need_qty", _QTY),
    ("warehouse_shipped_qty", _QTY),
    ("supplied_qty", _QTY),
    ("pending_supply_qty", _QTY),
    ("returned_qty", _QTY),
    ("pending_return_qty", _QTY),
    ("consumed_qty", _QTY),
    ("demand_pending_return_qty", _QTY),
    ("return_old_part", sa.String(16)),
    ("whole_or_part", sa.String(8)),
    ("whole_machine_purchase_part", sa.Text()),
    ("whole_machine_part_purchased", sa.String(16)),
    ("purchase_note", sa.Text()),
    ("line_note", sa.Text()),
    ("line_image_urls", sa.Text()),
    ("warehouse_stock_raw", sa.Text()),
    ("adjust_warehouse_flag", sa.Boolean()),
    ("adjust_warehouse", sa.String(64)),
    ("adjust_storage_center", sa.String(64)),
    ("adjust_keeper", sa.String(64)),
    ("ship_warehouse", sa.String(64)),
    ("ship_warehouse_object_id", sa.String(64)),
    ("ship_stock", _QTY),
]

assert len(_ORDER_COLUMNS) == 34, "plan v1.3 §3.1 约定头级新增 34 列"
assert len(_LINE_COLUMNS) == 28, "plan v1.3 §3.2 约定明细级新增 28 列"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    for name, type_ in _ORDER_COLUMNS:
        op.add_column("f_maintenance_order", sa.Column(name, type_, nullable=True))
    for name, type_ in _LINE_COLUMNS:
        op.add_column("f_maintenance_line", sa.Column(name, type_, nullable=True))

    op.create_table(
        "maintenance_wbdd_import_receipt",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "batch_id", sa.Integer(),
            sa.ForeignKey("sys_import_batch.id"), nullable=False, unique=True,
        ),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("uploaded_by", sa.String(64), nullable=False),
        sa.Column("file_hash", sa.String(64), nullable=False),
        sa.Column("layout", sa.String(4), nullable=True),
        sa.Column("report_json", JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.UniqueConstraint(
            "uploaded_by", "idempotency_key",
            name="uq_maintenance_wbdd_import_idempotency",
        ),
    )
    op.create_index(
        "ix_maintenance_wbdd_receipt_hash",
        "maintenance_wbdd_import_receipt", ["file_hash"],
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_index(
        "ix_maintenance_wbdd_receipt_hash",
        table_name="maintenance_wbdd_import_receipt",
    )
    op.drop_table("maintenance_wbdd_import_receipt")
    for name, _ in reversed(_LINE_COLUMNS):
        op.drop_column("f_maintenance_line", name)
    for name, _ in reversed(_ORDER_COLUMNS):
        op.drop_column("f_maintenance_order", name)

"""D-0050: WBDD 字段保真（展示文本扩 Text + 个别调整发货仓原文列）。

展示/排查文本列不再按旧 varchar 上限静默切片：源单元格原样保留。新增
``f_maintenance_line.adjust_warehouse_raw`` 保存「个别调整发货仓」源文本，既有
``adjust_warehouse_flag`` Boolean 仍只在明确是/否时置值。本迁移不读取、不回填
业务数据；既有行原值不变。

Revision ID: c7e1a3b5d9f2
Revises: a3c5e7f9b2d4
"""

from alembic import op
import sqlalchemy as sa

revision = "c7e1a3b5d9f2"
down_revision = "a3c5e7f9b2d4"
branch_labels = None
depends_on = None

# 旧定长 → Text。顺序不影响结果；downgrade 按同一张表校验超长值。
_ORDER_TEXT_WIDEN = {
    64: (
        "maintainer_raw", "work_order_no", "created_by_raw", "purchaser_raw",
        "purchaser2_raw", "project_manager_raw", "project_manager_staff_raw",
        "co_salesperson_raw", "partner_raw", "sales_dept_raw",
        "warehouse_keeper_raw", "storage_center", "warehouse_raw",
        "change_warehouse", "change_warehouse_handler", "warehouse_handler",
        "receiver",
    ),
    128: ("delivery_address_option", "express_no", "express_no2"),
    32: ("receiver_phone", "created_at_raw", "modified_at_raw"),
    16: ("whole_machine_check",),
}
_LINE_TEXT_WIDEN = {
    64: (
        "adjust_warehouse", "adjust_storage_center", "adjust_keeper",
        "ship_warehouse", "ship_warehouse_object_id",
    ),
    16: ("return_old_part", "whole_machine_part_purchased"),
    8: ("whole_or_part",),
}
_WIDEN = (
    ("f_maintenance_order", _ORDER_TEXT_WIDEN),
    ("f_maintenance_line", _LINE_TEXT_WIDEN),
)


def _guard_too_long(table: str, column: str, limit: int) -> None:
    """downgrade 前拒绝会截断既有新长值的收窄；锁定由调用方统一持有。"""
    op.execute(f"""
        DO $migration$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM {table} WHERE char_length({column}) > {limit}
            ) THEN
                RAISE EXCEPTION
                    'downgrade blocked: % rows in {table}.{column} exceed {limit} chars; export or clean them before narrowing',
                    (SELECT count(*) FROM {table} WHERE char_length({column}) > {limit});
            END IF;
        END
        $migration$;
    """)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "f_maintenance_line",
        sa.Column("adjust_warehouse_raw", sa.Text(), nullable=True),
    )
    for table, widen in _WIDEN:
        for old_length, columns in widen.items():
            for column in columns:
                op.alter_column(
                    table,
                    column,
                    existing_type=sa.String(old_length),
                    type_=sa.Text(),
                    existing_nullable=True,
                )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # 检查与收窄同事务持锁，阻止检查后的并发写入造成隐式截断/失败。
    op.execute(
        "LOCK TABLE f_maintenance_order, f_maintenance_line "
        "IN ACCESS EXCLUSIVE MODE"
    )
    for table, widen in _WIDEN:
        for old_length, columns in widen.items():
            for column in columns:
                _guard_too_long(table, column, old_length)
    # 新增原文列可能已承载无法退化的源文本；非空即拒绝删除，由运维显式导出/清理。
    op.execute("""
        DO $migration$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM f_maintenance_line
                WHERE adjust_warehouse_raw IS NOT NULL
            ) THEN
                RAISE EXCEPTION
                    'downgrade blocked: % rows in f_maintenance_line.adjust_warehouse_raw have data; export or clear them before dropping the column',
                    (SELECT count(*) FROM f_maintenance_line
                     WHERE adjust_warehouse_raw IS NOT NULL);
            END IF;
        END
        $migration$;
    """)
    for table, widen in _WIDEN:
        for old_length, columns in widen.items():
            for column in columns:
                op.alter_column(
                    table,
                    column,
                    existing_type=sa.Text(),
                    type_=sa.String(old_length),
                    existing_nullable=True,
                )
    op.drop_column("f_maintenance_line", "adjust_warehouse_raw")

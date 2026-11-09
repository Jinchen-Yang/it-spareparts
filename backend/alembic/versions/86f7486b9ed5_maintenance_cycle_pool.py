"""维修循环池：单位/事件/可售意图 + 五个页键（D-0057 T1）。

坏件循环池与闲鱼可售意图的数据库地基（任务书 §5.1–5.4）。本修订只做 schema
与权限默认值，不写任何业务数据，也不写氚云/库存/变卖台账。

- 一行一件：maintenance_cycle_unit 不设 qty 列，出站 qty 恒为 1；
- maintenance_cycle_sell_intent 的租约/幂等质量对齐 catalog_delivery：
  领取用 (state, available_at/lease_until) 部分索引，回执用租约 CAS；
- 五个页面键对全部内置角色模板（含 admin）默认 false：运行时由
  permissions.ACCOUNT_SCOPED_BETA_PAGE_KEYS 逐实名账号显式授权，
  迁移不得给 admin 打开（避免生产迁移后菜单自动出现）。

父修订 98afc0d2157b 是 v1.46.0 后的冻结唯一 head；本修订不使用 #520 的
a8c2e4f6b0d1（该分支 HOLD 且整文件弃用）。

Revision ID: 86f7486b9ed5
Revises: 98afc0d2157b
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "86f7486b9ed5"
down_revision: str | None = "98afc0d2157b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CYCLE_PAGE_KEYS = (
    "page_cycle_recycle",
    "page_cycle_test",
    "page_cycle_repair",
    "page_cycle_manage",
    "page_cycle_wip",
)


def _cycle_page_keys_false() -> None:
    """把五个页面键以 false 写入全部模板与存量账号权限图（fail-closed）。

    F-01：**不得为 ``template_perms IS NULL``/非 object 的旧账号创建快照**。
    这类账号的有效权限来自 legacy ``permissions``（或角色模板回退）；若升级时把
    template_perms 从 NULL 变成「只有五个 false 键的 {} 快照」，effective_for_user
    会把它当账号快照使用，从而丢掉该账号原有全部权限。

    处理口径：
    - sys_role_template.permissions：JSONB 模板，合并 false（非 object 退化为 {} 的
      旧行为保留，模板本身由迁移/seed 维护）。
    - sys_user.template_perms：**仅当已是 object 快照时**合并 false；NULL/非 object 不动。
    - sys_user.permissions（legacy 图）：仅当已是 object 时合并 false；NULL 不动。
    - sys_user.perm_overrides：所有 object 覆盖层都删掉该键（避免稀疏覆盖重新打开）；
      NULL/非 object 不动。
    """
    for key in CYCLE_PAGE_KEYS:
        op.execute(
            sa.text(
                """
                UPDATE sys_role_template
                SET permissions = CASE
                      WHEN jsonb_typeof(permissions) = 'object' THEN permissions
                      ELSE '{}'::jsonb
                    END || jsonb_build_object(:key, false)
                """
            ).bindparams(key=key)
        )
        op.execute(
            sa.text(
                """
                UPDATE sys_user
                SET template_perms = template_perms || jsonb_build_object(:key, false)
                WHERE jsonb_typeof(template_perms) = 'object'
                """
            ).bindparams(key=key)
        )
        op.execute(
            sa.text(
                """
                UPDATE sys_user
                SET perm_overrides = perm_overrides - :key
                WHERE jsonb_typeof(perm_overrides) = 'object'
                """
            ).bindparams(key=key)
        )
        op.execute(
            sa.text(
                """
                UPDATE sys_user
                SET permissions = permissions || jsonb_build_object(:key, false)
                WHERE jsonb_typeof(permissions) = 'object'
                """
            ).bindparams(key=key)
        )


def _cycle_page_keys_removed() -> None:
    """F-01：降级只从 object 图里删键，绝不制造 '{}' 快照。"""
    for key in CYCLE_PAGE_KEYS:
        for table, column in (
            ("sys_role_template", "permissions"),
            ("sys_user", "template_perms"),
            ("sys_user", "perm_overrides"),
            ("sys_user", "permissions"),
        ):
            op.execute(
                sa.text(
                    f"UPDATE {table} SET {column} = {column} - :key "
                    f"WHERE jsonb_typeof({column}) = 'object'"
                ).bindparams(key=key)
            )


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")

    # ---- 1. 循环单位（一行一件）----
    op.create_table(
        "maintenance_cycle_unit",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "unit_uid",
            postgresql.UUID(as_uuid=False),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("part_id", sa.Integer(), nullable=False),
        sa.Column("pn", sa.String(length=128), nullable=False),
        sa.Column(
            "station", sa.String(length=16), server_default="pool", nullable=False
        ),
        sa.Column("outcome", sa.String(length=16), nullable=True),
        sa.Column("condition_bucket", sa.String(length=8), nullable=True),
        sa.Column("intake_condition", sa.String(length=64), nullable=True),
        sa.Column("repair_round", sa.Integer(), server_default="0", nullable=False),
        sa.Column("source_kind", sa.String(length=16), nullable=False),
        sa.Column("source_receipt_id", sa.String(length=36), nullable=True),
        sa.Column("serial_number", sa.String(length=128), nullable=True),
        sa.Column("vendor_name", sa.String(length=128), nullable=True),
        sa.Column("assignee_username", sa.String(length=64), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("updated_by", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "station IN ('pool', 'testing', 'repairing', 'outsource', 'ready', 'closed')",
            name="ck_maintenance_cycle_unit_station",
        ),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('to_shop', 'to_company', 'scrap', 'sold')",
            name="ck_maintenance_cycle_unit_outcome",
        ),
        sa.CheckConstraint(
            "condition_bucket IS NULL OR condition_bucket IN ('good', 'bad')",
            name="ck_maintenance_cycle_unit_bucket",
        ),
        sa.CheckConstraint(
            "source_kind IN ('receipt', 'external')",
            name="ck_maintenance_cycle_unit_source_kind",
        ),
        sa.CheckConstraint(
            "(source_kind = 'receipt' AND source_receipt_id IS NOT NULL) OR "
            "(source_kind = 'external' AND source_receipt_id IS NULL)",
            name="ck_maintenance_cycle_unit_source_shape",
        ),
        sa.CheckConstraint(
            "(station = 'closed') = (outcome IS NOT NULL AND closed_at IS NOT NULL)",
            name="ck_maintenance_cycle_unit_closed_shape",
        ),
        sa.CheckConstraint(
            "withdrawn_at IS NULL OR "
            "(station = 'pool' AND outcome IS NULL AND closed_at IS NULL)",
            name="ck_maintenance_cycle_unit_withdrawn",
        ),
        sa.CheckConstraint(
            "(outcome IS NULL OR outcome NOT IN ('to_shop', 'sold')) OR "
            "(condition_bucket IS NOT NULL AND condition_bucket IN ('good', 'bad'))",
            name="ck_maintenance_cycle_unit_shop_bucket",
        ),
        sa.CheckConstraint(
            "repair_round >= 0 AND version >= 1",
            name="ck_maintenance_cycle_unit_counters",
        ),
        sa.CheckConstraint(
            "char_length(btrim(pn)) > 0",
            name="ck_maintenance_cycle_unit_pn",
        ),
        sa.ForeignKeyConstraint(["part_id"], ["dim_part.id"]),
        sa.ForeignKeyConstraint(
            ["source_receipt_id"], ["maintenance_rkd_return_line.rkd_line_id"]
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("unit_uid", name="uq_maintenance_cycle_unit_uid"),
    )
    op.create_index(
        "ix_maintenance_cycle_unit_station",
        "maintenance_cycle_unit",
        ["station", "assignee_username"],
    )
    op.create_index(
        "ix_maintenance_cycle_unit_part",
        "maintenance_cycle_unit",
        ["part_id", "station"],
    )
    op.create_index(
        "ix_maintenance_cycle_unit_receipt",
        "maintenance_cycle_unit",
        ["source_receipt_id"],
    )
    # 同一收货行的非空 SN 在未撤回单位中唯一（SN 可选；撤回后释放）。
    op.create_index(
        "ux_maintenance_cycle_unit_receipt_sn",
        "maintenance_cycle_unit",
        ["source_receipt_id", "serial_number"],
        unique=True,
        postgresql_where=sa.text(
            "withdrawn_at IS NULL AND serial_number IS NOT NULL"
        ),
    )

    # ---- 2. 循环事件（只追加；payload 主管审计字段由服务层白名单控制）----
    op.create_table(
        "maintenance_cycle_event",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("unit_id", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["unit_id"], ["maintenance_cycle_unit.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_maintenance_cycle_event_unit",
        "maintenance_cycle_event",
        ["unit_id", "id"],
    )

    # ---- 3. 可售意图 outbox（租约/回执质量对齐 catalog_delivery）----
    op.create_table(
        "maintenance_cycle_sell_intent",
        sa.Column("delivery_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("unit_id", sa.BigInteger(), nullable=False),
        sa.Column("part_id", sa.Integer(), nullable=False),
        sa.Column("qty", sa.Numeric(14, 3), nullable=False),
        sa.Column("condition_bucket", sa.String(length=8), nullable=False),
        sa.Column(
            "state", sa.String(length=16), server_default="pending", nullable=False
        ),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("receipt_status", sa.String(length=16), nullable=True),
        sa.Column("block_code", sa.String(length=40), nullable=True),
        sa.Column("last_error_code", sa.String(length=40), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "condition_bucket IN ('good', 'bad')",
            name="ck_maintenance_cycle_sell_bucket",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'delivering', 'delivered', 'blocked', 'failed')",
            name="ck_maintenance_cycle_sell_state",
        ),
        sa.CheckConstraint("qty = 1", name="ck_maintenance_cycle_sell_qty"),
        sa.CheckConstraint(
            "attempts >= 0", name="ck_maintenance_cycle_sell_attempts"
        ),
        sa.CheckConstraint(
            "(state = 'delivering') = "
            "(lease_token IS NOT NULL AND lease_until IS NOT NULL)",
            name="ck_maintenance_cycle_sell_lease",
        ),
        sa.CheckConstraint(
            "receipt_status IS NULL OR "
            "receipt_status IN ('applied', 'unchanged', 'blocked', 'conflict')",
            name="ck_maintenance_cycle_sell_receipt",
        ),
        sa.ForeignKeyConstraint(["part_id"], ["dim_part.id"]),
        sa.ForeignKeyConstraint(["unit_id"], ["maintenance_cycle_unit.id"]),
        sa.PrimaryKeyConstraint("delivery_id"),
        sa.UniqueConstraint(
            "unit_id", name="uq_maintenance_cycle_sell_intent_unit"
        ),
    )
    op.create_index(
        "ix_maintenance_cycle_sell_pending",
        "maintenance_cycle_sell_intent",
        ["available_at", "created_at"],
        postgresql_where=sa.text("state = 'pending'"),
    )
    op.create_index(
        "ix_maintenance_cycle_sell_lease",
        "maintenance_cycle_sell_intent",
        ["lease_until"],
        postgresql_where=sa.text("state = 'delivering'"),
    )
    op.create_index(
        "ix_maintenance_cycle_sell_blocked",
        "maintenance_cycle_sell_intent",
        ["available_at"],
        postgresql_where=sa.text("state = 'blocked'"),
    )

    # ---- 4. 五个页面键默认 false（含 admin）----
    _cycle_page_keys_false()


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")

    _cycle_page_keys_removed()

    op.drop_index(
        "ix_maintenance_cycle_sell_blocked",
        table_name="maintenance_cycle_sell_intent",
    )
    op.drop_index(
        "ix_maintenance_cycle_sell_lease",
        table_name="maintenance_cycle_sell_intent",
    )
    op.drop_index(
        "ix_maintenance_cycle_sell_pending",
        table_name="maintenance_cycle_sell_intent",
    )
    op.drop_table("maintenance_cycle_sell_intent")

    op.drop_index(
        "ix_maintenance_cycle_event_unit", table_name="maintenance_cycle_event"
    )
    op.drop_table("maintenance_cycle_event")

    op.drop_index(
        "ux_maintenance_cycle_unit_receipt_sn",
        table_name="maintenance_cycle_unit",
    )
    op.drop_index(
        "ix_maintenance_cycle_unit_receipt",
        table_name="maintenance_cycle_unit",
    )
    op.drop_index(
        "ix_maintenance_cycle_unit_part", table_name="maintenance_cycle_unit"
    )
    op.drop_index(
        "ix_maintenance_cycle_unit_station", table_name="maintenance_cycle_unit"
    )
    op.drop_table("maintenance_cycle_unit")

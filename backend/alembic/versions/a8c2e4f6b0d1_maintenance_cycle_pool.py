"""D-0057 坏件循环池表与权限键。

Revision ID: a8c2e4f6b0d1
Revises: 98afc0d2157b
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a8c2e4f6b0d1"
down_revision: Union[str, Sequence[str], None] = "98afc0d2157b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_KEYS = (
    "page_cycle_recycle",
    "page_cycle_test",
    "page_cycle_repair",
    "page_cycle_manage",
    "page_cycle_wip",
)
# admin 模板全开；其余角色默认关，由权限中心显式授予。
_ENABLED = {
    "admin": _KEYS,
}


def _permission_defaults(enabled: bool) -> None:
    for key in _KEYS:
        enabled_roles = tuple(role for role, keys in _ENABLED.items() if key in keys)
        role_list = ", ".join(f"'{role}'" for role in enabled_roles) or "'__none__'"
        for table, column, role_column in (
            ("sys_role_template", "permissions", "code"),
            ("sys_user", "template_perms", "role"),
            ("sys_user", "permissions", "role"),
        ):
            condition = (
                ""
                if table == "sys_role_template"
                else f" WHERE jsonb_typeof({column}) = 'object'"
            )
            if enabled:
                op.execute(
                    sa.text(
                        f"""
                        UPDATE {table}
                        SET {column} = CASE WHEN jsonb_typeof({column}) = 'object'
                                            THEN {column} ELSE '{{}}'::jsonb END
                            || jsonb_build_object(:key, {role_column} IN ({role_list}))
                        {condition}
                        """
                    ).bindparams(key=key)
                )
            else:
                op.execute(
                    sa.text(
                        f"""
                        UPDATE {table}
                        SET {column} = {column} - :key
                        WHERE jsonb_typeof({column}) = 'object'
                        """
                    ).bindparams(key=key)
                )


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

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
        sa.Column("station", sa.String(length=16), server_default="pool", nullable=False),
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
        sa.UniqueConstraint("unit_uid"),
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

    op.create_table(
        "maintenance_cycle_sell_intent",
        sa.Column("delivery_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("unit_id", sa.BigInteger(), nullable=False),
        sa.Column("part_id", sa.Integer(), nullable=False),
        sa.Column("qty", sa.Numeric(14, 3), nullable=False),
        sa.Column("condition_bucket", sa.String(length=8), nullable=False),
        sa.Column("state", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("lease_token", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
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
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "condition_bucket IN ('good', 'bad')",
            name="ck_maintenance_cycle_sell_bucket",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'delivering', 'delivered', 'blocked', 'failed')",
            name="ck_maintenance_cycle_sell_state",
        ),
        sa.CheckConstraint("qty > 0", name="ck_maintenance_cycle_sell_qty"),
        sa.CheckConstraint(
            "attempt_count >= 0", name="ck_maintenance_cycle_sell_attempts"
        ),
        sa.ForeignKeyConstraint(["part_id"], ["dim_part.id"]),
        sa.ForeignKeyConstraint(["unit_id"], ["maintenance_cycle_unit.id"]),
        sa.PrimaryKeyConstraint("delivery_id"),
        sa.UniqueConstraint("unit_id", name="uq_maintenance_cycle_sell_intent_unit"),
    )
    op.create_index(
        "ix_maintenance_cycle_sell_state",
        "maintenance_cycle_sell_intent",
        ["state", "created_at"],
    )

    _permission_defaults(True)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    _permission_defaults(False)
    op.drop_index(
        "ix_maintenance_cycle_sell_state", table_name="maintenance_cycle_sell_intent"
    )
    op.drop_table("maintenance_cycle_sell_intent")
    op.drop_index(
        "ix_maintenance_cycle_event_unit", table_name="maintenance_cycle_event"
    )
    op.drop_table("maintenance_cycle_event")
    op.drop_index(
        "ix_maintenance_cycle_unit_receipt", table_name="maintenance_cycle_unit"
    )
    op.drop_index("ix_maintenance_cycle_unit_part", table_name="maintenance_cycle_unit")
    op.drop_index(
        "ix_maintenance_cycle_unit_station", table_name="maintenance_cycle_unit"
    )
    op.drop_table("maintenance_cycle_unit")

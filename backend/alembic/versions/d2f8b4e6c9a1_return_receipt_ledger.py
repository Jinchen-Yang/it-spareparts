"""Return receipt ledger: unify RKD inbound rows and manual registrations.

Extends maintenance_rkd_return_line into the unified return-receipt ledger
(2026-09-11 decision): nullable batch/head for manual rows, demand linkage
(source_order_id), void/version/audit columns, and a partial index for
(project, demand) aggregation over active rows. Existing rows are untouched:
they keep source='rkd_import', line_status='active' via server defaults.

Revision ID: d2f8b4e6c9a1
Revises: c9e5a1b7d3f8
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d2f8b4e6c9a1"
down_revision: str | None = "c9e5a1b7d3f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "maintenance_rkd_return_line"


def upgrade() -> None:
    op.alter_column(_TABLE, "batch_id", existing_type=sa.String(36), nullable=True)
    op.alter_column(_TABLE, "head_row_id", existing_type=sa.String(36), nullable=True)
    op.add_column(
        _TABLE,
        sa.Column("source_order_id", sa.String(64), nullable=True),
    )
    op.create_foreign_key(
        "fk_maintenance_rkd_return_source_order",
        _TABLE,
        "f_maintenance_order",
        ["source_order_id"],
        ["raw_order_id"],
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "source",
            sa.String(16),
            nullable=False,
            server_default="'rkd_import'",
        ),
    )
    op.add_column(_TABLE, sa.Column("description", sa.String(256), nullable=True))
    op.add_column(_TABLE, sa.Column("note", sa.String(512), nullable=True))
    op.add_column(_TABLE, sa.Column("evidence_ref", sa.String(128), nullable=True))
    op.add_column(
        _TABLE,
        sa.Column(
            "line_status",
            sa.String(16),
            nullable=False,
            server_default="'active'",
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "created_by",
            sa.String(64),
            nullable=False,
            server_default="'rkd_import'",
        ),
    )
    op.add_column(_TABLE, sa.Column("updated_by", sa.String(64), nullable=True))
    op.add_column(_TABLE, sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(_TABLE, sa.Column("voided_by", sa.String(64), nullable=True))
    op.add_column(_TABLE, sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(_TABLE, sa.Column("void_reason", sa.String(256), nullable=True))
    op.create_check_constraint("ck_maintenance_rkd_return_source", _TABLE,
                               "source IN ('rkd_import', 'manual')")
    op.create_check_constraint("ck_maintenance_rkd_return_status", _TABLE,
                               "line_status IN ('active', 'voided')")
    op.create_check_constraint("ck_maintenance_rkd_return_version", _TABLE,
                               "version >= 1")
    op.create_check_constraint(
        "ck_maintenance_rkd_return_source_shape",
        _TABLE,
        "(source = 'manual' AND batch_id IS NULL AND head_row_id IS NULL) "
        "OR (source = 'rkd_import' AND batch_id IS NOT NULL "
        "AND head_row_id IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_maintenance_rkd_return_void_shape",
        _TABLE,
        "(line_status = 'voided') = (voided_at IS NOT NULL "
        "AND voided_by IS NOT NULL)",
    )
    op.create_index(
        "ix_maintenance_rkd_return_active_demand",
        _TABLE,
        ["project_id", "source_order_id"],
        postgresql_where=sa.text("line_status = 'active'"),
    )


def downgrade() -> None:
    # Reversible: manual rows cannot survive NOT NULL batch/head, so they are
    # removed explicitly (audit rows are string-keyed and keep the history).
    op.drop_index("ix_maintenance_rkd_return_active_demand", table_name=_TABLE)
    op.drop_constraint("ck_maintenance_rkd_return_void_shape", _TABLE, type_="check")
    op.drop_constraint("ck_maintenance_rkd_return_source_shape", _TABLE, type_="check")
    op.drop_constraint("ck_maintenance_rkd_return_version", _TABLE, type_="check")
    op.drop_constraint("ck_maintenance_rkd_return_status", _TABLE, type_="check")
    op.drop_constraint("ck_maintenance_rkd_return_source", _TABLE, type_="check")
    op.drop_column(_TABLE, "void_reason")
    op.drop_column(_TABLE, "voided_at")
    op.drop_column(_TABLE, "voided_by")
    op.drop_column(_TABLE, "updated_at")
    op.drop_column(_TABLE, "updated_by")
    op.drop_column(_TABLE, "created_by")
    op.drop_column(_TABLE, "version")
    op.drop_column(_TABLE, "line_status")
    op.drop_column(_TABLE, "evidence_ref")
    op.drop_column(_TABLE, "note")
    op.drop_column(_TABLE, "description")
    op.drop_column(_TABLE, "source")
    op.drop_constraint("fk_maintenance_rkd_return_source_order", _TABLE, type_="foreignkey")
    op.drop_column(_TABLE, "source_order_id")
    op.execute(f"DELETE FROM {_TABLE} WHERE batch_id IS NULL OR head_row_id IS NULL")
    op.alter_column(_TABLE, "batch_id", existing_type=sa.String(36), nullable=False)
    op.alter_column(_TABLE, "head_row_id", existing_type=sa.String(36), nullable=False)

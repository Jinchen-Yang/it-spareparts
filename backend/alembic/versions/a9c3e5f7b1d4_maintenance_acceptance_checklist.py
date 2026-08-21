"""验收需求清单导入（2026-08-21 客户反馈）：两表 + 权限键回填

DDL：maintenance_acceptance_checklist_batch / _item（doc_import 范式）。
数据：action_maintenance_acceptance_checklist_import 写入全部模板与存量账号，
一律 false（fail-closed，范式同 c5d9e3f7a2b4）。

Revision ID: a9c3e5f7b1d4
Revises: c5d7e9f1a3b5
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "a9c3e5f7b1d4"
down_revision: str | None = "c5d7e9f1a3b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KEY = "action_maintenance_acceptance_checklist_import"


def upgrade() -> None:
    op.create_table(
        "maintenance_acceptance_checklist_batch",
        sa.Column("batch_id", sa.String(length=36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(length=36),
            sa.ForeignKey("maintenance_project.project_id"),
            nullable=False,
        ),
        sa.Column("file_hash", sa.String(length=64), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("uploaded_by", sa.String(length=64), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("item_rows", sa.Integer(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("issue_rows", sa.Integer(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("status", sa.String(length=16), nullable=False,
                  server_default=sa.text("'pending'")),
        sa.Column("report_json", JSONB(), nullable=True),
        sa.Column("applied_by", sa.String(length=64), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replaced_batch_id", sa.String(length=36), nullable=True),
        sa.CheckConstraint("item_rows >= 0",
                           name="ck_acceptance_checklist_batch_item_rows"),
        sa.CheckConstraint("issue_rows >= 0",
                           name="ck_acceptance_checklist_batch_issue_rows"),
        sa.CheckConstraint(
            "status IN ('pending', 'applied', 'failed')",
            name="ck_acceptance_checklist_batch_status"),
        sa.CheckConstraint(
            "(status = 'applied') = (applied_at IS NOT NULL AND applied_by IS NOT NULL)",
            name="ck_acceptance_checklist_batch_applied"),
        sa.UniqueConstraint("uploaded_by", "idempotency_key",
                            name="uq_acceptance_checklist_batch_idempotency"),
    )
    op.create_index("ix_acceptance_checklist_batch_hash",
                    "maintenance_acceptance_checklist_batch", ["file_hash"])
    op.create_index("ix_acceptance_checklist_batch_project",
                    "maintenance_acceptance_checklist_batch",
                    ["project_id", "uploaded_at"])

    op.create_table(
        "maintenance_acceptance_checklist_item",
        sa.Column("item_id", sa.String(length=36), primary_key=True),
        sa.Column(
            "batch_id",
            sa.String(length=36),
            sa.ForeignKey("maintenance_acceptance_checklist_batch.batch_id"),
            nullable=False,
        ),
        sa.Column("row_no", sa.Integer(), nullable=False),
        sa.Column("raw_json", JSONB(), nullable=False),
        sa.Column("requirement", sa.Text(), nullable=False),
        sa.Column("done", sa.Boolean(), nullable=True),
        sa.Column("issues", sa.dialects.postgresql.ARRAY(sa.String(length=128)),
                  nullable=True),
        sa.CheckConstraint("row_no >= 1",
                           name="ck_acceptance_checklist_item_row_no"),
    )
    op.create_index("ix_acceptance_checklist_item_batch",
                    "maintenance_acceptance_checklist_item", ["batch_id"])

    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        sa.text(
            """
            UPDATE sys_role_template
            SET permissions = CASE
                  WHEN jsonb_typeof(permissions) = 'object' THEN permissions
                  ELSE '{}'::jsonb
                END || jsonb_build_object(:key, false)
            """
        ).bindparams(key=_KEY)
    )
    op.execute(
        sa.text(
            """
            UPDATE sys_user
            SET template_perms = CASE
                  WHEN jsonb_typeof(template_perms) = 'object' THEN template_perms
                  ELSE '{}'::jsonb
                END || jsonb_build_object(:key, false),
                perm_overrides = CASE
                  WHEN jsonb_typeof(perm_overrides) = 'object' THEN perm_overrides
                  ELSE '{}'::jsonb
                END - :key
            """
        ).bindparams(key=_KEY)
    )
    op.execute(
        sa.text(
            """
            UPDATE sys_user
            SET permissions = CASE
                  WHEN jsonb_typeof(permissions) = 'object' THEN permissions
                  ELSE '{}'::jsonb
                END || jsonb_build_object(:key, false)
            WHERE permissions IS NOT NULL
            """
        ).bindparams(key=_KEY)
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
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
            ).bindparams(key=_KEY)
        )
    op.drop_index("ix_acceptance_checklist_item_batch",
                  table_name="maintenance_acceptance_checklist_item")
    op.drop_table("maintenance_acceptance_checklist_item")
    op.drop_index("ix_acceptance_checklist_batch_project",
                  table_name="maintenance_acceptance_checklist_batch")
    op.drop_index("ix_acceptance_checklist_batch_hash",
                  table_name="maintenance_acceptance_checklist_batch")
    op.drop_table("maintenance_acceptance_checklist_batch")

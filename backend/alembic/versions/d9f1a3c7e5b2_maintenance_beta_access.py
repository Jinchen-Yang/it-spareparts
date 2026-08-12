"""add maintenance Beta whitelist permission

Revision ID: d9f1a3c7e5b2
Revises: c7e2a9f4b6d1
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "d9f1a3c7e5b2"
down_revision: str | None = "c7e2a9f4b6d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KEY = "page_maintenance_beta"


def upgrade() -> None:
    """包括实名管理员在内，所有存量账号必须逐个加入 Beta 白名单。"""
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

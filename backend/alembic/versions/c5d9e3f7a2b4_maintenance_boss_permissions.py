"""维保展示板权限键回填（plan v1.3 M1-6）：page_maintenance_boss + action_maintenance_wbdd_import

纯数据迁移（无 DDL）：两键写入全部模板与存量账号，一律 false（fail-closed）。
运行时 admin 走 require_page/require_action 内置 bypass（非 ACCOUNT_SCOPED 键），
其余账号须在权限中心逐个勾选。

Revision ID: c5d9e3f7a2b4
Revises: b4c8d2e6f1a3
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c5d9e3f7a2b4"
down_revision: str | None = "b4c8d2e6f1a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KEYS = ("page_maintenance_boss", "action_maintenance_wbdd_import")


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    for key in _KEYS:
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
                SET template_perms = CASE
                      WHEN jsonb_typeof(template_perms) = 'object' THEN template_perms
                      ELSE '{}'::jsonb
                    END || jsonb_build_object(:key, false),
                    perm_overrides = CASE
                      WHEN jsonb_typeof(perm_overrides) = 'object' THEN perm_overrides
                      ELSE '{}'::jsonb
                    END - :key
                """
            ).bindparams(key=key)
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
            ).bindparams(key=key)
        )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    for key in _KEYS:
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

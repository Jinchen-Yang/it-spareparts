"""采购分析页面键（D-39，客户需求 Round1）：page_purchase_analytics

纯数据迁移（无 DDL）：模板仅 boss 开 true（admin 由 page_permission_allowed 运行时
恒放行，与 c5d9e3f7a2b4 同口径），其余角色与存量账号一律回填 false（fail-closed）；
boss 存量账号（template_perms 快照 + legacy permissions 双写）补 true 并清掉该键
覆盖（a9e2f7c4d1b8 同款"覆盖层让位"，管理员之后仍可在权限中心按账号收紧）。

注意：down_revision 当前指向本分支开工时的唯一 head 7f73424a7f65；PR #395
（f_sales_reconciliation_snapshot 迁移 b8f2d6c4a1e9）合并后需 rebase 改指新 head。

Revision ID: a5e8c3f7b2d9
Revises: 7f73424a7f65
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a5e8c3f7b2d9"
down_revision: str | None = "7f73424a7f65"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KEY = "page_purchase_analytics"


def _merge(column: str, value: bool) -> str:
    return f"""
        CASE WHEN jsonb_typeof({column}) = 'object' THEN {column}
             ELSE '{{}}'::jsonb
        END || jsonb_build_object(:key, {str(value).lower()})
    """


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")

    # 1) 角色模板：先全员 fail-closed，再给 boss 开（admin 模板随 _full() 同步 true，
    #    保持模板与代码 effective() 一致；运行时 admin 本就短路）。
    op.execute(
        sa.text(f"UPDATE sys_role_template SET permissions = {_merge('permissions', False)}").bindparams(key=_KEY)
    )
    op.execute(
        sa.text(f"UPDATE sys_role_template SET permissions = {_merge('permissions', True)} WHERE code = 'boss'").bindparams(key=_KEY)
    )
    op.execute(
        sa.text(f"UPDATE sys_role_template SET permissions = {_merge('permissions', True)} WHERE code = 'admin'").bindparams(key=_KEY)
    )

    # 2) 存量账号快照：boss 补 true 并清覆盖层；其余角色 fail-closed（不动覆盖层，
    #    之后仍可由管理员在权限中心显式授权）。
    op.execute(
        sa.text(
            f"""
            UPDATE sys_user
            SET template_perms = {_merge('template_perms', True)},
                perm_overrides = CASE WHEN jsonb_typeof(perm_overrides) = 'object'
                                      THEN perm_overrides - :key
                                      ELSE perm_overrides END
            WHERE role = 'boss' AND jsonb_typeof(template_perms) = 'object'
            """
        ).bindparams(key=_KEY)
    )
    op.execute(
        sa.text(
            f"""
            UPDATE sys_user
            SET template_perms = {_merge('template_perms', False)}
            WHERE role <> 'boss' AND jsonb_typeof(template_perms) = 'object'
            """
        ).bindparams(key=_KEY)
    )

    # 3) legacy permissions 图双写（权限中心 v2 之前的老账号读这里）。
    op.execute(
        sa.text(
            f"""
            UPDATE sys_user
            SET permissions = {_merge('permissions', True)}
            WHERE role = 'boss' AND permissions IS NOT NULL
            """
        ).bindparams(key=_KEY)
    )
    op.execute(
        sa.text(
            f"""
            UPDATE sys_user
            SET permissions = {_merge('permissions', False)}
            WHERE role <> 'boss' AND permissions IS NOT NULL
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

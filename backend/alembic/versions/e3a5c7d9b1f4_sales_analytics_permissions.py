"""销售分析页权限键（D-40 §6 / D-41 §1，PR-3b）：page_sales_analysis + data_reconciliation

纯数据迁移（无 DDL）。口径：
- 全部模板与存量账号先一律回填 false（fail-closed，c5d9e3f7a2b4 同款）；
- boss 模板与 boss 存量账号两键回填 true（D-40 §6 / D-41 §1：老板、管理员可见；
  admin 非账号白名单键，运行时由 _full()/page_permission_allowed 恒放行，快照不特殊处理）；
- admin 模板行同步 true（与 Python _full() 全开图对齐，防模板/代码口径漂移）；
- perm_overrides 中两键剥离：升级前不存在这两个键，剥离仅清除同批误写，升级后
  管理员仍可在权限中心按账号重新授予/收紧。

Revision ID: e3a5c7d9b1f4
Revises: b8f2d6c4a1e9
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e3a5c7d9b1f4"
down_revision: str | None = "b8f2d6c4a1e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 键名是本迁移私有常量，插值为字面量与 a9e2f7c4d1b8 同款
_KEYS = ("page_sales_analysis", "data_reconciliation")
_TRUE = ", ".join(f"'{k}', true" for k in _KEYS)
_STRIP = " ".join(f"- '{k}'" for k in _KEYS)


def _merge_false(key: str, col: str) -> str:
    """列名走参数插值（不嵌套 .format，避免 '{} '::jsonb 字面量大括号被误当占位符）。"""
    return f"""
        CASE WHEN jsonb_typeof({col}) = 'object' THEN {col}
        ELSE '{{}}'::jsonb END || jsonb_build_object('{key}', false)
    """


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    for key in _KEYS:
        # 1) 全部模板 fail-closed
        op.execute(
            f"UPDATE sys_role_template SET permissions = {_merge_false(key, 'permissions')}"
        )
        # 2) 存量账号快照 fail-closed + 覆盖层让位（管理员之后可逐账号重新授予）
        op.execute(
            f"""
            UPDATE sys_user
            SET template_perms = {_merge_false(key, 'template_perms')},
                perm_overrides = CASE
                    WHEN jsonb_typeof(perm_overrides) = 'object'
                    THEN perm_overrides - '{key}'
                    ELSE perm_overrides
                END
            WHERE jsonb_typeof(template_perms) = 'object'
            """
        )
        # 3) 老 legacy permissions 图双写（无快照的老账号读这里）
        op.execute(
            f"""
            UPDATE sys_user
            SET permissions = {_merge_false(key, 'permissions')}
            WHERE permissions IS NOT NULL
            """
        )
    # 4) boss 模板与 admin 模板开启两键（admin 与 Python _full() 对齐，防口径漂移）
    op.execute(
        f"""
        UPDATE sys_role_template
        SET permissions = CASE
                WHEN jsonb_typeof(permissions) = 'object' THEN permissions
                ELSE '{{}}'::jsonb
            END || jsonb_build_object({_TRUE})
        WHERE code IN ('boss', 'admin')
        """
    )
    # 5) boss 存量账号快照 + legacy 双写开启两键；覆盖层让位
    op.execute(
        f"""
        UPDATE sys_user
        SET template_perms = template_perms || jsonb_build_object({_TRUE}),
            perm_overrides = CASE
                WHEN jsonb_typeof(perm_overrides) = 'object'
                THEN perm_overrides {_STRIP}
                ELSE perm_overrides
            END
        WHERE role = 'boss'
          AND jsonb_typeof(template_perms) = 'object'
        """
    )
    op.execute(
        f"""
        UPDATE sys_user
        SET permissions = CASE
                WHEN jsonb_typeof(permissions) = 'object' THEN permissions
                ELSE '{{}}'::jsonb
            END || jsonb_build_object({_TRUE})
        WHERE role = 'boss'
          AND permissions IS NOT NULL
        """
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
                f"UPDATE {table} SET {column} = {column} - '{key}' "
                f"WHERE jsonb_typeof({column}) = 'object'"
            )

"""维保负责人角色 + own_maintenance_projects_only 行键（2026-08-21 客户反馈）。

1. seed 系统模板 maintenance_manager（page_maintenance + 行键开，其余全关）；
2. own_maintenance_projects_only 写入全部既有模板与存量账号，一律 false
   （fail-closed，范式同 c5d9e3f7a2b4；acceptance_checklist 动作键已在
   a9c3e5f7b1d4 回填，此处不重复）。

Revision ID: b1d4f6a8c2e7
Revises: a9c3e5f7b1d4
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b1d4f6a8c2e7"
down_revision: str | None = "a9c3e5f7b1d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BACKFILL_KEYS = ("own_maintenance_projects_only",)

# 与 permissions.ROLE_TEMPLATES["maintenance_manager"] 保持同构（迁移内冻结，
# 避免 import 应用代码带来的执行环境耦合——a3f8c1d9e5b2 同款取舍）
_TEMPLATE_PERMS = {
    "page_maintenance": True,
    "own_maintenance_projects_only": True,
}


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # maintenance_manager（19 字符）超旧 varchar(16)：角色三列同步加宽到 32
    with op.batch_alter_table("sys_role_template") as batch:
        batch.alter_column("base_role",
                           existing_type=sa.String(16), type_=sa.String(32))
    with op.batch_alter_table("sys_user") as batch:
        batch.alter_column("role",
                           existing_type=sa.String(16), type_=sa.String(32))
    with op.batch_alter_table("sys_access_log") as batch:
        batch.alter_column("role",
                           existing_type=sa.String(16), type_=sa.String(32))
    conn = op.get_bind()
    exists = conn.execute(
        sa.text("SELECT 1 FROM sys_role_template WHERE code = 'maintenance_manager'")
    ).fetchone()
    if not exists:
        conn.execute(
            sa.text("""
                INSERT INTO sys_role_template
                    (code, name, description, base_role, permissions,
                     is_system, is_active, version, created_by)
                VALUES ('maintenance_manager', '维保负责人',
                        '整套维保页面（主页/项目面板/数据分析），默认不可见成本金额，'
                        '行级收敛到「我是维保负责人 ∪ 项目销售是我」的项目',
                        'maintenance_manager', CAST(:perms AS jsonb),
                        true, true, 1, 'migration')
            """),
            {"perms": json.dumps(_TEMPLATE_PERMS)},
        )
    for key in _BACKFILL_KEYS:
        op.execute(
            sa.text(
                """
                UPDATE sys_role_template
                SET permissions = CASE
                      WHEN jsonb_typeof(permissions) = 'object' THEN permissions
                      ELSE '{}'::jsonb
                    END || jsonb_build_object(:key, false)
                WHERE code <> 'maintenance_manager'
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
    op.execute(
        sa.text("DELETE FROM sys_role_template WHERE code = 'maintenance_manager'")
    )
    # 收窄前先把超长角色值归位为 readonly（防 downgrade 卡在旧值上）
    op.execute(sa.text(
        "UPDATE sys_user SET role = 'readonly' "
        "WHERE char_length(role) > 16"))
    op.execute(sa.text(
        "UPDATE sys_access_log SET role = 'readonly' "
        "WHERE role IS NOT NULL AND char_length(role) > 16"))
    with op.batch_alter_table("sys_role_template") as batch:
        batch.alter_column("base_role",
                           existing_type=sa.String(32), type_=sa.String(16))
    with op.batch_alter_table("sys_user") as batch:
        batch.alter_column("role",
                           existing_type=sa.String(32), type_=sa.String(16))
    with op.batch_alter_table("sys_access_log") as batch:
        batch.alter_column("role",
                           existing_type=sa.String(32), type_=sa.String(16))
    for key in _BACKFILL_KEYS:
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

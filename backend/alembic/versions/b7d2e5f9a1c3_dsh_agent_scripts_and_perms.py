"""DSH 企业助手集成：白名单脚本表 + 两个新权限键回填

- 新表 sys_dsh_script：助手服务端写库通道的白名单脚本（名称/内容/所需动作键/超时/启用）。
- 新权限键 action_agent_sql（只读 SQL 直查）与 action_agent_dsn_ro（本地脚本只读 DSN）：
  模板/账号快照回填——admin 模板 True，其余 False；账号覆盖里清掉这两个键。

Revision ID: b7d2e5f9a1c3
Revises: a8e4f1c7d3b9
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "b7d2e5f9a1c3"
down_revision: str | None = "a8e4f1c7d3b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KEYS = ("action_agent_sql", "action_agent_dsn_ro")


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "sys_dsh_script",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=64), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("required_action", sa.String(length=64), nullable=True),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_by", sa.String(length=64), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    for key in _KEYS:
        op.execute(
            f"""
            UPDATE sys_role_template
            SET permissions = CASE
                    WHEN jsonb_typeof(permissions) = 'object' THEN permissions
                    ELSE '{{}}'::jsonb
                END || jsonb_build_object('{key}', code = 'admin')
            """
        )
        op.execute(
            f"""
            UPDATE sys_user
            SET template_perms = template_perms || jsonb_build_object('{key}', role = 'admin'),
                perm_overrides = CASE
                        WHEN jsonb_typeof(perm_overrides) = 'object' THEN perm_overrides
                        ELSE '{{}}'::jsonb
                    END - '{key}'
            WHERE jsonb_typeof(template_perms) = 'object'
            """
        )
        op.execute(
            f"""
            UPDATE sys_user
            SET permissions = CASE
                    WHEN jsonb_typeof(permissions) = 'object' THEN permissions
                    ELSE '{{}}'::jsonb
                END || jsonb_build_object('{key}', role = 'admin')
            WHERE permissions IS NOT NULL
            """
        )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    for key in _KEYS:
        op.execute(f"UPDATE sys_role_template SET permissions = permissions - '{key}' WHERE jsonb_typeof(permissions) = 'object'")
        op.execute(f"UPDATE sys_user SET template_perms = template_perms - '{key}' WHERE jsonb_typeof(template_perms) = 'object'")
        op.execute(f"UPDATE sys_user SET perm_overrides = perm_overrides - '{key}' WHERE jsonb_typeof(perm_overrides) = 'object'")
        op.execute(f"UPDATE sys_user SET permissions = permissions - '{key}' WHERE permissions IS NOT NULL")
    op.drop_table("sys_dsh_script")

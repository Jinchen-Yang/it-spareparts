"""project-level viewer assignments (基础信息编辑·项目可见账号多选)

2026-08-25 客户需求：维保项目可见性支持项目级多选已有账号。挂靠表
responsibility_type 放开增加 'viewer'（负责人唯一索引保持只约束
primary_manager；viewer 需要同项目同账号防重，补部分唯一索引）。

Revision ID: d5c1f8a3b7e2
Revises: c4d9a2e7f1b0
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "d5c1f8a3b7e2"
down_revision: str | None = "c4d9a2e7f1b0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint(
        "ck_maintenance_project_user_assignment_type",
        "maintenance_project_user_assignment",
        type_="check",
    )
    op.create_check_constraint(
        "ck_maintenance_project_user_assignment_type",
        "maintenance_project_user_assignment",
        "responsibility_type IN ('primary_manager', 'viewer')",
    )
    op.create_index(
        "ux_maintenance_project_user_assignment_viewer_active",
        "maintenance_project_user_assignment",
        ["project_id", "user_id"],
        unique=True,
        postgresql_where=sa.text(
            "archived_at IS NULL AND responsibility_type = 'viewer'"),
    )
    # 审计实体类型放开 viewer_assignment（模型同步）
    op.drop_constraint(
        "ck_maintenance_project_audit_entity_type",
        "maintenance_project_audit_log",
        type_="check",
    )
    op.create_check_constraint(
        "ck_maintenance_project_audit_entity_type",
        "maintenance_project_audit_log",
        "entity_type IN ('project', 'project_contract', 'manager_assignment', "
        "'source_order_assignment', 'viewer_assignment')",
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # 审计实体类型还原
    op.drop_constraint(
        "ck_maintenance_project_audit_entity_type",
        "maintenance_project_audit_log",
        type_="check",
    )
    op.create_check_constraint(
        "ck_maintenance_project_audit_entity_type",
        "maintenance_project_audit_log",
        "entity_type IN ('project', 'project_contract', 'manager_assignment', "
        "'source_order_assignment')",
    )
    # 旧 CHECK 只认 primary_manager：先清掉 viewer 行（软归档，保留审计）
    op.execute(
        """
        UPDATE maintenance_project_user_assignment
        SET archived_at = now(),
            archived_by = 'migration',
            archive_reason = 'downgrade d5c1f8a3b7e2: viewer type removed'
        WHERE responsibility_type = 'viewer' AND archived_at IS NULL
        """
    )
    op.drop_index(
        "ux_maintenance_project_user_assignment_viewer_active",
        table_name="maintenance_project_user_assignment",
    )
    op.drop_constraint(
        "ck_maintenance_project_user_assignment_type",
        "maintenance_project_user_assignment",
        type_="check",
    )
    op.create_check_constraint(
        "ck_maintenance_project_user_assignment_type",
        "maintenance_project_user_assignment",
        "responsibility_type = 'primary_manager'",
    )

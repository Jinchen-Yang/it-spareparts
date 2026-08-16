"""maintenance doc head project_id marker (F3 return-rate readiness)

Revision ID: d7f1a3c5e8b2
Revises: c3e9d1b7f5a2
Create Date: 2026-08-16
"""
from alembic import op
import sqlalchemy as sa

revision = "d7f1a3c5e8b2"
down_revision = "c3e9d1b7f5a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "maintenance_doc_head_row",
        sa.Column("project_id", sa.String(length=36), nullable=True),
    )
    op.create_foreign_key(
        "fk_maintenance_doc_head_project",
        "maintenance_doc_head_row",
        "maintenance_project",
        ["project_id"],
        ["project_id"],
    )
    op.create_index(
        "ix_maintenance_doc_head_project",
        "maintenance_doc_head_row",
        ["project_id"],
    )
    # 存量回填（round-6 Blocker 1）：已应用 RKD 头可从返还事实推导项目归属；
    # 无法推导的（无坏件返还行的头）保持 NULL，视为待重导治理，不虚构归属。
    op.execute(
        """
        UPDATE maintenance_doc_head_row AS head
        SET project_id = fact.project_id
        FROM maintenance_rkd_return_line AS fact
        WHERE fact.head_row_id = head.row_id
          AND head.project_id IS NULL
        """
    )


def downgrade() -> None:
    # 项目归属是返还率就绪判定依据；已有归属事实时禁止回滚（round-6 Blocker 1）
    op.execute(
        "LOCK TABLE maintenance_doc_head_row IN ACCESS EXCLUSIVE MODE"
    )
    op.execute(
        """
        DO $guard$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM maintenance_doc_head_row WHERE project_id IS NOT NULL
          )
          THEN
            RAISE EXCEPTION
              'd7f1a3c5e8b2 downgrade blocked: resolved doc head projects exist';
          END IF;
        END
        $guard$;
        """
    )
    op.drop_index(
        "ix_maintenance_doc_head_project", table_name="maintenance_doc_head_row"
    )
    op.drop_constraint(
        "fk_maintenance_doc_head_project",
        "maintenance_doc_head_row",
        type_="foreignkey",
    )
    op.drop_column("maintenance_doc_head_row", "project_id")

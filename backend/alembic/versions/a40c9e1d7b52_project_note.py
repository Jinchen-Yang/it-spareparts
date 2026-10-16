"""需求00009：独立项目备注，与免返标注并存。

Revision ID: a40c9e1d7b52
Revises: f307604fa4d7
"""

from alembic import op
import sqlalchemy as sa

revision = "a40c9e1d7b52"
down_revision = "f307604fa4d7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("maintenance_project", sa.Column("project_note", sa.Text(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # 检查与删除同事务持锁，阻止检查后的并发写入造成文字丢失。
    op.execute("LOCK TABLE maintenance_project IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $migration$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM maintenance_project
                WHERE project_note IS NOT NULL AND project_note <> ''
            ) THEN
                RAISE EXCEPTION 'downgrade blocked: export and preserve nonempty project_note before removing the column';
            END IF;
        END
        $migration$;
    """)
    op.drop_column("maintenance_project", "project_note")

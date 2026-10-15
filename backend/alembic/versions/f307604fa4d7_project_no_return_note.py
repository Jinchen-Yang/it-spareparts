"""D-32: project reminder text, independent of return facts.

Revision ID: f307604fa4d7
Revises: c8f3d1a7e2b9
"""

from alembic import op
import sqlalchemy as sa

revision = "f307604fa4d7"
down_revision = "c8f3d1a7e2b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("maintenance_project", sa.Column("no_return_note", sa.Text(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # 检查与 DROP 同事务持锁，不能在检查后允许另一事务写入再丢弃文字。
    op.execute("LOCK TABLE maintenance_project IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $migration$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM maintenance_project
                WHERE no_return_note IS NOT NULL AND no_return_note <> ''
            ) THEN
                RAISE EXCEPTION 'downgrade blocked: export and preserve nonempty no_return_note before removing the column';
            END IF;
        END
        $migration$;
    """)
    op.drop_column("maintenance_project", "no_return_note")

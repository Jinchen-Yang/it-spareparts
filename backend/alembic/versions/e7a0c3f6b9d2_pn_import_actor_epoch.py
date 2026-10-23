"""Persist the original submitting authorization epoch for import workers.

Revision ID: e7a0c3f6b9d2
Revises: d6f9b2e5a8c1
"""

from alembic import op
import sqlalchemy as sa

revision = "e7a0c3f6b9d2"
down_revision = "d6f9b2e5a8c1"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("sys_import_job", "sys_import_batch"):
        op.add_column(
            table, sa.Column("actor_authorization_epoch", sa.Integer(), nullable=True)
        )
        op.create_check_constraint(
            f"ck_{table}_actor_epoch",
            table,
            "actor_authorization_epoch IS NULL OR (actor_user_id IS NOT NULL AND actor_authorization_epoch >= 0)",
        )


def downgrade():
    for table in ("sys_import_job", "sys_import_batch"):
        if (
            op.get_bind()
            .execute(
                sa.text(
                    f"SELECT EXISTS (SELECT 1 FROM {table} WHERE actor_authorization_epoch IS NOT NULL)"
                )
            )
            .scalar_one()
        ):
            raise RuntimeError(
                "PN import authorization history requires explicit reconciliation before downgrade"
            )
    for table in ("sys_import_job", "sys_import_batch"):
        op.drop_constraint(f"ck_{table}_actor_epoch", table, type_="check")
        op.drop_column(table, "actor_authorization_epoch")

"""maintenance collection evidence dedup: partial unique (milestone_id, md5)

Revision ID: f1b3d5e7a9c2
Revises: e3c5a7f9d1b2
Create Date: 2026-08-16
"""
from alembic import op

revision = "f1b3d5e7a9c2"
down_revision = "e3c5a7f9d1b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 同一节点同一内容只能有一份生效凭证（并发 check-then-insert 由 DB 兜底）
    op.execute(
        "CREATE UNIQUE INDEX uq_maintenance_collection_evidence_milestone_md5 "
        "ON maintenance_collection_evidence (milestone_id, md5) "
        "WHERE is_active"
    )


def downgrade() -> None:
    op.execute(
        "DROP INDEX IF EXISTS uq_maintenance_collection_evidence_milestone_md5"
    )

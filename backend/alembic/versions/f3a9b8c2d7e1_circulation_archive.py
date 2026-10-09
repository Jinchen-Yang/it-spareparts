"""循环档案附件表 + 上架状态（板块 D，D-3/D-22）。

Revision ID: f3a9b8c2d7e1
Revises: e2b7d9c4a6f8
Create Date: 2026-10-08

additive：新建 circulation_archive / circulation_attachment。
口径依据 docs/decisions/0002 D-22（2026-10-07 甲方确认）：
可上架 = 照片 + 检测报告齐全；有权限账号可对资料不全件强制上架（留痕审计）。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f3a9b8c2d7e1"
down_revision: Union[str, Sequence[str], None] = "e2b7d9c4a6f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "circulation_archive",
        sa.Column("archive_id", sa.String(36), primary_key=True),
        sa.Column("pn_std", sa.String(128), nullable=False),
        sa.Column("part_id", sa.Integer, sa.ForeignKey("dim_part.id"), nullable=True),
        sa.Column("listing_status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("force_listed_by", sa.String(64)),
        sa.Column("force_listed_at", sa.DateTime(timezone=True)),
        sa.Column("force_reason", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "listing_status IN ('pending', 'listed', 'force_listed', 'delisted')",
            name="ck_circulation_archive_listing_status",
        ),
        sa.UniqueConstraint("pn_std", name="uq_circulation_archive_pn"),
    )
    op.create_index("ix_circulation_archive_part_id", "circulation_archive", ["part_id"])
    op.create_table(
        "circulation_attachment",
        sa.Column("attachment_id", sa.String(36), primary_key=True),
        sa.Column("archive_id", sa.String(36), sa.ForeignKey("circulation_archive.archive_id"), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("original_filename", sa.String(255), nullable=False),
        sa.Column("mime_type", sa.String(128), nullable=False),
        sa.Column("size_bytes", sa.Integer, nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("storage_key", sa.String(256), nullable=False),
        sa.Column("uploaded_by", sa.String(64), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("kind IN ('photo', 'report')", name="ck_circulation_attachment_kind"),
        sa.CheckConstraint("size_bytes > 0", name="ck_circulation_attachment_size"),
    )
    op.create_index("ix_circulation_attachment_archive_id", "circulation_attachment", ["archive_id"])


def downgrade() -> None:
    op.drop_index("ix_circulation_attachment_archive_id", table_name="circulation_attachment")
    op.drop_table("circulation_attachment")
    op.drop_index("ix_circulation_archive_part_id", table_name="circulation_archive")
    op.drop_table("circulation_archive")

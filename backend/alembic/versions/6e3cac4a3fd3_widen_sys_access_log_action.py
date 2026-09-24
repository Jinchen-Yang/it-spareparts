"""widen sys_access_log.action to varchar(64)

Revision ID: 6e3cac4a3fd3
Revises: e7c1f9a3b5d2
Create Date: 2026-09-24 12:55:00.000000

动作名演进到 33-40 字符（maintenance_expense_attribution_backfill=40、
upload_maintenance_acceptance_attachment=40 等）后，varchar(32) 写入直接
报 "value too long" 被 best-effort 吞掉——生产自 2026-09 起累计静默丢审计
6166 条。放宽到 64（元数据变更，不重写表）。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6e3cac4a3fd3"
down_revision: Union[str, Sequence[str], None] = "e7c1f9a3b5d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "sys_access_log", "action",
        existing_type=sa.String(length=32), type_=sa.String(length=64),
        existing_nullable=False,
    )


def downgrade() -> None:
    # 收窄前须先清理超长动作行，否则回滚失败——这里不做隐式删除。
    op.execute(
        "DELETE FROM sys_access_log WHERE length(action) > 32"
    )
    op.alter_column(
        "sys_access_log", "action",
        existing_type=sa.String(length=64), type_=sa.String(length=32),
        existing_nullable=False,
    )

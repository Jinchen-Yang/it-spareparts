"""为 D-34 全局幂等回执建立专用 GIN 表达式索引。

回执不能按当前批次、日期或 success 状态缩窗，否则同操作者跨批次复用
幂等键将漏检。索引只覆盖取消确认子对象，不索引整份上传报告。

升级仅并发建索引，不改数据；autocommit_block 会提交其前事务，发布时先
到 c9d4f2a6b8e1，再单独应用本 revision。并发建索引失败可能留下 INVALID
索引，检查并删除该无效索引后重试；不用 IF NOT EXISTS 掩盖不完整索引。
降级保留普通事务内 DROP INDEX，使后续历史降级守卫失败时能够整体回滚。

Revision ID: d8f6a2c4b9e7
Revises: c9d4f2a6b8e1
"""

import sqlalchemy as sa

from alembic import op

revision: str = "d8f6a2c4b9e7"
down_revision: str | None = "c9d4f2a6b8e1"
branch_labels: str | None = None
depends_on: str | None = None

_INDEX = "ix_batch_source_cancel_receipts"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            _INDEX,
            "sys_import_batch",
            [sa.text("(report_json -> '_source_cancel_confirmations')")],
            postgresql_using="gin",
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="sys_import_batch")

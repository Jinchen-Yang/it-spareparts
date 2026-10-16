"""dim_part 加 (updated_at, id) 复合索引：v1 /list keyset 分页专用。

D-36 PR1 整目录列举按 (updated_at, id) 行值比较翻页；无索引时每页都是
全表过滤再排序，商城反复翻完整目录会按页数放大读放大。2.8 万行建索引
秒级完成，不用 CONCURRENTLY（不能放进迁移事务）。

回滚兼容：仅新增索引，旧代码不依赖、可在新 schema 上运行；
downgrade 仅 DROP INDEX。

Revision ID: 7f73424a7f65
Revises: baff4b809f12
"""

from collections.abc import Sequence

from alembic import op

revision: str = "7f73424a7f65"
down_revision: str | None = "baff4b809f12"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 按仓库惯例带 lock_timeout，避免极端情况下等锁堵住迁移事务。
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_index("ix_part_updated_id", "dim_part", ["updated_at", "id"])


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_index("ix_part_updated_id", table_name="dim_part")

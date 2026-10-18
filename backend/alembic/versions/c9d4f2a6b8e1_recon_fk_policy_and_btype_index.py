"""快照→订单 FK 补索引与显式 ON DELETE、销售分析 business_type 筛选索引（#397/#395 复审）

三项索引/约束工程（PR-A 分析正确性批次）：

1. f_sales_reconciliation_snapshot.sales_order_id FK 补索引（#395 检察官）：PG 的 FK
   不会自动在引用侧建索引，删/改父行（f_sales_order）时引用侧全表扫。快照表只增不删，
   订单侧重导/清理会触发该检查路径。
2. 同 FK 显式 ON DELETE SET NULL：快照是按 raw_order_id 追加的**历史观测**（D-40），
   订单删除时既不能级联删观测（RESTRICT/CASCADE 都会破坏观测序列），也不应阻塞订单
   删除——置空链接、保留观测，订单重导后按 raw_order_id 可再关联。
3. f_sales_order (data_status, business_type) 索引（#397 (7)）：销售分析 business_type
   多选是选择性筛选。30 万行合成数据的 EXPLAIN 对拍：稀有类型 IN (...) + 金额倒序
   LIMIT 50 从 147ms（_bitmap 扫全量已生效再过滤 26.7 万行）降到 16ms
   （Bitmap Index Scan 直取 7856 行）；高频类型+日期窗口仍走既有 ix_so_status_date，
   options 聚合走并行计划——新索引只服务选择性筛选，不替换既有索引。

Revision ID: c9d4f2a6b8e1
Revises: a5e8c3f7b2d9
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c9d4f2a6b8e1"
down_revision: str | None = "a5e8c3f7b2d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SNAP = "f_sales_reconciliation_snapshot"
_LEGACY_FK = "f_sales_reconciliation_snapshot_sales_order_id_fkey"
_FK = "fk_sales_recon_snapshot_sales_order"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # 先建索引再换 FK：约束校验可借道索引
    op.create_index(
        "ix_sales_recon_snapshot_sales_order", _SNAP, ["sales_order_id"]
    )
    op.drop_constraint(_LEGACY_FK, _SNAP, type_="foreignkey")
    op.create_foreign_key(
        _FK, _SNAP, "f_sales_order",
        ["sales_order_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index(
        "ix_so_status_btype", "f_sales_order", ["data_status", "business_type"]
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_index("ix_so_status_btype", table_name="f_sales_order")
    op.drop_constraint(_FK, _SNAP, type_="foreignkey")
    # 回到 b8f2d6c4a1e9 原状：PG 默认命名、无 ON DELETE 策略、无索引
    op.create_foreign_key(
        _LEGACY_FK, _SNAP, "f_sales_order", ["sales_order_id"], ["id"]
    )
    op.drop_index("ix_sales_recon_snapshot_sales_order", table_name=_SNAP)

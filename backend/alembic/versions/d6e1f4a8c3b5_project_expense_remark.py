"""维保报销行备注列（验收补丁 P2，REQUIREMENTS #47）。

`04_报销订单` sheet 的「备注」是黄底可编辑列，但 f_project_expense 没有落点。
业务 2026-08-16 批准为此新增 **1 条纯加法迁移**，追加在链尾保持线性（M0-E 口径：
整链发布，只增不改）。

严格纯加法：单列 nullable、无 default、无回填、无索引、无约束变更。
旧应用不引用该列，向前兼容；回滚仍是「关 flag」，不做 downgrade（铁律 7）——
本文件的 downgrade 只为迁移测试的 upgrade↔downgrade 往返，不用于生产回滚。

类型取 Text，与 maintenance_collection_snapshot.remark 一致：报销备注是人写的
说明，不该因为一个人为的长度上限被截断。

Revision ID: d6e1f4a8c3b5
Revises: c5d9e3f7a2b4
"""
from alembic import op
import sqlalchemy as sa

revision = "d6e1f4a8c3b5"
down_revision = "c5d9e3f7a2b4"
branch_labels = None
depends_on = None

_TABLE = "f_project_expense"
_COLUMN = "remark"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column(_TABLE, _COLUMN)

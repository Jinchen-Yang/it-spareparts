"""make fk_alias_part_pn DEFERRABLE (for part_rename / standardize)

标准化改名（part_rename）要在同一事务里把 dim_part.pn_std 与名下 part_alias.pn_std 一起改。
复合外键 fk_alias_part_pn 非延迟时，无论先改哪张表都会在语句末违约（另一张尚未跟上）。
故改为 DEFERRABLE INITIALLY IMMEDIATE：默认仍即时校验（合并/导入不受影响），
仅改名事务内 `SET CONSTRAINTS fk_alias_part_pn DEFERRED`，改完在 commit 时统一校验。
纯约束重定义，无数据变更。

Revision ID: f7c2a9b4e1d8
Revises: b2e7d5c1f9a3
Create Date: 2026-06-13

"""
from typing import Sequence, Union

from alembic import op

revision: str = "f7c2a9b4e1d8"
down_revision: Union[str, None] = "b2e7d5c1f9a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLS = (["part_id", "pn_std"], ["id", "pn_std"])


def upgrade() -> None:
    op.drop_constraint("fk_alias_part_pn", "part_alias", type_="foreignkey")
    op.create_foreign_key("fk_alias_part_pn", "part_alias", "dim_part",
                          *_COLS, deferrable=True, initially="IMMEDIATE")


def downgrade() -> None:
    op.drop_constraint("fk_alias_part_pn", "part_alias", type_="foreignkey")
    op.create_foreign_key("fk_alias_part_pn", "part_alias", "dim_part", *_COLS)

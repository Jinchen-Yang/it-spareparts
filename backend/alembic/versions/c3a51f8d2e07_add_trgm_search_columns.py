"""add pg_trgm extension and generated search columns

近似检索地基（二期）：
- pg_trgm 扩展（相似度运算符 % / similarity()）
- dim_part.pn_compact / search_doc、part_alias.pn_compact 均为 STORED 生成列，
  由库自动维护，loader 零改动、与源字段零漂移
- GIN(gin_trgm_ops) 索引支撑模糊匹配毫秒级

Revision ID: c3a51f8d2e07
Revises: 876dad34e9c5
Create Date: 2026-06-09

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'c3a51f8d2e07'
down_revision: Union[str, Sequence[str], None] = '876dad34e9c5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 与 app/models/dimensions.py 中的表达式保持一致
_COMPACT_PN = "upper(regexp_replace(pn_std, '[^a-zA-Z0-9]', '', 'g'))"
_COMPACT_RAW = "upper(regexp_replace(pn_raw, '[^a-zA-Z0-9]', '', 'g'))"
_SEARCH_DOC = (
    f"pn_std || ' ' || {_COMPACT_PN}"
    " || ' ' || coalesce(brand, '')"
    " || ' ' || coalesce(category_major, '')"
    " || ' ' || coalesce(category_minor, '')"
    " || ' ' || coalesce(description, '')"
)


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        f"ALTER TABLE dim_part ADD COLUMN pn_compact TEXT "
        f"GENERATED ALWAYS AS ({_COMPACT_PN}) STORED"
    )
    op.execute(
        f"ALTER TABLE dim_part ADD COLUMN search_doc TEXT "
        f"GENERATED ALWAYS AS ({_SEARCH_DOC}) STORED"
    )
    op.execute(
        f"ALTER TABLE part_alias ADD COLUMN pn_compact TEXT "
        f"GENERATED ALWAYS AS ({_COMPACT_RAW}) STORED"
    )
    op.execute("CREATE INDEX ix_part_pn_compact ON dim_part (pn_compact)")
    op.execute(
        "CREATE INDEX ix_part_pn_compact_trgm ON dim_part "
        "USING gin (pn_compact gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX ix_part_search_doc_trgm ON dim_part "
        "USING gin (search_doc gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX ix_alias_pn_compact_trgm ON part_alias "
        "USING gin (pn_compact gin_trgm_ops)"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP INDEX IF EXISTS ix_alias_pn_compact_trgm")
    op.execute("DROP INDEX IF EXISTS ix_part_search_doc_trgm")
    op.execute("DROP INDEX IF EXISTS ix_part_pn_compact_trgm")
    op.execute("DROP INDEX IF EXISTS ix_part_pn_compact")
    op.execute("ALTER TABLE part_alias DROP COLUMN pn_compact")
    op.execute("ALTER TABLE dim_part DROP COLUMN search_doc")
    op.execute("ALTER TABLE dim_part DROP COLUMN pn_compact")
    # 扩展可能被其他对象使用，保留 pg_trgm 不删

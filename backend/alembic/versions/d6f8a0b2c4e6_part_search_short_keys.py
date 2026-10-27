"""为一至二字符检索增加同事务维护的短词投影与 GIN 索引。

Revision ID: d6f8a0b2c4e6
Revises: c4e6a8b1d3f5
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d6f8a0b2c4e6"
down_revision: str | None = "c4e6a8b1d3f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与查询端相同：基本、扩展及兼容汉字；标点和 ASCII 会切断连续串。
_CJK = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002fa1f\U00030000-\U000323af"


def _replace_refresh(*, short_keys: bool) -> None:
    delete_keys = "DELETE FROM part_search_short_key WHERE part_id = target_id;" if short_keys else ""
    upsert_keys = """
            INSERT INTO part_search_short_key(part_id, keys)
            VALUES (target_id, part_search_short_keys(full_doc))
            ON CONFLICT (part_id) DO UPDATE SET keys = excluded.keys;
    """ if short_keys else ""
    # 保留上一迁移的加锁、别名归属和墓碑语义，同事务维护独立窄表。
    op.execute(f"""
        CREATE OR REPLACE FUNCTION refresh_part_search_text(target_id integer) RETURNS void
        LANGUAGE plpgsql AS $refresh$
        DECLARE
            part_row dim_part%ROWTYPE;
            full_doc text;
        BEGIN
            SELECT * INTO part_row FROM dim_part WHERE id = target_id FOR NO KEY UPDATE;
            IF NOT FOUND OR part_row.status = 'merged' THEN
                DELETE FROM part_search_text WHERE part_id = target_id;
                {delete_keys}
                RETURN;
            END IF;
            SELECT part_search_normalize(concat_ws(' ',
                part_row.pn_std, part_row.pn_raw_sample,
                part_row.brand, part_row.manufacturer, part_row.summary,
                part_row.description, part_row.category_major, part_row.category_minor,
                (SELECT string_agg(a.pn_raw, ' ' ORDER BY a.id)
                 FROM part_alias a
                 WHERE a.status = 'active'
                   AND (a.part_id = target_id OR
                        (a.part_id IS NULL AND a.pn_std = part_row.pn_std))),
                (SELECT string_agg(concat_ws(' ', coalesce(k.label, s.spec_key),
                                            s.spec_value, k.unit), ' ' ORDER BY s.spec_key)
                 FROM product_specs s LEFT JOIN spec_key_label k ON k.spec_key = s.spec_key
                 WHERE s.part_id = target_id)
            )) INTO full_doc;
            INSERT INTO part_search_text(part_id, doc, doc_compact, updated_at)
            VALUES (target_id, full_doc, part_search_compact(full_doc), clock_timestamp())
            ON CONFLICT (part_id) DO UPDATE
            SET doc = excluded.doc, doc_compact = excluded.doc_compact,
                updated_at = excluded.updated_at;
            {upsert_keys}
        END
        $refresh$
    """)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # 阻止旧刷新函数跨过回填继续写入；只锁源表写入，检索读取不受阻。
    op.execute("LOCK TABLE dim_part, product_specs, part_alias IN SHARE ROW EXCLUSIVE MODE")
    op.execute(f"""
        CREATE FUNCTION part_search_short_keys(raw text) RETURNS text[]
        LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $keys$
            WITH normalized AS MATERIALIZED (
                SELECT part_search_normalize(raw) AS value
            ), ascii_tokens AS (
                SELECT matches[1] AS token FROM normalized,
                    LATERAL regexp_matches(value COLLATE "C", '[A-Z0-9]+', 'g') AS matches
            ), cjk_runs AS (
                SELECT matches[1] AS run FROM normalized,
                    LATERAL regexp_matches(value COLLATE "C", '[{_CJK}]+', 'g') AS matches
            ), keys AS (
                SELECT left(token, size) AS key FROM ascii_tokens,
                    LATERAL generate_series(1, least(2, char_length(token))) AS size
                UNION ALL
                SELECT substr(run, position, size) AS key FROM cjk_runs,
                    LATERAL generate_series(1, char_length(run)) AS position,
                    generate_series(1, 2) AS size
                WHERE position + size - 1 <= char_length(run)
            )
            SELECT coalesce(array_agg(DISTINCT key ORDER BY key), ARRAY[]::text[]) FROM keys
        $keys$
    """)
    op.create_table(
        "part_search_short_key",
        sa.Column("part_id", sa.Integer(), sa.ForeignKey("dim_part.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("keys", postgresql.ARRAY(sa.Text()), nullable=False),
    )
    _replace_refresh(short_keys=True)
    # 只写窄表，原投影的列、heap 和更新时间均保持基线。
    op.execute("""
        INSERT INTO part_search_short_key(part_id, keys)
        SELECT part_id, part_search_short_keys(doc) FROM part_search_text
    """)
    op.create_index(
        "ix_part_search_short_key_keys", "part_search_short_key", ["keys"],
        postgresql_using="gin", postgresql_ops={"keys": "array_ops"},
    )
    # 非 ASCII 原文才可能需要 NFKC 回退，避免短词精确匹配扫描全部型号/别名。
    op.create_index(
        "ix_part_search_non_ascii", "dim_part", ["pn_std"],
        postgresql_where=sa.text("pn_std ~ '[^ -~]'"),
    )
    op.create_index(
        "ix_alias_search_non_ascii", "part_alias", ["pn_std"],
        postgresql_where=sa.text("pn_raw ~ '[^ -~]'"),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("LOCK TABLE dim_part, product_specs, part_alias IN SHARE ROW EXCLUSIVE MODE")
    op.drop_index("ix_alias_search_non_ascii", table_name="part_alias")
    op.drop_index("ix_part_search_non_ascii", table_name="dim_part")
    # 先恢复旧函数，确保删除窄表后源表触发器仍可工作。
    _replace_refresh(short_keys=False)
    op.drop_table("part_search_short_key")
    op.execute("DROP FUNCTION part_search_short_keys(text)")

"""实时检索投影、规格键标签与同事务刷新触发器。

Revision ID: c4e6a8b1d3f5
Revises: b2f4a6c8d1e3
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c4e6a8b1d3f5"
down_revision: str | None = "b2f4a6c8d1e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 迁移冻结规格词典，不从活代码导入；契约测试防止之后只改一端。
_SPEC_KEYS = {
    "part_type": ("部件类型", None),
    "capacity": ("容量", "GB"),
    "interface": ("接口", None),
    "speed": ("接口速率", "Gbps"),
    "rpm": ("转速", "RPM"),
    "cache": ("缓存", "MB"),
    "form_factor": ("尺寸/封装", None),
    "generation": ("内存代数", None),
    "frequency": ("内存频率", "MHz"),
    "rank": ("Rank/位宽", None),
}


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _create_normalize() -> None:
    # PostgreSQL upper 依赖 libc locale，且不保证 ß→SS 等多字符映射。
    # 安装时比对 Python 映射，将差异固化；常见 ASCII/中文走原生 upper 快路径。
    mappings: dict[str, str] = {}
    for codepoint in range(0x110000):
        char = chr(codepoint)
        upper = char.upper()
        if char != upper:
            mappings[char] = upper
    sql_mappings = op.get_bind().execute(sa.text(
        "SELECT char, upper(char) FROM unnest(CAST(:chars AS text[])) AS chars(char)"
    ), {"chars": list(mappings)}).all()
    corrections = [(char, mappings[char]) for char, upper in sql_mappings
                   if upper != mappings[char]]
    correction_chars = _literal(
        "[" + "".join(char for char, _ in corrections) + "]" if corrections else "a^"
    )
    replacements = "\n".join(
        f"value := replace(value, {_literal(char)}, {_literal(upper)});"
        for char, upper in corrections
    )
    bad_spaces = "".join(chr(codepoint) for codepoint in (
        *range(9, 14), *range(28, 32), 133, 160, 5760,
        *range(8192, 8203), 8232, 8233, 8239, 8287, 12288,
    ))
    noncanonical = _literal(
        "(^ | $|  |[" + "".join(char for char, _ in corrections) + bad_spaces + "])"
    )
    # 与 Python str.split 的 Unicode White_Space + U+001C..U+001F 完全一致。
    whitespace = (
        r"U&'[\0009-\000D\001C-\0020\0085\00A0\1680"
        r"\2000-\200A\2028\2029\202F\205F\3000]+'"
    )
    op.execute(sa.text(f"""
        CREATE FUNCTION part_search_normalize(raw text) RETURNS text
        LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $normalize$
        DECLARE value text;
        BEGIN
            -- 已规范的 PN、品牌和中文字段不再分配 NFKC/空白替换结果。
            -- 差异字符仍需进入下方校正，不能只用 PostgreSQL upper 判定。
            IF raw IS NFKC NORMALIZED AND raw = upper(raw) AND raw !~ {noncanonical} THEN
                RETURN raw;
            END IF;
            value := normalize(coalesce(raw, ''), NFKC);
            IF value ~ {correction_chars} THEN
                {replacements}
            END IF;
            RETURN btrim(regexp_replace(upper(value), {whitespace}, ' ', 'g'));
        END
        $normalize$
    """))
    op.execute("""
        CREATE FUNCTION part_search_compact(raw text) RETURNS text
        LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $compact$
            SELECT regexp_replace(part_search_normalize(raw), '[^A-Z0-9]', '', 'g')
        $compact$
    """)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    labels = op.create_table(
        "spec_key_label",
        sa.Column("spec_key", sa.String(32), primary_key=True),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("unit", sa.Text(), nullable=True),
    )
    op.bulk_insert(labels, [
        {"spec_key": key, "label": label, "unit": unit}
        for key, (label, unit) in _SPEC_KEYS.items()
    ])
    op.create_table(
        "part_search_text",
        sa.Column("part_id", sa.Integer(), sa.ForeignKey("dim_part.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("doc", sa.Text(), nullable=False),
        sa.Column("doc_compact", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    _create_normalize()
    op.execute("""
        CREATE FUNCTION refresh_part_search_text(target_id integer) RETURNS void
        LANGUAGE plpgsql AS $refresh$
        DECLARE
            part_row dim_part%ROWTYPE;
            full_doc text;
        BEGIN
            -- 与 FK 的 KEY SHARE 兼容，串行化同一型号的并发规格/别名更新。
            SELECT * INTO part_row FROM dim_part WHERE id = target_id FOR NO KEY UPDATE;
            IF NOT FOUND OR part_row.status = 'merged' THEN
                DELETE FROM part_search_text WHERE part_id = target_id;
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
        END
        $refresh$
    """)
    op.execute("""
        CREATE FUNCTION trigger_part_search_text() RETURNS trigger
        LANGUAGE plpgsql AS $trigger$
        DECLARE
            old_id integer;
            new_id integer;
            target_id integer;
        BEGIN
            -- UPDATE OF 只判断语句是否提及列；再比较值，避开同值回写。
            IF TG_OP = 'UPDATE' THEN
                IF TG_TABLE_NAME = 'dim_part' THEN
                    IF ROW(OLD.pn_std, OLD.pn_raw_sample, OLD.brand, OLD.manufacturer,
                           OLD.summary, OLD.description, OLD.category_major,
                           OLD.category_minor, OLD.status)
                       IS NOT DISTINCT FROM
                       ROW(NEW.pn_std, NEW.pn_raw_sample, NEW.brand, NEW.manufacturer,
                           NEW.summary, NEW.description, NEW.category_major,
                           NEW.category_minor, NEW.status) THEN
                        RETURN NULL;
                    END IF;
                ELSIF TG_TABLE_NAME = 'product_specs' THEN
                    IF ROW(OLD.part_id, OLD.spec_key, OLD.spec_value)
                       IS NOT DISTINCT FROM ROW(NEW.part_id, NEW.spec_key, NEW.spec_value) THEN
                        RETURN NULL;
                    END IF;
                ELSE
                    IF ROW(OLD.part_id, OLD.pn_std, OLD.pn_raw, OLD.status)
                       IS NOT DISTINCT FROM ROW(NEW.part_id, NEW.pn_std, NEW.pn_raw, NEW.status) THEN
                        RETURN NULL;
                    END IF;
                END IF;
            END IF;
            IF TG_TABLE_NAME = 'dim_part' THEN
                -- 改名按当前 pn_std 重新关联无 part_id 的别名：旧名退出、新名进入。
                -- 这里只维护投影，不替业务改写别名；别名之后改名也会刷新新旧归属。
                IF TG_OP <> 'INSERT' THEN old_id := OLD.id; END IF;
                IF TG_OP <> 'DELETE' THEN new_id := NEW.id; END IF;
            ELSE
                IF TG_OP <> 'INSERT' THEN
                    old_id := OLD.part_id;
                    IF TG_TABLE_NAME = 'part_alias' AND old_id IS NULL THEN
                        SELECT id INTO old_id FROM dim_part WHERE pn_std = OLD.pn_std;
                    END IF;
                END IF;
                IF TG_OP <> 'DELETE' THEN
                    new_id := NEW.part_id;
                    IF TG_TABLE_NAME = 'part_alias' AND new_id IS NULL THEN
                        SELECT id INTO new_id FROM dim_part WHERE pn_std = NEW.pn_std;
                    END IF;
                END IF;
            END IF;
            FOR target_id IN
                SELECT DISTINCT id FROM unnest(ARRAY[old_id, new_id]) AS ids(id)
                WHERE id IS NOT NULL ORDER BY id
            LOOP
                PERFORM refresh_part_search_text(target_id);
            END LOOP;
            RETURN NULL;
        END
        $trigger$
    """)
    trigger_columns = {
        "dim_part": ("pn_std, pn_raw_sample, brand, manufacturer, summary, description, "
                     "category_major, category_minor, status"),
        "product_specs": "part_id, spec_key, spec_value",
        "part_alias": "part_id, pn_std, pn_raw, status",
    }
    for table, columns in trigger_columns.items():
        op.execute(f"""
            CREATE TRIGGER trg_{table}_search_text
            AFTER INSERT OR DELETE OR UPDATE OF {columns} ON {table}
            FOR EACH ROW EXECUTE FUNCTION trigger_part_search_text()
        """)
    op.execute("SELECT refresh_part_search_text(id) FROM dim_part ORDER BY id")
    for column in ("doc", "doc_compact"):
        op.create_index(
            f"ix_part_search_text_{column}_trgm", "part_search_text", [column],
            postgresql_using="gin", postgresql_ops={column: "gin_trgm_ops"},
        )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    for table in ("dim_part", "product_specs", "part_alias"):
        op.execute(f"DROP TRIGGER trg_{table}_search_text ON {table}")
    op.execute("DROP FUNCTION trigger_part_search_text()")
    op.execute("DROP FUNCTION refresh_part_search_text(integer)")
    op.execute("DROP FUNCTION part_search_compact(text)")
    op.execute("DROP FUNCTION part_search_normalize(text)")
    op.drop_table("part_search_text")
    op.drop_table("spec_key_label")

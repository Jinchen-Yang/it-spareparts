"""短词投影：字符契约、同事务刷新和迁移往返。"""

import os

import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import inspect, text

from app.models.dimensions import DimPart, PartAlias
from app.models.master_data import ProductSpec
from app.models.part_search import PartSearchShortKey, PartSearchText
from app.services.part_search_normalize import normalize

_PREV = "c4e6a8b1d3f5"
_HEAD = "d6f8a0b2c4e6"


def _part(db, pn="KEYS-900", **fields):
    part = DimPart(pn_std=pn, **fields)
    db.add(part)
    db.flush()
    return part


def _keys(db, part_id):
    value = db.scalar(text("SELECT keys FROM part_search_short_key WHERE part_id=:id"),
                      {"id": part_id})
    return None if value is None else set(value)


def _text_columns(conn):
    return [(column["name"], str(column["type"]), column["nullable"], column["default"])
            for column in inspect(conn).get_columns("part_search_text")]


def _doc(db, part_id):
    return db.execute(text(
        "SELECT doc, doc_compact, updated_at FROM part_search_text WHERE part_id=:id"
    ), {"id": part_id}).one_or_none()


@pytest.mark.parametrize("value, expected", [
    (None, set()),
    ("", set()),
    (" -_*?% ", set()),
    ("SAS ABS M1-12345", {"S", "SA", "A", "AB", "M", "M1", "1", "12"}),
    ("SAS SAS S SA", {"S", "SA"}),
    ("机械硬盘 内存", {"机", "械", "硬", "盘", "机械", "械硬", "硬盘", "内", "存", "内存"}),
    ("硬A盘 硬-盘", {"硬", "盘", "A"}),
    ("ＳＡＳ　Ｍ１－１２３４５ １ＴＢ", {"S", "SA", "M", "M1", "1", "12", "1T"}),
    ("㍑ Straße", {"S", "ST"}),
    ("\u3400硬\U00020000盘\U00030000", {
        "\u3400", "硬", "\U00020000", "盘", "\U00030000", "\u3400硬",
        "硬\U00020000", "\U00020000盘", "盘\U00030000",
    }),
])
def test_short_key_generator_content(db, value, expected):
    keys = db.scalar(text("SELECT part_search_short_keys(:value)"), {"value": value})
    assert set(keys) == expected
    assert len(keys) == len(expected)
    assert all(1 <= len(key) <= 2 for key in keys)
    # 查询依赖该不变量省去冗余 doc LIKE，避免短词重新读取宽文档。
    assert all(key in normalize(value) for key in keys)
    assert db.scalar(text("SELECT part_search_short_keys(:value)"),
                     {"value": normalize(value)}) == keys


def test_short_keys_follow_dim_part_updates_and_rollback(db):
    part = _part(db, brand="ＳＡＳ", description="机械硬盘")
    before = _doc(db, part.id)
    assert {"S", "SA", "硬", "盘", "硬盘"} <= _keys(db, part.id)
    nested = db.begin_nested()
    part.brand = "HP"
    part.description = "服务器内存"
    db.flush()
    changed = _keys(db, part.id)
    assert {"H", "HP", "内", "存", "内存"} <= changed
    assert not {"S", "SA", "硬", "盘", "硬盘"} & changed
    assert "服务器内存" in _doc(db, part.id).doc
    nested.rollback()
    restored = _keys(db, part.id)
    assert {"S", "SA", "硬", "盘", "硬盘"} <= restored
    assert not {"H", "HP", "内", "存", "内存"} & restored
    assert _doc(db, part.id) == before
    db.delete(part)
    db.flush()
    assert _keys(db, part.id) is None
    assert _doc(db, part.id) is None


def test_short_key_insert_rollback_removes_both_projections(db):
    nested = db.begin_nested()
    part = _part(db, description="机械硬盘")
    part_id = part.id
    assert _keys(db, part_id) is not None
    assert _doc(db, part_id) is not None
    nested.rollback()
    assert _keys(db, part_id) is None
    assert _doc(db, part_id) is None


def test_short_keys_refresh_for_spec_insert_update_move_delete(db):
    first = _part(db)
    second = _part(db, "SECOND-901")
    spec = ProductSpec(part_id=first.id, spec_key="interface", spec_value="VZ")
    db.add(spec)
    db.flush()
    assert {"接", "口", "接口", "V", "VZ"} <= _keys(db, first.id)
    spec.spec_value = "QX"
    db.flush()
    assert {"Q", "QX"} <= _keys(db, first.id)
    assert "VZ" not in _keys(db, first.id)
    spec.part_id = second.id
    db.flush()
    assert "QX" not in _keys(db, first.id)
    assert "QX" in _keys(db, second.id)
    db.delete(spec)
    db.flush()
    assert not {"接口", "QX"} & _keys(db, second.id)


def test_short_keys_refresh_for_active_aliases_and_pn_fallback(db):
    first = _part(db)
    second = _part(db, "SECOND-901")
    alias = PartAlias(pn_raw="ＶＺ－硬盘", pn_std=first.pn_std, status="pending")
    db.add(alias)
    db.flush()
    assert not {"VZ", "硬盘"} & _keys(db, first.id)
    alias.status = "active"
    db.flush()
    assert {"VZ", "硬盘"} <= _keys(db, first.id)
    alias.pn_std = second.pn_std
    db.flush()
    assert not {"VZ", "硬盘"} & _keys(db, first.id)
    assert {"VZ", "硬盘"} <= _keys(db, second.id)
    alias.part_id = first.id
    alias.pn_std = first.pn_std
    db.flush()
    assert {"VZ", "硬盘"} <= _keys(db, first.id)
    assert not {"VZ", "硬盘"} & _keys(db, second.id)
    alias.pn_raw = "QX-内存"
    db.flush()
    assert {"QX", "内存"} <= _keys(db, first.id)
    assert not {"VZ", "硬盘"} & _keys(db, first.id)
    db.delete(alias)
    db.flush()
    assert not {"QX", "内存"} & _keys(db, first.id)


def test_tombstones_remove_short_keys_and_restoring_regenerates_them(db):
    survivor = _part(db, "SURVIVOR-901")
    ghost = _part(db, description="硬盘")
    ghost.status = "merged"
    ghost.merged_into_id = survivor.id
    db.flush()
    assert _keys(db, ghost.id) is None
    assert _doc(db, ghost.id) is None
    db.add(ProductSpec(part_id=ghost.id, spec_key="interface", spec_value="QX"))
    db.flush()
    assert _keys(db, ghost.id) is None
    assert _doc(db, ghost.id) is None
    ghost.status = "active"
    ghost.merged_into_id = None
    db.flush()
    assert {"硬盘", "QX"} <= _keys(db, ghost.id)
    assert "硬盘" in _doc(db, ghost.id).doc


def test_short_key_narrow_table_and_array_ops_index_match_model(db):
    conn = db.connection()
    assert [column[0] for column in _text_columns(conn)] == [
        "part_id", "doc", "doc_compact", "updated_at",
    ]
    assert list(PartSearchText.__table__.columns.keys()) == [
        "part_id", "doc", "doc_compact", "updated_at",
    ]
    columns = {column["name"]: column for column in inspect(conn).get_columns("part_search_short_key")}
    assert list(columns) == ["part_id", "keys"]
    assert list(PartSearchShortKey.__table__.columns.keys()) == ["part_id", "keys"]
    assert not columns["keys"]["nullable"]
    assert columns["keys"]["default"] is None
    assert inspect(conn).get_pk_constraint("part_search_short_key")["constrained_columns"] == ["part_id"]
    foreign_key = inspect(conn).get_foreign_keys("part_search_short_key")[0]
    assert foreign_key["constrained_columns"] == ["part_id"]
    assert foreign_key["referred_table"] == "dim_part"
    assert foreign_key["referred_columns"] == ["id"]
    assert foreign_key["options"]["ondelete"] == "CASCADE"
    index = next(index for index in inspect(conn).get_indexes("part_search_short_key")
                 if index["name"] == "ix_part_search_short_key_keys")
    assert index["column_names"] == ["keys"]
    assert index["dialect_options"]["postgresql_using"] == "gin"
    assert db.scalar(text("""
        SELECT opc.opcname FROM pg_index i
        JOIN pg_class c ON c.oid=i.indexrelid
        JOIN pg_opclass opc ON opc.oid=i.indclass[0]
        WHERE c.relname='ix_part_search_short_key_keys'
    """)) == "array_ops"
    for table, name, predicate_column in [
        ("dim_part", "ix_part_search_non_ascii", "pn_std"),
        ("part_alias", "ix_alias_search_non_ascii", "pn_raw"),
    ]:
        partial = next(index for index in inspect(conn).get_indexes(table)
                       if index["name"] == name)
        assert partial["column_names"] == ["pn_std"]
        predicate = str(partial["dialect_options"]["postgresql_where"])
        assert predicate_column in predicate
        assert "[^ -~]" in predicate
        assert "status" not in predicate
        assert conn.scalar(text("""
            SELECT am.amname FROM pg_class c JOIN pg_am am ON am.oid=c.relam
            WHERE c.relname=:name
        """), {"name": name}) == "btree"


def test_short_keys_migration_backfill_and_upgrade_downgrade(db):
    engine = db.get_bind()
    db.close()
    cfg = AlembicConfig(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(os.path.dirname(__file__), "..", "alembic"))
    try:
        alembic_command.downgrade(cfg, _PREV)
        with engine.begin() as conn:
            before_columns = _text_columns(conn)
            assert "part_search_short_key" not in inspect(conn).get_table_names()
            for table, name in [("dim_part", "ix_part_search_non_ascii"),
                                ("part_alias", "ix_alias_search_non_ascii")]:
                assert name not in {index["name"] for index in inspect(conn).get_indexes(table)}
            part_id = conn.scalar(text(
                "INSERT INTO dim_part(pn_std, description) VALUES ('ＳＡＳ－９００', '机械硬盘') RETURNING id"
            ))
            before = conn.execute(text(
                "SELECT doc, doc_compact, updated_at FROM part_search_text WHERE part_id=:id"
            ), {"id": part_id}).one()
            # 墓碑不会出现在旧投影里，回填也不能把它重新加入短词投影。
            ghost_id = conn.scalar(text(
                "INSERT INTO dim_part(pn_std, status, merged_into_id) "
                "VALUES ('KEYS-GHOST-900', 'merged', :survivor_id) RETURNING id"
            ), {"survivor_id": part_id})
            before_heap = conn.scalar(text("SELECT pg_relation_size('part_search_text')"))
        alembic_command.upgrade(cfg, _HEAD)
        with engine.begin() as conn:
            assert _text_columns(conn) == before_columns
            assert conn.scalar(text("SELECT pg_relation_size('part_search_text')")) == before_heap
            for table, name in [("dim_part", "ix_part_search_non_ascii"),
                                ("part_alias", "ix_alias_search_non_ascii")]:
                assert name in {index["name"] for index in inspect(conn).get_indexes(table)}
            after = conn.execute(text(
                "SELECT doc, doc_compact, updated_at FROM part_search_text WHERE part_id=:id"
            ), {"id": part_id}).one()
            assert after == before
            assert conn.scalar(text(
                "SELECT keys FROM part_search_short_key WHERE part_id=:id"
            ), {"id": ghost_id}) is None
            assert {"S", "SA", "9", "90", "硬盘"} <= set(conn.scalar(text(
                "SELECT keys FROM part_search_short_key WHERE part_id=:id"
            ), {"id": part_id}))
            conn.execute(text("UPDATE dim_part SET description='内存' WHERE id=:id"), {"id": part_id})
            assert "内存" in conn.scalar(text(
                "SELECT keys FROM part_search_short_key WHERE part_id=:id"
            ), {"id": part_id})
        alembic_command.downgrade(cfg, _PREV)
        with engine.begin() as conn:
            assert conn.scalar(text("SELECT to_regprocedure('part_search_short_keys(text)')")) is None
            assert _text_columns(conn) == before_columns
            assert "part_search_short_key" not in inspect(conn).get_table_names()
            for table, name in [("dim_part", "ix_part_search_non_ascii"),
                                ("part_alias", "ix_alias_search_non_ascii")]:
                assert name not in {index["name"] for index in inspect(conn).get_indexes(table)}
            conn.execute(text("UPDATE dim_part SET description='降级后硬盘' WHERE id=:id"), {"id": part_id})
            assert "降级后硬盘" in conn.scalar(text(
                "SELECT doc FROM part_search_text WHERE part_id=:id"
            ), {"id": part_id})
    finally:
        alembic_command.upgrade(cfg, "head")

"""实时检索投影：源字段、同事务刷新、重建与归一化契约。"""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from time import monotonic, sleep

import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.models.dimensions import DimPart, PartAlias
from app.models.master_data import ProductSpec
from app.services.part_search_normalize import compact, normalize
from app.services.spec_extract import SPEC_KEYS
from scripts.rebuild_part_search_text import rebuild

_PREV = "b2f4a6c8d1e3"
_HEAD = "c4e6a8b1d3f5"


def _part(db, pn="PROJECTION-001", **kwargs):
    part = DimPart(pn_std=pn, **kwargs)
    db.add(part)
    db.flush()
    return part


def _doc(db, part_id):
    return db.execute(text(
        "SELECT doc, doc_compact FROM part_search_text WHERE part_id=:id"
    ), {"id": part_id}).mappings().one_or_none()


@pytest.mark.parametrize("value", [
    None, "", "   \t\r\n", "  ａｂＣ ０２３１－ＡＣ４Ｌ　容量  ",
    "straße ﬃ ı ſ ᾀ ΐ ǰ", "A\u00a0B\u0085C\u001cD\u2028E\u3000F",
    "SAS_12%\\x *? 1.2TB", "e\u0301 É ㍑ ① ²",
    "机械硬盘 容量 SAS 1200GB", " AA  BB \t CC", "ᾈ",
])
def test_python_and_sql_normalization_match(db, value):
    row = db.execute(text(
        "SELECT part_search_normalize(:value), part_search_compact(:value)"
    ), {"value": value}).one()
    assert tuple(row) == (normalize(value), compact(value))


def test_python_and_sql_unicode_case_mapping_match(db):
    # 不只覆盖 ASCII；捕获 libc 的简单大写和 Python 完整大写映射的差异。
    value = " ".join(chr(codepoint) for codepoint in range(0x110000)
                     if chr(codepoint).upper() != chr(codepoint))
    assert db.scalar(text("SELECT part_search_normalize(:value)"),
                     {"value": value}) == normalize(value)


def test_spec_key_labels_exactly_match_code(db):
    rows = db.execute(text("SELECT spec_key, label, unit FROM spec_key_label")).all()
    assert {key: (label, unit) for key, label, unit in rows} == SPEC_KEYS


def test_projection_includes_all_fields_and_active_aliases(db):
    part = _part(
        db, pn="0231-AC4L", pn_raw_sample="ｒａｗ－ｓａｍｐｌｅ", brand="品牌华为",
        manufacturer="制造商 Seagate", summary="中文简介", description="详细描述",
        category_major="存储大类", category_minor="机械小类",
    )
    db.add_all([
        PartAlias(pn_raw="别名-active", pn_std=part.pn_std, part_id=part.id, status="active"),
        PartAlias(pn_raw="别名-pending", pn_std=part.pn_std, part_id=part.id, status="pending"),
        PartAlias(pn_raw="别名-rejected", pn_std=part.pn_std, part_id=part.id, status="rejected"),
        PartAlias(pn_raw="别名-legacy", pn_std=part.pn_std, status="active"),
        ProductSpec(part_id=part.id, spec_key="capacity", spec_value="1200GB"),
        ProductSpec(part_id=part.id, spec_key="interface", spec_value="SAS"),
        ProductSpec(part_id=part.id, spec_key="rpm", spec_value="10K"),
    ])
    db.flush()
    row = _doc(db, part.id)
    for term in [part.pn_std, "RAW-SAMPLE", "品牌华为", "制造商 SEAGATE", "中文简介",
                 "详细描述", "存储大类", "机械小类", "别名-ACTIVE", "别名-LEGACY",
                 "容量 1200GB GB", "接口 SAS", "转速 10K RPM"]:
        assert term in row["doc"]
    assert "PENDING" not in row["doc"]
    assert "REJECTED" not in row["doc"]
    assert "0231AC4L" in row["doc_compact"]
    assert row["doc_compact"] == compact(row["doc"])


def test_dim_part_insert_update_delete_refreshes_same_transaction(db):
    part = _part(db, manufacturer="旧厂商", summary="旧简介")
    assert "旧厂商" in _doc(db, part.id)["doc"]
    part.manufacturer = "新厂商"
    part.summary = "新简介"
    part.description = "新描述"
    db.flush()
    assert "旧厂商" not in _doc(db, part.id)["doc"]
    assert "新厂商 新简介 新描述" in _doc(db, part.id)["doc"]
    part_id = part.id
    db.delete(part)
    db.flush()
    assert _doc(db, part_id) is None


def test_specs_insert_update_move_delete_refreshes_both_parts(db):
    first = _part(db, pn="SPEC-FIRST")
    second = _part(db, pn="SPEC-SECOND")
    spec = ProductSpec(part_id=first.id, spec_key="capacity", spec_value="1234GB")
    db.add(spec)
    db.flush()
    assert "容量 1234GB GB" in _doc(db, first.id)["doc"]
    spec.spec_value = "5678GB"
    db.flush()
    assert "1234GB" not in _doc(db, first.id)["doc"]
    assert "5678GB" in _doc(db, first.id)["doc"]
    spec.part_id = second.id
    db.flush()
    assert "5678GB" not in _doc(db, first.id)["doc"]
    assert "5678GB" in _doc(db, second.id)["doc"]
    db.delete(spec)
    db.flush()
    assert "5678GB" not in _doc(db, second.id)["doc"]


def test_alias_status_update_move_delete_refreshes_both_parts(db):
    first = _part(db, pn="ALIAS-FIRST")
    second = _part(db, pn="ALIAS-SECOND")
    alias = PartAlias(pn_raw="UNIQUE-ALIAS", pn_std=first.pn_std,
                      part_id=first.id, status="pending")
    db.add(alias)
    db.flush()
    assert "UNIQUE-ALIAS" not in _doc(db, first.id)["doc"]
    alias.status = "active"
    db.flush()
    assert "UNIQUE-ALIAS" in _doc(db, first.id)["doc"]
    alias.pn_raw = "RENAMED-ALIAS"
    db.flush()
    assert "UNIQUE-ALIAS" not in _doc(db, first.id)["doc"]
    assert "RENAMED-ALIAS" in _doc(db, first.id)["doc"]
    alias.part_id = second.id
    alias.pn_std = second.pn_std
    db.flush()
    assert "RENAMED-ALIAS" not in _doc(db, first.id)["doc"]
    assert "RENAMED-ALIAS" in _doc(db, second.id)["doc"]
    alias.status = "rejected"
    db.flush()
    assert "RENAMED-ALIAS" not in _doc(db, second.id)["doc"]
    alias.status = "active"
    db.flush()
    db.delete(alias)
    db.flush()
    assert "RENAMED-ALIAS" not in _doc(db, second.id)["doc"]


def test_merged_tombstone_has_no_projection(db):
    target = _part(db, pn="MERGE-TARGET")
    source = _part(db, pn="MERGE-SOURCE")
    source.status = "merged"
    source.merged_into_id = target.id
    db.flush()
    assert _doc(db, source.id) is None
    db.add(ProductSpec(part_id=source.id, spec_key="capacity", spec_value="999GB"))
    db.flush()
    assert _doc(db, source.id) is None
    assert _doc(db, target.id) is not None


def test_rebuild_is_repeatable_and_dry_run_does_not_write(db):
    first = _part(db, pn="REBUILD-FIRST")
    second = _part(db, pn="REBUILD-SECOND")
    db.execute(text("DELETE FROM part_search_text WHERE part_id=:id"), {"id": first.id})
    assert rebuild(db, dry_run=True) == {"dry_run": True, "rows": 2, "stale_rows": 0}
    assert _doc(db, first.id) is None
    assert rebuild(db) == {"dry_run": False, "rows": 2, "stale_rows": 0}
    first_docs = [_doc(db, first.id), _doc(db, second.id)]
    rebuild(db)
    assert [_doc(db, first.id), _doc(db, second.id)] == first_docs
    assert db.scalar(text("SELECT count(*) FROM part_search_text")) == 2


def test_rebuild_cli_dry_run_and_repeated_execution(db):
    part = _part(db, pn="REBUILD-CLI")
    part_id = part.id
    db.execute(text("DELETE FROM part_search_text WHERE part_id=:id"), {"id": part_id})
    db.commit()

    def run(*args):
        result = subprocess.run(
            [sys.executable, "scripts/rebuild_part_search_text.py", *args],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
            check=True,
        )
        return json.loads(result.stdout)

    assert run("--dry-run") == {"dry_run": True, "rows": 1, "stale_rows": 0}
    assert _doc(db, part_id) is None
    assert run() == {"dry_run": False, "rows": 1, "stale_rows": 0}
    assert _doc(db, part_id) is not None
    assert run() == {"dry_run": False, "rows": 1, "stale_rows": 0}
    assert db.scalar(text("SELECT count(*) FROM part_search_text")) == 1


def test_concurrent_child_updates_do_not_lose_committed_projection(db):
    part = _part(db, pn="CONCURRENT-PROJECTION")
    db.add_all([
        ProductSpec(part_id=part.id, spec_key="capacity", spec_value="OLD-CAPACITY"),
        ProductSpec(part_id=part.id, spec_key="interface", spec_value="OLD-INTERFACE"),
    ])
    db.commit()
    part_id = part.id
    engine = db.get_bind()
    started = Event()
    worker_pid = []

    def update_interface():
        with Session(engine) as other:
            worker_pid.append(other.scalar(text("SELECT pg_backend_pid()")))
            started.set()
            other.execute(text(
                "UPDATE product_specs SET spec_value='NEW-INTERFACE'"
                " WHERE part_id=:id AND spec_key='interface'"
            ), {"id": part_id})
            other.commit()

    db.execute(text(
        "UPDATE product_specs SET spec_value='NEW-CAPACITY'"
        " WHERE part_id=:id AND spec_key='capacity'"
    ), {"id": part_id})
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(update_interface)
        try:
            assert started.wait(3)
            deadline = monotonic() + 3
            waiting = False
            while monotonic() < deadline:
                waiting = db.scalar(text(
                    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid=:pid AND NOT granted)"
                ), {"pid": worker_pid[0]})
                if waiting:
                    break
                sleep(0.01)
            assert waiting, "第二个子表写入应等待同一型号的投影刷新锁"
        finally:
            db.commit()
        future.result(timeout=5)
    doc = _doc(db, part_id)["doc"]
    assert "NEW-CAPACITY" in doc
    assert "NEW-INTERFACE" in doc
    assert "OLD-CAPACITY" not in doc
    assert "OLD-INTERFACE" not in doc


def test_migration_downgrade_upgrade_and_existing_row_backfill(db):
    engine = db.get_bind()
    db.close()
    cfg = AlembicConfig(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(os.path.dirname(__file__), "..", "alembic"))
    try:
        alembic_command.downgrade(cfg, _PREV)
        with engine.begin() as conn:
            assert "part_search_text" not in inspect(conn).get_table_names()
            assert "spec_key_label" not in inspect(conn).get_table_names()
            part_id = conn.scalar(text(
                "INSERT INTO dim_part(pn_std, manufacturer) VALUES ('BACKFILL-001', '回填厂商')"
                " RETURNING id"
            ))
            conn.execute(text(
                "INSERT INTO product_specs(part_id, spec_key, spec_value)"
                " VALUES (:id, 'interface', 'BACKFILL-SAS')"
            ), {"id": part_id})
        alembic_command.upgrade(cfg, _HEAD)
        with engine.connect() as conn:
            doc = conn.scalar(text("SELECT doc FROM part_search_text WHERE part_id=:id"),
                              {"id": part_id})
            assert "回填厂商" in doc
            assert "接口 BACKFILL-SAS" in doc
            indexes = inspect(conn).get_indexes("part_search_text")
            assert {index["name"] for index in indexes} == {
                "ix_part_search_text_doc_trgm", "ix_part_search_text_doc_compact_trgm",
            }
            assert all(index["dialect_options"]["postgresql_using"] == "gin" for index in indexes)
        alembic_command.downgrade(cfg, _PREV)
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT to_regprocedure('part_search_normalize(text)')")) is None
            assert conn.scalar(text("SELECT to_regprocedure('refresh_part_search_text(integer)')")) is None
    finally:
        alembic_command.upgrade(cfg, "head")

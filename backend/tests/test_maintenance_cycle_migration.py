"""D-0058：待修池迁移从 86f7486b9ed5 替换空的一件一行表，有行则拒绝。"""
from __future__ import annotations

import json
import os

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import DatabaseError, IntegrityError

from app import permissions
from app.db import engine

REVISION = "c3e8a1d94b27"
CYCLE_REVISION = "86f7486b9ed5"
BASELINE = "98afc0d2157b"
CYCLE_PAGE_KEYS = (
    "page_cycle_recycle",
    "page_cycle_test",
    "page_cycle_repair",
    "page_cycle_manage",
    "page_cycle_wip",
)
BUILTIN_ROLES = ("admin", "boss", "sales", "purchaser", "readonly")


def _cfg() -> AlembicConfig:
    cfg = AlembicConfig(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option(
        "script_location",
        os.path.join(os.path.dirname(__file__), "..", "alembic"),
    )
    return cfg


def _current_head() -> str:
    return ScriptDirectory.from_config(_cfg()).get_current_head()


def _db_version() -> str:
    with engine.connect() as connection:
        return connection.scalar(text("SELECT version_num FROM alembic_version"))


def _ensure_head() -> None:
    if _db_version() != _current_head():
        alembic_command.upgrade(_cfg(), "head")


def _set_timeouts(conn) -> None:
    conn.execute(text("SET LOCAL lock_timeout = '2000ms'"))
    conn.execute(text("SET LOCAL statement_timeout = '30000ms'"))


def _insert_part(conn, pn: str) -> int:
    return conn.execute(
        text("INSERT INTO dim_part (pn_std) VALUES (:pn) RETURNING id"),
        {"pn": pn},
    ).scalar_one()


def test_single_head_is_repair_pool_revision():
    assert _current_head() == REVISION


def test_cycle_page_keys_registered_and_fail_closed_by_default():
    for key in CYCLE_PAGE_KEYS:
        assert key in permissions.PAGE_KEYS
        assert key in permissions.CYCLE_PAGE_KEYS
        assert key in permissions.ACCOUNT_SCOPED_BETA_PAGE_KEYS
        assert key in permissions.LABELS
        meta = permissions.PERMISSION_META[key]
        assert meta["label"] == permissions.LABELS[key]
        for field in ("label", "summary", "can", "cannot", "typical", "sensitivity", "risk"):
            assert meta.get(field), f"{key} 缺业务语言字段 {field}"
    full = permissions._full()
    for key in CYCLE_PAGE_KEYS:
        assert full[key] is False
        for role in BUILTIN_ROLES:
            assert permissions.ROLE_TEMPLATES[role].get(key, False) is False
            assert permissions.effective(role, None)[key] is False
    page_group = next(group for group in permissions.UI_GROUPS if group["key"] == "page")
    assert set(CYCLE_PAGE_KEYS) <= set(page_group["keys"])


def test_admin_requires_explicit_account_snapshot_for_cycle_pages():
    class _User:
        role = "admin"
        permissions = None
        template_perms = None
        perm_overrides = None

    no_snapshot = permissions.effective_for_user(_User())
    for key in CYCLE_PAGE_KEYS:
        assert no_snapshot[key] is False
    granted = _User()
    granted.template_perms = {key: True for key in CYCLE_PAGE_KEYS}
    granted.perm_overrides = {}
    effective = permissions.effective_for_user(granted)
    for key in CYCLE_PAGE_KEYS:
        assert effective[key] is True


def test_migrated_db_templates_default_cycle_pages_false(db):
    with engine.connect() as conn:
        rows = dict(conn.execute(text("SELECT code, permissions FROM sys_role_template")).all())
    for role in BUILTIN_ROLES:
        for key in CYCLE_PAGE_KEYS:
            assert rows[role].get(key) is False


def test_repair_pool_schema_replaces_unit_tables(db):
    inspector = inspect(engine)
    assert inspector.has_table("maintenance_repair_record")
    assert inspector.has_table("maintenance_repair_record_source")
    assert inspector.has_table("maintenance_repair_command")
    assert inspector.has_table("maintenance_cycle_sell_intent")
    assert not inspector.has_table("maintenance_cycle_unit")
    assert not inspector.has_table("maintenance_cycle_event")
    record_columns = {column["name"] for column in inspector.get_columns("maintenance_repair_record")}
    assert {
        "record_id", "wire_unit_id", "record_no", "part_id", "pn",
        "fixed_qty", "scrap_qty", "operator", "status", "shop_adjusted_manually",
        "version", "created_at",
    } <= record_columns
    intent_columns = {column["name"] for column in inspector.get_columns("maintenance_cycle_sell_intent")}
    assert "record_id" in intent_columns and "wire_unit_id" in intent_columns
    assert "unit_id" not in intent_columns
    with engine.connect() as conn:
        view = conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'v_repair_pool' ORDER BY ordinal_position"
            )
        ).scalars().all()
    assert view == ["part_id", "pending_qty", "shop_qty", "scrap_qty"]


def test_empty_cycle_revision_upgrades_and_downgrades(db):
    db.rollback()
    cfg = _cfg()
    try:
        alembic_command.downgrade(cfg, CYCLE_REVISION)
        assert _db_version() == CYCLE_REVISION
        inspector = inspect(engine)
        assert inspector.has_table("maintenance_cycle_unit")
        assert inspector.has_table("maintenance_cycle_event")
        assert not inspector.has_table("maintenance_repair_record")
        alembic_command.upgrade(cfg, REVISION)
        assert _db_version() == REVISION
        inspector = inspect(engine)
        assert inspector.has_table("maintenance_repair_record")
        assert not inspector.has_table("maintenance_cycle_unit")
    finally:
        _ensure_head()


def test_legacy_unit_row_blocks_upgrade_and_remains(db):
    db.rollback()
    cfg = _cfg()
    try:
        alembic_command.downgrade(cfg, CYCLE_REVISION)
        with engine.begin() as conn:
            _set_timeouts(conn)
            part_id = _insert_part(conn, "PN-REPAIR-MIG-UNIT")
            conn.execute(
                text(
                    "INSERT INTO maintenance_cycle_unit"
                    " (part_id, pn, source_kind, created_by, updated_by)"
                    " VALUES (:part_id, 'PN-REPAIR-MIG-UNIT', 'external', 'mig', 'mig')"
                ),
                {"part_id": part_id},
            )
        with pytest.raises(DatabaseError, match="refusing repair-pool replacement"):
            alembic_command.upgrade(cfg, REVISION)
        assert _db_version() == CYCLE_REVISION
        with engine.connect() as conn:
            count = conn.execute(text("SELECT count(*) FROM maintenance_cycle_unit")).scalar_one()
        assert count == 1
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM maintenance_cycle_unit"))
        alembic_command.upgrade(cfg, REVISION)
        assert _db_version() == REVISION
    finally:
        _ensure_head()


def test_legacy_work_item_blocks_upgrade_and_remains(db):
    db.rollback()
    cfg = _cfg()
    try:
        alembic_command.downgrade(cfg, CYCLE_REVISION)
        with engine.begin() as conn:
            _set_timeouts(conn)
            conn.execute(
                text(
                    "INSERT INTO work_item (subject_type, subject_id, pool_key, status)"
                    " VALUES ('cycle_test', 1, 'cycle_test', 'pending')"
                )
            )
        with pytest.raises(DatabaseError, match="refusing repair-pool replacement"):
            alembic_command.upgrade(cfg, REVISION)
        with engine.connect() as conn:
            count = conn.execute(
                text(
                    "SELECT count(*) FROM work_item"
                    " WHERE subject_type IN ('cycle_test', 'cycle_repair')"
                )
            ).scalar_one()
        assert count == 1
        with engine.begin() as conn:
            conn.execute(
                text(
                    "DELETE FROM work_item"
                    " WHERE subject_type IN ('cycle_test', 'cycle_repair')"
                )
            )
        alembic_command.upgrade(cfg, REVISION)
    finally:
        _ensure_head()


def test_repair_record_row_blocks_downgrade_and_remains(db):
    db.rollback()
    cfg = _cfg()
    try:
        with engine.begin() as conn:
            _set_timeouts(conn)
            part_id = _insert_part(conn, "PN-REPAIR-MIG-RECORD")
            conn.execute(
                text(
                    "INSERT INTO maintenance_repair_record"
                    " (record_id, wire_unit_id, record_no, part_id, pn,"
                    "  fixed_qty, scrap_qty, operator)"
                    " VALUES (gen_random_uuid(), 1, 'WX00000001', :part_id,"
                    "         'PN-REPAIR-MIG-RECORD', 1, 0, 'mig')"
                ),
                {"part_id": part_id},
            )
        with pytest.raises(DatabaseError, match="refusing repair-pool downgrade"):
            alembic_command.downgrade(cfg, CYCLE_REVISION)
        assert _db_version() == REVISION
        with engine.connect() as conn:
            count = conn.execute(
                text("SELECT count(*) FROM maintenance_repair_record")
            ).scalar_one()
        assert count == 1
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM maintenance_repair_record"))
    finally:
        _ensure_head()


def test_downgrade_to_baseline_still_removes_cycle_page_keys(db):
    db.rollback()
    cfg = _cfg()
    try:
        alembic_command.downgrade(cfg, BASELINE)
        assert _db_version() == BASELINE
        inspector = inspect(engine)
        assert not inspector.has_table("maintenance_cycle_unit")
        assert not inspector.has_table("maintenance_repair_record")
        with engine.begin() as conn:
            _set_timeouts(conn)
            permissions_json = conn.execute(
                text("SELECT permissions FROM sys_role_template WHERE code = 'admin'")
            ).scalar_one()
            assert all(key not in permissions_json for key in CYCLE_PAGE_KEYS)
            conn.execute(
                text(
                    "UPDATE sys_role_template SET permissions = permissions || "
                    "CAST(:blob AS jsonb) WHERE code = 'admin'"
                ),
                {"blob": json.dumps({"page_cycle_test": True})},
            )
        alembic_command.upgrade(cfg, "head")
        assert _db_version() == REVISION
        with engine.connect() as conn:
            permissions_json = conn.execute(
                text("SELECT permissions FROM sys_role_template WHERE code = 'admin'")
            ).scalar_one()
        assert all(permissions_json[key] is False for key in CYCLE_PAGE_KEYS)
    finally:
        _ensure_head()


def test_null_template_perms_legacy_account_not_snapshotted(db):
    from types import SimpleNamespace

    db.rollback()
    cfg = _cfg()
    try:
        alembic_command.downgrade(cfg, BASELINE)
        with engine.begin() as conn:
            _set_timeouts(conn)
            conn.execute(
                text(
                    "INSERT INTO sys_user"
                    " (username, role, display_name, password_hash, is_active,"
                    "  template_perms, perm_overrides, permissions)"
                    " VALUES ('f01-legacy-null', 'sales', '旧账号', 'x', true,"
                    "         NULL, NULL, CAST(:p AS jsonb))"
                ),
                {"p": json.dumps({"page_parts": True})},
            )
        alembic_command.upgrade(cfg, "head")
        with engine.connect() as conn:
            template_perms, perm_overrides, legacy = conn.execute(
                text(
                    "SELECT template_perms, perm_overrides, permissions"
                    " FROM sys_user WHERE username = 'f01-legacy-null'"
                )
            ).one()
        assert template_perms is None
        assert perm_overrides is None
        assert legacy["page_parts"] is True
        for key in CYCLE_PAGE_KEYS:
            assert legacy[key] is False
        user = SimpleNamespace(
            role="sales",
            permissions=legacy,
            template_perms=None,
            perm_overrides=None,
            is_active=True,
        )
        effective = permissions.effective_for_user(user)
        assert effective["page_parts"] is True
        alembic_command.downgrade(cfg, BASELINE)
        with engine.connect() as conn:
            template_perms2, legacy2 = conn.execute(
                text(
                    "SELECT template_perms, permissions FROM sys_user"
                    " WHERE username = 'f01-legacy-null'"
                )
            ).one()
        assert template_perms2 is None
        assert legacy2 == {"page_parts": True}
    finally:
        _ensure_head()


def test_record_no_keeps_full_wire_unit_id_past_eight_digits(db):
    from app.services.maintenance_repair import format_record_no

    assert format_record_no(100_000_000) == "WX100000000"
    part_id = db.scalar(
        text("INSERT INTO dim_part (pn_std) VALUES ('PN-BIG-WIRE') RETURNING id")
    )
    db.execute(
        text(
            "INSERT INTO maintenance_repair_record"
            " (record_id, wire_unit_id, record_no, part_id, pn,"
            "  fixed_qty, scrap_qty, operator)"
            " VALUES (gen_random_uuid(), 100000000, 'WX100000000', :part_id,"
            "         'PN-BIG-WIRE', 1, 0, 'big')"
        ),
        {"part_id": part_id},
    )
    db.flush()
    with pytest.raises(IntegrityError):
        with db.begin_nested():
            db.execute(
                text(
                    "INSERT INTO maintenance_repair_record"
                    " (record_id, wire_unit_id, record_no, part_id, pn,"
                    "  fixed_qty, scrap_qty, operator)"
                    " VALUES (gen_random_uuid(), 100000001, 'WX10000000', :part_id,"
                    "         'PN-BIG-WIRE', 1, 0, 'trunc')"
                ),
                {"part_id": part_id},
            )
    db.rollback()

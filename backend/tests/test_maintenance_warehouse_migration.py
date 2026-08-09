"""Schema lifecycle and fail-stop downgrade for warehouse business history."""

from __future__ import annotations

import os

import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app import permissions
from app.db import engine


REVISION = "a6d1e9c3b7f2"
PREVIOUS = "f4b8c2d1e7a6"


def _cfg() -> AlembicConfig:
    config = AlembicConfig(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    config.set_main_option(
        "script_location", os.path.join(os.path.dirname(__file__), "..", "alembic")
    )
    return config


def test_schema_has_all_fact_tables_and_database_guards(db):
    tables = set(db.scalars(text(
        "SELECT tablename FROM pg_tables WHERE schemaname = current_schema() "
        "AND tablename LIKE 'maintenance_warehouse_%'"
    )))
    assert tables == {
        "maintenance_warehouse_import_batch",
        "maintenance_warehouse_document",
        "maintenance_warehouse_document_line",
        "maintenance_warehouse_document_link",
        "maintenance_warehouse_ambiguity",
        "maintenance_warehouse_audit_event",
    }
    triggers = set(db.scalars(text(
        "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal "
        "AND tgname LIKE 'trg_maintenance_warehouse_%'"
    )))
    assert triggers == {
        "trg_maintenance_warehouse_import_batch_immutable",
        "trg_maintenance_warehouse_document_immutable",
        "trg_maintenance_warehouse_document_line_immutable",
        "trg_maintenance_warehouse_document_link_supersession",
        "trg_maintenance_warehouse_audit_event_immutable",
        "trg_maintenance_warehouse_ambiguity_resolution",
    }
    constraints = set(db.scalars(text(
        "SELECT conname FROM pg_constraint "
        "WHERE conrelid = 'maintenance_warehouse_document_link'::regclass"
    )))
    assert "ck_maintenance_wh_link_target_matrix" in constraints


def test_permission_is_high_risk_page_bound_and_default_admin_only():
    key = "action_maintenance_warehouse_manage"
    assert key in permissions.ACTION_KEYS
    assert key in permissions.HIGH_RISK_KEYS
    assert permissions.ACTION_PAGE_DEPENDENCIES[key] == "page_maintenance"
    assert permissions.effective("admin", None)[key] is True
    for role in ("boss", "sales", "purchaser", "readonly", "guest"):
        assert permissions.effective(role, None)[key] is False


def test_empty_schema_downgrades_and_reupgrades(db):
    db.close()
    config = _cfg()
    alembic_command.downgrade(config, PREVIOUS)
    try:
        with engine.connect() as connection:
            assert connection.scalar(text(
                "SELECT to_regclass('maintenance_warehouse_import_batch')"
            )) is None
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == PREVIOUS
        alembic_command.upgrade(config, "head")
        with engine.connect() as connection:
            assert connection.scalar(text(
                "SELECT to_regclass('maintenance_warehouse_import_batch')"
            )) == "maintenance_warehouse_import_batch"
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == REVISION
    finally:
        alembic_command.upgrade(config, "head")


def test_nonempty_batch_blocks_downgrade_before_any_ddl(db):
    db.execute(text(
        """
        INSERT INTO maintenance_warehouse_import_batch
          (import_id, source_file_hash, source_filename, adapter_key,
           adapter_version, version_state, header_signature, header_pairs_json,
           status, document_count, line_count, ambiguity_count, result_json,
           reason, applied_by)
        VALUES
          ('00000000-0000-0000-0000-000000000209', repeat('a', 64),
           'synthetic.xlsx', 'shipment', 'shipment_v1', 'known', repeat('b', 64),
           '[]'::jsonb, 'applied', 0, 0, 0, '{}'::jsonb,
           'synthetic downgrade guard', 'synthetic-admin')
        """
    ))
    db.commit()
    try:
        with pytest.raises(DBAPIError, match="downgrade blocked"):
            alembic_command.downgrade(_cfg(), PREVIOUS)
    finally:
        alembic_command.upgrade(_cfg(), "head")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == REVISION
        assert connection.scalar(text(
            "SELECT count(*) FROM maintenance_warehouse_import_batch"
        )) == 1

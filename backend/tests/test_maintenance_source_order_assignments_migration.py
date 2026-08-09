"""Schema and rollback contract for manual source-order assignments (#201)."""

import os

import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError

from app.db import engine


_PREV = "c4e8a1d7f2b6"
_HEAD = "e6f1a9c3b7d2"
_TABLE = "maintenance_source_order_assignment"


def _cfg() -> AlembicConfig:
    cfg = AlembicConfig(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option(
        "script_location",
        os.path.join(os.path.dirname(__file__), "..", "alembic"),
    )
    return cfg


def test_source_assignment_schema_has_history_and_active_uniqueness(db):
    inspector = inspect(db.get_bind())
    assert _TABLE in inspector.get_table_names()
    assert {
        "assignment_id",
        "source_order_id",
        "project_id",
        "is_active",
        "version",
        "created_by",
        "created_at",
        "archived_by",
        "archived_at",
    } == {column["name"] for column in inspector.get_columns(_TABLE)}
    indexes = {index["name"]: index for index in inspector.get_indexes(_TABLE)}
    assert indexes["ux_maintenance_source_assignment_active_order"]["unique"] is True
    assert indexes["ix_maintenance_source_assignment_project_active"]["unique"] is False
    constraints = {
        constraint["name"]
        for constraint in inspector.get_check_constraints(_TABLE)
    }
    assert {
        "ck_maintenance_source_assignment_version",
        "ck_maintenance_source_assignment_creator",
        "ck_maintenance_source_assignment_archive_state",
    } <= constraints
    audit_constraint = db.execute(
        text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'ck_maintenance_project_audit_entity_type'"
        )
    ).scalar_one()
    assert "source_order_assignment" in audit_constraint


def test_empty_source_assignment_schema_downgrades_and_upgrades(db):
    db.close()
    cfg = _cfg()
    alembic_command.downgrade(cfg, _PREV)
    try:
        with engine.connect() as connection:
            assert _TABLE not in inspect(connection).get_table_names()
            audit_constraint = connection.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = 'ck_maintenance_project_audit_entity_type'"
                )
            ).scalar_one()
            assert "source_order_assignment" not in audit_constraint
        alembic_command.upgrade(cfg, _HEAD)
        with engine.connect() as connection:
            assert _TABLE in inspect(connection).get_table_names()
    finally:
        alembic_command.upgrade(cfg, "head")


def test_downgrade_blocks_nonempty_assignment_history(db):
    db.execute(
        text(
            "INSERT INTO sys_import_batch "
            "(filename, file_type, file_hash, status) VALUES "
            "('source-assignment-migration.xlsx', 'maintenance', "
            "'source-assignment-migration-hash', 'success')"
        )
    )
    batch_id = db.execute(
        text(
            "SELECT id FROM sys_import_batch "
            "WHERE file_hash = 'source-assignment-migration-hash'"
        )
    ).scalar_one()
    db.execute(
        text(
            "INSERT INTO f_maintenance_order "
            "(raw_order_id, order_no, project_raw, project_std, import_batch_id) "
            "VALUES ('WBDD-MIGRATION-SOURCE-001', 'WBDD-MIGRATION-001', "
            "'迁移保护原始项目', '迁移保护原始项目', :batch_id)"
        ),
        {"batch_id": batch_id},
    )
    db.execute(
        text(
            "INSERT INTO maintenance_project "
            "(project_id, project_code, display_name, lifecycle_status) VALUES "
            "('source-assignment-migration-project', 'MAINT-MIGRATION-201', "
            "'迁移保护稳定项目', 'missing')"
        )
    )
    db.execute(
        text(
            "INSERT INTO maintenance_source_order_assignment "
            "(assignment_id, source_order_id, project_id, created_by) VALUES "
            "('source-assignment-migration-row', 'WBDD-MIGRATION-SOURCE-001', "
            "'source-assignment-migration-project', 'migration-test')"
        )
    )
    db.commit()
    db.close()

    cfg = _cfg()
    try:
        with pytest.raises(DBAPIError, match="downgrade blocked"):
            alembic_command.downgrade(cfg, _PREV)
        with engine.connect() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version"))\
                .scalar_one() == _HEAD
            assert connection.execute(
                text("SELECT count(*) FROM maintenance_source_order_assignment")
            ).scalar_one() == 1
    finally:
        alembic_command.upgrade(cfg, "head")

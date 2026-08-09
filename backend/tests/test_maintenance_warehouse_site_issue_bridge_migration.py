"""Combined schema contract for the exact warehouse-to-site-issue bridge."""

from __future__ import annotations

import os

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError

from app.db import engine


DOWNGRADE_TARGET = "f5a7c9e1b3d4"
PARENTS = {
    "a6d1e9c3b7f2",
    "c2f7a9d4e6b1",
    "e6f1a9c3b7d2",
    DOWNGRADE_TARGET,
}


def _cfg() -> AlembicConfig:
    cfg = AlembicConfig(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option(
        "script_location",
        os.path.join(os.path.dirname(__file__), "..", "alembic"),
    )
    return cfg


def test_bridge_schema_has_adapter_audit_union_and_wide_source_identity(db):
    inspector = inspect(db.get_bind())
    delivery_columns = {
        column["name"]: column
        for column in inspector.get_columns("maintenance_site_issue_delivery_source")
    }
    issue_line_columns = {
        column["name"]: column
        for column in inspector.get_columns("maintenance_site_issue_line")
    }
    assert delivery_columns["source_line_id"]["type"].length == 128
    assert delivery_columns["delivery_no"]["type"].length == 128
    assert issue_line_columns["source_line_id"]["type"].length == 128

    delivery_adapter = db.scalar(
        text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'ck_maintenance_site_issue_delivery_adapter'"
        )
    )
    warehouse_audit = db.scalar(
        text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'ck_maintenance_wh_audit_action'"
        )
    )
    project_audit = db.scalar(
        text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'ck_maintenance_project_audit_entity_type'"
        )
    )
    assert "warehouse_shipment_v1" in delivery_adapter
    assert "integration_reconciled" in warehouse_audit
    assert "manager_assignment" in project_audit
    assert "source_order_assignment" in project_audit


def test_empty_combined_bridge_downgrades_and_reupgrades(db):
    db.close()
    cfg = _cfg()
    with engine.connect() as connection:
        versions_before = set(connection.scalars(text("SELECT version_num FROM alembic_version")))
    alembic_command.downgrade(cfg, DOWNGRADE_TARGET)
    try:
        with engine.connect() as connection:
            versions = set(connection.scalars(text("SELECT version_num FROM alembic_version")))
            assert versions == PARENTS
        alembic_command.upgrade(cfg, "head")
        with engine.connect() as connection:
            versions = set(connection.scalars(text("SELECT version_num FROM alembic_version")))
            assert versions == versions_before
    finally:
        alembic_command.upgrade(cfg, "head")


def test_integration_audit_history_blocks_bridge_revision_downgrade(db):
    db.execute(
        text(
            "INSERT INTO maintenance_warehouse_audit_event "
            "(event_id, action, before_json, after_json, reason, operated_by) VALUES "
            "('00000000-0000-0000-0000-000000000913', "
            "'integration_reconciled', NULL, '{}'::jsonb, "
            "'synthetic downgrade guard', 'synthetic-admin')"
        )
    )
    db.commit()
    db.close()
    with engine.connect() as connection:
        versions_before = set(connection.scalars(text("SELECT version_num FROM alembic_version")))
        tables_before = set(inspect(connection).get_table_names())
        replenishment_permission_before = connection.execute(
            text(
                "SELECT permissions ->> 'page_replenishment_beta' "
                "FROM sys_role_template WHERE code = 'admin'"
            )
        ).scalar_one()
    with pytest.raises(DBAPIError, match="downgrade blocked"):
        alembic_command.downgrade(_cfg(), DOWNGRADE_TARGET)
    with engine.connect() as connection:
        assert set(connection.scalars(text("SELECT version_num FROM alembic_version"))) == versions_before
        assert set(inspect(connection).get_table_names()) == tables_before
        assert connection.execute(
            text(
                "SELECT permissions ->> 'page_replenishment_beta' "
                "FROM sys_role_template WHERE code = 'admin'"
            )
        ).scalar_one() == replenishment_permission_before

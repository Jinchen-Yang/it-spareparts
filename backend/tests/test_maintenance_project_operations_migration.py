"""Real migration checks for stable-project operating facts and workbook state."""

import os

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import inspect

from app.db import engine


_PREV = "d8a3c7e4f2b1"
_TABLES = {
    "maintenance_collection_snapshot",
    "maintenance_project_operation_audit",
    "maintenance_site_issue",
    "maintenance_site_issue_line",
    "maintenance_project_expense_attribution",
    "maintenance_project_workbook_state",
    "maintenance_project_workbook_operation",
    "maintenance_project_workbook_validation",
}


def _cfg() -> AlembicConfig:
    cfg = AlembicConfig(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option(
        "script_location", os.path.join(os.path.dirname(__file__), "..", "alembic")
    )
    return cfg


def test_operating_fact_schema_contains_evidence_and_server_validation(db):
    inspector = inspect(db.get_bind())
    assert _TABLES <= set(inspector.get_table_names())
    cost_columns = {
        column["name"]
        for column in inspector.get_columns("maintenance_site_issue_line")
    }
    assert {
        "manual_unit_cost",
        "manual_evidence",
        "price_basis",
        "reference_samples",
        "algorithm_version",
    } <= cost_columns
    validation_columns = {
        column["name"]
        for column in inspector.get_columns("maintenance_project_workbook_validation")
    }
    assert {
        "expected_revision",
        "plan_json",
        "issues_json",
        "error_workbook",
        "expires_at",
        "applied_at",
    } <= validation_columns


def test_operating_fact_empty_schema_downgrades_and_upgrades(db):
    db.close()
    cfg = _cfg()
    alembic_command.downgrade(cfg, _PREV)
    try:
        with engine.connect() as connection:
            assert not (_TABLES & set(inspect(connection).get_table_names()))
        alembic_command.upgrade(cfg, "head")
        with engine.connect() as connection:
            assert _TABLES <= set(inspect(connection).get_table_names())
    finally:
        alembic_command.upgrade(cfg, "head")

"""Real migration checks for stable-project operating facts and workbook state."""

import os

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import inspect, text

from app.db import engine


_PREV = "d8a3c7e4f2b1"
_PRE_PROVENANCE = "f3b7d9e1c5a2"
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
    collection_columns = {
        column["name"]
        for column in inspector.get_columns("maintenance_collection_snapshot")
    }
    assert {"source", "import_batch_id"} <= collection_columns
    site_issue_columns = {
        column["name"]
        for column in inspector.get_columns("maintenance_site_issue")
    }
    assert {"source", "import_batch_id"} <= site_issue_columns
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
    state_columns = {
        column["name"]
        for column in inspector.get_columns("maintenance_project_workbook_state")
    }
    assert "expense_ready_through" in state_columns
    operation_columns = {
        column["name"]
        for column in inspector.get_columns("maintenance_project_workbook_operation")
    }
    assert "entity_id" in operation_columns


def test_provenance_migration_backfills_legacy_facts_and_round_trips(db):
    db.close()
    cfg = _cfg()
    alembic_command.downgrade(cfg, _PRE_PROVENANCE)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO maintenance_project "
                    "(project_id, project_code, display_name, lifecycle_status) "
                    "VALUES ('migration-provenance-project', "
                    "'MIGRATION-PROVENANCE', '迁移血缘项目', 'ongoing')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO maintenance_project_contract "
                    "(project_contract_id, project_id, contract_id, contract_no, "
                    "contract_amount, status_mapping_state, status_mapping_version, "
                    "included_in_total, effective_from, source) VALUES "
                    "('migration-provenance-contract', 'migration-provenance-project', "
                    "'migration-contract', 'XS-MIGRATION-PROVENANCE', 1000, "
                    "'mapped', 'migration-map-v1', true, DATE '2026-01-01', 'legacy-test')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO maintenance_collection_snapshot "
                    "(collection_id, project_id, project_contract_id, report_month, "
                    "cumulative_amount, status) VALUES "
                    "('migration-collection', 'migration-provenance-project', "
                    "'migration-provenance-contract', DATE '2026-08-01', 320, 'confirmed')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO maintenance_site_issue "
                    "(issue_id, project_id, issue_no, issue_date, raw_status, "
                    "status_mapping_state, normalized_status, status_mapping_version) "
                    "VALUES ('migration-site-issue', 'migration-provenance-project', "
                    "'ISSUE-MIGRATION', DATE '2026-08-01', 'legacy-confirmed', "
                    "'mapped', 'confirmed', 'migration-map-v1')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO maintenance_project_workbook_operation "
                    "(project_id, export_id, file_sha256, operation_key, payload_hash, "
                    "operation_type, operated_by) VALUES "
                    "('migration-provenance-project', 'migration-export', "
                    f"'{('a' * 64)}', 'migration-operation', '{('b' * 64)}', "
                    "'collection_create', 'migration-test')"
                )
            )

        alembic_command.upgrade(cfg, "head")
        with engine.connect() as connection:
            collection = connection.execute(
                text(
                    "SELECT source, import_batch_id "
                    "FROM maintenance_collection_snapshot "
                    "WHERE collection_id = 'migration-collection'"
                )
            ).one()
            site_issue = connection.execute(
                text(
                    "SELECT source, import_batch_id FROM maintenance_site_issue "
                    "WHERE issue_id = 'migration-site-issue'"
                )
            ).one()
            operation_entity = connection.execute(
                text(
                    "SELECT entity_id FROM maintenance_project_workbook_operation "
                    "WHERE operation_key = 'migration-operation'"
                )
            ).scalar_one()
            assert tuple(collection) == ("legacy", None)
            assert tuple(site_issue) == ("legacy", None)
            assert operation_entity is None

        alembic_command.downgrade(cfg, _PRE_PROVENANCE)
        with engine.connect() as connection:
            inspector = inspect(connection)
            assert "source" not in {
                column["name"]
                for column in inspector.get_columns(
                    "maintenance_collection_snapshot"
                )
            }
            assert connection.execute(
                text(
                    "SELECT count(*) FROM maintenance_collection_snapshot "
                    "WHERE collection_id = 'migration-collection'"
                )
            ).scalar_one() == 1

        alembic_command.upgrade(cfg, "head")
        with engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT source FROM maintenance_collection_snapshot "
                    "WHERE collection_id = 'migration-collection'"
                )
            ).scalar_one() == "legacy"
    finally:
        alembic_command.upgrade(cfg, "head")


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

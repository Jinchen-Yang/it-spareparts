"""Real migration checks for stable-project operating facts and workbook state."""

import os
from datetime import date
from decimal import Decimal

import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.db import engine
from app.security import UserContext
from app.services import maintenance_project_operations as operations_service


_PREV = "d8a3c7e4f2b1"
_PRE_PROVENANCE = "f3b7d9e1c5a2"
_PRE_DUAL_TAX = "a4c9e1f2b6d8"
_PRE_STATUS_PAIR = "b7d2f4a6c8e1"
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
        "manual_unit_cost_inc_tax",
        "unit_cost_ex_tax",
        "unit_cost_inc_tax",
        "cost_amount_ex_tax",
        "cost_amount_inc_tax",
        "tax_rate_used",
        "manual_evidence",
        "price_basis",
        "reference_samples",
        "algorithm_version",
    } <= cost_columns
    expense_columns = {
        column["name"]
        for column in inspector.get_columns(
            "maintenance_project_expense_attribution"
        )
    }
    assert {"amount_ex_tax", "amount_inc_tax", "tax_rate_used"} <= expense_columns
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


def test_operation_audit_rows_are_database_append_only(db):
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO maintenance_project "
                "(project_id, project_code, display_name, lifecycle_status) "
                "VALUES ('migration-audit-project', 'MIGRATION-AUDIT', "
                "'迁移审计项目', 'ongoing')"
            )
        )
        audit_id = connection.execute(
            text(
                "INSERT INTO maintenance_project_operation_audit "
                "(project_id, entity_type, entity_id, action, before_json, "
                "after_json, reason, operated_by) VALUES "
                "('migration-audit-project', 'site_issue', 'issue-001', "
                "'cost_recomputed', NULL, CAST('{\"cost\": 10}' AS jsonb), "
                "'append-only migration test', 'migration-test') "
                "RETURNING id"
            )
        ).scalar_one()

    with pytest.raises(DBAPIError, match="append-only"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE maintenance_project_operation_audit "
                    "SET reason = 'rewritten' WHERE id = :audit_id"
                ),
                {"audit_id": audit_id},
            )

    with pytest.raises(DBAPIError, match="append-only"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "DELETE FROM maintenance_project_operation_audit "
                    "WHERE id = :audit_id"
                ),
                {"audit_id": audit_id},
            )

    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT reason, after_json FROM "
                "maintenance_project_operation_audit WHERE id = :audit_id"
            ),
            {"audit_id": audit_id},
        ).one()
    assert row.reason == "append-only migration test"
    assert row.after_json == {"cost": 10}


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


def test_dual_tax_migration_backfills_and_round_trips(db):
    db.close()
    cfg = _cfg()
    alembic_command.downgrade(cfg, _PRE_DUAL_TAX)
    try:
        with engine.begin() as connection:
            part_id = connection.execute(
                text(
                    "INSERT INTO dim_part (pn_std) VALUES ('PN-MIGRATION-DUAL-TAX') "
                    "RETURNING id"
                )
            ).scalar_one()
            connection.execute(
                text(
                    "INSERT INTO maintenance_project "
                    "(project_id, project_code, display_name, lifecycle_status) "
                    "VALUES ('migration-dual-tax-project', "
                    "'MIGRATION-DUAL-TAX', '迁移双税项目', 'ongoing')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO maintenance_site_issue "
                    "(issue_id, project_id, issue_no, issue_date, raw_status, "
                    "status_mapping_state, normalized_status, status_mapping_version) "
                    "VALUES ('migration-dual-tax-issue', "
                    "'migration-dual-tax-project', 'ISSUE-MIGRATION-DUAL-TAX', "
                    "DATE '2026-08-01', 'confirmed', 'mapped', 'confirmed', "
                    "'migration-map-v1')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO maintenance_site_issue_line "
                    "(issue_line_id, issue_id, line_no, part_id, pn, quantity, "
                    "manual_unit_cost, manual_evidence, unit_cost, cost_amount, "
                    "cost_source, algorithm_version) VALUES "
                    "('migration-dual-tax-line', 'migration-dual-tax-issue', 1, "
                    ":part_id, 'PN-MIGRATION-DUAL-TAX', 2, 5, 'legacy evidence', "
                    "10, 20, 'manual', 'legacy-v1')"
                ),
                {"part_id": part_id},
            )
            connection.execute(
                text(
                    "INSERT INTO maintenance_project_expense_attribution "
                    "(expense_id, project_id, expense_ref, expense_date, "
                    "amount_ex_tax, raw_status, status_mapping_state, "
                    "normalized_status, status_mapping_version) VALUES "
                    "('migration-dual-tax-expense', 'migration-dual-tax-project', "
                    "'BX-MIGRATION-DUAL-TAX', DATE '2026-08-01', 50, "
                    "'approved', 'mapped', 'approved', 'migration-map-v1')"
                )
            )

        # The predecessor allowed a legacy result whose stored amount did not
        # equal quantity × unit cost.  The new invariant must fail closed with
        # a useful message, not silently rewrite historical accounting facts.
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO maintenance_site_issue_line "
                    "(issue_line_id, issue_id, line_no, part_id, pn, quantity, "
                    "unit_cost, cost_amount, cost_source, algorithm_version) "
                    "VALUES ('migration-dual-tax-invalid-line', "
                    "'migration-dual-tax-issue', 2, :part_id, "
                    "'PN-MIGRATION-DUAL-TAX', 2, 10, 19, 'manual', 'legacy-v1')"
                ),
                {"part_id": part_id},
            )
        with pytest.raises(DBAPIError, match="legacy cost_amount does not equal"):
            alembic_command.upgrade(cfg, "head")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "DELETE FROM maintenance_site_issue_line "
                    "WHERE issue_line_id = 'migration-dual-tax-invalid-line'"
                )
            )

        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE maintenance_project_expense_attribution "
                    "SET amount_ex_tax = 884955752212.39 "
                    "WHERE expense_id = 'migration-dual-tax-expense'"
                )
            )
        with pytest.raises(DBAPIError, match="cannot be represented"):
            alembic_command.upgrade(cfg, "head")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE maintenance_project_expense_attribution "
                    "SET amount_ex_tax = 50 "
                    "WHERE expense_id = 'migration-dual-tax-expense'"
                )
            )

        alembic_command.upgrade(cfg, "head")
        with engine.connect() as connection:
            line = connection.execute(
                text(
                    "SELECT manual_unit_cost_inc_tax, unit_cost_ex_tax, "
                    "unit_cost_inc_tax, cost_amount_ex_tax, cost_amount_inc_tax, "
                    "tax_rate_used FROM maintenance_site_issue_line "
                    "WHERE issue_line_id = 'migration-dual-tax-line'"
                )
            ).one()
            assert tuple(line) == (
                Decimal("5.65"),
                Decimal("10.00"),
                Decimal("11.30"),
                Decimal("20.00"),
                Decimal("22.60"),
                Decimal("0.1300"),
            )
            expense = connection.execute(
                text(
                    "SELECT amount_ex_tax, amount_inc_tax, tax_rate_used "
                    "FROM maintenance_project_expense_attribution "
                    "WHERE expense_id = 'migration-dual-tax-expense'"
                )
            ).one()
            assert tuple(expense) == (
                Decimal("50.00"),
                Decimal("56.50"),
                Decimal("0.1300"),
            )

        alembic_command.downgrade(cfg, _PRE_DUAL_TAX)
        with engine.connect() as connection:
            inspector = inspect(connection)
            assert "unit_cost_inc_tax" not in {
                column["name"]
                for column in inspector.get_columns("maintenance_site_issue_line")
            }
            assert connection.execute(
                text(
                    "SELECT unit_cost, cost_amount FROM maintenance_site_issue_line "
                    "WHERE issue_line_id = 'migration-dual-tax-line'"
                )
            ).one() == (Decimal("10.00"), Decimal("20.00"))

        alembic_command.upgrade(cfg, "head")
        with engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT cost_amount_inc_tax FROM maintenance_site_issue_line "
                    "WHERE issue_line_id = 'migration-dual-tax-line'"
                )
            ).scalar_one() == Decimal("22.60")
    finally:
        alembic_command.upgrade(cfg, "head")


def test_strict_status_pair_migration_preserves_legacy_anomalies_and_blocks_new(db):
    db.close()
    cfg = _cfg()
    alembic_command.downgrade(cfg, _PRE_STATUS_PAIR)
    try:
        with engine.begin() as connection:
            part_id = connection.execute(
                text(
                    "INSERT INTO dim_part (pn_std) "
                    "VALUES ('PN-MIGRATION-STATUS-PAIR') RETURNING id"
                )
            ).scalar_one()
            connection.execute(
                text(
                    "INSERT INTO maintenance_project "
                    "(project_id, project_code, display_name, lifecycle_status) "
                    "VALUES ('migration-status-pair-project', "
                    "'MIGRATION-STATUS-PAIR', '迁移状态对项目', 'ongoing')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO maintenance_site_issue "
                    "(issue_id, project_id, issue_no, issue_date, raw_status, "
                    "status_mapping_state, normalized_status, status_mapping_version) "
                    "VALUES ('migration-status-pair-issue', "
                    "'migration-status-pair-project', 'ISSUE-STATUS-PAIR', "
                    "DATE '2026-08-01', 'legacy-unknown', 'mapped', 'unknown', "
                    "'legacy-map-v1')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO maintenance_site_issue "
                    "(issue_id, project_id, issue_no, issue_date, raw_status, "
                    "status_mapping_state, normalized_status, status_mapping_version) "
                    "VALUES ('migration-status-pair-issue-multi', "
                    "'migration-status-pair-project', 'ISSUE-STATUS-PAIR-MULTI', "
                    "DATE '2026-08-01', 'legacy-unknown', 'mapped', 'unknown', "
                    "'legacy-map-v1')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO maintenance_site_issue_line "
                    "(issue_line_id, issue_id, line_no, part_id, pn, quantity, "
                    "algorithm_version) VALUES "
                    "('migration-status-pair-line-1', "
                    "'migration-status-pair-issue-multi', 1, :part_id, "
                    "'PN-MIGRATION-STATUS-PAIR', 1, 'legacy-v1'), "
                    "('migration-status-pair-line-2', "
                    "'migration-status-pair-issue-multi', 2, :part_id, "
                    "'PN-MIGRATION-STATUS-PAIR', 2, 'legacy-v1')"
                ),
                {"part_id": part_id},
            )
            connection.execute(
                text(
                    "INSERT INTO maintenance_project_expense_attribution "
                    "(expense_id, project_id, expense_ref, expense_date, "
                    "amount_ex_tax, amount_inc_tax, tax_rate_used, raw_status, "
                    "status_mapping_state, normalized_status, status_mapping_version) "
                    "VALUES ('migration-status-pair-expense', "
                    "'migration-status-pair-project', 'BX-STATUS-PAIR', "
                    "DATE '2026-08-01', 10, 11.30, 0.13, 'legacy-unknown', "
                    "'mapped', 'unknown', 'legacy-map-v1')"
                )
            )

        alembic_command.upgrade(cfg, "head")
        with engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT count(*) "
                    "FROM maintenance_site_issue "
                    "WHERE project_id = 'migration-status-pair-project' "
                    "AND status_mapping_state = 'mapped' "
                    "AND normalized_status = 'unknown'"
                )
            ).scalar_one() == 2
            assert connection.execute(
                text(
                    "SELECT status_mapping_state, normalized_status "
                    "FROM maintenance_project_expense_attribution "
                    "WHERE expense_id = 'migration-status-pair-expense'"
                )
            ).one() == ("mapped", "unknown")

        with Session(engine) as session:
            workspace = operations_service.project_workspace(
                session,
                project_id="migration-status-pair-project",
                as_of=date(2026, 8, 31),
                user_ctx=UserContext(
                    user_id="migration-status-pair-reviewer",
                    role="admin",
                    permissions=None,
                ),
            )
            assert workspace is not None
            assert workspace["project"]["metrics"]["cost_complete"] is False
            assert {row["code"] for row in workspace["completeness"]["issues"]} >= {
                "unmapped_site_issue_status",
                "unmapped_expense_status",
            }
            site_issue = next(
                row
                for row in workspace["completeness"]["issues"]
                if row["code"] == "unmapped_site_issue_status"
            )
            assert site_issue["line_count"] == 2

            directory = operations_service.project_operations(
                session,
                as_of=date(2026, 8, 31),
                user_ctx=UserContext(
                    user_id="migration-status-pair-reviewer",
                    role="admin",
                    permissions=None,
                ),
                q_text="MIGRATION-STATUS-PAIR",
                reminder="completeness:unmapped_site_issue_status",
            )
            assert directory["total"] == 1
            assert directory["rows"][0]["metrics"]["cost_complete"] is False
            assert directory["rows"][0]["metrics"]["cost_status"] == "unknown"

        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO maintenance_project_expense_attribution "
                        "(expense_id, project_id, expense_ref, expense_date, "
                        "amount_ex_tax, amount_inc_tax, tax_rate_used, raw_status, "
                        "status_mapping_state, normalized_status, "
                        "status_mapping_version) VALUES "
                        "('migration-status-pair-new-invalid', "
                        "'migration-status-pair-project', 'BX-STATUS-PAIR-INVALID', "
                        "DATE '2026-08-02', 10, 11.30, 0.13, 'new-unknown', "
                        "'mapped', 'unknown', 'new-map-v1')"
                    )
                )

        alembic_command.downgrade(cfg, _PRE_STATUS_PAIR)
        with engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT normalized_status "
                    "FROM maintenance_project_expense_attribution "
                    "WHERE expense_id = 'migration-status-pair-expense'"
                )
            ).scalar_one() == "unknown"
        alembic_command.upgrade(cfg, "head")
    finally:
        alembic_command.upgrade(cfg, "head")

"""Schema, permission, and downgrade safety for maintenance cutover controls."""

import os
from pathlib import Path

import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError

from app import permissions
from app.config import Settings, get_settings
from app.db import engine


_PREV = "e6a9c3f1b2d4"
_ROOT = Path(__file__).resolve().parents[2]
_TABLES = {
    "maintenance_migration_run",
    "maintenance_project_cutover_plan",
    "maintenance_historical_cost_baseline",
    "maintenance_inventory_opening_balance",
    "maintenance_migration_discrepancy",
    "maintenance_migration_event",
}


def _cfg() -> AlembicConfig:
    cfg = AlembicConfig(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option(
        "script_location", os.path.join(os.path.dirname(__file__), "..", "alembic")
    )
    return cfg


def _insert_preview_run(connection, *, run_id: str) -> None:
    connection.execute(
        text(
            """
            INSERT INTO maintenance_migration_run
                (run_id, idempotency_key, request_fingerprint, rule_version,
                 source_snapshot_hash, business_as_of, status, preview_json,
                 created_by)
            VALUES
                (:run_id, :idempotency_key, :fingerprint,
                 'maintenance-cutover-v1', :snapshot_hash, DATE '2026-08-10',
                 'previewed',
                 CAST('{"can_approve": true}' AS jsonb), 'migration-test')
            """
        ),
        {
            "run_id": run_id,
            "idempotency_key": f"key-{run_id}",
            "fingerprint": "a" * 64,
            "snapshot_hash": "b" * 64,
        },
    )


def _insert_project_plan(
    connection, *, run_id: str, project_id: str, plan_id: str
) -> None:
    _insert_preview_run(connection, run_id=run_id)
    connection.execute(
        text(
            """
            INSERT INTO maintenance_project
                (project_id, project_code, display_name, lifecycle_status)
            VALUES
                (:project_id, :project_code, '迁移约束合成项目', 'ongoing')
            """
        ),
        {"project_id": project_id, "project_code": project_id.upper()},
    )
    connection.execute(
        text(
            """
            INSERT INTO maintenance_project_cutover_plan
                (plan_id, run_id, project_id, cutover_date, business_as_of,
                 historical_mode, source_snapshot_hash, input_fingerprint,
                 truth_comparison_hash, historical_cost_ex_tax,
                 historical_cost_inc_tax, post_cutover_cost_ex_tax,
                 post_cutover_cost_inc_tax, approved_expense_ex_tax,
                 approved_expense_inc_tax, sales_estimate_cost_ex_tax,
                 sales_estimate_cost_inc_tax, sales_estimate_lines,
                 cost_progress_includes_sales_estimate, cost_progress_label,
                 total_cost_ex_tax, total_cost_inc_tax, blocker_count, status)
            VALUES
                (:plan_id, :run_id, :project_id, DATE '2026-08-01',
                 DATE '2026-08-10', 'approved_cost_baseline', :source_hash,
                 :input_hash, :truth_hash, 100.00, 113.00, 0.00, 0.00,
                 0.00, 0.00, 0.00, 0.00, 0, FALSE,
                 'priced_cost_without_sales_estimate', 100.00, 113.00, 0,
                 'previewed')
            """
        ),
        {
            "plan_id": plan_id,
            "run_id": run_id,
            "project_id": project_id,
            "source_hash": "c" * 64,
            "input_hash": "d" * 64,
            "truth_hash": "e" * 64,
        },
    )


def _insert_baseline(
    connection, *, baseline_id: str, plan_id: str, project_id: str, **overrides
) -> None:
    values = {
        "baseline_id": baseline_id,
        "plan_id": plan_id,
        "project_id": project_id,
        "amount_ex_tax": "100.00",
        "amount_inc_tax": "113.00",
        "evidence_hash": "a" * 64,
        "coverage_from": "2025-01-01",
        "coverage_through": "2026-07-31",
        "scope": "site_issue_parts_only",
        "excludes_expenses": True,
        "source_artifact_locator": "artifact://migration/schema/history.xlsx",
        "source_row_count": 10,
        "aggregation_fingerprint": "f" * 64,
    }
    values.update(overrides)
    connection.execute(
        text(
            """
            INSERT INTO maintenance_historical_cost_baseline
                (baseline_id, plan_id, project_id, amount_ex_tax,
                 amount_inc_tax, evidence_hash, coverage_from,
                 coverage_through, scope, excludes_expenses,
                 source_artifact_locator, source_row_count,
                 aggregation_fingerprint, approval_state)
            VALUES
                (:baseline_id, :plan_id, :project_id, :amount_ex_tax,
                 :amount_inc_tax, :evidence_hash, :coverage_from,
                 :coverage_through, :scope, :excludes_expenses,
                 :source_artifact_locator, :source_row_count,
                 :aggregation_fingerprint, 'pending')
            """
        ),
        values,
    )


def test_migration_control_schema_and_permission_are_fail_closed(db):
    inspector = inspect(db.get_bind())
    assert _TABLES <= set(inspector.get_table_names())

    run_columns = {
        column["name"] for column in inspector.get_columns("maintenance_migration_run")
    }
    assert {
        "request_fingerprint",
        "source_snapshot_hash",
        "business_as_of",
        "manifest_json",
        "manifest_hash",
        "manifest_key_id",
        "created_by",
        "reconciled_by",
        "approved_by",
        "version",
    } <= run_columns
    run_checks = {
        constraint["name"]: constraint["sqltext"]
        for constraint in inspector.get_check_constraints("maintenance_migration_run")
    }
    assert "ck_maintenance_migration_run_independent_reconciliation" in run_checks
    plan_uniques = {
        constraint["name"]: constraint["column_names"]
        for constraint in inspector.get_unique_constraints(
            "maintenance_project_cutover_plan"
        )
    }
    assert plan_uniques["uq_maintenance_project_cutover_run_project"] == [
        "run_id",
        "project_id",
    ]
    plan_columns = {
        column["name"]
        for column in inspector.get_columns("maintenance_project_cutover_plan")
    }
    assert {
        "business_as_of",
        "truth_comparison_hash",
        "sales_estimate_cost_ex_tax",
        "sales_estimate_cost_inc_tax",
        "sales_estimate_lines",
        "cost_progress_includes_sales_estimate",
        "cost_progress_label",
    } <= plan_columns
    baseline_columns = {
        column["name"]
        for column in inspector.get_columns("maintenance_historical_cost_baseline")
    }
    assert {
        "coverage_from",
        "coverage_through",
        "scope",
        "excludes_expenses",
        "source_artifact_locator",
        "source_row_count",
        "aggregation_fingerprint",
    } <= baseline_columns
    baseline_checks = {
        constraint["name"]: constraint["sqltext"]
        for constraint in inspector.get_check_constraints(
            "maintenance_historical_cost_baseline"
        )
    }
    assert {
        "ck_maintenance_historical_baseline_amounts",
        "ck_maintenance_historical_baseline_coverage",
        "ck_maintenance_historical_baseline_identity",
    } <= set(baseline_checks)

    key = "action_maintenance_migration_review"
    assert key in permissions.ACTION_KEYS
    assert key in permissions.HIGH_RISK_KEYS
    assert permissions.ACTION_PAGE_DEPENDENCIES[key] == "page_maintenance"
    assert permissions.ACTION_DATA_DEPENDENCIES[key] == "data_profit"
    assert permissions.effective("admin", None)[key] is True
    for role in ("boss", "sales", "purchaser", "readonly", "guest"):
        assert permissions.effective(role, None)[key] is False
    assert get_settings().maintenance_cutover_enabled is False
    signing_key_id, signing_key = get_settings().maintenance_manifest_signing_material()
    assert signing_key_id
    assert len(signing_key) >= 32
    assert signing_key != get_settings().secret_key.encode("utf-8")

    rotated = Settings(
        _env_file=None,
        maintenance_manifest_active_key_id="v2",
        maintenance_manifest_active_hmac_key="a" * 32,
        maintenance_manifest_previous_hmac_keys_json=(
            '{"v1":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}'
        ),
    )
    assert set(rotated.maintenance_manifest_verification_keys()) == {"v1", "v2"}

    shared_secret = "shared-secret-material-that-is-long-enough"
    with pytest.raises(ValueError, match="独立于 SECRET_KEY"):
        Settings(
            _env_file=None,
            secret_key=shared_secret,
            maintenance_manifest_active_hmac_key=shared_secret,
        )
    with pytest.raises(ValueError, match="历史密钥必须独立"):
        Settings(
            _env_file=None,
            secret_key=shared_secret,
            maintenance_manifest_previous_hmac_keys_json=(
                '{"v0":"shared-secret-material-that-is-long-enough"}'
            ),
        )


def test_cutover_gate_is_explicitly_wired_and_defaults_off():
    root_env = (_ROOT / ".env.example").read_text(encoding="utf-8")
    backend_env = (_ROOT / "backend" / ".env.example").read_text(encoding="utf-8")
    compose = (_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "MAINTENANCE_CUTOVER_ENABLED=false" in root_env
    assert "MAINTENANCE_CUTOVER_ENABLED=false" in backend_env
    assert (
        "MAINTENANCE_CUTOVER_ENABLED: ${MAINTENANCE_CUTOVER_ENABLED:-false}"
    ) in compose
    assert get_settings().maintenance_cutover_enabled is False


def test_database_rejects_creator_as_reconciler(db):
    with engine.begin() as connection:
        _insert_preview_run(connection, run_id="self-reconcile-run")

    with pytest.raises(DBAPIError, match="independent_reconciliation"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE maintenance_migration_run "
                    "SET status = 'reconciled', reconciled_by = created_by, "
                    "reconciled_at = now() "
                    "WHERE run_id = 'self-reconcile-run'"
                )
            )

    with engine.begin() as connection:
        connection.execute(
            text(
                "DELETE FROM maintenance_migration_run "
                "WHERE run_id = 'self-reconcile-run'"
            )
        )


def test_database_rejects_invalid_historical_baseline_contract(db):
    run_id = "baseline-constraint-run"
    project_id = "baseline-constraint-project"
    plan_id = "baseline-constraint-plan"
    with engine.begin() as connection:
        _insert_project_plan(
            connection,
            run_id=run_id,
            project_id=project_id,
            plan_id=plan_id,
        )

    invalid_cases = [
        (
            "bad-tax",
            "ck_maintenance_historical_baseline_amounts",
            {"amount_inc_tax": "114.00"},
        ),
        (
            "empty-range",
            "ck_maintenance_historical_baseline_coverage",
            {"coverage_from": "2026-08-01"},
        ),
        (
            "wrong-scope",
            "ck_maintenance_historical_baseline_coverage",
            {"scope": "all_project_costs"},
        ),
        (
            "includes-expenses",
            "ck_maintenance_historical_baseline_coverage",
            {"excludes_expenses": False},
        ),
        (
            "blank-artifact",
            "ck_maintenance_historical_baseline_coverage",
            {"source_artifact_locator": " "},
        ),
        (
            "too-many-rows",
            "ck_maintenance_historical_baseline_coverage",
            {"source_row_count": 10_000_001},
        ),
        (
            "short-fingerprint",
            "ck_maintenance_historical_baseline_identity",
            {"aggregation_fingerprint": "short"},
        ),
    ]
    for case_id, constraint_name, overrides in invalid_cases:
        with pytest.raises(DBAPIError, match=constraint_name):
            with engine.begin() as connection:
                _insert_baseline(
                    connection,
                    baseline_id=f"baseline-{case_id}",
                    plan_id=plan_id,
                    project_id=project_id,
                    **overrides,
                )


def test_migration_event_is_database_append_only(db):
    with engine.begin() as connection:
        _insert_preview_run(connection, run_id="append-only-run")
        connection.execute(
            text(
                """
                INSERT INTO maintenance_migration_event
                    (event_id, operation_key, run_id, action, from_status, to_status,
                     payload_json, reason, operated_by)
                VALUES
                    ('append-only-event', 'append-only-operation',
                     'append-only-run', 'preview', NULL,
                     'previewed', '{}'::jsonb, '建立 dry-run', 'migration-test')
                """
            )
        )

    with pytest.raises(DBAPIError, match="append-only"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE maintenance_migration_event "
                    "SET reason = 'rewritten' WHERE event_id = 'append-only-event'"
                )
            )

    with pytest.raises(DBAPIError, match="append-only"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "DELETE FROM maintenance_migration_event "
                    "WHERE event_id = 'append-only-event'"
                )
            )


def test_downgrade_fails_closed_when_control_history_exists(db):
    db.close()
    cfg = _cfg()
    with engine.begin() as connection:
        _insert_preview_run(connection, run_id="downgrade-blocker-run")
    with engine.connect() as connection:
        versions_before = set(
            connection.scalars(text("SELECT version_num FROM alembic_version"))
        )
        replenishment_tables_before = {
            name
            for name in inspect(connection).get_table_names()
            if name.startswith("replenishment_")
        }
        beta_permission_before = connection.execute(
            text(
                "SELECT permissions ->> 'page_maintenance_beta' "
                "FROM sys_role_template WHERE code = 'admin'"
            )
        ).scalar_one()

    with pytest.raises(DBAPIError, match="history is not empty"):
        alembic_command.downgrade(cfg, _PREV)

    with engine.begin() as connection:
        assert set(
            connection.scalars(text("SELECT version_num FROM alembic_version"))
        ) == versions_before
        assert {
            name
            for name in inspect(connection).get_table_names()
            if name.startswith("replenishment_")
        } == replenishment_tables_before
        assert connection.execute(
            text(
                "SELECT permissions ->> 'page_maintenance_beta' "
                "FROM sys_role_template WHERE code = 'admin'"
            )
        ).scalar_one() == beta_permission_before
        connection.execute(
            text(
                "DELETE FROM maintenance_migration_run "
                "WHERE run_id = 'downgrade-blocker-run'"
            )
        )


def test_empty_control_schema_downgrades_and_upgrades(db):
    db.close()
    cfg = _cfg()
    alembic_command.downgrade(cfg, _PREV)
    try:
        with engine.connect() as connection:
            assert not (_TABLES & set(inspect(connection).get_table_names()))
            admin_template = connection.execute(
                text(
                    "SELECT permissions ? 'action_maintenance_migration_review' "
                    "FROM sys_role_template WHERE code = 'admin'"
                )
            ).scalar_one()
            assert admin_template is False
        alembic_command.upgrade(cfg, "head")
        with engine.connect() as connection:
            assert _TABLES <= set(inspect(connection).get_table_names())
    finally:
        alembic_command.upgrade(cfg, "head")


def test_permission_upgrade_uses_actual_role_and_clears_stale_override(db):
    db.close()
    cfg = _cfg()
    alembic_command.downgrade(cfg, _PREV)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO sys_user
                        (username, role, password_hash, template_code,
                         template_perms, permissions, perm_overrides)
                    VALUES
                        ('migration-control-boss', 'boss', 'not-used', 'admin',
                         '{"sentinel": true}'::jsonb, '{"sentinel": true}'::jsonb,
                         '{"action_maintenance_migration_review": true}'::jsonb),
                        ('migration-control-admin', 'admin', 'not-used', 'boss',
                         '{"sentinel": true}'::jsonb, '{"sentinel": true}'::jsonb,
                         '{"action_maintenance_migration_review": false}'::jsonb)
                    """
                )
            )

        alembic_command.upgrade(cfg, "head")
        with engine.connect() as connection:
            rows = {
                row.username: row
                for row in connection.execute(
                    text(
                        """
                        SELECT username,
                               (template_perms->>'action_maintenance_migration_review')::boolean
                                   AS template_value,
                               (permissions->>'action_maintenance_migration_review')::boolean
                                   AS legacy_value,
                               perm_overrides ? 'action_maintenance_migration_review'
                                   AS has_override
                        FROM sys_user
                        WHERE username IN
                            ('migration-control-boss', 'migration-control-admin')
                        """
                    )
                )
            }
            assert rows["migration-control-boss"].template_value is False
            assert rows["migration-control-boss"].legacy_value is False
            assert rows["migration-control-boss"].has_override is False
            assert rows["migration-control-admin"].template_value is True
            assert rows["migration-control-admin"].legacy_value is True
            assert rows["migration-control-admin"].has_override is False
    finally:
        alembic_command.upgrade(cfg, "head")

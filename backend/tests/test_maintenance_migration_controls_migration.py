"""Schema, permission, and downgrade safety for maintenance cutover controls."""

import os

import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError

from app import permissions
from app.config import Settings, get_settings
from app.db import engine


_PREV = "e6a9c3f1b2d4"
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
                 source_snapshot_hash, status, preview_json, created_by)
            VALUES
                (:run_id, :idempotency_key, :fingerprint,
                 'maintenance-cutover-v1', :snapshot_hash, 'previewed',
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


def test_migration_control_schema_and_permission_are_fail_closed(db):
    inspector = inspect(db.get_bind())
    assert _TABLES <= set(inspector.get_table_names())

    run_columns = {
        column["name"] for column in inspector.get_columns("maintenance_migration_run")
    }
    assert {
        "request_fingerprint",
        "source_snapshot_hash",
        "manifest_json",
        "manifest_hash",
        "manifest_key_id",
        "created_by",
        "reconciled_by",
        "approved_by",
        "version",
    } <= run_columns
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
        assert set(connection.scalars(text("SELECT version_num FROM alembic_version"))) == versions_before
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

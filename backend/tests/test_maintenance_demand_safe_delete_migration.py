"""Schema and permission gates for WBDD logical deletion."""

import os

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import text

from app import permissions
from app.db import engine


_PREV = "e6a9c3f1b2d4"


def _cfg() -> AlembicConfig:
    cfg = AlembicConfig(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option(
        "script_location",
        os.path.join(os.path.dirname(__file__), "..", "alembic"),
    )
    return cfg


def test_safe_delete_schema_has_state_constraints_and_append_only_triggers(db):
    tables = set(
        db.execute(
            text(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = current_schema() "
                "AND tablename LIKE 'maintenance_demand_%'"
            )
        ).scalars()
    )
    assert {
        "maintenance_demand_delete_intent",
        "maintenance_demand_delete_intent_item",
        "maintenance_demand_tombstone",
        "maintenance_demand_delete_event",
    } <= tables

    triggers = set(
        db.execute(
            text(
                "SELECT tgname FROM pg_trigger "
                "WHERE NOT tgisinternal AND tgname LIKE 'trg_maintenance_demand_%'"
            )
        ).scalars()
    )
    assert triggers == {
        "trg_maintenance_demand_delete_event_append_only",
        "trg_maintenance_demand_delete_intent_identity_immutable",
        "trg_maintenance_demand_delete_intent_item_immutable",
    }


def test_safe_delete_permission_defaults_and_page_dependency_fail_closed():
    key = "action_maintenance_demand_delete"
    assert key in permissions.ACTION_KEYS
    assert key in permissions.HIGH_RISK_KEYS
    assert permissions.ACTION_PAGE_DEPENDENCIES[key] == "page_maintenance"
    assert permissions.effective("admin", None)[key] is True
    for role in ("boss", "sales", "purchaser", "readonly", "guest"):
        assert permissions.effective(role, None)[key] is False


def test_safe_delete_upgrade_backfills_permission_by_real_role(db):
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
                        ('demand-migration-boss', 'boss', 'not-used', 'admin',
                         '{"sentinel": true}'::jsonb,
                         '{"sentinel": true}'::jsonb,
                         '{"action_maintenance_demand_delete": true}'::jsonb),
                        ('demand-migration-admin', 'admin', 'not-used', 'boss',
                         '{"sentinel": true}'::jsonb,
                         '{"sentinel": true}'::jsonb,
                         '{"action_maintenance_demand_delete": false}'::jsonb)
                    """
                )
            )

        alembic_command.upgrade(cfg, "head")
        with engine.connect() as connection:
            template_values = dict(
                connection.execute(
                    text(
                        """
                        SELECT code,
                               (permissions->>'action_maintenance_demand_delete')::boolean
                        FROM sys_role_template
                        WHERE code IN ('admin', 'boss')
                        """
                    )
                ).all()
            )
            assert template_values == {"admin": True, "boss": False}
            users = {
                row.username: row
                for row in connection.execute(
                    text(
                        """
                        SELECT username,
                               (template_perms->>'action_maintenance_demand_delete')::boolean
                                   AS template_value,
                               (permissions->>'action_maintenance_demand_delete')::boolean
                                   AS legacy_value,
                               perm_overrides ? 'action_maintenance_demand_delete'
                                   AS has_override
                        FROM sys_user
                        WHERE username LIKE 'demand-migration-%'
                        """
                    )
                )
            }
            assert users["demand-migration-boss"].template_value is False
            assert users["demand-migration-boss"].legacy_value is False
            assert users["demand-migration-boss"].has_override is False
            assert users["demand-migration-admin"].template_value is True
            assert users["demand-migration-admin"].legacy_value is True
            assert users["demand-migration-admin"].has_override is False
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM sys_user WHERE username LIKE 'demand-migration-%'")
            )
        alembic_command.upgrade(cfg, "head")

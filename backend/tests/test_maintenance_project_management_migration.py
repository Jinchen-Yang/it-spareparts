"""Schema and permission contract for project-master management."""

import os

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import text

from app import permissions
from app.db import engine


_PREV = "c6f2a8e9d4b1"


def _cfg() -> AlembicConfig:
    cfg = AlembicConfig(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option(
        "script_location",
        os.path.join(os.path.dirname(__file__), "..", "alembic"),
    )
    return cfg


def test_project_master_audit_schema_uses_string_entity_identity(db):
    tables = set(
        db.execute(
            text(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = current_schema() "
                "AND tablename = 'maintenance_project_audit_log'"
            )
        ).scalars()
    )
    assert tables == {"maintenance_project_audit_log"}

    columns = dict(
        db.execute(
            text(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = 'maintenance_project_audit_log'"
            )
        ).all()
    )
    assert columns["project_id"] == "character varying"
    assert columns["entity_id"] == "character varying"
    assert columns["reason"] == "text"
    assert columns["operated_by"] == "character varying"

    indexes = set(
        db.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname = current_schema() "
                "AND tablename IN "
                "('maintenance_project', 'maintenance_project_audit_log')"
            )
        ).scalars()
    )
    assert {
        "ux_maintenance_project_code_ci",
        "ix_maintenance_project_audit_project_time",
        "ix_maintenance_project_audit_entity_time",
    } <= indexes


def test_project_master_permission_defaults_and_dependencies_fail_closed():
    key = "action_maintenance_project_manage"
    assert key in permissions.ACTION_KEYS
    assert key in permissions.HIGH_RISK_KEYS
    assert permissions.ACTION_PAGE_DEPENDENCIES[key] == "page_maintenance"
    assert permissions.ACTION_DATA_DEPENDENCIES[key] == "data_profit"
    assert permissions.effective("admin", None)[key] is True
    for role in ("boss", "sales", "purchaser", "readonly", "guest"):
        assert permissions.effective(role, None)[key] is False


def test_project_master_upgrade_backfills_permission_by_real_role(db):
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
                        ('project-migration-boss', 'boss', 'not-used', 'admin',
                         '{"sentinel": true}'::jsonb,
                         '{"sentinel": true}'::jsonb,
                         '{"action_maintenance_project_manage": true}'::jsonb),
                        ('project-migration-admin', 'admin', 'not-used', 'boss',
                         '{"sentinel": true}'::jsonb,
                         '{"sentinel": true}'::jsonb,
                         '{"action_maintenance_project_manage": false}'::jsonb)
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
                               (permissions->>'action_maintenance_project_manage')::boolean
                        FROM sys_role_template
                        WHERE code IN ('admin', 'boss')
                        """
                    )
                ).all()
            )
            assert template_values == {"admin": True, "boss": False}

            rows = {
                row.username: row
                for row in connection.execute(
                    text(
                        """
                        SELECT username,
                               (template_perms->>'action_maintenance_project_manage')::boolean
                                   AS template_value,
                               (permissions->>'action_maintenance_project_manage')::boolean
                                   AS legacy_value,
                               perm_overrides ? 'action_maintenance_project_manage'
                                   AS has_override
                        FROM sys_user
                        WHERE username IN
                            ('project-migration-boss', 'project-migration-admin')
                        """
                    )
                )
            }
            assert rows["project-migration-boss"].template_value is False
            assert rows["project-migration-boss"].legacy_value is False
            assert rows["project-migration-boss"].has_override is False
            assert rows["project-migration-admin"].template_value is True
            assert rows["project-migration-admin"].legacy_value is True
            assert rows["project-migration-admin"].has_override is False
    finally:
        alembic_command.upgrade(cfg, "head")

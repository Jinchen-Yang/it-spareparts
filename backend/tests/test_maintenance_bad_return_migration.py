"""Migration safety invariants for bad-part return obligations."""

import os
from datetime import UTC, datetime

import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError

from app import permissions
from app.models.maintenance_bad_return import MaintenanceBadReturn
from app.models.maintenance_project import MaintenanceProject


_PREV = "f4b8d2e6a1c3"
_BAD_RETURN_BASE = "a8d3c7e5f1b2"


def _alembic_cfg() -> AlembicConfig:
    cfg = AlembicConfig(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option(
        "script_location",
        os.path.join(os.path.dirname(__file__), "..", "alembic"),
    )
    return cfg


def test_bad_return_permission_contract_fails_closed():
    key = "action_maintenance_bad_return_manage"
    assert key in permissions.ACTION_KEYS
    assert key in permissions.HIGH_RISK_KEYS
    assert permissions.ACTION_PAGE_DEPENDENCIES[key] == "page_maintenance"
    assert key not in permissions.ACTION_DATA_DEPENDENCIES
    assert permissions.effective("admin", None)[key] is True
    for role in ("boss", "sales", "purchaser", "readonly", "guest"):
        assert permissions.effective(role, None)[key] is False


def test_bad_return_business_history_blocks_destructive_downgrade(db):
    project = MaintenanceProject(
        project_id="bad-return-migration-project",
        project_code="SYNTH-BAD-RETURN-MIGRATION",
        display_name="合成坏件返还迁移保护项目",
        lifecycle_status="ongoing",
    )
    db.add(project)
    db.flush()
    db.add(
        MaintenanceBadReturn(
            return_id="bad-return-migration-document",
            return_no="BHR-SYNTH-MIGRATION",
            project_id=project.project_id,
            status="draft",
            created_by="synthetic-migration-test",
        )
    )
    db.commit()

    with pytest.raises(
        DBAPIError,
        match="ck_maintenance_bad_return_state_evidence",
    ):
        with db.begin_nested():
            db.execute(
                text(
                    "UPDATE maintenance_bad_return SET status = 'void' "
                    "WHERE return_id = 'bad-return-migration-document'"
                )
            )
    with pytest.raises(
        DBAPIError,
        match="ck_maintenance_bad_return_replacement_not_self",
    ):
        with db.begin_nested():
            db.execute(
                text(
                    "UPDATE maintenance_bad_return "
                    "SET replaces_return_id = return_id "
                    "WHERE return_id = 'bad-return-migration-document'"
                )
            )

    with pytest.raises(DBAPIError, match="downgrade blocked"):
        alembic_command.downgrade(_alembic_cfg(), _PREV)

    db.rollback()
    tables = set(inspect(db.get_bind()).get_table_names())
    assert "maintenance_bad_return" in tables
    assert "maintenance_demand_delete_event" in tables
    assert "maintenance_project_user_assignment" in tables


def test_void_history_blocks_void_workflow_downgrade(db):
    project = MaintenanceProject(
        project_id="bad-return-void-migration-project",
        project_code="SYNTH-BAD-RETURN-VOID-MIGRATION",
        display_name="合成坏件返还作废迁移保护项目",
        lifecycle_status="ongoing",
    )
    db.add(project)
    db.flush()
    db.add(
        MaintenanceBadReturn(
            return_id="bad-return-void-migration-doc",
            return_no="BHR-SYNTH-VOID-MIGRATION",
            project_id=project.project_id,
            status="void",
            created_by="synthetic-migration-test",
            voided_at=datetime.now(UTC),
        )
    )
    db.commit()

    with pytest.raises(DBAPIError, match="downgrade blocked"):
        alembic_command.downgrade(_alembic_cfg(), _BAD_RETURN_BASE)

    db.rollback()
    assert "maintenance_bad_return" in set(inspect(db.get_bind()).get_table_names())

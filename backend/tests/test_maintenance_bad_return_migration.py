"""Migration safety invariants for bad-part return obligations."""

import os

import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy.exc import DBAPIError

from app import permissions
from app.models.maintenance_bad_return import MaintenanceBadReturn
from app.models.maintenance_project import MaintenanceProject


_PREV = "f4b8d2e6a1c3"


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

    with pytest.raises(DBAPIError, match="downgrade blocked"):
        alembic_command.downgrade(_alembic_cfg(), _PREV)

    db.rollback()

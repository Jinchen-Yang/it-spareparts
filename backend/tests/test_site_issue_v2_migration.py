"""Migration safety invariants for the site-consumption v2 workflow."""

import os
from datetime import date

import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import inspect
from sqlalchemy.exc import DBAPIError

from app.models.dimensions import DimPart
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_project_operations import (
    MaintenanceSiteIssueDeliverySource,
)


_PREV = "e6a9c3f1b2d4"


def _alembic_cfg() -> AlembicConfig:
    cfg = AlembicConfig(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option(
        "script_location",
        os.path.join(os.path.dirname(__file__), "..", "alembic"),
    )
    return cfg


def test_site_issue_v2_business_history_blocks_destructive_downgrade(db):
    project = MaintenanceProject(
        project_id="site-issue-v2-migration-project",
        project_code="SYNTH-SITE-ISSUE-MIGRATION",
        display_name="合成迁移保护项目",
        lifecycle_status="ongoing",
    )
    part = DimPart(pn_std="SYNTH-SITE-ISSUE-MIGRATION-PN")
    db.add_all([project, part])
    db.flush()
    db.add(
        MaintenanceSiteIssueDeliverySource(
            delivery_line_id="site-issue-v2-migration-delivery-line",
            adapter_key="synthetic_delivery_v1",
            project_id=project.project_id,
            source_order_id="site-issue-v2-migration-order",
            source_line_id="site-issue-v2-migration-source-line",
            delivery_no="SYNTH-SITE-ISSUE-MIGRATION-DELIVERY",
            delivery_date=date(2026, 8, 8),
            part_id=part.id,
            pn=part.pn_std,
            delivered_quantity="1",
            mapping_state="ready",
            mapping_version="synthetic-delivery-map-v1",
        )
    )
    db.commit()

    with pytest.raises(DBAPIError, match="downgrade blocked"):
        alembic_command.downgrade(_alembic_cfg(), _PREV)

    db.rollback()
    tables = set(inspect(db.get_bind()).get_table_names())
    assert "maintenance_demand_delete_event" in tables
    assert "maintenance_project_user_assignment" in tables

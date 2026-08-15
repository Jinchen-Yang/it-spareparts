"""B3 no_return HTTP→DB→confirm→obligation 全链测试（审核反例）。"""

import pytest
from sqlalchemy import select

from app.models.maintenance_bad_return import MaintenanceReturnObligation
from app.models.maintenance_project_operations import MaintenanceSiteIssueLine
from tests.test_maintenance_bad_returns_api import _client
from tests.test_site_issue_v2_api import _delivery_source, _project


def _create_draft(client, project_id, *, suffix, no_return, delivery_line_id="dl-no-return-1"):
    return client.post(
        f"/api/maintenance/site-issues/projects/{project_id}",
        json={
            "idempotency_key": f"synthetic-no-return-create-{suffix}",
            "issue_date": "2026-08-09",
            "receiver": "合成接收人",
            "issued_by": "合成发出人",
            "site_location": "合成现场",
            "lines": [
                {
                    "delivery_line_id": delivery_line_id,
                    "quantity": 2,
                    "no_return": no_return,
                }
            ],
            "reason": "行级不返还标记全链验证",
        },
    )


def test_no_return_line_flag_persists_and_projects(db):
    project = _project(db, project_id="no-return-project-1")
    _delivery_source(db, project=project, delivery_line_id="dl-no-return-1")
    client = _client(db, username="no-return-admin")

    created = _create_draft(
        client, project.project_id, suffix="chain-true", no_return=True
    )
    assert created.status_code == 201, created.text
    draft = created.json()
    assert draft["lines"][0]["no_return"] is True

    confirmed = client.post(
        f"/api/maintenance/site-issues/{draft['issue_id']}/confirm",
        json={
            "project_id": project.project_id,
            "version": draft["version"],
            "idempotency_key": "synthetic-no-return-confirm-chain-true",
            "reason": "确认并生成返还义务",
        },
    )
    assert confirmed.status_code == 200, confirmed.text

    # 行级不返还 → 义务豁免，exemption_source=line_no_return
    obligation = db.execute(
        select(MaintenanceReturnObligation).where(
            MaintenanceReturnObligation.issue_id == draft["issue_id"]
        )
    ).scalar_one()
    assert obligation.classification == "exempt"
    assert obligation.exemption_source == "line_no_return"
    assert float(obligation.required_quantity) == 0.0


def test_no_return_project_default_projects_exempt(db):
    project = _project(db, project_id="no-return-project-2")
    project.no_return_default = True
    db.commit()
    _delivery_source(db, project=project, delivery_line_id="dl-no-return-2")
    client = _client(db, username="no-return-admin-2")

    created = _create_draft(
        client, project.project_id, suffix="default-true", no_return=None,
        delivery_line_id="dl-no-return-2",
    )
    assert created.status_code == 201, created.text
    draft = created.json()
    confirmed = client.post(
        f"/api/maintenance/site-issues/{draft['issue_id']}/confirm",
        json={
            "project_id": project.project_id,
            "version": draft["version"],
            "idempotency_key": "synthetic-no-return-confirm-default",
            "reason": "确认并生成返还义务",
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    obligation = db.execute(
        select(MaintenanceReturnObligation).where(
            MaintenanceReturnObligation.issue_id == draft["issue_id"]
        )
    ).scalar_one()
    assert obligation.classification == "exempt"
    assert obligation.exemption_source == "project_default_no_return"


def test_same_idempotency_key_changed_no_return_conflicts(db):
    project = _project(db, project_id="no-return-project-3")
    _delivery_source(db, project=project, delivery_line_id="dl-no-return-3")
    client = _client(db, username="no-return-admin-3")

    first = _create_draft(
        client, project.project_id, suffix="conflict", no_return=True,
        delivery_line_id="dl-no-return-3",
    )
    assert first.status_code == 201, first.text
    # 同幂等键、不同行级标记 → 指纹冲突 409
    second = _create_draft(
        client, project.project_id, suffix="conflict", no_return=False,
        delivery_line_id="dl-no-return-3",
    )
    assert second.status_code == 409, second.text

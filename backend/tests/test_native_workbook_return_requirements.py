"""Workbook corrections must retain native delivery-based obligation consumers."""
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.dimensions import DimPart
from app.models.master_data import ProductCategory
from app.models.maintenance_bad_return import MaintenanceReturnObligation
from app.models.maintenance_project_operations import MaintenanceSiteIssueReturnEvent
from tests.test_site_issue_v2_api import _client, _create_draft, _delivery_source, _project
from tests.test_site_return_requirements import _apply, _download


def test_native_confirmed_issue_workbook_flags_reproject_existing_obligations(db):
    project = _project(db, project_id=str(uuid4()))
    delivery = _delivery_source(db, project=project)
    category = ProductCategory(category_major="网络设备", category_minor="交换机")
    db.add(category)
    db.flush()
    db.get(DimPart, delivery.part_id).category_id = category.id
    db.commit()
    client = _client(db, username="native-workbook-operator")
    draft = _create_draft(client, project_id=project.project_id,
        delivery_line_id=delivery.delivery_line_id, quantity="2", key=str(uuid4()))
    confirmed = client.post(f"/api/maintenance/site-issues/{draft['issue_id']}/confirm", json={
        "project_id": project.project_id, "version": draft["version"],
        "idempotency_key": str(uuid4()), "reason": "native confirmation",
    })
    assert confirmed.status_code == 200, confirmed.text
    obligation = db.scalar(select(MaintenanceReturnObligation).where(
        MaintenanceReturnObligation.issue_id == draft["issue_id"]))
    assert obligation.required_quantity == Decimal(2)
    line_id = obligation.issue_line_id
    for flag, quantity, classification in (("否", 0, "exempt"), ("是", 2, "required"), ("", 2, "required")):
        wb, ws, columns, row = _download(db, project.project_id, line_id)
        ws.cell(row, columns["是否应返还"]).value = flag
        _apply(db, project.project_id, wb)
        db.refresh(obligation)
        assert obligation.required_quantity == Decimal(quantity)
        assert obligation.classification == classification
        assert obligation.is_active is True
    events = db.scalars(select(MaintenanceSiteIssueReturnEvent).where(
        MaintenanceSiteIssueReturnEvent.issue_id == draft["issue_id"])).all()
    assert len(events) == 4
    assert all(e.payload["schema_version"] == "maintenance-return-obligation-interface-v1" for e in events)
    assert all(e.downstream_reference.startswith("maintenance-return-obligations:") for e in events)


@pytest.mark.parametrize("original", ["required", "pending_category", "exempt"])
def test_native_requirement_read_uses_frozen_quantity_category_and_flag_result(db, original):
    from app.models.maintenance_project_operations import MaintenanceSiteIssueLine
    from app.services import maintenance_project_operations as operations
    project = _project(db, project_id=str(uuid4()))
    delivery = _delivery_source(db, project=project)
    category = ProductCategory(category_major="网络设备", category_minor="交换机")
    db.add(category)
    db.flush()
    part = db.get(DimPart, delivery.part_id)
    if original != "pending_category":
        part.category_id = category.id
    db.commit()
    client = _client(db, username="native-frozen-operator")
    draft = _create_draft(client, project_id=project.project_id,
        delivery_line_id=delivery.delivery_line_id, quantity="2", key=str(uuid4()))
    line = db.scalar(select(MaintenanceSiteIssueLine).where(
        MaintenanceSiteIssueLine.issue_id == draft["issue_id"]))
    if original == "exempt":
        line.no_return = True
        db.commit()
    response = client.post(f"/api/maintenance/site-issues/{draft['issue_id']}/confirm", json={
        "project_id": project.project_id, "version": draft["version"],
        "idempotency_key": str(uuid4()), "reason": "freeze evidence",
    })
    assert response.status_code == 200, response.text
    obligation = db.scalar(select(MaintenanceReturnObligation).where(
        MaintenanceReturnObligation.issue_id == draft["issue_id"]))
    assert obligation.classification == original
    # A live fact/master-data change must not masquerade as a reprojected native
    # obligation. Description stays live, while the established obligation stays frozen.
    part.category_id = category.id
    part.description = "current description"
    line.quantity = Decimal(9)
    line.no_return = original == "required"
    db.commit()
    payload = operations.search_site_issues(db, project_id=project.project_id,
        q_text=None, workflow_statuses=["confirmed"], page=1, page_size=20)
    visible = payload["rows"][0]["lines"][0]
    assert visible["description"] == "current description"
    requirement = visible["return_requirement"]
    assert requirement["requirement_status"] == obligation.classification
    assert requirement["required_quantity"] == format(obligation.required_quantity, ".3f")
    expected_key = {"required": "required_quantity", "pending_category": "pending_quantity", "exempt": "exempt_quantity"}[original]
    assert requirement[expected_key] == "2.000"
    assert requirement["basis"]["category_id_snapshot"] == obligation.category_id_snapshot
    assert requirement["basis"]["exemption_source"] == obligation.exemption_source

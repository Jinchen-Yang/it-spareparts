"""D-27 raw-file upload -> review -> confirmed soft-void HTTP contract."""
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder
from app.models.maintenance_project_operations import MaintenanceSiteIssue
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from tests.boss_board_helpers import client_for
from tests.test_maintenance_analytics import _project
from tests.test_maintenance_return_metrics import _issue
from tests.test_maintenance_wbdd_import_api import _upload
from tests.wbdd_fixtures import COLUMNS_91, make_rows, write_workbook

URL = "/api/maintenance/wbdd-imports"


def test_legacy_initial_page_returns_consistent_empty_coverage(db):
    client = client_for(db, username="empty-sync-admin", role="admin")
    response = client.get(f"{URL}/latest/missing")
    assert response.status_code == 200
    assert response.json()["readiness"] == "snapshot_required"
    assert response.json()["coverage"] == {
        "daily_windows": [], "source_order_count": 0, "blocked_order_count": 0}
    assert response.json()["candidate_digest"] is None


def _setup(db, tmp_path):
    client = client_for(db, username="sync-http-admin", role="admin")
    rows = make_rows(orders=1, lines_per_order=2)
    for row in rows:
        row["需求单号"] = "WBDD-20260609-0001"
        row["制单日期"] = "2026-06-09"
        row["销售订单"] = ""
    first = write_workbook(str(tmp_path / "first.xlsx"), COLUMNS_91, rows)
    response = _upload(client, first)
    assert response.status_code == 200, response.text
    order = db.scalar(select(FMaintenanceOrder))
    lines = list(db.scalars(select(FMaintenanceLine).order_by(FMaintenanceLine.id)))
    assignment = db.scalar(select(MaintenanceSourceOrderAssignment).where(
        MaintenanceSourceOrderAssignment.source_order_id == order.raw_order_id,
        MaintenanceSourceOrderAssignment.is_active.is_(True)))
    if assignment is None:
        project = _project(db, "SYNC-HTTP")
        db.add(MaintenanceSourceOrderAssignment(
            assignment_id=str(uuid4()), project_id=project.project_id,
            source_order_id=order.raw_order_id, is_active=True, created_by="test"))
    else:
        from app.models.maintenance_project import MaintenanceProject
        project = db.get(MaintenanceProject, assignment.project_id)
    from app.models.dimensions import DimPart
    part = db.get(DimPart, lines[1].part_id)
    site_line = _issue(db, project, part, "2", source_order_id=order.raw_order_id)
    site_line.source_line_id = lines[1].raw_line_id
    site_id = site_line.issue_id
    db.commit()
    rows[0]["需求明细.需求数量"] = "9"
    second = write_workbook(str(tmp_path / "second.xlsx"), COLUMNS_91, rows[:1])
    response = _upload(client, second)
    assert response.status_code == 200, response.text
    assert "_wbdd_source_snapshot" not in response.json()
    missing = client.get(f"{URL}/latest/missing")
    assert missing.status_code == 200, missing.text
    payload = missing.json()
    assert payload["missing_count"] == 0
    assert payload["missing_line_count"] == 1
    assert payload["missing_lines"][0]["line_id"] == lines[1].id
    return client, payload, [line.id for line in lines], site_id


def _body(missing):
    return {"expected_digest": missing["candidate_digest"], "source_order_ids": [],
            "line_ids": [row["line_id"] for row in missing["missing_lines"]],
            "reason": "核对原件，确认该行已删除", "idempotency_key": str(uuid4())}


def test_upload_only_reports_missing_then_confirm_updates_real_facts(db, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "maintenance_cutover_enabled", False)
    client, missing, line_ids, site_id = _setup(db, tmp_path)
    db.expire_all()
    assert db.get(FMaintenanceLine, line_ids[0]).qty == Decimal("9")
    assert db.get(FMaintenanceLine, line_ids[1]).is_active
    body = _body(missing)
    response = client.post(f"{URL}/{missing['batch_id']}/reconcile", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["voided_lines"] == 1
    assert response.json()["voided_site_lines"] == 1
    db.expire_all()
    assert not db.get(FMaintenanceLine, line_ids[1]).is_active
    assert db.get(MaintenanceSiteIssue, site_id).normalized_status == "void"
    replay = client.post(f"{URL}/{missing['batch_id']}/reconcile", json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json()["replayed"] is True
    after = client.get(f"{URL}/latest/missing").json()
    assert after["missing_line_count"] == 0
    assert "_wbdd_sync_results" not in after
    history = client.get(f"/api/import/batches/{missing['batch_id']}")
    assert history.status_code == 200, history.text
    assert not any(key.startswith("_") for key in history.json()["report"])


def test_confirmation_rejects_changed_candidate_without_writing(db, tmp_path):
    client, missing, line_ids, site_id = _setup(db, tmp_path)
    stale = _body(missing)
    line = db.get(FMaintenanceLine, line_ids[1])
    line.qty = Decimal("12")
    db.commit()
    response = client.post(f"{URL}/{missing['batch_id']}/reconcile", json=stale)
    assert response.status_code == 409, response.text
    db.expire_all()
    assert db.get(FMaintenanceLine, line_ids[1]).is_active
    assert db.get(MaintenanceSiteIssue, site_id).normalized_status == "confirmed"


def test_confirmation_requires_delete_permission_and_explicit_candidate(db, tmp_path):
    client, missing, line_ids, _ = _setup(db, tmp_path)
    other = client_for(db, username="sync-http-import-only", overrides={
        "page_maintenance": True, "action_maintenance_wbdd_import": True,
        "action_maintenance_demand_delete": False,
    })
    path = f"{URL}/{missing['batch_id']}/reconcile"
    response = other.post(path, json=_body(missing))
    assert response.status_code == 403
    body = _body(missing)
    body["line_ids"] = [line_ids[0]]
    response = client.post(path, json=body)
    assert response.status_code == 409
    db.expire_all()
    assert all(db.get(FMaintenanceLine, line_id).is_active for line_id in line_ids)


def test_cancelled_header_is_updated_even_when_child_pn_is_invalid(db, tmp_path):
    client = client_for(db, username="cancel-http-admin", role="admin")
    rows = make_rows(orders=1, lines_per_order=1)
    rows[0]["需求单号"] = "WBDD-20260609-0001"
    first = write_workbook(str(tmp_path / "active.xlsx"), COLUMNS_91, rows)
    assert _upload(client, first).status_code == 200
    rows[0]["数据状态"] = "已取消"
    rows[0]["需求明细.需供货产品"] = ""
    second = write_workbook(str(tmp_path / "cancelled.xlsx"), COLUMNS_91, rows)
    response = _upload(client, second)
    assert response.status_code == 200, response.text
    db.expire_all()
    assert db.scalar(select(FMaintenanceOrder)).data_status == "已取消"
    # Rejected child remains an observed identity, never an inferred deletion.
    assert db.scalar(select(FMaintenanceLine)).is_active
    assert client.get(f"{URL}/latest/missing").json()["missing_line_count"] == 0


def test_same_upload_key_cannot_replay_different_file(db, tmp_path):
    client = client_for(db, username="idem-http-admin", role="admin")
    rows = make_rows(orders=1, lines_per_order=1)
    first = write_workbook(str(tmp_path / "one.xlsx"), COLUMNS_91, rows)
    rows[0]["需求明细.需求数量"] = "99"
    second = write_workbook(str(tmp_path / "two.xlsx"), COLUMNS_91, rows)
    key = str(uuid4())
    assert _upload(client, first, key=key).status_code == 200
    response = _upload(client, second, key=key)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "idempotency_file_mismatch"
    db.expire_all()
    assert db.scalar(select(FMaintenanceLine)).qty == Decimal("3")

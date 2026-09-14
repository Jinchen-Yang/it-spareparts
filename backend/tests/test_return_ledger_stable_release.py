"""Exercise the actual main app: stable receipts do not require Beta access."""
from uuid import uuid4

from app import config, permissions
from tests.test_maintenance_beta_gate import _client
from tests.test_maintenance_return_receipts_api import _wbdd
from tests.test_return_receipt_hardening import _grant
from tests.test_site_issue_v2_api import _project, _workbook_issue


def _stable_client(db, *, username, manage=True):
    return _client(db, username=username, role="readonly", overrides={
        "page_maintenance": True,
        "page_maintenance_beta": False,
        "page_maintenance_boss": False,
        "data_purchase_cost": True,
        "action_maintenance_site_issue_manage": manage,
        "action_maintenance_bad_return_manage": manage,
    })


def test_main_stable_receipts_issue_void_and_candidates_with_beta_closed(db, monkeypatch):
    settings = config.get_settings()
    monkeypatch.setattr(settings, "maintenance_beta_enabled", False)
    monkeypatch.setattr(settings, "maintenance_boss_dashboard_enabled", True)
    project = _project(db, project_id=str(uuid4()))
    other = _project(db, project_id=str(uuid4()))
    issue, _ = _workbook_issue(db, project=project, issue_no="STABLE-RELEASE-ISSUE")
    first = _wbdd(db, project=project, order_no="WBDD-STABLE-1")
    _wbdd(db, project=project, order_no="WBDD-STABLE-2")
    invalid = _wbdd(db, project=project, order_no="WBDD-INACTIVE")
    invalid.data_status = "已作废"
    db.commit()
    _wbdd(db, project=other, order_no="WBDD-OTHER")
    client = _stable_client(db, username="stable-release-user")
    _grant(db, project.project_id, "stable-release-user")
    pid, issue_id, issue_version = project.project_id, issue.issue_id, issue.version
    candidates_url = f"/api/maintenance/projects/stable/{pid}/return-receipt-demands"
    response = client.get(candidates_url, params={"page_size": 1})
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 2
    assert response.json()["rows"][0]["order_no"] == first.order_no
    second = client.get(candidates_url, params={"page": 2, "page_size": 1})
    assert second.json()["rows"][0]["order_no"] == "WBDD-STABLE-2"
    assert client.get(f"/api/maintenance/projects/stable/{other.project_id}/return-receipt-demands").status_code == 403
    projects = client.get("/api/maintenance/projects/stable", params={"include_inactive": False})
    assert projects.status_code == 200, projects.text
    assert {r["project_id"] for r in projects.json()["rows"]} == {pid}

    workspace = client.get(f"/api/maintenance/projects/stable/{pid}/workspace")
    assert workspace.status_code == 200, workspace.text
    assert client.get(f"/api/maintenance/projects/stable/{other.project_id}/workspace").status_code == 403
    listing = client.post("/api/maintenance/site-issues/search", json={"project_id": pid})
    assert listing.status_code == 200, listing.text
    assert listing.json()["rows"][0]["issue_id"] == issue_id
    assert client.post("/api/maintenance/site-issues/search", json={"project_id": other.project_id}).status_code == 403
    receipt_base = f"/api/maintenance/projects/stable/{pid}/return-receipts"
    registered = client.post(receipt_base, json={"pn": "DIFFERENT-PN", "qty": 2, "wbdd_no": first.order_no})
    assert registered.status_code == 201, registered.text
    receipt_id = registered.json()["receipt_id"]
    assert registered.json()["source_order_id"] == first.raw_order_id
    assert client.get(receipt_base).json()["total"] == 1
    assert client.post(f"/api/maintenance/return-receipts/{receipt_id}/void", json={
        "version": 1, "reason": "stable receipt void",
    }).status_code == 200
    voided = client.post(f"/api/maintenance/site-issues/{issue_id}/void", json={
        "project_id": pid, "version": issue_version, "idempotency_key": str(uuid4()),
        "reason": "stable workbook issue void",
    })
    assert voided.status_code == 200, voided.text

    # Draft/preview/confirm/correct and old return workflow remain independently gated.
    for method, url in (
        ("post", f"/api/maintenance/site-issues/projects/{pid}"),
        ("post", f"/api/maintenance/site-issues/{issue_id}/preview"),
        ("post", f"/api/maintenance/site-issues/{issue_id}/confirm"),
        ("patch", f"/api/maintenance/site-issues/{issue_id}"),
        ("post", "/api/maintenance/demands/search"),
        ("post", "/api/maintenance/bad-returns/search"),
    ):
        assert getattr(client, method)(url, json={}).status_code == 404, url
    monkeypatch.setattr(settings, "maintenance_beta_enabled", True)
    # This identity does have the stable manage action: the old draft is denied
    # specifically by the missing Beta whitelist, even when its total gate opens.
    assert client.post(f"/api/maintenance/site-issues/projects/{pid}", json={}).status_code == 403
    # Multipart validation is reached on the published import route even with Beta closed.
    no_file = client.post("/api/maintenance/doc-imports/return-receipts/jobs")
    assert no_file.status_code in (400, 422), no_file.text

    monkeypatch.setattr(settings, "maintenance_boss_dashboard_enabled", False)
    for method, url, body in (
        ("get", candidates_url, None),
        ("get", "/api/maintenance/projects/stable", None),
        ("get", receipt_base, None),
        ("get", f"/api/maintenance/projects/stable/{pid}/workspace", None),
        ("post", receipt_base, {"pn": "TEST", "qty": 1}),
        ("post", "/api/maintenance/site-issues/search", {"project_id": pid}),
        ("post", f"/api/maintenance/site-issues/{issue_id}/void", {}),
        ("post", f"/api/maintenance/return-receipts/{receipt_id}/void", {}),
        ("post", "/api/maintenance/doc-imports/return-receipts/jobs", {}),
    ):
        response = (getattr(client, method)(url) if body is None
                    else getattr(client, method)(url, json=body))
        assert response.status_code == 404, (url, response.text)


def test_stable_release_keeps_action_and_beta_whitelist_boundaries(db, monkeypatch):
    settings = config.get_settings()
    monkeypatch.setattr(settings, "maintenance_beta_enabled", True)
    monkeypatch.setattr(settings, "maintenance_boss_dashboard_enabled", True)
    project = _project(db, project_id=str(uuid4()))
    issue, _ = _workbook_issue(db, project=project, issue_no="STABLE-NO-ACTION")
    client = _stable_client(db, username="stable-no-action", manage=False)
    _grant(db, project.project_id, "stable-no-action")
    pid = project.project_id
    assert client.post("/api/maintenance/site-issues/search", json={"project_id": pid}).status_code == 200
    assert client.post(f"/api/maintenance/projects/stable/{pid}/return-receipts",
                       json={"pn": "TEST", "qty": 1}).status_code == 403
    assert client.post(f"/api/maintenance/site-issues/{issue.issue_id}/void", json={
        "project_id": pid, "version": issue.version, "idempotency_key": str(uuid4()), "reason": "denied",
    }).status_code == 403
    assert client.post(f"/api/maintenance/site-issues/projects/{pid}", json={}).status_code == 403


def test_permission_graph_allows_explicit_stable_actions_without_granting_them():
    graph = permissions.effective("readonly", {
        "page_maintenance": True, "data_purchase_cost": True,
        "page_maintenance_beta": False,
        "action_maintenance_site_issue_manage": True,
        "action_maintenance_bad_return_manage": True,
    })
    assert permissions.combo_errors(graph) == []
    safe = permissions.runtime_safe(graph)
    assert safe["action_maintenance_site_issue_manage"] is True
    assert safe["action_maintenance_bad_return_manage"] is True
    assert safe["page_maintenance_beta"] is False
    baseline = permissions.effective("readonly", {"page_maintenance": True})
    assert baseline["action_maintenance_site_issue_manage"] is False
    assert baseline["action_maintenance_bad_return_manage"] is False

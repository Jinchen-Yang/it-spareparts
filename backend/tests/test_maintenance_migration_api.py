from fastapi.testclient import TestClient

from app.api import maintenance_migration as migration_api
from app.auth import hash_password
from app.config import get_settings
from app.main import app
from app.models.maintenance_project import MaintenanceProject
from app.models.system import SysUser


def _client(db, *, username: str, role: str = "admin", permissions=None):
    db.add(
        SysUser(
            username=username,
            role=role,
            display_name="迁移合成账号",
            password_hash=hash_password("synthetic-password-123"),
            permissions=permissions,
        )
    )
    db.commit()
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": username, "password": "synthetic-password-123"},
    )
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    return client


def _seed_project(db):
    db.add(
        MaintenanceProject(
            project_id="migration-api-project",
            project_code="MIGRATION-API",
            display_name="迁移接口合成项目",
            lifecycle_status="ongoing",
        )
    )
    db.commit()


def _loader(_db, project_id, _cutover_date):
    return (
        [
            {
                "movement_id": "migration-api-delivery-line",
                "document_id": "migration-api-delivery",
                "document_no": "FH-MIGRATION-API",
                "document_date": "2026-08-02",
                "movement_type": "delivery",
                "balance_key": f"{project_id}:part-1",
                "pn": "PN-MIGRATION-API",
                "quantity": "2",
            }
        ],
        True,
    )


def _preview_body():
    return {
        "idempotency_key": "migration-api-preview-key",
        "reason": "建立接口合成 dry-run",
        "projects": [
            {
                "project_id": "migration-api-project",
                "cutover_date": "2026-08-01",
                "historical_mode": "approved_cost_baseline",
                "historical_baseline": {
                    "amount_ex_tax": "100.00",
                    "amount_inc_tax": "113.00",
                    "evidence_hash": "a" * 64,
                },
                "opening_balances": [
                    {
                        "balance_key": "migration-api-project:part-1",
                        "pn": "PN-MIGRATION-API",
                        "quantity": "10",
                        "evidence_hash": "b" * 64,
                    }
                ],
            }
        ],
    }


def _project_signoffs(preview):
    plan = preview["plans"][0]
    return [
        {
            "project_id": plan["project_id"],
            "expected_plan_version": plan["version"],
            "reason": "逐项核对接口项目候选",
            "historical_baseline": {
                "baseline_id": plan["historical_baseline"]["baseline_id"],
                "expected_version": plan["historical_baseline"]["version"],
            },
            "opening_balances": [
                {
                    "opening_balance_id": row["opening_balance_id"],
                    "expected_version": row["version"],
                }
                for row in plan["opening_balances"]
            ],
        }
    ]


def test_public_api_supports_preview_search_reconcile_and_independent_approval(
    db, monkeypatch
):
    _seed_project(db)
    monkeypatch.setattr(migration_api, "load_project_inventory_movements", _loader)
    creator = _client(db, username="migration-api-creator")

    preview_response = creator.post(
        "/api/maintenance/migration-runs/preview", json=_preview_body()
    )
    assert preview_response.status_code == 201, preview_response.text
    assert preview_response.headers["cache-control"] == "no-store"
    preview = preview_response.json()
    assert preview["status"] == "previewed"
    assert preview["preview"]["approval_blocker_count"] == 2

    searched = creator.post(
        "/api/maintenance/migration-runs/search",
        json={"statuses": ["previewed"], "page": 1, "page_size": 20},
    )
    assert searched.status_code == 200, searched.text
    assert searched.headers["cache-control"] == "no-store"
    assert searched.json()["items"][0]["run_id"] == preview["run_id"]

    read_back = creator.get(f"/api/maintenance/migration-runs/{preview['run_id']}")
    assert read_back.status_code == 200, read_back.text
    assert read_back.headers["cache-control"] == "no-store"
    assert read_back.json()["run_id"] == preview["run_id"]
    evidence = creator.get(
        f"/api/maintenance/migration-runs/{preview['run_id']}"
        "/projects/migration-api-project/evidence",
        params={"section": "inventory_movements", "page": 1, "page_size": 1},
    )
    assert evidence.status_code == 200, evidence.text
    assert evidence.headers["cache-control"] == "no-store"
    assert evidence.json()["items"][0]["document_no"] == "FH-MIGRATION-API"

    reconciled_response = creator.post(
        f"/api/maintenance/migration-runs/{preview['run_id']}/reconcile",
        json={
            "expected_version": preview["version"],
            "operation_key": "migration-api-reconcile-key",
            "reason": "实名完成成本和库存对账",
            "project_signoffs": _project_signoffs(preview),
        },
    )
    assert reconciled_response.status_code == 200, reconciled_response.text
    reconciled = reconciled_response.json()
    assert reconciled["status"] == "reconciled"
    assert reconciled["preview"]["can_approve"] is True

    approver = _client(db, username="migration-api-approver")
    approved_response = approver.post(
        f"/api/maintenance/migration-runs/{preview['run_id']}/approve",
        json={
            "expected_version": reconciled["version"],
            "operation_key": "migration-api-approve-key",
            "reason": "独立审批接口合成 manifest",
            "supplied_fingerprint": reconciled["preview"]["input_fingerprint"],
        },
    )
    assert approved_response.status_code == 200, approved_response.text
    approved = approved_response.json()
    assert approved["status"] == "approved"
    assert approved["manifest"]["production_activation_included"] is False


def test_missing_action_permission_and_shared_admin_both_fail_closed(db, monkeypatch):
    _seed_project(db)
    monkeypatch.setattr(migration_api, "load_project_inventory_movements", _loader)
    denied = _client(
        db,
        username="migration-api-denied",
        role="boss",
        permissions={
            "page_maintenance": True,
            "data_purchase_cost": True,
            "data_profit": True,
            "action_maintenance_migration_review": False,
        },
    )
    response = denied.post(
        "/api/maintenance/migration-runs/preview", json=_preview_body()
    )
    assert response.status_code == 403

    shared = TestClient(app)
    login = shared.post(
        "/api/auth/login",
        json={"username": "admin", "password": get_settings().admin_password},
    )
    assert login.status_code == 200, login.text
    assert login.json()["permissions"]["action_maintenance_migration_review"] is False
    shared.headers["Authorization"] = f"Bearer {login.json()['token']}"
    shared_response = shared.post(
        "/api/maintenance/migration-runs/preview", json=_preview_body()
    )
    assert shared_response.status_code == 403

    creator = _client(db, username="migration-api-read-seed")
    created = creator.post(
        "/api/maintenance/migration-runs/preview", json=_preview_body()
    ).json()
    shared_search = shared.post(
        "/api/maintenance/migration-runs/search",
        json={"statuses": [], "page": 1, "page_size": 20},
    )
    shared_detail = shared.get(f"/api/maintenance/migration-runs/{created['run_id']}")
    assert shared_search.status_code == 403
    assert shared_detail.status_code == 403


def test_api_rejects_unknown_fields_and_changed_idempotent_command(db, monkeypatch):
    _seed_project(db)
    monkeypatch.setattr(migration_api, "load_project_inventory_movements", _loader)
    client = _client(db, username="migration-api-validation")

    invalid = _preview_body()
    invalid["projects"][0]["source_snapshot_hash"] = "c" * 64
    rejected = client.post("/api/maintenance/migration-runs/preview", json=invalid)
    assert rejected.status_code == 422

    created = client.post(
        "/api/maintenance/migration-runs/preview", json=_preview_body()
    )
    assert created.status_code == 201, created.text
    changed = _preview_body()
    changed["reason"] = "复用幂等键但改变审计理由"
    conflict = client.post("/api/maintenance/migration-runs/preview", json=changed)
    assert conflict.status_code == 409


def test_preview_request_limits_reject_oversized_body_and_aggregate_candidates(
    db, monkeypatch
):
    _seed_project(db)
    monkeypatch.setattr(migration_api, "load_project_inventory_movements", _loader)
    client = _client(db, username="migration-api-size-limit")

    oversized = client.post(
        "/api/maintenance/migration-runs/preview",
        content=b"{" + (b"x" * 2_000_001),
        headers={"content-type": "application/json"},
    )
    assert oversized.status_code == 413

    body = _preview_body()
    template = body["projects"][0]
    body["projects"] = []
    for project_index in range(11):
        project = {
            **template,
            "project_id": f"aggregate-project-{project_index}",
            "opening_balances": [],
        }
        for opening_index in range(500):
            project["opening_balances"].append(
                {
                    "balance_key": f"{project_index}:{opening_index}",
                    "pn": "PN-LIMIT",
                    "quantity": "1",
                    "evidence_hash": "b" * 64,
                }
            )
        body["projects"].append(project)
    aggregate = client.post("/api/maintenance/migration-runs/preview", json=body)
    assert aggregate.status_code == 422
    assert "总数" in aggregate.text

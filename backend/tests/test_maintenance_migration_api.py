from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api import maintenance_migration as migration_api
from app.auth import hash_password
from app.business_time import business_today
from app.config import get_settings
from app.main import app
from app.models.dimensions import DimPart
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_project_operations import MaintenanceProjectWorkbookState
from app.models.system import SysUser
from app.services import maintenance_migration_controls as controls


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
    db.add_all(
        [
            MaintenanceProject(
                project_id="migration-api-project",
                project_code="MIGRATION-API",
                display_name="迁移接口合成项目",
                lifecycle_status="ongoing",
            ),
            DimPart(id=21002, pn_std="PN-MIGRATION-API"),
            MaintenanceProjectWorkbookState(
                project_id="migration-api-project",
                revision=0,
                data_version="migration-api-version-0",
                expense_ready_through=business_today().replace(day=1),
            ),
        ]
    )
    db.commit()


def _loader(_db, project_id, _cutover_date, _warehouse_ready_through):
    return (
        [
            {
                "movement_id": "migration-api-delivery:migration-api-delivery-line",
                "document_id": "migration-api-delivery",
                "line_id": "migration-api-delivery-line",
                "document_no": "FH-MIGRATION-API",
                "document_date": "2026-08-02",
                "movement_type": "delivery",
                "source": "maintenance_warehouse_v1",
                "source_document_type": "shipment",
                "source_status": "confirmed",
                "formal_available": False,
                "project_id": project_id,
                "part_id": 21002,
                "balance_key": f"{project_id}:21002",
                "pn": "PN-MIGRATION-API",
                "quantity": "2",
            }
        ],
        True,
    )


def _legacy_loader(_db, project_id, as_of):
    evidence = {
        "cost_lines": [
            {
                "source_order_id": f"{project_id}-legacy-order",
                "source_line_id": f"{project_id}-legacy-line",
                "order_no": "WBDD-MIGRATION-API-LEGACY",
                "order_date": "2026-07-31",
                "pn": "PN-MIGRATION-API",
                "sn": None,
                "demand_quantity": "1",
                "return_quantity": "0",
                "effective_quantity": "1",
                "unit_cost_ex_tax": "100.00",
                "unit_cost_inc_tax": "113.00",
                "cost_tax_basis": "ex",
                "cost_amount_ex_tax": "100.00",
                "cost_amount_inc_tax": "113.00",
            }
        ],
        "expenses": [],
        "source_coverage": {
            "legacy_truth_version": "test-v1",
            "business_as_of": as_of.isoformat(),
        },
    }
    return {
        **evidence,
        "source_hash": controls.canonical_hash(evidence),
        "source_ready": True,
        "blockers": [],
    }


def _preview_body():
    baseline = {
        "amount_ex_tax": "100.00",
        "amount_inc_tax": "113.00",
        "evidence_hash": "a" * 64,
        "coverage_from": "2025-01-01",
        "coverage_through": "2026-07-31",
        "scope": "site_issue_parts_only",
        "excludes_expenses": True,
        "source_artifact_locator": "artifact://migration/api-project/history.xlsx",
        "source_row_count": 10,
    }
    baseline["aggregation_fingerprint"] = (
        controls.historical_baseline_aggregation_fingerprint(baseline)
    )
    return {
        "idempotency_key": "migration-api-preview-key",
        "reason": "建立接口合成 dry-run",
        "projects": [
            {
                "project_id": "migration-api-project",
                "cutover_date": "2026-08-01",
                "warehouse_ready_through": business_today().isoformat(),
                "historical_mode": "approved_cost_baseline",
                "historical_baseline": baseline,
                "opening_balances": [
                    {
                        "balance_key": "migration-api-project:21002",
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
            "expected_truth_comparison_hash": plan["truth_comparison"][
                "truth_comparison_hash"
            ],
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


def test_historical_baseline_request_contract_is_exact_and_half_up():
    project = deepcopy(_preview_body()["projects"][0])
    baseline = project["historical_baseline"]
    baseline["amount_ex_tax"] = "0.50"
    baseline["amount_inc_tax"] = "0.57"
    baseline["aggregation_fingerprint"] = (
        controls.historical_baseline_aggregation_fingerprint(baseline)
    )
    validated = migration_api.ProjectCutoverInput.model_validate(project)
    assert validated.historical_baseline is not None
    assert str(validated.historical_baseline.amount_inc_tax) == "0.57"

    forged = deepcopy(project)
    forged["historical_baseline"]["aggregation_fingerprint"] = "f" * 64
    with pytest.raises(ValidationError, match="聚合指纹"):
        migration_api.ProjectCutoverInput.model_validate(forged)

    wrong_boundary = deepcopy(project)
    wrong_boundary["historical_baseline"]["coverage_through"] = "2026-07-30"
    wrong_boundary["historical_baseline"]["aggregation_fingerprint"] = (
        controls.historical_baseline_aggregation_fingerprint(
            wrong_boundary["historical_baseline"]
        )
    )
    with pytest.raises(ValidationError, match="切换日前一日"):
        migration_api.ProjectCutoverInput.model_validate(wrong_boundary)

    empty_interval = deepcopy(project)
    empty_interval["historical_baseline"]["coverage_from"] = "2026-08-01"
    empty_interval["historical_baseline"]["aggregation_fingerprint"] = (
        controls.historical_baseline_aggregation_fingerprint(
            empty_interval["historical_baseline"]
        )
    )
    with pytest.raises(ValidationError, match="不能为空或倒置"):
        migration_api.ProjectCutoverInput.model_validate(empty_interval)


def test_public_api_supports_preview_search_reconcile_and_independent_approval(
    db, monkeypatch
):
    _seed_project(db)
    monkeypatch.setattr(migration_api, "load_project_inventory_movements", _loader)
    monkeypatch.setattr(migration_api, "load_project_legacy_truth", _legacy_loader)
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
    assert searched.json()["items"][0]["as_of"] == preview["as_of"]

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

    creator_reconcile = creator.post(
        f"/api/maintenance/migration-runs/{preview['run_id']}/reconcile",
        json={
            "expected_version": preview["version"],
            "operation_key": "migration-api-creator-reconcile-key",
            "reason": "创建人不得兼任实名对账人",
            "project_signoffs": _project_signoffs(preview),
        },
    )
    assert creator_reconcile.status_code == 409, creator_reconcile.text
    assert "独立" in creator_reconcile.json()["detail"]

    reconciler = _client(db, username="migration-api-reconciler")
    reconciled_response = reconciler.post(
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
    assert approved["manifest"]["signing_key_id"] == approved["manifest_key_id"]
    assert set(approved["manifest"]["approval_chain"].values()) >= {
        "migration-api-creator",
        "migration-api-reconciler",
        "migration-api-approver",
    }
    manifest_response = approver.get(
        f"/api/maintenance/migration-runs/{preview['run_id']}/manifest"
    )
    assert manifest_response.status_code == 200, manifest_response.text
    assert manifest_response.headers["cache-control"] == "no-store"
    assert manifest_response.json()["projects"][0]["project_id"] == (
        "migration-api-project"
    )


def test_missing_action_permission_and_shared_admin_both_fail_closed(db, monkeypatch):
    _seed_project(db)
    monkeypatch.setattr(migration_api, "load_project_inventory_movements", _loader)
    monkeypatch.setattr(migration_api, "load_project_legacy_truth", _legacy_loader)
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
    monkeypatch.setattr(migration_api, "load_project_legacy_truth", _legacy_loader)
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
    monkeypatch.setattr(migration_api, "load_project_legacy_truth", _legacy_loader)
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

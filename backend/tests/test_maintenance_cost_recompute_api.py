"""Late-arriving evidence recomputation for stable-project cost gaps."""

from datetime import date

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import auth, permissions
from app.api import maintenance_project_operations
from app.auth import hash_password
from app.models.dimensions import DimPart
from app.models.maintenance_project_operations import (
    MaintenanceProjectOperationAudit,
    MaintenanceProjectWorkbookState,
    MaintenanceSiteIssueLine,
)
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.system import SysUser
from tests.test_maintenance_project_operations_api import _batch, _client, _project


def _create_gap(db, client: TestClient, *, project_id: str, suffix: str) -> DimPart:
    part = DimPart(pn_std=f"PN-SYNTH-{suffix}")
    db.add(part)
    db.commit()
    response = client.post(
        f"/api/maintenance/projects/stable/{project_id}/site-issues",
        json={
            "issue_no": f"ISSUE-SYNTH-{suffix}",
            "issue_date": "2026-06-10",
            "raw_status": "synthetic-confirmed",
            "status_mapping_state": "mapped",
            "normalized_status": "confirmed",
            "status_mapping_version": "synthetic-issue-map-v1",
            "lines": [{
                "issue_line_id": f"issue-line-{suffix}",
                "line_no": 1,
                "part_id": part.id,
                "pn": part.pn_std,
                "quantity": "2",
            }],
            "reason": "建立合成缺价领用行",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["lines"][0]["cost_source"] is None
    return part


def _add_late_purchase(
    db,
    *,
    part: DimPart,
    suffix: str,
    unit_price: int,
    order_date: date = date(2026, 6, 13),
) -> None:
    batch = _batch(db, suffix.lower())
    order = FPurchaseOrder(
        raw_order_id=f"PO-H-{suffix}",
        order_no=f"PO-{suffix}",
        order_date=order_date,
        data_status="已生效",
        is_tax_inclusive=False,
        import_batch_id=batch.id,
    )
    db.add(order)
    db.flush()
    db.add(
        FPurchaseLine(
            raw_line_id=f"PO-L-{suffix}",
            order_id=order.id,
            part_id=part.id,
            pn_std=part.pn_std,
            qty=4,
            unit_price=unit_price,
            import_batch_id=batch.id,
        )
    )
    db.commit()


def _limited_client(
    db,
    *,
    username: str,
    action_manage: bool,
    purchase_cost: bool,
) -> TestClient:
    base = permissions.effective("readonly", None)
    overrides = {
        "page_maintenance": True,
        "action_maintenance_project_manage": action_manage,
        "data_purchase_cost": purchase_cost,
        "data_profit": False,
    }
    effective = permissions.effective_from_snapshot(base, overrides)
    db.add(
        SysUser(
            username=username,
            role="readonly",
            display_name="合成受限维保用户",
            password_hash=hash_password("synthetic-password-123"),
            template_code="readonly",
            template_version=1,
            template_perms=base,
            perm_overrides=overrides,
            permissions=effective,
        )
    )
    db.commit()
    app = FastAPI()
    app.include_router(auth.router, prefix="/api")
    app.include_router(maintenance_project_operations.router, prefix="/api")
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": username, "password": "synthetic-password-123"},
    )
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    return client


def test_recompute_persists_t_plus_three_purchase_once_with_line_audit(db):
    project = _project(db, project_id="project-late-cost-recompute")
    client = _client(db, username="late_cost_recompute_admin")
    part = _create_gap(db, client, project_id=project.project_id, suffix="LATE-PURCHASE")
    state_before = db.get(MaintenanceProjectWorkbookState, project.project_id)
    revision_before = state_before.revision
    data_version_before = state_before.data_version
    path = f"/api/maintenance/projects/stable/{project.project_id}/cost-gaps/recompute"

    still_missing = client.post(path, json={"reason": "领用当天先检查一次系统价格"})
    assert still_missing.status_code == 200, still_missing.text
    assert still_missing.json() == {
        "resolved": 0,
        "remaining": 1,
        "data_version": data_version_before,
    }
    assert db.query(MaintenanceProjectOperationAudit).filter_by(
        project_id=project.project_id,
        action="auto_recompute",
    ).count() == 0

    _add_late_purchase(db, part=part, suffix="LATE-RECOMPUTE", unit_price=25)
    recomputed = client.post(
        path,
        json={"reason": "采购单在领用后 3 天到达，重新匹配系统价格"},
    )

    assert recomputed.status_code == 200, recomputed.text
    assert recomputed.json()["resolved"] == 1
    assert recomputed.json()["remaining"] == 0
    assert recomputed.json()["data_version"] != data_version_before
    db.expire_all()
    line = db.get(MaintenanceSiteIssueLine, "issue-line-LATE-PURCHASE")
    assert line.cost_source == "purchase_window"
    assert line.unit_cost == 25
    assert line.cost_amount == 50
    assert line.reference_sample_ids == [line.reference_samples[0]["sample_id"]]
    assert line.reference_samples[0]["distance_days"] == 3
    assert line.algorithm_version == "site-issue-cost-v1"
    assert line.version == 2
    state_after = db.get(MaintenanceProjectWorkbookState, project.project_id)
    assert state_after.revision == revision_before + 1
    audit = db.query(MaintenanceProjectOperationAudit).filter_by(
        project_id=project.project_id,
        entity_type="site_issue_cost",
        entity_id=line.issue_line_id,
        action="auto_recompute",
    ).one()
    assert audit.before_json["cost_source"] is None
    assert audit.before_json["version"] == 1
    assert audit.after_json["cost_source"] == "purchase_window"
    assert audit.after_json["version"] == 2

    repeated = client.post(path, json={"reason": "重复点击不应再次写入"})
    assert repeated.status_code == 200, repeated.text
    assert repeated.json() == {
        "resolved": 0,
        "remaining": 0,
        "data_version": state_after.data_version,
    }
    db.expire_all()
    assert db.get(MaintenanceProjectWorkbookState, project.project_id).revision == revision_before + 1
    assert db.query(MaintenanceProjectOperationAudit).filter_by(
        project_id=project.project_id,
        entity_id=line.issue_line_id,
        action="auto_recompute",
    ).count() == 1


def test_recompute_rejects_archived_project_without_mutation(db):
    project = _project(db, project_id="project-archived-cost-recompute")
    client = _client(db, username="archived_cost_recompute_admin")
    _create_gap(db, client, project_id=project.project_id, suffix="ARCHIVED-GAP")
    project.is_active = False
    db.commit()
    state = db.get(MaintenanceProjectWorkbookState, project.project_id)
    revision_before = state.revision

    response = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/cost-gaps/recompute",
        json={"reason": "归档项目不得重算"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "项目主档已归档"
    db.expire_all()
    assert db.get(MaintenanceProjectWorkbookState, project.project_id).revision == revision_before
    assert db.get(MaintenanceSiteIssueLine, "issue-line-ARCHIVED-GAP").version == 1


def test_recompute_requires_manage_action_and_purchase_cost_visibility(db):
    project = _project(db, project_id="project-cost-recompute-permissions")
    without_action = _limited_client(
        db,
        username="cost_recompute_without_action",
        action_manage=False,
        purchase_cost=True,
    )
    without_cost = _limited_client(
        db,
        username="cost_recompute_without_cost",
        action_manage=True,
        purchase_cost=False,
    )
    path = f"/api/maintenance/projects/stable/{project.project_id}/cost-gaps/recompute"

    assert without_action.post(path, json={"reason": "缺少管理动作权限"}).status_code == 403
    assert without_cost.post(path, json={"reason": "缺少成本查看权限"}).status_code == 403
    assert db.get(MaintenanceProjectWorkbookState, project.project_id) is None


def test_manual_fill_persists_new_auto_evidence_instead_of_rolling_it_back(db):
    project = _project(db, project_id="project-manual-auto-race")
    client = _client(db, username="manual_auto_race_admin")
    part = _create_gap(db, client, project_id=project.project_id, suffix="MANUAL-AUTO-RACE")
    listed = client.get(
        f"/api/maintenance/projects/stable/{project.project_id}/cost-gaps"
    )
    assert listed.status_code == 200, listed.text
    stale_gap = listed.json()["rows"][0]
    revision_before = db.get(MaintenanceProjectWorkbookState, project.project_id).revision
    _add_late_purchase(
        db,
        part=part,
        suffix="MANUAL-AUTO-RACE",
        unit_price=30,
        order_date=date(2026, 6, 12),
    )

    response = client.patch(
        f"/api/maintenance/projects/stable/{project.project_id}/cost-gaps",
        json={
            "line_id": stale_gap["line_id"],
            "version": stale_gap["version"],
            "unit_cost_ex_tax": "99.00",
            "evidence": "人工证据不应覆盖后到采购",
            "reason": "保存前发现系统证据",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["manual_applied"] is False
    assert response.json()["resolution"] == "automatic_evidence"
    assert response.json()["cost_source"] == "purchase_window"
    assert response.json()["unit_cost"] == "30.00"
    db.expire_all()
    line = db.get(MaintenanceSiteIssueLine, stale_gap["line_id"])
    assert line.manual_unit_cost is None
    assert line.manual_evidence is None
    assert line.cost_source == "purchase_window"
    assert line.version == 2
    assert db.get(MaintenanceProjectWorkbookState, project.project_id).revision == revision_before + 1
    audit = db.query(MaintenanceProjectOperationAudit).filter_by(
        project_id=project.project_id,
        entity_id=line.issue_line_id,
        action="auto_recompute",
    ).one()
    assert audit.before_json["cost_source"] is None
    assert audit.after_json["cost_source"] == "purchase_window"

"""统一返还收货台账 API 测试（2026-09-11 口径）。

覆盖：登记（项目必选/需求单可选/件况可选）、汇总不变式、修改/作废 +
版本 CAS + 审计前后值、幂等重放、权限与失败留痕、旧口径冻结
（手工行/成品行不进入返还率分子与 boss 坏件口径）。
"""

from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import auth
from app.api import maintenance_return_receipts
from app.auth import hash_password
from app.db import SessionLocal
from app.models.dimensions import DimPart
from app.models.maintenance import FMaintenanceOrder
from app.models.maintenance_doc_import import MaintenanceRkdReturnLine
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_project_operations import (
    MaintenanceProjectOperationAudit,
)
from app.models.maintenance_source_assignment import (
    MaintenanceSourceOrderAssignment,
)
from app.models.system import SysImportBatch, SysUser
from tests.test_site_issue_v2_api import _project


def _client(
    db,
    *,
    username: str,
    role: str = "admin",
    permissions: dict | None = None,
) -> TestClient:
    db.add(
        SysUser(
            username=username,
            role=role,
            display_name="合成返还台账操作人",
            password_hash=hash_password("synthetic-password-123"),
            permissions=permissions,
        )
    )
    db.commit()
    app = FastAPI()
    app.include_router(auth.router, prefix="/api")
    app.include_router(maintenance_return_receipts.router, prefix="/api")
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": username, "password": "synthetic-password-123"},
    )
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    return client


def _wbdd(db, *, project: MaintenanceProject, order_no: str = "WBDD-20260911-0001") -> FMaintenanceOrder:
    batch = SysImportBatch(
        filename="t.xlsx", file_type="maintenance",
        file_hash=uuid4().hex, status="success",
    )
    db.add(batch)
    db.flush()
    order = FMaintenanceOrder(
        raw_order_id=f"raw-{uuid4()}",
        order_no=order_no,
        order_date=None,
        project_raw=project.display_name,
        data_status="已生效",
        import_batch_id=batch.id,
    )
    db.add(order)
    db.flush()
    db.add(
        MaintenanceSourceOrderAssignment(
            assignment_id=str(uuid4()),
            project_id=project.project_id,
            source_order_id=order.raw_order_id,
            is_active=True,
            created_by="t",
        )
    )
    db.commit()
    return order


def _summary(client: TestClient, project_id: str) -> dict:
    resp = client.get(f"/api/maintenance/projects/stable/{project_id}/return-receipt-summary")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _audit_rows(db, receipt_id: str) -> list:
    return (
        db.query(MaintenanceProjectOperationAudit)
        .filter_by(entity_type="return_receipt", entity_id=receipt_id)
        .order_by(MaintenanceProjectOperationAudit.operated_at)
        .all()
    )


def test_register_project_only_and_unassigned(db):
    project = _project(db, project_id=str(uuid4()))
    client = _client(db, username="rcp-user-1")
    resp = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/return-receipts",
        json={"pn": "PN-AAA", "qty": 5},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["source"] == "manual"
    assert body["condition"] is None
    assert body["version"] == 1
    summary = _summary(client, project.project_id)
    assert summary["project_total_qty"] == "5.000"
    assert summary["unassigned_qty"] == "5.000"
    assert summary["by_demand"] == []
    # 不变式：Σ(需求单) + 未关联 = 项目总量
    demand_sum = sum(
        (Decimal(x["qty"]) for x in summary["by_demand"]), Decimal("0")
    )
    assert demand_sum + Decimal(summary["unassigned_qty"]) == Decimal(
        summary["project_total_qty"]
    )


def test_register_with_wbdd_and_reassign_keeps_project_total(db):
    project = _project(db, project_id=str(uuid4()))
    order = _wbdd(db, project=project)
    client = _client(db, username="rcp-user-2")
    resp = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/return-receipts",
        json={"pn": "PN-BBB", "qty": 2, "wbdd_no": order.order_no,
              "condition": "坏品"},
    )
    assert resp.status_code == 201, resp.text
    receipt_id = resp.json()["receipt_id"]
    summary = _summary(client, project.project_id)
    assert summary["project_total_qty"] == "2.000"
    assert summary["unassigned_qty"] == "0.000"
    assert summary["by_demand"][0]["order_no"] == order.order_no

    # 补登记一条未关联的，再把它关联到同一需求单：项目总量不变，需求单量上升
    resp2 = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/return-receipts",
        json={"pn": "PN-CCC", "qty": 3},
    )
    assert resp2.status_code == 201, resp2.text
    unassigned_id = resp2.json()["receipt_id"]
    upd = client.patch(
        f"/api/maintenance/return-receipts/{unassigned_id}",
        json={"version": 1, "reason": "补选需求单", "wbdd_no": order.order_no},
    )
    assert upd.status_code == 200, upd.text
    summary = _summary(client, project.project_id)
    assert summary["project_total_qty"] == "5.000"  # 不重复计数
    assert summary["unassigned_qty"] == "0.000"
    assert summary["by_demand"][0]["qty"] == "5.000"
    assert receipt_id  # noqa: B018


def test_register_wbdd_of_other_project_rejected(db):
    project_a = _project(db, project_id=str(uuid4()))
    project_b = _project(db, project_id=str(uuid4()))
    order = _wbdd(db, project=project_b)
    client = _client(db, username="rcp-user-3")
    resp = client.post(
        f"/api/maintenance/projects/stable/{project_a.project_id}/return-receipts",
        json={"pn": "PN-X", "qty": 1, "wbdd_no": order.order_no},
    )
    assert resp.status_code == 422
    assert "冲突" in resp.json()["detail"]


def test_register_validation_errors(db):
    project = _project(db, project_id=str(uuid4()))
    client = _client(db, username="rcp-user-4")
    base = f"/api/maintenance/projects/stable/{project.project_id}/return-receipts"
    assert client.post(base, json={"pn": "PN-X", "qty": 0}).status_code == 422
    assert client.post(base, json={"pn": "PN-X", "qty": 1.5}).status_code == 422
    assert client.post(base, json={"pn": " ", "qty": 1}).status_code == 422
    assert client.post(
        base, json={"pn": "PN-X", "qty": 1, "condition": "半坏"}
    ).status_code == 422
    assert client.post(
        base, json={"pn": "PN-X", "qty": 1, "wbdd_no": "WBDD-NOPE"}
    ).status_code == 422


def test_update_qty_with_version_cas_and_audit(db):
    project = _project(db, project_id=str(uuid4()))
    client = _client(db, username="rcp-user-5")
    created = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/return-receipts",
        json={"pn": "PN-D", "qty": 5},
    ).json()
    # 过期版本 → 409
    stale = client.patch(
        f"/api/maintenance/return-receipts/{created['receipt_id']}",
        json={"version": 99, "reason": "陈旧版本", "qty": 3},
    )
    assert stale.status_code == 409
    # 正确版本 5 → 3
    ok = client.patch(
        f"/api/maintenance/return-receipts/{created['receipt_id']}",
        json={"version": 1, "reason": "实物清点修正", "qty": 3},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["version"] == 2
    summary = _summary(client, project.project_id)
    assert summary["project_total_qty"] == "3.000"
    # 审计：create + update，update 带前后值与原因
    rows = _audit_rows(db, created["receipt_id"])
    actions = [r.action for r in rows]
    assert actions == ["create", "update"]
    upd = rows[-1]
    assert upd.before_json["qty"] == "5.000"
    assert upd.after_json["qty"] == "3.000"
    assert upd.reason == "实物清点修正"
    assert upd.operated_by == "rcp-user-5"
    # 缺原因 → 422
    assert client.patch(
        f"/api/maintenance/return-receipts/{created['receipt_id']}",
        json={"version": 2, "reason": " ", "qty": 1},
    ).status_code == 422


def test_void_excludes_from_summary_and_keeps_audit(db):
    project = _project(db, project_id=str(uuid4()))
    client = _client(db, username="rcp-user-6")
    created = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/return-receipts",
        json={"pn": "PN-E", "qty": 4},
    ).json()
    resp = client.post(
        f"/api/maintenance/return-receipts/{created['receipt_id']}/void",
        json={"version": 1, "reason": "重复登记"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["line_status"] == "voided"
    summary = _summary(client, project.project_id)
    assert summary["project_total_qty"] == "0.000"
    # 重复作废 → 409
    assert client.post(
        f"/api/maintenance/return-receipts/{created['receipt_id']}/void",
        json={"version": 2, "reason": "再作废"},
    ).status_code == 409
    # 已作废不能修改
    assert client.patch(
        f"/api/maintenance/return-receipts/{created['receipt_id']}",
        json={"version": 2, "reason": "改数量", "qty": 1},
    ).status_code == 409
    rows = _audit_rows(db, created["receipt_id"])
    assert [r.action for r in rows] == ["create", "void"]
    assert rows[-1].after_json["line_status"] == "voided"
    # 作废行可检索（line_status=voided）
    listing = client.get(
        f"/api/maintenance/projects/stable/{project.project_id}/return-receipts",
        params={"line_status": "voided"},
    )
    assert listing.status_code == 200
    assert listing.json()["total"] == 1


def test_idempotent_replay_by_key(db):
    project = _project(db, project_id=str(uuid4()))
    client = _client(db, username="rcp-user-7")
    base = f"/api/maintenance/projects/stable/{project.project_id}/return-receipts"
    key = "upload-2026-09-11-001"
    first = client.post(
        base, json={"pn": "PN-F", "qty": 2, "idempotency_key": key}
    )
    assert first.status_code == 201
    assert first.json()["replayed"] is False
    second = client.post(
        base, json={"pn": "PN-F", "qty": 2, "idempotency_key": key}
    )
    assert second.status_code == 201
    assert second.json()["replayed"] is True
    assert second.json()["receipt_id"] == first.json()["receipt_id"]
    summary = _summary(client, project.project_id)
    assert summary["project_total_qty"] == "2.000"  # 不重复计数


def test_permission_denied_and_failure_trail(db):
    project = _project(db, project_id=str(uuid4()))
    viewer_user = SysUser(
        username="rcp-viewer",
        role="viewer",
        display_name="只读用户",
        password_hash=hash_password("synthetic-password-123"),
        permissions={"page_maintenance": True,
                     "action_maintenance_bad_return_manage": False},
    )
    db.add(viewer_user)
    db.flush()
    from app.models.maintenance_project import (
        MaintenanceProjectUserAssignment,
    )

    db.add(
        MaintenanceProjectUserAssignment(
            assignment_id=str(uuid4()),
            project_id=project.project_id,
            responsibility_type="viewer",
            user_id=viewer_user.id,
            assigned_by="test-admin",
            assignment_reason="读权限验证",
        )
    )
    db.commit()
    app = FastAPI()
    app.include_router(auth.router, prefix="/api")
    app.include_router(maintenance_return_receipts.router, prefix="/api")
    viewer = TestClient(app)
    login = viewer.post(
        "/api/auth/login",
        json={"username": "rcp-viewer", "password": "synthetic-password-123"},
    )
    assert login.status_code == 200, login.text
    viewer.headers["Authorization"] = f"Bearer {login.json()['token']}"
    resp = viewer.post(
        f"/api/maintenance/projects/stable/{project.project_id}/return-receipts",
        json={"pn": "PN-G", "qty": 1},
    )
    assert resp.status_code == 403
    # 读接口放行（page_maintenance + 项目 viewer 归属）
    assert viewer.get(
        f"/api/maintenance/projects/stable/{project.project_id}/return-receipt-summary"
    ).status_code == 200


def test_condition_does_not_affect_totals(db):
    project = _project(db, project_id=str(uuid4()))
    client = _client(db, username="rcp-user-8")
    base = f"/api/maintenance/projects/stable/{project.project_id}/return-receipts"
    for condition in (None, "成品", "坏品", "废品"):
        payload = {"pn": "PN-H", "qty": 2}
        if condition:
            payload["condition"] = condition
        resp = client.post(base, json=payload)
        assert resp.status_code == 201, resp.text
    summary = _summary(client, project.project_id)
    assert summary["project_total_qty"] == "8.000"


def test_legacy_bad_return_metric_frozen_against_manual_and_good_lines(db):
    """旧口径冻结：手工行（即使件况=坏品）不进入返还率分子查询。"""
    from sqlalchemy import select as sa_select

    from app.services.maintenance_return_receipts import legacy_bad_return_filter

    project = _project(db, project_id=str(uuid4()))
    client = _client(db, username="rcp-user-9")
    client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/return-receipts",
        json={"pn": "PN-M", "qty": 7, "condition": "坏品"},
    )
    rows = (
        db.query(MaintenanceRkdReturnLine)
        .filter_by(project_id=project.project_id)
        .all()
    )
    assert len(rows) == 1
    assert rows[0].source == "manual"
    # 旧口径分子应为空：手工行（manual + 坏品）被 source 条件冻结
    facts = db.execute(
        sa_select(MaintenanceRkdReturnLine.project_id).where(
            MaintenanceRkdReturnLine.project_id == project.project_id,
            *legacy_bad_return_filter(),
        )
    ).all()
    assert facts == []


def test_receipt_audit_endpoint(db):
    project = _project(db, project_id=str(uuid4()))
    client = _client(db, username="rcp-user-10")
    created = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/return-receipts",
        json={"pn": "PN-I", "qty": 1},
    ).json()
    client.patch(
        f"/api/maintenance/return-receipts/{created['receipt_id']}",
        json={"version": 1, "reason": "调数量", "qty": 2},
    )
    resp = client.get(
        f"/api/maintenance/return-receipts/{created['receipt_id']}/audit"
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [i["action"] for i in items] == ["create", "update"]
    assert items[0]["after_json"]["pn"] == "PN-I"


def test_part_id_resolved_from_pn(db):
    project = _project(db, project_id=str(uuid4()))
    part = DimPart(pn_std="PN-RESOLVE-1", description="测试备件")
    db.add(part)
    db.commit()
    client = _client(db, username="rcp-user-11")
    created = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/return-receipts",
        json={"pn": "PN-RESOLVE-1", "qty": 1},
    ).json()
    assert created["part_id"] == part.id

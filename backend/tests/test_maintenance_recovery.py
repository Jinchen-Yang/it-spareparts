"""F4 项目结束收回清单：好件（返库单）/坏件（入库单）/未收回结存。"""

from datetime import datetime, timezone
from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import auth
from app.api import maintenance_recovery
from app.auth import hash_password
from app.models.dimensions import DimPart
from app.models.maintenance_doc_import import (
    MaintenanceDocHeadRow,
    MaintenanceDocImportBatch,
    MaintenanceRkdReturnLine,
)
from app.models.maintenance_project import MaintenanceProject
from app.models.system import SysUser
from app.services import maintenance_front_stock as front_stock
from app.services import maintenance_recovery as recovery
from sqlalchemy import select


def _seed_recovery(db):
    project = MaintenanceProject(
        project_id="recovery-project-1",
        project_code="回收清单测试项目",
        display_name="回收清单测试项目",
        lifecycle_status="ongoing",
        is_active=True,
    )
    db.add(project)
    db.flush()
    part = DimPart(pn_std="RC-A-001", description="收回测试件")
    db.add(part)
    db.flush()
    # 前置库：发货单入账 5 件
    front_stock.apply_movement(
        db,
        project_id="recovery-project-1",
        part_id=part.id,
        kind="shipment_in",
        source_type="f_maintenance_line",
        source_ref="RC-SHIP-1",
        qty=Decimal("5"),
        warehouse_name="回收清单测试项目",
        operated_by="合成测试员",
    )
    # 好件收回：返库单 return_out 2 件
    front_stock.apply_movement(
        db,
        project_id="recovery-project-1",
        part_id=part.id,
        kind="return_out",
        source_type="return_order_line",
        source_ref="return:RKN-001:LID-1",
        qty=Decimal("2"),
        warehouse_name="回收清单测试项目",
        occurred_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
        reason="返库单 RKN-001 未用件收回",
        operated_by="合成测试员",
    )
    # 坏件返还：RKD 入库单事实 1 件（不扣前置库）
    batch = MaintenanceDocImportBatch(
        batch_id="rc-batch-1",
        doc_type="rkd_inbound",
        file_hash="h-rc",
        filename="入库单.xlsx",
        idempotency_key="rc-key-1",
        uploaded_by="合成测试员",
    )
    db.add(batch)
    db.flush()
    head = MaintenanceDocHeadRow(
        row_id="rc-head-1",
        batch_id="rc-batch-1",
        row_no=1,
        raw_json={},
        head_no="RKD-20260811-0001",
    )
    db.add(head)
    db.flush()
    db.add(
        MaintenanceRkdReturnLine(
            rkd_line_id="rc-line-1",
            batch_id="rc-batch-1",
            head_row_id="rc-head-1",
            project_id="recovery-project-1",
            head_no="RKD-20260811-0001",
            source_ref="rkd:rc-1",
            pn="RC-A-001",
            part_id=part.id,
            qty=Decimal("1"),
            test_result="坏品",
            occurred_at=datetime(2026, 8, 11, tzinfo=timezone.utc),
        )
    )
    db.commit()
    return {"project_id": project.project_id, "part_id": part.id}


def _recovery_client(db, *, username: str) -> TestClient:
    existing = db.scalar(select(SysUser).where(SysUser.username == username))
    if existing is None:
        db.add(
            SysUser(
                username=username,
                role="admin",
                display_name="合成收回清单操作人",
                password_hash=hash_password("synthetic-password-123"),
            )
        )
        db.commit()
    app = FastAPI()
    app.include_router(auth.router, prefix="/api")
    app.include_router(maintenance_recovery.router, prefix="/api")
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": username, "password": "synthetic-password-123"},
    )
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    return client


def test_recovery_summary_separates_good_bad_remaining(db):
    _seed_recovery(db)
    summary = recovery.recovery_summary(db, "recovery-project-1")
    assert summary["good_returned_total_qty"] == 2.0
    assert len(summary["good_returned"]) == 1
    assert summary["good_returned"][0]["source_ref"] == "return:RKN-001:LID-1"
    assert summary["bad_returned_total_qty"] == 1.0
    assert len(summary["bad_returned"]) == 1
    assert summary["bad_returned"][0]["head_no"] == "RKD-20260811-0001"
    assert summary["bad_returned"][0]["pn"] == "RC-A-001"
    assert summary["remaining_total_qty"] == 3.0  # 5 入 − 2 收回；坏件不扣账本
    assert len(summary["remaining_stock"]) == 1
    assert summary["remaining_stock"][0]["qty"] == 3.0


def test_recovery_summary_api_requires_project(db):
    _seed_recovery(db)
    client = _recovery_client(db, username="recovery_api_admin")
    ok = client.get(
        "/api/maintenance/projects/stable/recovery-project-1/recovery-summary"
    )
    assert ok.status_code == 200, ok.text
    payload = ok.json()
    assert payload["good_returned_total_qty"] == 2.0
    assert payload["bad_returned_total_qty"] == 1.0
    assert payload["remaining_total_qty"] == 3.0

    missing = client.get(
        "/api/maintenance/projects/stable/no-such-project/recovery-summary"
    )
    assert missing.status_code == 404


def test_recovery_api_masks_cost_without_data_permission(db):
    """无 data_purchase_cost：200 但成本/估值字段脱敏（round-6 Blocker 11）。"""
    from app import permissions as _perms
    from app.auth import hash_password
    from app.models.maintenance_project import MaintenanceProjectUserAssignment
    from datetime import datetime, timezone

    _seed_recovery(db)
    graph = _perms.effective("sales", None)
    graph.update({"page_maintenance": True})
    user = SysUser(
        username="recovery_limited",
        role="sales",
        display_name="无成本权限销售",
        password_hash=hash_password("synthetic-password-123"),
        permissions=graph,
    )
    db.add(user)
    db.flush()
    db.add(
        MaintenanceProjectUserAssignment(
            assignment_id="recovery-assign-1",
            project_id="recovery-project-1",
            responsibility_type="primary_manager",
            user_id=user.id,
            assigned_at=datetime.now(timezone.utc),
            assigned_by="synthetic-admin",
            assignment_reason="合成负责人映射",
        )
    )
    db.commit()
    client = _recovery_client(db, username="recovery_limited")
    ok = client.get(
        "/api/maintenance/projects/stable/recovery-project-1/recovery-summary"
    )
    assert ok.status_code == 200, ok.text
    payload = ok.json()
    for row in payload["remaining_stock"]:
        assert row["unit_cost_ex_tax"] is None
        assert row["value_inc_tax"] is None
    assert payload["remaining_total_qty"] == 3.0

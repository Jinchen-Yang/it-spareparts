"""M4-3：上传顺序无关（relink）——先传 RKD、后建 WBDD 归属也能关联（plan v1.3 §2.4）。"""
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth import hash_password
from app.config import get_settings
from app.etl import pipeline
from app.main import app
from app.models.maintenance import FMaintenanceOrder
from app.models.maintenance_doc_import import (
    MaintenanceDocHeadRow,
    MaintenanceDocImportBatch,
    MaintenanceDocLineRow,
)
from app.models.maintenance_project import MaintenanceProject
from app.models.system import SysUser
from app.services import maintenance_doc_import as docs
from tests.wbdd_fixtures import COLUMNS_91, make_rows, write_workbook

_PASSWORD = "synthetic-password-123"


@pytest.fixture(autouse=True)
def _flag_on():
    """relink 端点是 v1.3 新增写端点，与展示板同受总闸约束（铁律 7）。"""
    settings = get_settings()
    original = settings.maintenance_boss_dashboard_enabled
    settings.maintenance_boss_dashboard_enabled = True
    try:
        yield
    finally:
        settings.maintenance_boss_dashboard_enabled = original


def _admin_client(db, username="relink-admin") -> TestClient:
    db.add(SysUser(username=username, role="admin", display_name=username,
                   password_hash=hash_password(_PASSWORD),
                   permissions={"page_maintenance_beta": True}))
    db.commit()
    client = TestClient(app)
    login = client.post("/api/auth/login",
                        json={"username": username, "password": _PASSWORD})
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    return client


def _project(db, code="合成项目A") -> MaintenanceProject:
    proj = MaintenanceProject(project_id=str(uuid.uuid4()), project_code=code,
                              display_name=code, lifecycle_status="missing")
    db.add(proj)
    db.commit()
    return proj


def _rkd_batch_unlinked(db, *, wbdd_no):
    """先传的 RKD：此刻 WBDD 尚未导入/未归属 → project_id 停在 NULL。"""
    batch = MaintenanceDocImportBatch(
        batch_id=str(uuid.uuid4()), doc_type="rkd_inbound", file_hash="h" * 64,
        filename="rkd.xlsx", idempotency_key=str(uuid.uuid4()), uploaded_by="tester",
        head_rows=1, line_rows=1, issue_rows=0, status="applied",
        applied_by="tester", applied_at=datetime.now(timezone.utc))
    db.add(batch)
    db.flush()
    head = MaintenanceDocHeadRow(
        row_id=str(uuid.uuid4()), batch_id=batch.batch_id, row_no=1, raw_json={},
        head_no="RKD-1", head_date=date(2026, 7, 28), category="维保拆旧返件",
        wbdd_no=wbdd_no, data_status="已生效", project_id=None)
    db.add(head)
    db.flush()
    db.add(MaintenanceDocLineRow(
        row_id=str(uuid.uuid4()), batch_id=batch.batch_id, head_row_id=head.row_id,
        row_no=1, raw_json={}, line_key="L1", pn="PN-SYN-0011",
        qty=Decimal("2"), test_result="坏品"))
    db.commit()
    return head.row_id


def _import_wbdd(db, tmp_path, project="合成项目A"):
    path = write_workbook(str(tmp_path / f"{uuid.uuid4().hex}.xlsx"), COLUMNS_91,
                          make_rows(orders=1, lines_per_order=1, project=project))
    pipeline.run_import(db, path, "w.xlsx", uploaded_by="tester", mode="upsert")
    db.commit()
    return db.execute(select(FMaintenanceOrder)).scalars().one()


def test_rkd_first_then_wbdd_assignment_relinks(db, tmp_path):
    proj = _project(db)
    order_no = "WBDD-20260001"
    head_row_id = _rkd_batch_unlinked(db, wbdd_no=order_no)
    assert db.get(MaintenanceDocHeadRow, head_row_id).project_id is None

    order = _import_wbdd(db, tmp_path)
    assert order.order_no == order_no
    # 仅导入 WBDD 还不够——须有活跃归属才可解析
    assert docs.relink_projects(db)["relinked"] == 0

    client = _admin_client(db)
    assign = client.post("/api/maintenance/project-assignments/orders/assign",
                         json={"project_id": proj.project_id,
                               "items": [{"source_order_id": order.raw_order_id}],
                               "reason": "合成测试确认归属"})
    assert assign.status_code == 200, assign.text
    # 归属确认时自动 relink（同事务），无需再手工调用
    db.expire_all()
    assert db.get(MaintenanceDocHeadRow, head_row_id).project_id == proj.project_id


def test_relink_endpoint_is_idempotent_and_reports_counts(db, tmp_path):
    proj = _project(db)
    head_row_id = _rkd_batch_unlinked(db, wbdd_no="WBDD-20260001")
    order = _import_wbdd(db, tmp_path)
    client = _admin_client(db)
    client.post("/api/maintenance/project-assignments/orders/assign",
                json={"project_id": proj.project_id,
                      "items": [{"source_order_id": order.raw_order_id}],
                      "reason": "合成测试确认归属"})
    # 端点重复调用幂等：已解析行不再计入 relinked
    first = client.post("/api/maintenance/doc-imports/relink-projects")
    assert first.status_code == 200, first.text
    assert first.json() == {"relinked": 0, "still_unlinked": 0, "out_of_scope": 0}
    db.expire_all()
    assert db.get(MaintenanceDocHeadRow, head_row_id).project_id == proj.project_id


def test_relink_reports_still_unlinked(db, tmp_path):
    _rkd_batch_unlinked(db, wbdd_no="WBDD-UNKNOWN")
    client = _admin_client(db)
    result = client.post("/api/maintenance/doc-imports/relink-projects").json()
    assert result["relinked"] == 0 and result["still_unlinked"] == 1


def test_relink_never_overwrites_existing_project(db, tmp_path):
    proj_a, proj_b = _project(db, "项目A"), _project(db, "项目B")
    head_row_id = _rkd_batch_unlinked(db, wbdd_no="WBDD-20260001")
    head = db.get(MaintenanceDocHeadRow, head_row_id)
    head.project_id = proj_b.project_id      # 已有归属（人工/早前解析）
    db.commit()
    order = _import_wbdd(db, tmp_path)
    client = _admin_client(db)
    client.post("/api/maintenance/project-assignments/orders/assign",
                json={"project_id": proj_a.project_id,
                      "items": [{"source_order_id": order.raw_order_id}],
                      "reason": "合成测试确认归属"})
    db.expire_all()
    assert db.get(MaintenanceDocHeadRow, head_row_id).project_id == proj_b.project_id


def test_relink_is_retracted_when_flag_off(db, tmp_path):
    """铁律 7「回滚=关 flag」：关闭总闸后，M4-3 的新写路径必须整体收回。

    端点 404（与未发布不可区分），且归属确认端点回到 v1.2 语义——不再顺带
    改写已应用单据头的 project_id。否则回滚只关掉了展示板，写行为还在跑。
    """
    proj = _project(db)
    head_row_id = _rkd_batch_unlinked(db, wbdd_no="WBDD-20260001")
    order = _import_wbdd(db, tmp_path)
    client = _admin_client(db, username="relink-flagoff-admin")

    settings = get_settings()
    settings.maintenance_boss_dashboard_enabled = False
    resp = client.post("/api/maintenance/doc-imports/relink-projects")
    assert resp.status_code == 404
    # 2026-08-18：归属挂靠路由随 boss 总闸（#45/#48 挂 boss_dependencies）——
    # 关闸后与 relink 一同收回为 404；重新开闸后恢复。
    assign = client.post("/api/maintenance/project-assignments/orders/assign",
                         json={"project_id": proj.project_id,
                               "items": [{"source_order_id": order.raw_order_id}],
                               "reason": "合成测试确认归属"})
    assert assign.status_code == 404, assign.text
    db.expire_all()
    assert db.get(MaintenanceDocHeadRow, head_row_id).project_id is None

    # 重新打开总闸 → 新行为回来（同一份数据，仅 flag 差异）
    settings.maintenance_boss_dashboard_enabled = True
    again = client.post("/api/maintenance/doc-imports/relink-projects")
    assert again.status_code == 200
    assert again.json()["relinked"] == 1
    db.expire_all()
    assert db.get(MaintenanceDocHeadRow, head_row_id).project_id == proj.project_id


def test_relink_permission_matrix(db, tmp_path):
    _rkd_batch_unlinked(db, wbdd_no="WBDD-20260001")
    anon = TestClient(app)
    assert anon.post("/api/maintenance/doc-imports/relink-projects").status_code == 401
    from app import permissions
    base = permissions.effective("readonly", None)
    overrides = {"page_maintenance": True, "page_maintenance_beta": True}
    db.add(SysUser(
        username="relink-noaction", role="readonly", display_name="x",
        password_hash=hash_password(_PASSWORD), is_active=True,
        template_code="readonly", template_version=1, template_perms=base,
        perm_overrides=overrides,
        permissions=permissions.effective_from_snapshot(base, overrides)))
    db.commit()
    client = TestClient(app)
    login = client.post("/api/auth/login",
                        json={"username": "relink-noaction", "password": _PASSWORD})
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    assert client.post("/api/maintenance/doc-imports/relink-projects").status_code == 403

"""M1-6：WBDD 专用上传端点契约（plan v1.3 §4.1）——矩阵/错类型零写入/幂等/flag。"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import permissions
from app.auth import hash_password
from app.config import get_settings
from app.main import app
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder
from app.models.maintenance_wbdd_import import MaintenanceWbddImportReceipt
from app.models.system import SysUser
from tests.wbdd_fixtures import COLUMNS_91, make_rows, write_workbook

_PASSWORD = "synthetic-wbdd-password-1"


@pytest.fixture(autouse=True)
def _boss_flag_on():
    settings = get_settings()
    original = settings.maintenance_boss_dashboard_enabled
    settings.maintenance_boss_dashboard_enabled = True
    try:
        yield
    finally:
        settings.maintenance_boss_dashboard_enabled = original


def _client(db, *, username: str, role: str = "readonly",
            overrides: dict[str, bool] | None = None) -> TestClient:
    base = permissions.effective(role, None)
    effective = permissions.effective_from_snapshot(base, overrides or {})
    db.add(SysUser(
        username=username, role=role, display_name=username,
        password_hash=hash_password(_PASSWORD), is_active=True,
        template_code=role, template_version=1, template_perms=base,
        perm_overrides=overrides or {}, permissions=effective,
    ))
    db.commit()
    client = TestClient(app)
    login = client.post("/api/auth/login",
                        json={"username": username, "password": _PASSWORD})
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    return client


def _upload(client: TestClient, path: str, *, key: str | None = None):
    headers = {"Idempotency-Key": key or f"idem-{uuid.uuid4()}"}
    with open(path, "rb") as f:
        return client.post(
            "/api/maintenance/wbdd-imports",
            files={"file": ("wbdd.xlsx", f,
                            "application/vnd.openxmlformats-officedocument"
                            ".spreadsheetml.sheet")},
            headers=headers,
        )


def _wbdd_file(tmp_path, name="wbdd.xlsx", **kw):
    return write_workbook(str(tmp_path / name), COLUMNS_91,
                          make_rows(orders=1, lines_per_order=1, **kw))


def _zero_rows(db) -> bool:
    return (db.execute(select(func.count(FMaintenanceOrder.id))).scalar_one() == 0
            and db.execute(select(func.count(FMaintenanceLine.id))).scalar_one() == 0)


# ---------- 权限矩阵 ----------

def test_upload_requires_auth(db, tmp_path):
    client = TestClient(app)
    resp = _upload(client, _wbdd_file(tmp_path))
    assert resp.status_code == 401


def test_upload_denied_without_action_key(db, tmp_path):
    client = _client(db, username="wbdd-page-only",
                     overrides={"page_maintenance": True})
    resp = _upload(client, _wbdd_file(tmp_path))
    assert resp.status_code == 403
    assert _zero_rows(db)


def test_upload_denied_without_page(db, tmp_path):
    client = _client(db, username="wbdd-no-page",
                     overrides={"action_maintenance_wbdd_import": True})
    resp = _upload(client, _wbdd_file(tmp_path))
    assert resp.status_code == 403


def test_upload_allowed_with_page_and_action(db, tmp_path):
    client = _client(db, username="wbdd-uploader",
                     overrides={"page_maintenance": True,
                                "action_maintenance_wbdd_import": True})
    resp = _upload(client, _wbdd_file(tmp_path))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["layout"] == "91"
    assert body["orders_inserted"] == 1
    assert "recompute" in body and "snapshot_diff" in body
    assert body["replayed"] is False


def test_wbdd_only_account_cannot_use_generic_import(db, tmp_path):
    """WBDD-only 账号连 /api/import/upload 都是 403（无 page_import）——铁律 6。"""
    client = _client(db, username="wbdd-only",
                     overrides={"page_maintenance": True,
                                "action_maintenance_wbdd_import": True})
    path = _wbdd_file(tmp_path)
    with open(path, "rb") as f:
        resp = client.post("/api/import/upload",
                           files={"file": ("wbdd.xlsx", f, "application/octet-stream")})
    assert resp.status_code == 403


# ---------- 文件类型门（零写入） ----------

def test_non_wbdd_file_rejected_zero_write(db, tmp_path):
    """销售导出（订单编号+业务类型特征）传到 WBDD 端点 → 422 not_wbdd_file 零写入。"""
    from openpyxl import Workbook
    path = str(tmp_path / "sales.xlsx")
    wb = Workbook()
    ws = wb.active
    ws.append(["订单编号", "业务类型", "订单日期"])
    ws.append(["XSDD-1", "备件销售", "2026-01-01"])
    wb.save(path)
    client = _client(db, username="wbdd-up2",
                     overrides={"page_maintenance": True,
                                "action_maintenance_wbdd_import": True})
    resp = _upload(client, path)
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "not_wbdd_file"
    assert _zero_rows(db)
    assert db.execute(select(func.count(MaintenanceWbddImportReceipt.id))
                      ).scalar_one() == 0


def test_narrow_wbdd_file_rejected_layout_unknown(db, tmp_path):
    """能被识别为 maintenance 但非完整 90/91 列布局（截断导出）→ 422 layout_unknown 零写入。"""
    from openpyxl import Workbook
    path = str(tmp_path / "narrow.xlsx")
    wb = Workbook()
    ws = wb.active
    ws.append(["需求单号", "需求类型", "需求明细.数据ID(不可修改)", "需求明细.需求数量"])
    ws.append(["WBDD-1", "报修供货", "L1", "1"])
    wb.save(path)
    client = _client(db, username="wbdd-up3",
                     overrides={"page_maintenance": True,
                                "action_maintenance_wbdd_import": True})
    resp = _upload(client, path)
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "layout_unknown"
    assert _zero_rows(db)


# ---------- 幂等 ----------

def test_idempotency_key_required(db, tmp_path):
    client = _client(db, username="wbdd-up4",
                     overrides={"page_maintenance": True,
                                "action_maintenance_wbdd_import": True})
    path = _wbdd_file(tmp_path)
    with open(path, "rb") as f:
        resp = client.post("/api/maintenance/wbdd-imports",
                           files={"file": ("wbdd.xlsx", f, "application/octet-stream")})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_idempotency_key"


def test_same_idempotency_key_replays_original_report(db, tmp_path):
    client = _client(db, username="wbdd-up5",
                     overrides={"page_maintenance": True,
                                "action_maintenance_wbdd_import": True})
    path = _wbdd_file(tmp_path)
    key = "replay-key-0001"
    first = _upload(client, path, key=key)
    assert first.status_code == 200
    lines_after_first = db.execute(
        select(func.count(FMaintenanceLine.id))).scalar_one()
    second = _upload(client, path, key=key)
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["batch_id"] == first.json()["batch_id"]
    # 重放零新写
    assert db.execute(select(func.count(FMaintenanceLine.id))
                      ).scalar_one() == lines_after_first
    assert db.execute(select(func.count(MaintenanceWbddImportReceipt.id))
                      ).scalar_one() == 1


# ---------- flag 门 ----------

def test_flag_off_hides_endpoints_and_keeps_stable_routes(db, tmp_path):
    client = _client(db, username="wbdd-up6", role="admin",
                     overrides={})
    settings = get_settings()
    settings.maintenance_boss_dashboard_enabled = False
    try:
        resp = _upload(client, _wbdd_file(tmp_path))
        assert resp.status_code == 404
        assert client.get("/api/maintenance/wbdd-imports/latest").status_code == 404
        # 稳定维保端点不受影响
        assert client.get("/api/maintenance/projects").status_code == 200
    finally:
        settings.maintenance_boss_dashboard_enabled = True


# ---------- /latest ----------

def test_latest_health_before_and_after_upload(db, tmp_path):
    client = _client(db, username="wbdd-up7",
                     overrides={"page_maintenance": True,
                                "action_maintenance_wbdd_import": True})
    empty = client.get("/api/maintenance/wbdd-imports/latest")
    assert empty.status_code == 200
    body = empty.json()
    assert body["readiness"] == "not_imported"
    assert body["orders_total"] is None          # 未导入绝不显示 0（铁律 5）
    _upload(client, _wbdd_file(tmp_path))
    after = client.get("/api/maintenance/wbdd-imports/latest").json()
    assert after["readiness"] == "ready"
    assert after["as_of"] == "2026-07-15"
    assert after["layout"] == "91"

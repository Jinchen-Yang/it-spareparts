"""M2-1/M2-3：归属候选生成（只读）+ 预交付方案 B（plan v1.3 §2.3）。"""
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from app.auth import hash_password
from app.etl import pipeline
from app.main import app
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.models.system import SysUser
from app.services import project_names
from tests.wbdd_fixtures import COLUMNS_91, make_rows, write_workbook

_PASSWORD = "synthetic-password-123"


def _admin_client(db, username="cand-admin") -> TestClient:
    db.add(SysUser(
        username=username, role="admin", display_name=username,
        password_hash=hash_password(_PASSWORD),
        permissions={"page_maintenance_beta": True},
    ))
    db.commit()
    client = TestClient(app)
    login = client.post("/api/auth/login",
                        json={"username": username, "password": _PASSWORD})
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    return client


def _project(db, code: str, name: str | None = None) -> MaintenanceProject:
    proj = MaintenanceProject(
        project_id=str(uuid.uuid4()), project_code=code,
        display_name=name or code, lifecycle_status="missing",
    )
    db.add(proj)
    db.commit()
    return proj


def _import_orders(db, tmp_path, *, project: str, orders=1):
    rows = make_rows(orders=orders, lines_per_order=1, project=project)
    path = write_workbook(str(tmp_path / f"{uuid.uuid4().hex}.xlsx"), COLUMNS_91, rows)
    pipeline.run_import(db, path, "wbdd.xlsx", uploaded_by="tester", mode="upsert")
    db.commit()


def test_shared_prefix_helper_variants():
    """M2-3 前缀单一事实源：横线必需（四种横线容差）；无横线不剥。"""
    assert project_names.strip_pre_delivery("预交付-平安银行") == "平安银行"
    assert project_names.strip_pre_delivery("预交付—平安银行") == "平安银行"
    assert project_names.strip_pre_delivery("预交付－平安银行") == "平安银行"
    assert project_names.strip_pre_delivery("预交付实际项目名") == "预交付实际项目名"
    assert project_names.is_pre_delivery("预交付-X") is True
    assert project_names.is_pre_delivery("平安银行") is False
    assert project_names.is_pre_delivery(None) is False


def test_exact_candidate_ranked_first_and_read_only(db, tmp_path):
    _project(db, "平安银行整体维保")
    _project(db, "无关工程")
    _import_orders(db, tmp_path, project="平安银行整体维保")
    client = _admin_client(db)
    resp = client.get("/api/maintenance/project-assignments/orders",
                      params={"assignment_status": "unassigned",
                              "include_candidates": "true"})
    assert resp.status_code == 200, resp.text
    rows = resp.json()["rows"]
    assert len(rows) == 1
    cands = rows[0]["candidates"]
    assert cands, "应生成候选"
    assert cands[0]["match_type"] == "exact" and cands[0]["score"] == 1.0
    assert cands[0]["project_code"] == "平安银行整体维保"
    assert all(c["project_code"] != "无关工程" for c in cands)
    # 纯只读：不自动写归属
    assert db.execute(select(func.count(MaintenanceSourceOrderAssignment.assignment_id))
                      ).scalar_one() == 0


def test_pre_delivery_order_hits_real_project_with_badge(db, tmp_path):
    """预交付单（方案 B）：project_std 已剥前缀 → 候选=真实项目；徽标来自 project_raw。"""
    _project(db, "平安银行整体维保")
    _import_orders(db, tmp_path, project="预交付-平安银行整体维保")
    client = _admin_client(db)
    rows = client.get("/api/maintenance/project-assignments/orders",
                      params={"assignment_status": "unassigned",
                              "include_candidates": "true"}).json()["rows"]
    assert len(rows) == 1
    row = rows[0]
    assert row["is_pre_delivery"] is True
    assert row["project_raw"].startswith("预交付-")
    assert row["project_std"] == "平安银行整体维保"
    assert row["candidates"][0]["project_code"] == "平安银行整体维保"
    assert row["candidates"][0]["match_type"] == "exact"


def test_trgm_candidate_threshold_is_consistent_with_sql(db, tmp_path):
    """trgm 阈值 0.6 与 DB 计算自洽：相似度过线的近名出现在候选、低于线的不出现。"""
    near = _project(db, "平安银行整体维保项目")
    _project(db, "完全不同的另一个工程")
    _import_orders(db, tmp_path, project="平安银行整体维保")
    sim = db.execute(text(
        "SELECT similarity('平安银行整体维保项目', '平安银行整体维保')"
    )).scalar_one()
    client = _admin_client(db)
    rows = client.get("/api/maintenance/project-assignments/orders",
                      params={"assignment_status": "unassigned",
                              "include_candidates": "true"}).json()["rows"]
    codes = [c["project_code"] for c in rows[0]["candidates"]]
    if sim >= 0.6:
        assert near.project_code in codes
        entry = next(c for c in rows[0]["candidates"]
                     if c["project_code"] == near.project_code)
        assert entry["match_type"] == "trgm" and 0.6 <= entry["score"] <= 1.0
    else:
        assert near.project_code not in codes
    assert "完全不同的另一个工程" not in codes


def test_candidates_absent_by_default_and_empty_for_assigned(db, tmp_path):
    proj = _project(db, "合成项目A")
    _import_orders(db, tmp_path, project="合成项目A")
    client = _admin_client(db)
    # 默认不带 candidates 键（响应形状不变，兼容既有前端）
    plain = client.get("/api/maintenance/project-assignments/orders",
                       params={"assignment_status": "all"}).json()["rows"]
    assert "candidates" not in plain[0]
    # 人工确认归属后：include_candidates 下已归属行 candidates=[]
    raw_order_id = plain[0]["raw_order_id"]
    assign = client.post(
        "/api/maintenance/project-assignments/orders/assign",
        json={"project_id": proj.project_id,
              "items": [{"source_order_id": raw_order_id}],
              "reason": "合成测试确认"})
    assert assign.status_code == 200, assign.text
    rows = client.get("/api/maintenance/project-assignments/orders",
                      params={"assignment_status": "all",
                              "include_candidates": "true"}).json()["rows"]
    assert rows[0]["candidates"] == []
    assert rows[0]["assigned_project"]["project_id"] == proj.project_id

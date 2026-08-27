"""维保成本上线前质检修复的回归测试（对应 code-review CONFIRMED 项）。

C00 端点硬鉴权 / C01 readonly 关页 / C02 溢出隔离 / C07 空 qty 语义 /
C09 空单号拦截 / C12 清零收敛 flags / C03 合并 repoint 维保行 /
C15 项目前缀正则 / C19 合同不全标记。
"""
from datetime import date
from decimal import Decimal

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app import config
from app.auth import hash_password
from app.config import get_settings
from app.db import SessionLocal
from app.etl import loader, mapping
from app.etl.transform import transform
from app.main import app
from app.models.maintenance import FMaintenanceLine
from app.models.system import SysImportBatch, SysUser
from app.services import maintenance_cost, merge
from tests import factories as f


@pytest.fixture()
def batch(db):
    b = SysImportBatch(filename="t.xlsx", file_type="maintenance", file_hash="hm2")
    db.add(b)
    db.flush()
    return b


def _line(db, rl):
    return db.execute(select(FMaintenanceLine)
                      .where(FMaintenanceLine.raw_line_id == rl)).scalar_one()


# ---------- C00 / C01：鉴权 ----------

_ENDPOINTS = [
    ("get", "/api/maintenance/projects"),
    ("get", "/api/maintenance/lines?project=x"),
    ("get", "/api/maintenance/export"),
    ("get", "/api/maintenance/lines/export?project=x"),
    ("post", "/api/maintenance/recompute"),
]


@pytest.mark.parametrize("method,url", _ENDPOINTS)
def test_no_token_rejected(db, method, url):
    """匿名请求（无 token）四读端点 + 重算一律 401，绝不放行读成本/写库。"""
    c = TestClient(app)
    r = c.get(url) if method == "get" else c.post(url)
    assert r.status_code == 401, f"{method} {url} 未拦截匿名请求"


def _token(username, password):
    r = TestClient(app).post("/api/auth/login",
                             json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"], r.json()["role"]


def test_readonly_forbidden_admin_ok(db):
    """readonly（含未知用户回退）访问项目成本 403；admin 200。"""
    db.add(SysUser(username="mc_admin", role="admin", is_active=True,
                   password_hash=hash_password("pw_admin_123456")))
    db.commit()
    ro_token, ro_role = _token("ghost_readonly_never_seeded", get_settings().admin_password)
    assert ro_role == "readonly"
    c = TestClient(app)
    r = c.get("/api/maintenance/projects", headers={"Authorization": f"Bearer {ro_token}"})
    assert r.status_code == 403, "readonly 不应看到项目成本（方案 §5）"
    r2 = c.post("/api/maintenance/recompute", headers={"Authorization": f"Bearer {ro_token}"})
    assert r2.status_code == 403
    ad_token, _ = _token("mc_admin", "pw_admin_123456")
    r3 = c.get("/api/maintenance/projects", headers={"Authorization": f"Bearer {ad_token}"})
    assert r3.status_code == 200


def test_recompute_rejects_shared_password_admin_before_write(db, monkeypatch):
    called = False

    def fail_if_recomputed(_db):
        nonlocal called
        called = True
        return {"status": "unexpected"}

    monkeypatch.setattr(maintenance_cost, "recompute", fail_if_recomputed)
    shared_token, shared_role = _token("admin", get_settings().admin_password)
    assert shared_role == "admin"

    response = TestClient(app).post(
        "/api/maintenance/recompute",
        headers={"Authorization": f"Bearer {shared_token}"},
    )

    assert response.status_code == 403
    assert called is False


def test_recompute_allows_active_real_sys_user(db, monkeypatch):
    username = "mc_recompute_real_admin"
    db.add(
        SysUser(
            username=username,
            role="admin",
            is_active=True,
            password_hash=hash_password("pw_admin_123456"),
        )
    )
    db.commit()
    token, role = _token(username, "pw_admin_123456")
    assert role == "admin"
    observed: dict[str, object] = {}

    def successful_recompute(_db):
        observed["called"] = True
        return {"status": "success"}

    monkeypatch.setattr(maintenance_cost, "recompute", successful_recompute)

    response = TestClient(app).post(
        "/api/maintenance/recompute",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "success"}
    assert observed == {"called": True}


def test_recompute_fails_fast_while_import_lock_is_held(db):
    db.add(SysUser(
        username="mc_recompute_busy_admin",
        role="admin",
        is_active=True,
        password_hash=hash_password("pw_admin_123456"),
    ))
    db.commit()
    token, _ = _token("mc_recompute_busy_admin", "pw_admin_123456")

    with SessionLocal() as importer:
        importer.execute(
            text("SELECT pg_advisory_xact_lock(:k)"),
            {"k": config.DATA_CHANGE_ADVISORY_LOCK_KEY},
        )
        response = TestClient(app).post(
            "/api/maintenance/recompute",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 409
    assert response.headers["retry-after"] == "5"
    assert response.json()["detail"] == (
        "维保数据导入或另一轮成本重算正在进行，请稍后重试"
    )


def test_recompute_assignment_race_maps_to_retryable_conflict(db, monkeypatch):
    username = "mc_recompute_assignment_race_admin"
    db.add(SysUser(
        username=username,
        role="admin",
        is_active=True,
        password_hash=hash_password("pw_admin_123456"),
    ))
    db.commit()
    token, _ = _token(username, "pw_admin_123456")

    def conflict(_db):
        raise maintenance_cost.WorkbookInvalidationConflictError(
            "synthetic assignment race"
        )

    monkeypatch.setattr(maintenance_cost, "recompute", conflict)
    response = TestClient(app).post(
        "/api/maintenance/recompute",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 409
    assert response.headers["retry-after"] == "5"
    assert "整体回滚" in response.json()["detail"]


def test_readonly_template_closes_page_maintenance():
    from app import permissions
    assert permissions.template_for("readonly")["page_maintenance"] is False
    # _DEFAULT 与 guest 回退都指向 readonly 模板 → 一并被关
    assert permissions.effective("guest", None)["page_maintenance"] is False


# ---------- C02：Numeric 溢出隔离 ----------

def test_overflow_isolated(db, batch):
    def _load(orders, lines):
        loader.load(
            db, f.purchase_result(orders, lines), batch.id, date(2026, 6, 1)
        )

    _load({"P1": f.purchase_head("P1", on=date(2026, 3, 2))},
          [f.purchase_line("P1", "PL1", "PN-BIG", qty="1", price="99999999999.99")])
    loader.load(db, f.maintenance_result(
        {"M1": f.maintenance_head("M1", on=date(2026, 3, 9))},
        [f.maintenance_line("M1", "ML1", "PN-BIG", qty="9999")]),  # 金额远超 Numeric(14,2)
        batch.id, date(2026, 6, 1))
    db.commit()
    stats = maintenance_cost.recompute(db)
    assert stats["cost_overflow"] == 1
    ln = _line(db, "ML1")
    assert ln.cost_source == "none" and ln.unit_cost is None
    assert "cost_overflow" in ln.anomaly_flags


# ---------- C07：在期但 qty 缺失 ----------

def test_missing_qty_in_scope_is_none(db, batch):
    loader.load(db, f.maintenance_result(
        {"M1": f.maintenance_head("M1", on=date(2026, 3, 9))},
        [{**f.maintenance_line("M1", "ML1", "PN-Q", qty="1"), "qty": None}]),
        batch.id, date(2026, 6, 1))
    db.commit()
    stats = maintenance_cost.recompute(db)
    assert stats["missing_qty"] == 1 and stats["lines_in_scope"] == 1
    ln = _line(db, "ML1")
    assert ln.cost_source == "none" and "missing_qty" in ln.anomaly_flags


# ---------- C12：清零收敛 flags（口径外移后不残留 no_cost）----------

def test_flags_converge_on_scope_change(db, batch, monkeypatch):
    loader.load(db, f.maintenance_result(
        {"M1": f.maintenance_head("M1", on=date(2026, 3, 9))},
        [f.maintenance_line("M1", "ML1", "PN-NONE", qty="1")]),
        batch.id, date(2026, 6, 1))
    db.commit()
    maintenance_cost.recompute(db)
    assert "no_cost" in _line(db, "ML1").anomaly_flags
    # 把起算日移到该行之后 → 行变作用域外，no_cost 应被清零收敛掉
    monkeypatch.setattr(maintenance_cost.config, "MAINT_COST_START_DATE", date(2027, 1, 1))
    stats = maintenance_cost.recompute(db)
    assert stats["out_of_scope"] == 1
    ln = _line(db, "ML1")
    assert ln.cost_source is None and "no_cost" not in ln.anomaly_flags


# ---------- C09：空需求单号拦截 ----------

def test_empty_order_no_skipped():
    rows = [{
        "数据ID(不可修改)": "RID1", "需求单号": None, "制单日期": "2026-03-01",
        "销售订单": None, "项目名": "X", "客户名称": "C", "需求类型": "报修供货",
        "业务类型": "备件维保", "销售人员": "张", "出库仓库(必填)": "仓",
        "维保起始日期": None, "维保终止日期": None, "数据状态": "已生效",
        "需求明细.数据ID(不可修改)": "LID1", "需求明细.序号": 1,
        "需求明细.需供货产品": "PN-1", "需求明细.产品描述": "d",
        "需求明细.需求数量": 1, "需求明细.退货数量": None, "需求明细.发货SN": None,
    }]
    res = transform(pd.DataFrame(rows), mapping.MAINTENANCE)
    assert not res.lines
    assert res.errors and res.errors[0].error_type == "missing_order_no"


# ---------- C15：项目前缀正则（横线必需）----------

def test_project_prefix_requires_dash():
    def std(name):
        rows = [{
            "数据ID(不可修改)": "R", "需求单号": "WB", "制单日期": "2026-03-01",
            "销售订单": None, "项目名": name, "客户名称": "C", "需求类型": "报修供货",
            "业务类型": "备件维保", "销售人员": "张", "出库仓库(必填)": "仓",
            "维保起始日期": None, "维保终止日期": None, "数据状态": "已生效",
            "需求明细.数据ID(不可修改)": "L", "需求明细.序号": 1,
            "需求明细.需供货产品": "PN", "需求明细.产品描述": "d",
            "需求明细.需求数量": 1, "需求明细.退货数量": None, "需求明细.发货SN": None,
        }]
        return transform(pd.DataFrame(rows), mapping.MAINTENANCE).orders["R"]["project_std"]
    assert std("预交付-甲项目") == "甲项目"        # 有横线 → 剥
    assert std("预交付甲项目") == "预交付甲项目"    # 无横线 → 原样（不误剥）


# ---------- C03：合并 repoint 维保行，成本存活 ----------

def test_merge_repoints_maintenance_line(db, batch):
    def _load(orders, lines):
        loader.load(
            db, f.purchase_result(orders, lines), batch.id, date(2026, 6, 1)
        )

    # A、B 同物理件不同 PN；专属采购挂 B、维保出库用 A
    _load({"P1": f.purchase_head("P1", order_no="CG1", on=date(2026, 3, 2),
                                 source_type="维保需求", linked_maintenance_order_no="WB1")},
          [f.purchase_line("P1", "PL1", "PN-B", qty="1", price="100")])
    loader.load(db, f.maintenance_result(
        {"M1": f.maintenance_head("M1", order_no="WB1", on=date(2026, 3, 9))},
        [f.maintenance_line("M1", "ML1", "PN-B", qty="1")]),
        batch.id, date(2026, 6, 1))
    db.commit()
    maintenance_cost.recompute(db)
    assert _line(db, "ML1").cost_source == "direct"
    # 反向：另建一个 A 型号维保行 + A 的采购，然后把 A 合并进 B，成本仍应算得出
    _load({"P2": f.purchase_head("P2", order_no="CG2", on=date(2026, 3, 2),
                                 source_type="维保需求", linked_maintenance_order_no="WB2")},
          [f.purchase_line("P2", "PL2", "PN-A", qty="1", price="50")])
    loader.load(db, f.maintenance_result(
        {"M2": f.maintenance_head("M2", order_no="WB2", on=date(2026, 3, 9))},
        [f.maintenance_line("M2", "ML2", "PN-A", qty="1")]),
        batch.id, date(2026, 6, 1))
    db.commit()
    merge.merge_parts(db, "PN-A", "PN-B", "同物理件", "admin")
    maintenance_cost.recompute(db)
    ln = _line(db, "ML2")
    assert ln.cost_source == "direct" and ln.unit_cost == Decimal("50.00")
    # part_id 已指向合并目标
    target = db.execute(select(FMaintenanceLine.part_id)
                        .where(FMaintenanceLine.raw_line_id == "ML2")).scalar_one()
    b_part = db.execute(select(FMaintenanceLine.part_id)
                        .where(FMaintenanceLine.raw_line_id == "ML1")).scalar_one()
    assert target == b_part


# ---------- C19：合同不全标记 ----------

def test_contract_incomplete_flag(db, batch):
    # 项目关联 XSDD-2（未导入销售）→ contract_incomplete，合同额不按 0 静默低估
    def _load(orders, lines):
        loader.load(
            db, f.purchase_result(orders, lines), batch.id, date(2026, 6, 1)
        )

    _load({"P1": f.purchase_head("P1", on=date(2026, 3, 2))},
          [f.purchase_line("P1", "PL1", "PN-J", qty="1", price="100")])
    loader.load(db, f.maintenance_result(
        {"M1": f.maintenance_head("M1", on=date(2026, 3, 9), project="项目丙",
                                  sales_order="XSDD-MISSING")},
        [f.maintenance_line("M1", "ML1", "PN-J", qty="1")]),
        batch.id, date(2026, 6, 1))
    db.commit()
    maintenance_cost.recompute(db)
    row = maintenance_cost.projects_aggregate(db, lifecycle="all")["rows"][0]
    assert row["contract_incomplete"] is True
    assert row["contract_amount"] is None  # 无收入证据时失败关闭，不制造 0 合同额

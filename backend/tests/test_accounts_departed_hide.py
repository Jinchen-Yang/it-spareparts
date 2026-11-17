"""D-0063 离职销售：账号删除联动单据隐藏（显示口径，可恢复）。

覆盖：账号 API（删除勾选/恢复/独立切换/预览/已删除列表）与销售/采购分析
读路径过滤（列表+导出+人员候选+按销售分组剔除；KPI/其他维度照算）。
"""
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import config
from app.auth import hash_password
from app.main import app
from app.models.dimensions import DimPart
from app.models.purchase import FPurchaseOrder
from app.models.sales import FSalesOrder
from app.models.system import SysAuditLog, SysImportBatch, SysUser
from app.services import departed_visibility, sales_analytics, sales_dashboard
from app.services import purchase_analytics

PASSWORD = "pw123456"


@pytest.fixture()
def admin_client(db):
    db.add(SysUser(username="admin", role="admin", display_name="管理员",
                   password_hash=hash_password("adminpw")))
    db.commit()
    c = TestClient(app)
    tok = c.post("/api/auth/login",
                 json={"username": "admin", "password": "adminpw"}).json()["token"]
    c.headers.update({"Authorization": f"Bearer {tok}"})
    return c


def _departed_account(db, *, username="dep1", salesperson="李离职", hide=True):
    db.add(SysUser(username=username, role="sales", display_name=username,
                   password_hash=hash_password(PASSWORD),
                   salesperson_name=salesperson,
                   deleted_at=date(2026, 11, 17) if hide else None,
                   hide_documents=hide, is_active=False))
    db.commit()


@pytest.fixture()
def orders_seed(db):
    """在职张三 2 单、离职李离职 2 单（含 1 单无关联采购的对照）；采购 2 单其一挂李离职销售单。"""
    batch = SysImportBatch(filename="dep.xlsx", file_type="sales", file_hash="dep-h1")
    db.add(batch)
    db.flush()
    part = DimPart(pn_std="PN-DEP")
    db.add(part)
    db.flush()

    def so(raw, no, sp, *, ex="1000"):
        o = FSalesOrder(raw_order_id=raw, order_no=no, order_date=date(2026, 10, 1),
                        salesperson=sp, business_type="备件销售",
                        amount_ex_tax=Decimal(ex), amount_inc_tax=Decimal(ex),
                        data_status=config.ACTIVE_STATUS, import_batch_id=batch.id)
        db.add(o)
        db.flush()
        return o

    so("D-R1", "SO-DEP-1", "张三")
    so("D-R2", "SO-DEP-2", "张三")
    gone1 = so("D-R3", "SO-DEP-3", "李离职", ex="500")
    so("D-R4", "SO-DEP-4", "李离职", ex="700")
    # NULL 三值语义对照：无销售（NULL）的单不受隐藏影响（not_in 对 NULL 的放行）
    null_so = FSalesOrder(raw_order_id="D-R5", order_no="SO-DEP-5",
                          order_date=date(2026, 10, 4), salesperson=None,
                          business_type="备件销售", amount_ex_tax=Decimal("800"),
                          amount_inc_tax=Decimal("800"),
                          data_status=config.ACTIVE_STATUS,
                          import_batch_id=batch.id)
    db.add(null_so)
    db.flush()
    db.add(FPurchaseOrder(
        raw_order_id="D-P1", order_no="CG-DEP-1", order_date=date(2026, 10, 2),
        purchaser="王采购", source_type="备件采购", source_type_raw="备件采购",
        linked_sales_order_no=gone1.order_no, is_tax_inclusive=True,
        tax_rate=Decimal("0.13"), amount_ex_tax=Decimal("300"),
        amount_inc_tax=Decimal("339"), data_status=config.ACTIVE_STATUS,
        import_batch_id=batch.id))
    db.add(FPurchaseOrder(
        raw_order_id="D-P2", order_no="CG-DEP-2", order_date=date(2026, 10, 3),
        purchaser="王采购", source_type="备件采购", source_type_raw="备件采购",
        linked_sales_order_no="SO-DEP-1", is_tax_inclusive=True,
        tax_rate=Decimal("0.13"), amount_ex_tax=Decimal("400"),
        amount_inc_tax=Decimal("452"), data_status=config.ACTIVE_STATUS,
        import_batch_id=batch.id))
    # 未关联销售单（NULL）的采购单不受隐藏影响
    db.add(FPurchaseOrder(
        raw_order_id="D-P3", order_no="CG-DEP-3", order_date=date(2026, 10, 4),
        purchaser="王采购", source_type="备件采购", source_type_raw="备件采购",
        is_tax_inclusive=True, tax_rate=Decimal("0.13"),
        amount_ex_tax=Decimal("200"), amount_inc_tax=Decimal("226"),
        data_status=config.ACTIVE_STATUS, import_batch_id=batch.id))
    db.commit()


# ---------- 账号 API ----------

def test_delete_with_hide_documents_sets_flag_and_audits(db, admin_client, orders_seed):
    db.add(SysUser(username="dep-x", role="sales", password_hash=hash_password(PASSWORD),
                   salesperson_name="李离职"))
    db.commit()
    r = admin_client.request("DELETE", "/api/accounts/dep-x", json={"hide_documents": True})
    assert r.status_code == 200, r.text
    assert r.json()["hide_documents"] is True
    u = db.scalar(select(SysUser).where(SysUser.username == "dep-x"))
    assert u.deleted_at is not None and u.hide_documents is True
    audits = db.scalars(select(SysAuditLog).where(
        SysAuditLog.action == "account_delete",
        SysAuditLog.entity_id == u.id)).all()
    assert audits and audits[-1].after_json["hide_documents"] is True


def test_delete_with_hide_requires_salesperson_name(db, admin_client):
    db.add(SysUser(username="dep-nomap", role="sales",
                   password_hash=hash_password(PASSWORD)))
    db.commit()
    r = admin_client.request("DELETE", "/api/accounts/dep-nomap", json={"hide_documents": True})
    assert r.status_code == 400
    assert "对应销售名" in r.json()["detail"]
    # 不勾选隐藏时正常删除
    r = admin_client.request("DELETE", "/api/accounts/dep-nomap")
    assert r.status_code == 200 and r.json()["hide_documents"] is False


def test_restore_clears_delete_and_unhides(db, admin_client, orders_seed):
    _departed_account(db)
    r = admin_client.post("/api/accounts/dep1/restore")
    assert r.status_code == 200, r.text
    u = db.scalar(select(SysUser).where(SysUser.username == "dep1"))
    assert u.deleted_at is None and u.hide_documents is False
    assert u.is_active is False   # 恢复不自动启用
    # 未删除的账号不可恢复
    r = admin_client.post("/api/accounts/admin/restore")
    assert r.status_code == 400


def test_documents_visibility_toggle_and_guards(db, admin_client, orders_seed):
    _departed_account(db)
    r = admin_client.put("/api/accounts/dep1/documents-visibility",
                         json={"hidden": False})
    assert r.status_code == 200 and r.json()["hide_documents"] is False
    r = admin_client.put("/api/accounts/dep1/documents-visibility",
                         json={"hidden": True})
    assert r.status_code == 200 and r.json()["hide_documents"] is True
    # 在职账号不可切换
    r = admin_client.put("/api/accounts/admin/documents-visibility",
                         json={"hidden": True})
    assert r.status_code == 400


def test_documents_preview_counts(db, admin_client, orders_seed):
    db.add(SysUser(username="dep-p", role="sales", password_hash=hash_password(PASSWORD),
                   salesperson_name="李离职"))
    db.commit()
    r = admin_client.get("/api/accounts/dep-p/documents-preview")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["salesperson_name"] == "李离职"
    assert body["can_hide"] is True
    assert body["sales_order_count"] == 2          # SO-DEP-3/4（预览只数李离职名下）
    assert body["linked_purchase_order_count"] == 1  # 仅 CG-DEP-1 挂李离职销售单
    # 无对应销售名：can_hide False、计数 0
    db.add(SysUser(username="dep-q", role="sales", password_hash=hash_password(PASSWORD)))
    db.commit()
    body = admin_client.get("/api/accounts/dep-q/documents-preview").json()
    assert body["can_hide"] is False and body["sales_order_count"] == 0


def test_deleted_listing_and_audit_action(db, admin_client, orders_seed):
    _departed_account(db)
    body = admin_client.get("/api/accounts/deleted").json()
    rows = [row for row in body if row["username"] == "dep1"]
    assert rows and rows[0]["hide_documents"] is True
    assert rows[0]["salesperson_name"] == "李离职"
    admin_client.put("/api/accounts/dep1/documents-visibility", json={"hidden": False})
    audits = db.scalars(select(SysAuditLog).where(
        SysAuditLog.action == "account_documents_visibility")).all()
    assert audits, "独立切换必须留审计"


# ---------- 销售分析读路径 ----------

def test_sales_orders_list_and_export_exclude_hidden_but_kpi_keeps(db, orders_seed):
    _departed_account(db)
    assert departed_visibility.hidden_salespersons(db) == {"李离职"}
    # 列表：张三 2 单 + NULL 销售 1 单（NULL 放行）
    page = sales_analytics.orders(db, page=1, page_size=50)
    assert page["total"] == 3
    assert {r["order_no"] for r in page["items"]} == {"SO-DEP-1", "SO-DEP-2", "SO-DEP-5"}
    # 导出同源（同一函数）
    total, rows = sales_dashboard.export_orders(db, sort="order_date", order="desc")
    assert total == 3
    # 汇总照算：4 单金额全计（1000+1000+500+700=3200 未税）
    summary = sales_analytics.summary(db, date_from=date(2026, 9, 1),
                                      date_to=date(2026, 11, 1))
    assert summary["kpi"]["orders"] == 5   # 含 NULL 销售单，照算
    # 人员候选剔除李离职
    opts = sales_analytics.filter_options(db)
    names = [row["value"] for row in opts["salespeople"]]
    assert "李离职" not in names and "张三" in names
    assert opts["hidden_departed_salesperson_count"] == 1


def test_sales_groups_salesperson_dim_drops_hidden_other_dims_keep(db, orders_seed):
    _departed_account(db)
    grp = sales_dashboard.groups(db, group_by="salesperson",
                                 date_from=date(2026, 9, 1), date_to=date(2026, 11, 1))
    keys = [item["key"] for item in grp["groups"]]
    assert "李离职" not in keys and "张三" in keys
    # 其他维度（业务类型）照算 4 单
    bt = sales_dashboard.groups(db, group_by="business_type",
                                date_from=date(2026, 9, 1), date_to=date(2026, 11, 1))
    assert sum(item["orders"] for item in bt["groups"]) == 5


def test_no_hidden_accounts_means_no_filter(db, orders_seed):
    assert departed_visibility.hidden_salespersons(db) == set()
    page = sales_analytics.orders(db, page=1, page_size=50)
    assert page["total"] == 5   # 无人隐藏时行为与现状完全一致（含 NULL 销售单）


# ---------- 采购分析读路径 ----------

def test_purchase_orders_exclude_linked_hidden_but_kpi_keeps(db, orders_seed):
    _departed_account(db)
    assert departed_visibility.hidden_sales_order_nos(db) == {"SO-DEP-3", "SO-DEP-4"}
    page = purchase_analytics.orders(db, page=1, page_size=50)
    # CG-DEP1（挂 SO-DEP-3）被隐藏；CG-DEP2 保留；CG-DEP3（未关联 NULL）放行
    assert page["total"] == 2
    assert {r["order_no"] for r in page["items"]} == {"CG-DEP-2", "CG-DEP-3"}
    # 导出同源
    total, rows = purchase_analytics.export_orders(db)
    assert total == 2
    # KPI 照算：3 单都在
    overview = purchase_analytics.summary(db, group_by="purchaser")
    assert overview["kpi"]["orders"] == 3
    # 采购员分组照算 3 单；按销售分组剔除李离职
    by_purchaser = purchase_analytics.summary(db, group_by="purchaser")
    assert sum(g["orders"] for g in by_purchaser["groups"]) == 3
    by_sales = purchase_analytics.summary(db, group_by="salesperson")
    sales_keys = [g["key"] for g in by_sales["groups"]]
    assert "李离职" not in sales_keys and "张三" in sales_keys
    # 采购 options：关联销售候选剔除李离职 + 隐藏人数
    opts = purchase_analytics.options(db)
    assert "李离职" not in (opts["salespersons"] or [])
    assert opts["hidden_departed_salesperson_count"] == 1

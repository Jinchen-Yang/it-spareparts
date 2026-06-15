"""账号与权限管理：权限解析 + 字段脱敏 + 账号 API（建/改密/停用/勾权限/活动）。"""
import pytest
from fastapi.testclient import TestClient

from app import permissions, security
from app.auth import hash_password
from app.main import app
from app.models.system import SysUser


# ---------- 权限模型（纯函数）----------
def test_role_template_and_admin_full():
    sales = permissions.effective("sales", None)
    # 口径 2026-06-15：销售能看采购成本/毛利（整机拆解加点直卖），但供应商仍隐藏
    assert sales["data_purchase_cost"] is True
    assert sales["data_profit"] is True
    assert sales["data_supplier"] is False
    assert sales["page_purchases"] is True   # 合同重点：销售也能查采购记录
    assert sales["own_customers_only"] is True
    assert sales["page_governance"] is False
    # admin 恒全开，自定义也锁不住
    admin = permissions.effective("admin", {"data_supplier": False, "page_governance": False})
    assert admin["data_supplier"] is True
    assert all(admin[k] for k in permissions.PAGE_KEYS)


def test_custom_overrides_template():
    p = permissions.effective("sales", {"data_purchase_cost": True, "page_governance": True})
    assert p["data_purchase_cost"] is True and p["page_governance"] is True
    assert p["data_customer"] is True   # 模板里原有的保留


def test_hidden_groups_from_perms():
    g = permissions.hidden_groups(permissions.effective("sales", None))
    assert "supplier_info" in g           # 供应商仍遮
    assert "purchase_cost" not in g       # 口径 2026-06-15：销售看成本（整机拆解加点直卖）
    assert "profit_amount" not in g       # 销售看毛利
    assert "customer_info" not in g       # sales 看客户


# ---------- 字段脱敏按 per-user 权限 ----------
def test_field_visibility_masks_by_user_perms():
    ctx = security.UserContext(user_id="liu", role="sales",
                               permissions=permissions.effective("sales", None))
    out = security.apply_field_visibility(
        {"pn_std": "X", "avg_cost": 80, "gross_profit": 20, "supplier_name": "甲供"}, ctx)
    assert out["pn_std"] == "X"
    # 口径 2026-06-15：销售看成本/毛利（不遮），供应商仍遮
    assert out["avg_cost"] == 80 and out["gross_profit"] == 20
    assert out["supplier_name"] is None


def test_scoped_sales_by_perm():
    on = security.UserContext(user_id="liu", role="sales", salesperson_name="刘",
                              permissions={"own_customers_only": True})
    off = security.UserContext(user_id="liu", role="sales", salesperson_name="刘",
                               permissions={"own_customers_only": False})
    assert security.is_scoped_sales(on) and not security.is_scoped_sales(off)


# ---------- 账号 API ----------
@pytest.fixture()
def admin_client(db):
    db.add(SysUser(username="admin", role="admin", display_name="管理员",
                   password_hash=hash_password("adminpw")))
    db.commit()
    c = TestClient(app)
    tok = c.post("/api/auth/login", json={"username": "admin", "password": "adminpw"}).json()["token"]
    c.headers.update({"Authorization": f"Bearer {tok}"})
    return c


def _login(username, password):
    return TestClient(app).post("/api/auth/login", json={"username": username, "password": password})


def test_account_crud_flow(db, admin_client):
    c = admin_client
    r = c.post("/api/accounts", json={"username": "sales_x", "password": "pw123456",
                                      "role": "sales", "display_name": "小销"})
    assert r.status_code == 201, r.text
    assert r.json()["permissions"]["page_governance"] is False     # 套了 sales 模板

    assert any(u["username"] == "sales_x" for u in c.get("/api/accounts").json())

    r = c.put("/api/accounts/sales_x", json={"permissions": {"data_purchase_cost": True}})
    assert r.json()["permissions"]["data_purchase_cost"] is True
    # 他登录拿到的权限反映改动
    assert _login("sales_x", "pw123456").json()["permissions"]["data_purchase_cost"] is True

    assert c.put("/api/accounts/sales_x/password", json={"password": "newpw123"}).status_code == 200
    assert _login("sales_x", "pw123456").status_code == 401
    assert _login("sales_x", "newpw123").status_code == 200

    c.put("/api/accounts/sales_x/active", json={"is_active": False})
    assert _login("sales_x", "newpw123").status_code == 401      # 停用不能登录


def test_admin_protected(db, admin_client):
    assert admin_client.put("/api/accounts/admin/active", json={"is_active": False}).status_code == 400
    assert admin_client.put("/api/accounts/admin", json={"role": "sales"}).status_code == 400


def test_non_admin_forbidden(db):
    db.add(SysUser(username="liu", role="sales", password_hash=hash_password("pw123456")))
    db.commit()
    tok = _login("liu", "pw123456").json()["token"]
    r = TestClient(app).get("/api/accounts", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 403


def test_activity_logged(db, admin_client):
    db.add(SysUser(username="liu", role="sales", password_hash=hash_password("pw123456")))
    db.commit()
    security.record_access_log(security.UserContext(user_id="liu", role="sales"),
                               "search", "MZ7LH960", {"q": "三星"})
    body = admin_client.get("/api/accounts/liu/activity").json()
    assert body["total_actions"] >= 1
    assert body["recent"][0]["resource"] == "MZ7LH960"

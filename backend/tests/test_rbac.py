"""RBAC 防恶性竞争单测：销售只看匿名行情+自己明细，查不到同事报价。"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app import permissions, security
from app.agent import tools
from app.auth import hash_password, verify_password
from app.db import SessionLocal
from app.main import app
from app.models.system import SysUser
from app.services import part_overview as po


@pytest.fixture(scope="module")
def db():
    s = SessionLocal()
    yield s
    s.close()


def _sales_ctx(name):
    return security.UserContext(user_id=name, role="sales", salesperson_name=name)


def _admin_ctx():
    return security.UserContext(user_id="admin", role="admin")


def test_password_hash_roundtrip():
    h = hash_password("s3cret")
    assert verify_password("s3cret", h) and not verify_password("wrong", h)


def test_sales_gets_no_sales_lines():
    """收紧口径（2026-06-13）：受限销售看不到任何逐单成交明细，连自己的也不给。"""
    rows = [
        {"order_no": "A", "customer": "甲公司", "unit_price": 100, "salesperson": "刘青青"},
        {"order_no": "B", "customer": "乙公司", "unit_price": 110, "salesperson": "崔丽娜"},
    ]
    out = security.anonymize_sales_rows(rows, _sales_ctx("刘青青"))
    assert out == []  # 整段不可见——销售只能用聚合（平均售价/加权成交参考价）


def test_non_sales_keeps_lines_without_salesperson():
    rows = [{"order_no": "B", "customer": "乙公司", "unit_price": 110, "salesperson": "崔丽娜"}]
    out = security.anonymize_sales_rows(rows, _admin_ctx())
    assert out[0]["customer"] == "乙公司"      # admin 不脱敏，看得到成交明细
    assert out[0]["unit_price"] == 110
    assert "salesperson" not in out[0]         # 但不暴露是谁卖的


def test_overview_sales_recent_hidden_for_sales(db):
    """真库：销售看任何型号，sales_recent 必为空（聚合字段仍在）。"""
    ov = po.get_overview(db, "ST8000NM000A", _sales_ctx("刘青青"))
    if ov is not None:
        assert ov["sales_recent"] == []                       # 逐单成交明细全部隐藏
        assert "avg_sale_price" in ov["profit_summary"]       # 平均售价仍给
        assert "sale_price_ref" in ov                         # 近期加权成交参考价仍给
        assert "purchases_recent" in ov                       # 采购明细仍给


def test_profit_ranking_blocked_for_sales(db):
    r = tools.dispatch(db, "get_profit_ranking", {"dimension": "customer"}, _sales_ctx("刘青青"))
    assert "error" in r and "无权限" in r["error"]


def test_profit_ranking_ok_for_admin(db):
    r = tools.dispatch(db, "get_profit_ranking", {"dimension": "salesperson"}, _admin_ctx())
    assert "rows" in r  # admin 正常


def test_purchase_visible_to_sales(db):
    """整机拆解需要：销售能看到采购价（防恶性竞争只限同事报价，不限成本）。"""
    r = tools.dispatch(db, "lookup_prices_bulk", {"queries": ["ST8000NM000A"]}, _sales_ctx("刘青青"))
    item = r["results"][0]
    if item["status"] == "ok":
        assert "recent_purchase_avg" in item  # 采购价字段未被遮


# ---------------------------------------------------------------- 毛利反推成本（2026-07-14 审计）

def test_profit_without_cost_combo_flagged():
    """data_profit=True + data_purchase_cost=False：营收 − 毛利可精确重构被遮的
    移动加权成本（part_ranking / sales_orders 同理），combo_errors 必须拦下。"""
    bad = permissions.effective("sales", {"data_purchase_cost": False})
    errs = permissions.combo_errors(bad)
    assert len(errs) == 1 and "反推" in errs[0] and "采购进价" in errs[0]
    # 反向"看成本不看毛利"（purchaser 模板方向）合法
    assert permissions.combo_errors(
        permissions.effective("purchaser", {"data_purchase_cost": True})) == []
    # 成本毛利一起关也合法
    both_off = permissions.effective(
        "sales", {"data_purchase_cost": False, "data_profit": False})
    assert permissions.combo_errors(both_off) == []


def test_role_templates_free_of_illegal_combos():
    """内置角色模板（含 guest/未知角色兜底）必须全部通过 combo 校验——模板改动的回归护栏。"""
    for role in [*permissions.ROLE_TEMPLATES, "guest", "no_such_role"]:
        assert permissions.combo_errors(permissions.effective(role, None)) == [], role


def test_accounts_api_rejects_profit_without_cost(db):
    """账号管理保存路径：建号与改权限都拒绝"开毛利关成本"的自定义组合（唯一可达通道）。"""
    # 本模块 db 夹具不 TRUNCATE，先清同名账号保证可重复跑
    db.execute(delete(SysUser).where(
        SysUser.username.in_(["rbac_combo_admin", "rbac_combo_u"])))
    db.add(SysUser(username="rbac_combo_admin", role="admin",
                   password_hash=hash_password("pw123456")))
    db.commit()
    c = TestClient(app)
    tok = c.post("/api/auth/login", json={"username": "rbac_combo_admin",
                                          "password": "pw123456"}).json()["token"]
    c.headers.update({"Authorization": f"Bearer {tok}"})

    r = c.post("/api/accounts", json={
        "username": "rbac_combo_u", "password": "pw123456", "role": "readonly",
        "permissions": {"data_profit": True, "data_purchase_cost": False}})
    assert r.status_code == 400 and "反推" in r.json()["detail"]

    # 合法建号后，把权限改出该组合同样 400，且原权限不变
    ok = c.post("/api/accounts", json={
        "username": "rbac_combo_u", "password": "pw123456", "role": "sales"})
    assert ok.status_code == 201
    r2 = c.put("/api/accounts/rbac_combo_u",
               json={"permissions": {"data_purchase_cost": False}})
    assert r2.status_code == 400 and "反推" in r2.json()["detail"]
    eff = next(u for u in c.get("/api/accounts").json()
               if u["username"] == "rbac_combo_u")["permissions"]
    assert eff["data_purchase_cost"] is True and eff["data_profit"] is True

"""销售可见性回归（口径 2026-06-15 收敛）：销售能看采购成本/毛利（整机拆解加点直卖需要），
能访问采购记录页；脱敏机制本身（精确 key 匹配 + FIELD_GROUPS 登记派生键防反推）保留。

保留 #18 的脱敏基建（apply_field_visibility 精确匹配、派生键登记 FIELD_GROUPS），
但销售模板把 data_purchase_cost/data_profit/page_purchases 放开——故下面断言成本/毛利
对销售『可见』。真值源唯一在 permissions.ROLE_TEMPLATES。
"""
from fastapi.testclient import TestClient

from app import permissions, security
from app.auth import hash_password
from app.main import app
from app.models.system import SysUser


def _sales_ctx() -> security.UserContext:
    return security.UserContext(
        user_id="liu", role="sales", salesperson_name="刘",
        permissions=permissions.effective("sales", None), is_authenticated=True)


def test_sales_sees_cost_and_margin():
    """销售能看聚合成本/毛利（甲方 2026-06-15 确认：整机拆解加点直卖需要采购价）；
    供应商对销售仍遮（data_supplier=False）。逐单销售成交明细的隐藏不在本测试范围。"""
    payload = {
        "profit_summary": {
            "avg_purchase_cost": 1163.49, "avg_cost_moving": 1156.5, "avg_cost_fifo": 1156.26,
            "avg_margin_moving": 0.14, "avg_margin_fifo": 0.14,
            "avg_sale_price": 1523.8, "total_qty_sold": 1409.0,
        },
        "sale_price_ref": {"ref_sale_price": 1970.0, "ref_sale_samples": 5, "ref_window_days": 30},
        "quick_pricing": {
            "last_purchase_price": 1790.0, "recent_purchase_avg": 1800.0,
            "recent_purchase_min": 1700.0, "recent_purchase_max": 2000.0,
            "avg_sale_price_90d": 1500.0, "stock_total": 51,
            "supplier": "某供应商",
        },
    }
    out = security.apply_field_visibility(payload, _sales_ctx())
    ps = out["profit_summary"]
    for k in ("avg_purchase_cost", "avg_cost_moving", "avg_cost_fifo",
              "avg_margin_moving", "avg_margin_fifo"):
        assert ps[k] is not None, f"{k} 被误遮（销售看不到成本/毛利=整机拆解加点直卖失效）"
    assert ps["avg_sale_price"] == 1523.8
    assert out["sale_price_ref"]["ref_sale_price"] == 1970.0
    qp = out["quick_pricing"]
    for k in ("last_purchase_price", "recent_purchase_avg", "recent_purchase_max"):
        assert qp[k] is not None, f"{k} 被误遮（采购价对销售应可见）"
    assert qp["avg_sale_price_90d"] == 1500.0
    assert qp["supplier"] is None, "供应商对销售仍应遮蔽（data_supplier=False）"


def test_admin_sees_all_aggregates():
    ctx = security.UserContext(user_id="admin", role="admin",
                               permissions=permissions.effective("admin", None))
    out = security.apply_field_visibility(
        {"avg_purchase_cost": 1163.49, "avg_margin_fifo": 0.14}, ctx)
    assert out["avg_purchase_cost"] == 1163.49 and out["avg_margin_fifo"] == 0.14


def test_sales_template_has_no_profit_page():
    # 利润分析接口 require_admin，给 sales 该菜单只会点了 403 → 模板里关掉
    assert permissions.effective("sales", None)["page_profit"] is False


# ---------- require_page：采购记录页后端准入（前端藏菜单≠后端拦接口）----------
def _mk(db, username, role):
    db.add(SysUser(username=username, role=role, password_hash=hash_password("pw123456")))
    db.commit()


def _login(username):
    return TestClient(app).post("/api/auth/login",
                                json={"username": username, "password": "pw123456"})


def test_purchases_recent_accessible_to_sales_and_purchaser(db):
    """合同重点：销售和采购都能查最近采购记录（2026-06-15 确认，撤 #18 对销售的 403）。"""
    _mk(db, "liu", "sales")
    _mk(db, "cai", "purchaser")
    c = TestClient(app)
    sales_tok = _login("liu").json()["token"]
    buyer_tok = _login("cai").json()["token"]
    r_sales = c.get("/api/purchases/recent", headers={"Authorization": f"Bearer {sales_tok}"})
    r_buyer = c.get("/api/purchases/recent", headers={"Authorization": f"Bearer {buyer_tok}"})
    assert r_sales.status_code == 200, "销售应能访问采购记录接口（合同重点）"
    assert r_buyer.status_code == 200, "采购应能访问采购记录接口"

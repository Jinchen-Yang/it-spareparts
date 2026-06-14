"""销售可见性回归：聚合成本/毛利必须遮蔽、售价聚合参考必须保留、采购记录页后端准入。

针对 RBAC 审计发现的“漏字段被反推”泄露做护栏——脱敏走精确 key 匹配，
服务层派生键名（avg_purchase_cost / avg_cost_moving / last_purchase_price …）
必须逐字登记进 config.FIELD_GROUPS，否则会原值漏给 sales。
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


def test_sales_cost_and_margin_aggregates_masked():
    """型号全景 profit_summary + AI 批量查价 quick_pricing 的聚合成本/毛利对 sales 必须为 None；
    售价聚合（avg_sale_price / ref_sale_price / avg_sale_price_90d）必须保留。"""
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
        },
    }
    out = security.apply_field_visibility(payload, _sales_ctx())
    ps = out["profit_summary"]
    for k in ("avg_purchase_cost", "avg_cost_moving", "avg_cost_fifo",
              "avg_margin_moving", "avg_margin_fifo"):
        assert ps[k] is None, f"{k} 未被遮蔽（成本/毛利泄露给销售）"
    assert ps["avg_sale_price"] == 1523.8        # 售价聚合保留=销售定价依据
    assert ps["total_qty_sold"] == 1409.0
    assert out["sale_price_ref"]["ref_sale_price"] == 1970.0
    qp = out["quick_pricing"]
    for k in ("last_purchase_price", "recent_purchase_avg",
              "recent_purchase_min", "recent_purchase_max"):
        assert qp[k] is None, f"{k} 未被遮蔽（采购成本泄露给销售）"
    assert qp["avg_sale_price_90d"] == 1500.0    # 90天均售价保留
    assert qp["stock_total"] == 51


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


def test_purchases_recent_page_gated(db):
    _mk(db, "liu", "sales")
    _mk(db, "cai", "purchaser")
    c = TestClient(app)
    sales_tok = _login("liu").json()["token"]
    buyer_tok = _login("cai").json()["token"]
    r_sales = c.get("/api/purchases/recent", headers={"Authorization": f"Bearer {sales_tok}"})
    r_buyer = c.get("/api/purchases/recent", headers={"Authorization": f"Bearer {buyer_tok}"})
    assert r_sales.status_code == 403, "销售不该能访问采购记录接口"
    assert r_buyer.status_code == 200, "采购应能访问采购记录接口"

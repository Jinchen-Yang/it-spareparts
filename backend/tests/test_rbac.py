"""RBAC 防恶性竞争单测：销售只看匿名行情+自己明细，查不到同事报价。"""
import pytest

from app import security
from app.agent import tools
from app.auth import hash_password, verify_password
from app.db import SessionLocal
from app.services import part_overview as po


@pytest.fixture(scope="module")
def db():
    s = SessionLocal()
    yield s
    s.close()


def _sales_ctx(name):
    return security.UserContext(
        user_id=name,
        role="sales",
        salesperson_name=name,
        is_authenticated=True,
        authn="sys_user",
        token_version=0,
    )


def _admin_ctx():
    return security.UserContext(
        user_id="admin",
        role="admin",
        is_authenticated=True,
        authn="sys_user",
        token_version=0,
    )


@pytest.fixture()
def business_policy_context(monkeypatch):
    """Isolate legacy business-visibility assertions from live identity lifecycle tests."""
    monkeypatch.setattr(tools, "_reload_dispatch_context", lambda _db, ctx: ctx)


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


def test_profit_ranking_blocked_for_sales(db, business_policy_context):
    r = tools.dispatch(db, "get_profit_ranking", {"dimension": "customer"}, _sales_ctx("刘青青"))
    assert "error" in r and "无权限" in r["error"]


def test_profit_ranking_ok_for_admin(db, business_policy_context):
    r = tools.dispatch(db, "get_profit_ranking", {"dimension": "salesperson"}, _admin_ctx())
    assert "rows" in r  # admin 正常


def test_purchase_visible_to_sales(db, business_policy_context):
    """整机拆解需要：销售能看到采购价（防恶性竞争只限同事报价，不限成本）。"""
    r = tools.dispatch(db, "lookup_prices_bulk", {"queries": ["ST8000NM000A"]}, _sales_ctx("刘青青"))
    item = r["results"][0]
    if item["status"] == "ok":
        assert "recent_purchase_avg" in item  # 采购价字段未被遮

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
    return security.UserContext(user_id=name, role="sales", salesperson_name=name)


def _admin_ctx():
    return security.UserContext(user_id="admin", role="admin")


def test_password_hash_roundtrip():
    h = hash_password("s3cret")
    assert verify_password("s3cret", h) and not verify_password("wrong", h)


def test_anonymize_masks_peers():
    rows = [
        {"order_no": "A", "customer": "甲公司", "unit_price": 100, "salesperson": "刘青青"},
        {"order_no": "B", "customer": "乙公司", "unit_price": 110, "salesperson": "崔丽娜"},
    ]
    out = security.anonymize_sales_rows(rows, _sales_ctx("刘青青"))
    mine = next(r for r in out if r["order_no"] == "A")
    peer = next(r for r in out if r["order_no"] == "B")
    assert mine["customer"] == "甲公司"          # 自己的客户保留
    assert peer["customer"] == "（其他客户）"      # 同事的客户被抹
    assert "salesperson" not in peer             # 不暴露是谁卖的
    # 但行情价两行都在（销售能看匿名成交价）
    assert {r["unit_price"] for r in out} == {100, 110}


def test_admin_sees_everything():
    rows = [{"order_no": "B", "customer": "乙公司", "unit_price": 110, "salesperson": "崔丽娜"}]
    out = security.anonymize_sales_rows(rows, _admin_ctx())
    assert out[0]["customer"] == "乙公司"  # admin 不脱敏


def test_overview_sales_recent_scoped(db):
    """真库：销售看某热门型号，同事的客户名应被匿名化（看不到具体是哪家）。"""
    ov = po.get_overview(db, "ST8000NM000A", _sales_ctx("刘青青"))
    if ov and ov["sales_recent"]:
        for r in ov["sales_recent"]:
            assert "salesperson" not in r  # 一律不暴露销售员
        # 至少存在被匿名化的同事行（该型号成交者众多，刘青青不可能独占）
        assert any(r["customer"] == "（其他客户）" for r in ov["sales_recent"])


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

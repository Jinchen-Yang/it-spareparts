"""逐单销售成交明细对受限销售完全隐藏（用户 2026-06-13 确认收紧）。
种真数据验证数据层行为：销售看不到任何逐单成交，只看聚合 + 采购明细。
（与 #18 的 test_sales_visibility 互补：那个测聚合成本/毛利脱敏，这个测逐单明细隐藏。）
"""
from datetime import date

import pytest

from app import security
from app.etl import loader
from app.models.system import SysImportBatch
from app.services import part_overview as po
from tests import factories as f


def _sales_ctx(name="刘青青"):
    # permissions=None → is_scoped_sales 回退按角色=sales（与旧 token 路径一致）
    return security.UserContext(user_id=name, role="sales", salesperson_name=name)


def _admin_ctx():
    return security.UserContext(user_id="admin", role="admin")


@pytest.fixture()
def seeded(db):
    pb = SysImportBatch(filename="p.xlsx", file_type="purchase", file_hash="hpd")
    sb = SysImportBatch(filename="s.xlsx", file_type="sales", file_hash="hsd")
    db.add_all([pb, sb])
    db.flush()
    loader.load(db, f.purchase_result(
        {"PO1": f.purchase_head("PO1")},
        [f.purchase_line("PO1", "PL1", "PN-DTL", qty="10", price="1000")]),
        pb.id, date(2026, 6, 1), mode="skip")
    loader.load(db, f.sales_result(
        {"SO1": f.sales_head("SO1"), "SO2": f.sales_head("SO2")},
        [f.sales_line("SO1", "SL1", "PN-DTL", qty="2", price="1500"),
         f.sales_line("SO2", "SL2", "PN-DTL", qty="3", price="1600")]),
        sb.id, date(2026, 6, 1), mode="skip")
    db.commit()
    return "PN-DTL"


def test_sales_overview_hides_lines_keeps_aggregates(db, seeded):
    ov = po.get_overview(db, seeded, _sales_ctx())
    assert ov["sales_recent"] == []                              # 逐单成交明细：完全隐藏
    assert ov["profit_summary"]["avg_sale_price"] is not None    # 平均售价：仍给
    assert "ref_sale_price" in ov["sale_price_ref"]              # 近期加权成交参考价：仍给
    assert len(ov["purchases_recent"]) >= 1                      # 采购明细：仍给


def test_admin_overview_shows_lines(db, seeded):
    ov = po.get_overview(db, seeded, _admin_ctx())
    assert len(ov["sales_recent"]) == 2                          # admin 看得到两单
    assert all("salesperson" not in r for r in ov["sales_recent"])
    assert {float(r["unit_price"]) for r in ov["sales_recent"]} == {1500.0, 1600.0}


def test_sales_list_sales_endpoint_locked(db, seeded):
    out = po.list_sales(db, seeded, 1, 50, _sales_ctx())
    assert out["items"] == [] and out["total"] == 0
    assert out.get("restricted") is True                         # 区分"无数据"与"按权限不可见"


def test_admin_list_sales_visible(db, seeded):
    out = po.list_sales(db, seeded, 1, 50, _admin_ctx())
    assert out["total"] == 2 and len(out["items"]) == 2
    assert all("salesperson" not in r for r in out["items"])


def test_quick_pricing_is_aggregate_only(db, seeded):
    """批量查价（销售报价主力工具）只暴露聚合，本身不含逐单成交行——各角色同形。"""
    qp = po.quick_pricing(db, seeded)
    assert "avg_sale_price_90d" in qp and "ref_sale_price" in qp
    assert not any(k in qp for k in ("sales_recent", "customer", "order_no"))

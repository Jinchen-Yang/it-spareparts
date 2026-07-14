"""看板 v2：池详情——成员窗口指标（与池均值/约束价差额）、订单板块分页返回 total
（不静默截断）、受限销售板块整段不可见、旧字段兼容。"""
from datetime import date
from decimal import Decimal

import pytest

from app import permissions, security
from app.etl import loader
from app.models.dimensions import DimPart
from app.models.system import SysImportBatch
from app.services import pool, pool_catalog, profit
from tests import factories as f

AS_OF = date(2026, 6, 1)


def _ctx(**over):
    perms = permissions._full()
    perms.update(over)
    return security.UserContext(user_id="u", role="custom", permissions=perms,
                                is_authenticated=True)


@pytest.fixture()
def seeded(db):
    """池（A+B，上限 ex150/下限 ex180）。
    采购：A×10@ex100 + A×10@ex200、B×10@ex300 → 池均 ex200；A 均 150、B 均 300。
    销售：A×2@ex400 + A×1@ex100 → 池销售均 ex300。"""
    a = DimPart(pn_std="PD-A", brand="BA"); bp = DimPart(pn_std="PD-B", brand="BB")
    db.add_all([a, bp]); db.flush()
    created = pool_catalog.create_pool(db, name="池-详情", member_part_ids=[a.id, bp.id],
                                       operated_by="t")
    gid = created["group_id"]
    pool_catalog.set_price_policy(db, group_id=gid, version=1,
                                  purchase_value=Decimal("150"), purchase_basis="ex_tax",
                                  sales_value=Decimal("180"), sales_basis="ex_tax",
                                  operated_by="t")
    b = SysImportBatch(filename="t.xlsx", file_type="purchase", file_hash="hpd")
    db.add(b); db.flush()
    po = {"P1": f.purchase_head("P1", on=date(2026, 1, 5), is_tax_inclusive=True),
          "P2": f.purchase_head("P2", on=date(2026, 1, 10), is_tax_inclusive=True),
          "P3": f.purchase_head("P3", on=date(2026, 1, 15), is_tax_inclusive=True)}
    pl = [f.purchase_line("P1", "L1", "PD-A", qty="10", price="113"),
          f.purchase_line("P2", "L2", "PD-A", qty="10", price="226"),
          f.purchase_line("P3", "L3", "PD-B", qty="10", price="339")]
    loader.load(db, f.purchase_result(po, pl), b.id, AS_OF)
    so = {"S1": f.sales_head("S1", on=date(2026, 2, 1)),
          "S2": f.sales_head("S2", on=date(2026, 2, 10))}
    sl = [f.sales_line("S1", "SL1", "PD-A", qty="2", price="452"),
          f.sales_line("S2", "SL2", "PD-A", qty="1", price="113")]
    loader.load(db, f.sales_result(so, sl), b.id, AS_OF)
    db.commit(); profit.recompute(db)
    return {"gid": gid, "a": a.id, "b": bp.id}


def test_pool_level_metrics_and_policy(db, seeded):
    d = pool.analyze(db, seeded["gid"], as_of=AS_OF, with_v2=True)
    assert d["name"] == "池-详情"
    assert d["purchase_metrics"]["total_amount"] == 6000.0
    assert d["purchase_metrics"]["weighted_avg_unit_price"] == 200.0
    assert d["purchase_metrics"]["order_count"] == 3
    assert d["sales_metrics"]["total_amount"] == 900.0
    assert d["sales_metrics"]["weighted_avg_unit_price"] == 300.0
    assert d["max_purchase_price"] == 150.0 and d["min_sale_price"] == 180.0
    assert d["purchase_violation_count"] == 2      # ex200 + ex300 > 150
    assert d["sale_violation_count"] == 1          # ex100 < 180
    # 旧字段兼容：benchmark/savings/members 遗留结构仍在
    assert "benchmark" in d and "savings" in d and "supply_window" in d


def test_member_metrics_deltas(db, seeded):
    """成员指标：与池均值/人工约束价的差额与比例（成员加权均价为基准）。"""
    d = pool.analyze(db, seeded["gid"], as_of=AS_OF, with_v2=True)
    by = {m["pn_std"]: m for m in d["members"]}
    a, bm = by["PD-A"], by["PD-B"]
    # A：窗口采购均价 (1000+2000)/20 = 150；池均 200 → 差 -50 / -25%
    assert a["purchase_metrics"]["weighted_avg_unit_price"] == 150.0
    assert a["purchase_metrics"]["pool_avg_delta"] == -50.0
    assert a["purchase_metrics"]["pool_avg_delta_pct"] == -0.25
    # A vs 上限 150 → 差 0
    assert a["purchase_metrics"]["manual_limit_delta"] == 0.0
    # B：均价 300；池均 200 → +100 / +50%；vs 上限 150 → +150 / +100%
    assert bm["purchase_metrics"]["pool_avg_delta"] == 100.0
    assert bm["purchase_metrics"]["pool_avg_delta_pct"] == 0.5
    assert bm["purchase_metrics"]["manual_limit_delta"] == 150.0
    assert bm["purchase_metrics"]["manual_limit_delta_pct"] == 1.0
    # A 销售均价 300 = 池均 → 差 0；vs 下限 180 → +120
    assert a["sales_metrics"]["weighted_avg_unit_price"] == 300.0
    assert a["sales_metrics"]["pool_avg_delta"] == 0.0
    assert a["sales_metrics"]["manual_limit_delta"] == 120.0
    # B 无销售：均价 null、订单 0、差额 null（不是 0）
    assert bm["sales_metrics"]["weighted_avg_unit_price"] is None
    assert bm["sales_metrics"]["order_count"] == 0
    assert bm["sales_metrics"]["pool_avg_delta"] is None
    assert bm["sales_metrics"]["manual_limit_delta"] is None


def test_order_sections_paginated_with_total(db, seeded):
    """订单板块分页返回 total，不静默截断。"""
    d = pool.analyze(db, seeded["gid"], as_of=AS_OF, with_v2=True,
                     purchase_page=1, sales_page=1, orders_page_size=2)
    po = d["purchase_orders"]
    assert po["total"] == 3 and len(po["items"]) == 2
    assert po["items"][0]["order_no"] == "P3"      # 日期降序
    row = po["items"][0]
    assert row["pn_std"] == "PD-B" and row["unit_price_ex_tax"] == 300.0
    assert row["amount"] == 3000.0 and row["supplier"] == "测试供应商"
    d2 = pool.analyze(db, seeded["gid"], as_of=AS_OF, with_v2=True,
                      purchase_page=2, sales_page=1, orders_page_size=2)
    assert len(d2["purchase_orders"]["items"]) == 1
    assert d2["purchase_orders"]["total"] == 3
    so = d["sales_orders"]
    assert so["total"] == 2 and so["restricted"] is False
    assert so["items"][0]["order_no"] == "S2"
    assert so["items"][0]["customer"] == "测试客户"
    assert so["items"][0]["salesperson"] == "测试销售"


def test_sections_respect_window(db, seeded):
    d = pool.analyze(db, seeded["gid"], date_from=date(2026, 2, 1), as_of=AS_OF, with_v2=True)
    assert d["purchase_orders"]["total"] == 0
    assert d["sales_orders"]["total"] == 2


def test_scoped_sales_section_restricted(db, seeded):
    """受限销售：销售订单板块=逐单成交明细 → 整段不可见。"""
    ctx = _ctx(own_customers_only=True)
    d = pool.analyze(db, seeded["gid"], as_of=AS_OF, user_ctx=ctx, with_v2=True)
    assert d["sales_orders"]["restricted"] is True
    assert d["sales_orders"]["items"] == [] and d["sales_orders"]["total"] is None
    # 采购板块不受销售行级限制
    assert d["purchase_orders"]["total"] == 3


def test_governance_blind_detail(db, seeded):
    ctx = _ctx(data_pool_price_governance=False)
    d = security.apply_field_visibility(
        pool.analyze(db, seeded["gid"], as_of=AS_OF, user_ctx=ctx, with_v2=True), ctx)
    assert d["manual_reference_restricted"] is True
    assert d["max_purchase_price"] is None and d["min_sale_price"] is None
    assert d["purchase_violation_count"] is None and d["sale_violation_count"] is None
    for m in d["members"]:
        assert m["sales_metrics"]["manual_limit_delta"] is None


def test_list_pools_does_not_run_per_pool_analyze(db, seeded, monkeypatch):
    """池列表批量聚合摘要，不得回退为每池一次 analyze（N-per-pool）。"""
    detail = pool.analyze(db, seeded["gid"], as_of=AS_OF)

    def fail(*_args, **_kwargs):
        raise AssertionError("池列表不应调用单池 analyze")

    monkeypatch.setattr(pool, "analyze", fail)
    out = pool.list_pools(db, as_of=AS_OF, sort="savings")
    assert out["items"], "应有池条目"
    assert "purchase_orders" not in out["items"][0]
    assert out["items"][0]["purchase_metrics"] is not None   # 批量统计块照常合并
    assert out["items"][0]["demand_qty"] == detail["demand"]["total_qty"]
    assert out["items"][0]["demand_revenue_ex_tax"] == detail["demand"]["total_revenue_ex_tax"]
    assert out["items"][0]["theoretical_saving"] == detail["savings"]["theoretical_max"]
    assert out["items"][0]["supply_available_upper"] == detail["savings"]["supply_available_upper"]

"""看板 v2（验收 4/8）：型号排名筛选（时间/part_id/pn/池，单独与组合）+ 精确 PN 不混相似 +
排序分页 + 无成本行不进正式利润但进营收。"""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app import permissions, security
from app.etl import loader
from app.models.dimensions import DimPart
from app.models.system import SysImportBatch
from app.services import dashboard, pool_catalog, profit
from tests import factories as f

AS_OF = date(2026, 6, 1)


def _ctx(**over):
    perms = permissions._full()
    perms.update(over)
    return security.UserContext(user_id="u", role="custom", permissions=perms,
                                is_authenticated=True)


@pytest.fixture()
def seeded(db):
    """PN-A（池成员，有成本，赚）、PN-A1（相似 PN，有成本）、PN-B（池成员无销售）、
    PN-N（无成本行）。窗口：S1=2026-02-01、S2=2026-03-01。"""
    a = DimPart(pn_std="PN-A", brand="BA"); a1 = DimPart(pn_std="PN-A1", brand="BA")
    bp = DimPart(pn_std="PN-B", brand="BB"); n = DimPart(pn_std="PN-N", brand="BN")
    db.add_all([a, a1, bp, n]); db.flush()
    created = pool_catalog.create_pool(db, name="池-AB", member_part_ids=[a.id, bp.id],
                                       operated_by="t")
    b = SysImportBatch(filename="t.xlsx", file_type="purchase", file_hash="hrf")
    db.add(b); db.flush()
    po = {"P1": f.purchase_head("P1", on=date(2026, 1, 5), is_tax_inclusive=True)}
    pl = [f.purchase_line("P1", "PL1", "PN-A", qty="10", price="113"),    # ex100
          f.purchase_line("P1", "PL2", "PN-A1", qty="10", price="113")]   # ex100
    loader.load(db, f.purchase_result(po, pl), b.id, AS_OF)
    so = {"S1": f.sales_head("S1", on=date(2026, 2, 1)),
          "S2": f.sales_head("S2", on=date(2026, 3, 1))}
    sl = [f.sales_line("S1", "SL1", "PN-A", qty="1", price="452"),    # rev400 gp300
          f.sales_line("S1", "SL2", "PN-A1", qty="1", price="226"),   # rev200 gp100
          f.sales_line("S2", "SL3", "PN-A", qty="1", price="452"),    # rev400 gp300
          f.sales_line("S2", "SL4", "PN-N", qty="1", price="113")]    # rev100 无成本
    loader.load(db, f.sales_result(so, sl), b.id, AS_OF)
    db.commit(); profit.recompute(db)
    return {"gid": created["group_id"], "a": a.id, "a1": a1.id, "n": n.id}


def _pns(block):
    return [r["pn_std"] for r in block["items"]]


def test_pn_exact_no_similar_mixin(db, seeded):
    """精确 PN：搜 PN-A 绝不混入 PN-A1。"""
    d = dashboard.part_ranking(db, None, None, pn="PN-A", as_of=AS_OF)
    assert _pns(d["ranking"]) == ["PN-A"]
    assert d["ranking"]["total"] == 1
    assert d["filters"] == {"part_id": None, "pn": "PN-A", "pool_group_id": None}


def test_part_id_wins_over_pn(db, seeded):
    d = dashboard.part_ranking(db, None, None, part_id=seeded["a1"], pn="PN-A", as_of=AS_OF)
    assert _pns(d["ranking"]) == ["PN-A1"]


def test_pool_filter(db, seeded):
    d = dashboard.part_ranking(db, None, None, pool_group_id=seeded["gid"], as_of=AS_OF)
    # 池成员且窗口内有销售的只有 PN-A（PN-B 无销售不出现，PN-A1/PN-N 非成员）
    assert _pns(d["ranking"]) == ["PN-A"]
    row = d["ranking"]["items"][0]
    assert row["pool_group_id"] == seeded["gid"] and row["pool_name"] == "池-AB"
    assert row["order_count"] == 2


def test_date_and_combined_filters(db, seeded):
    # 只取 3 月：PN-A 只剩 S2 一单
    d = dashboard.part_ranking(db, date(2026, 3, 1), None, pn="PN-A", as_of=AS_OF)
    row = d["ranking"]["items"][0]
    assert row["order_count"] == 1 and row["revenue"] == 400.0
    # 时间 + 池组合：3 月窗口内池成员
    d2 = dashboard.part_ranking(db, date(2026, 3, 1), None,
                                pool_group_id=seeded["gid"], as_of=AS_OF)
    assert _pns(d2["ranking"]) == ["PN-A"]
    # 时间窗完全错开 → 空
    d3 = dashboard.part_ranking(db, date(2026, 4, 1), None, pn="PN-A", as_of=AS_OF)
    assert d3["ranking"]["total"] == 0 and d3["ranking"]["items"] == []


def test_sort_and_pagination(db, seeded):
    d = dashboard.part_ranking(db, None, None, sort="revenue", order="desc",
                               page=1, page_size=2, as_of=AS_OF)
    assert d["ranking"]["total"] == 3          # PN-A(800) PN-A1(200) PN-N(100)
    assert _pns(d["ranking"]) == ["PN-A", "PN-A1"]
    d2 = dashboard.part_ranking(db, None, None, sort="revenue", order="desc",
                                page=2, page_size=2, as_of=AS_OF)
    assert _pns(d2["ranking"]) == ["PN-N"]
    d3 = dashboard.part_ranking(db, None, None, sort="revenue", order="asc",
                                page=1, page_size=3, as_of=AS_OF)
    assert _pns(d3["ranking"]) == ["PN-N", "PN-A1", "PN-A"]


def test_no_cost_rows_in_revenue_not_in_profit(db, seeded):
    """验收 8：无成本 PN-N 不进正式利润（gp null、不进盈亏榜），但营收照常。"""
    d = dashboard.part_ranking(db, None, None, sort="gross_profit", as_of=AS_OF)
    assert "PN-N" not in [r["pn_std"] for r in d["profitable"] + d["loss"]]
    assert d["counts"]["no_cost_parts"] == 1
    n_row = next(r for r in d["ranking"]["items"] if r["pn_std"] == "PN-N")
    assert n_row["revenue"] == 100.0
    assert n_row["gross_profit_moving"] is None and n_row["revenue_costed"] is None
    # 毛利降序：无成本行垫底
    assert d["ranking"]["items"][-1]["pn_std"] == "PN-N"


def test_profit_restricted_gp_sort_falls_back(db, seeded):
    """无利润权限 + 毛利排序：行序即侧信道 → 退回营收排序并置旗标；盈亏榜撤下不变。"""
    ctx = _ctx(data_profit=False)
    d = dashboard.part_ranking(db, None, None, sort="gross_profit", as_of=AS_OF, user_ctx=ctx)
    assert d["profit_restricted"] is True
    assert d["profitable"] == [] and d["loss"] == []
    assert d["ranking"]["ranking_restricted"] is True
    assert d["ranking"]["effective_sort"] == "revenue"
    masked = security.apply_field_visibility(d, ctx)
    for r in masked["ranking"]["items"]:
        assert r["gross_profit_moving"] is None and r["gross_profit_fifo"] is None


def test_future_and_inactive_excluded(db, seeded):
    """验收 7（排名口径）：未来单与非已生效单不进统计。"""
    b = SysImportBatch(filename="t2.xlsx", file_type="sales", file_hash="hrf2")
    db.add(b); db.flush()
    so = {"SF": f.sales_head("SF", on=date(2026, 12, 1)),                       # 未来
          "SC": f.sales_head("SC", on=date(2026, 3, 5), data_status="已取消")}  # 取消
    sl = [f.sales_line("SF", "SLF", "PN-A", qty="100", price="452"),
          f.sales_line("SC", "SLC", "PN-A", qty="100", price="452")]
    loader.load(db, f.sales_result(so, sl), b.id, AS_OF)
    db.commit(); profit.recompute(db)
    d = dashboard.part_ranking(db, None, None, pn="PN-A", as_of=AS_OF)
    row = d["ranking"]["items"][0]
    assert row["revenue"] == 800.0 and row["order_count"] == 2   # 仍只有 S1+S2

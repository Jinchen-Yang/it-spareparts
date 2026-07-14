"""看板 v2（验收 5/6/7/10）：池列表窗口统计与独立手算一致、服务端排序、越线计数、
归档池排除、未来/非生效单排除、无约束 → violation_count=null。"""
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
    """池1（A+B，上限 ex150/下限 ex180）：
      采购 A×10@ex100、A×10@ex200（1 行越上限）、B×10@ex300（1 行越上限）
      销售 A×2@ex400、A×1@ex100（1 行破下限）
      干扰：已取消采购 A×100@ex500、未来销售 A×100@ex400 —— 均不得进统计。
    池2（C+D，无约束价）：采购 C×1@ex50。"""
    a = DimPart(pn_std="PL-A"); bp = DimPart(pn_std="PL-B")
    c = DimPart(pn_std="PL-C"); dp = DimPart(pn_std="PL-D")
    db.add_all([a, bp, c, dp]); db.flush()
    p1 = pool_catalog.create_pool(db, name="池一", member_part_ids=[a.id, bp.id], operated_by="t")
    pool_catalog.set_price_policy(db, group_id=p1["group_id"], version=1,
                                  purchase_value=Decimal("150"), purchase_basis="ex_tax",
                                  sales_value=Decimal("180"), sales_basis="ex_tax",
                                  operated_by="t")
    p2 = pool_catalog.create_pool(db, name="池二", member_part_ids=[c.id, dp.id], operated_by="t")
    b = SysImportBatch(filename="t.xlsx", file_type="purchase", file_hash="hpb")
    db.add(b); db.flush()
    po = {"P1": f.purchase_head("P1", on=date(2026, 1, 5), is_tax_inclusive=True),
          "P2": f.purchase_head("P2", on=date(2026, 1, 10), is_tax_inclusive=True),
          "P3": f.purchase_head("P3", on=date(2026, 1, 15), is_tax_inclusive=True),
          "P4": f.purchase_head("P4", on=date(2026, 1, 20), is_tax_inclusive=True),
          "PC": f.purchase_head("PC", on=date(2026, 1, 25), is_tax_inclusive=True,
                                data_status="已取消")}
    pl = [f.purchase_line("P1", "L1", "PL-A", qty="10", price="113"),     # ex100
          f.purchase_line("P2", "L2", "PL-A", qty="10", price="226"),     # ex200 越
          f.purchase_line("P3", "L3", "PL-B", qty="10", price="339"),     # ex300 越
          f.purchase_line("P4", "L4", "PL-C", qty="1", price="56.5"),     # ex50（池二）
          f.purchase_line("PC", "LC", "PL-A", qty="100", price="565")]    # 取消单，不计
    loader.load(db, f.purchase_result(po, pl), b.id, AS_OF)
    so = {"S1": f.sales_head("S1", on=date(2026, 2, 1)),
          "S2": f.sales_head("S2", on=date(2026, 2, 10)),
          "SF": f.sales_head("SF", on=date(2026, 12, 1))}                 # 未来单
    sl = [f.sales_line("S1", "SL1", "PL-A", qty="2", price="452"),        # ex400
          f.sales_line("S2", "SL2", "PL-A", qty="1", price="113"),        # ex100 破下限
          f.sales_line("SF", "SLF", "PL-A", qty="100", price="452")]      # 未来，不计
    loader.load(db, f.sales_result(so, sl), b.id, AS_OF)
    db.commit(); profit.recompute(db)
    return {"g1": p1["group_id"], "g2": p2["group_id"], "d": dp.id}


def _items(db, sort, ctx=None, **kw):
    return pool.list_pools(db, as_of=AS_OF, sort=sort, user_ctx=ctx, **kw)


def test_metrics_match_hand_computed(db, seeded):
    """验收 5：合计金额/加权均价/订单数/数量/最近日期与手算 fixture 一致；
    验收 7：取消单与未来单不进统计。"""
    out = _items(db, "purchase_total")
    by = {i["group_id"]: i for i in out["items"]}
    g1 = by[seeded["g1"]]
    # 采购：100*10 + 200*10 + 300*10 = 6000（取消单不计）
    assert g1["purchase_metrics"]["total_amount"] == 6000.0
    assert g1["purchase_metrics"]["total_quantity"] == 30.0
    assert g1["purchase_metrics"]["weighted_avg_unit_price"] == 200.0
    assert g1["purchase_metrics"]["order_count"] == 3
    assert g1["purchase_metrics"]["latest_date"] == "2026-01-15"
    # 销售：2*400 + 1*100 = 900（未来单不计）
    assert g1["sales_metrics"]["total_amount"] == 900.0
    assert g1["sales_metrics"]["total_quantity"] == 3.0
    assert g1["sales_metrics"]["weighted_avg_unit_price"] == 300.0
    assert g1["sales_metrics"]["order_count"] == 2
    assert g1["sales_metrics"]["latest_date"] == "2026-02-10"
    # 约束价与越线（严格不等；ex200/ex300 越上限、ex100 破下限）
    assert g1["max_purchase_price"] == 150.0 and g1["min_sale_price"] == 180.0
    assert g1["purchase_violation_count"] == 2
    assert g1["sale_violation_count"] == 1
    assert g1["member_count"] == 2


def test_no_policy_pool_violations_null(db, seeded):
    """验收 10：无约束价的池 violation_count=null（不是 0），约束价字段 null。"""
    out = _items(db, "purchase_total")
    g2 = {i["group_id"]: i for i in out["items"]}[seeded["g2"]]
    assert g2["max_purchase_price"] is None and g2["min_sale_price"] is None
    assert g2["purchase_violation_count"] is None
    assert g2["sale_violation_count"] is None
    assert g2["purchase_metrics"]["total_amount"] == 50.0
    # 无销售 → 数量 0：均价必须 null 而非 0
    assert g2["sales_metrics"]["weighted_avg_unit_price"] is None
    assert g2["sales_metrics"]["order_count"] == 0


def test_server_side_sorts(db, seeded):
    g1, g2 = seeded["g1"], seeded["g2"]
    assert [i["group_id"] for i in _items(db, "purchase_total")["items"]] == [g1, g2]
    assert [i["group_id"] for i in _items(db, "sales_total")["items"]] == [g1, g2]
    # purchase_average：池一 200 vs 池二 50
    assert [i["group_id"] for i in _items(db, "purchase_average")["items"]] == [g1, g2]
    # 越线排序：有约束的池一在前，无约束(null)垫底
    assert [i["group_id"] for i in _items(db, "purchase_violation_count")["items"]] == [g1, g2]
    assert [i["group_id"] for i in _items(db, "sale_violation_count")["items"]] == [g1, g2]
    out = _items(db, "sales_average")
    assert out["effective_sort"] == "sales_average" and out["ranking_restricted"] is False
    # 旧排序仍可用且带上统计块（兼容 + 增量）
    legacy = _items(db, "member_count")
    assert legacy["items"][0]["purchase_metrics"] is not None


def test_archived_pool_leaves_stats_and_sorts(db, seeded):
    """验收 6：归档池不进列表统计/排序/总数。"""
    pool_catalog.archive_pool(db, group_id=seeded["g2"], version=1, operated_by="t")
    for sort in ("purchase_total", "sales_average", "purchase_violation_count", "member_count"):
        out = _items(db, sort)
        assert out["total"] == 1, f"sort={sort}"
        assert [i["group_id"] for i in out["items"]] == [seeded["g1"]], f"sort={sort}"


def test_window_filter_respected(db, seeded):
    """时间筛选：只取 2 月 → 采购指标空、销售指标只剩 2 月两单。"""
    out = pool.list_pools(db, date_from=date(2026, 2, 1), date_to=date(2026, 2, 28),
                          as_of=AS_OF, sort="sales_total")
    g1 = {i["group_id"]: i for i in out["items"]}[seeded["g1"]]
    assert g1["purchase_metrics"]["total_amount"] is None
    assert g1["purchase_metrics"]["order_count"] == 0
    assert g1["sales_metrics"]["total_amount"] == 900.0
    assert out["window"]["date_from"] == "2026-02-01"


def test_restricted_sorts_fall_back(db, seeded):
    """结构性排序保护：被遮指标不能当排序键（行序即侧信道）。"""
    cost_blind = _ctx(data_purchase_cost=False)
    out = _items(db, "purchase_total", ctx=cost_blind)
    assert out["ranking_restricted"] is True and out["effective_sort"] == "member_count"
    gov_blind = _ctx(data_pool_price_governance=False)
    out2 = _items(db, "sale_violation_count", ctx=gov_blind)
    assert out2["ranking_restricted"] is True and out2["effective_sort"] == "member_count"
    # 销售聚合公开：cost-blind 仍可按销售额排序
    out3 = _items(db, "sales_total", ctx=cost_blind)
    assert out3["ranking_restricted"] is False and out3["effective_sort"] == "sales_total"


def test_governance_blind_hides_constraints_and_counts(db, seeded):
    ctx = _ctx(data_pool_price_governance=False)
    out = security.apply_field_visibility(_items(db, "member_count", ctx=ctx), ctx)
    for i in out["items"]:
        assert i["max_purchase_price"] is None and i["min_sale_price"] is None
        assert i["purchase_violation_count"] is None and i["sale_violation_count"] is None

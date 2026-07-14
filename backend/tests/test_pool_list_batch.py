"""池清单旧 savings 字段批量化（看板 v2 后续工单，PR #93 已知限制 6）。

list_pools 的 sort=savings / member_count 两条旧路径不再逐池调用 analyze：
- 批量结果与 analyze 单池语义**逐字段相等**（demand_qty / demand_revenue_ex_tax /
  theoretical_saving / supply_available_upper，含供应窗口不被页面窗口截断的场景）；
- 查询数不随池数增长（原 ~5-6 条/池 × 池数 → 固定条数）。
"""
from contextlib import contextmanager
from datetime import date

import pytest
from sqlalchemy import event

from app.db import engine
from app.etl import loader
from app.models.dimensions import DimPart
from app.models.system import SysImportBatch
from app.services import pool, pool_catalog, profit
from tests import factories as f

AS_OF = date(2026, 6, 1)


@contextmanager
def count_queries():
    """统计底层 cursor 执行次数（N+1 证据用）。"""
    counter = {"n": 0}

    def _before(conn, cursor, statement, parameters, context, executemany):
        counter["n"] += 1

    event.listen(engine, "before_cursor_execute", _before)
    try:
        yield counter
    finally:
        event.remove(engine, "before_cursor_execute", _before)


def _part(db, pn, brand=None):
    p = DimPart(pn_std=pn, brand=brand)
    db.add(p); db.flush()
    return p.id


def _pool(db, name, part_ids):
    return pool_catalog.create_pool(db, name=name, member_part_ids=part_ids,
                                    operated_by="t")["group_id"]


@pytest.fixture()
def seeded(db):
    """三种形态的池：
    池强：标杆 SX ex100×2 张不同采购单（可靠+供应可得），SY ex150 卖 10、SX 卖 2
          → theoretical=500、supply_upper=500、demand_qty=12。
    池弱：标杆 WX 仅 1 张采购单（供应不稳），WY ex150 卖 4
          → theoretical=200、supply_upper=0。
    池空：两成员无任何交易 → 全 0。"""
    sx = _part(db, "PN-SX", "BX"); sy = _part(db, "PN-SY", "BY")
    wx = _part(db, "PN-WX", "BX"); wy = _part(db, "PN-WY", "BY")
    e1 = _part(db, "PN-E1"); e2 = _part(db, "PN-E2")
    g_strong = _pool(db, "池强", [sx, sy])
    g_weak = _pool(db, "池弱", [wx, wy])
    g_empty = _pool(db, "池空", [e1, e2])

    b = SysImportBatch(filename="t.xlsx", file_type="purchase", file_hash="hbatch")
    db.add(b); db.flush()
    po = {"P1": f.purchase_head("P1", on=date(2026, 1, 5), is_tax_inclusive=True),
          "P2": f.purchase_head("P2", on=date(2026, 1, 10), is_tax_inclusive=True)}
    pl = [f.purchase_line("P1", "LSX1", "PN-SX", qty="5", price="113"),    # ex100 标杆
          f.purchase_line("P2", "LSX2", "PN-SX", qty="5", price="113"),    # 第二张单→可靠
          f.purchase_line("P1", "LSY", "PN-SY", qty="5", price="169.5"),   # ex150 溢价
          f.purchase_line("P1", "LWX", "PN-WX", qty="5", price="113"),     # ex100 单样本
          f.purchase_line("P1", "LWY", "PN-WY", qty="5", price="169.5")]   # ex150
    loader.load(db, f.purchase_result(po, pl), b.id, AS_OF)
    so = {"S1": f.sales_head("S1", on=date(2026, 2, 1))}
    sl = [f.sales_line("S1", "SSY", "PN-SY", qty="10", price="300"),
          f.sales_line("S1", "SSX", "PN-SX", qty="2", price="300"),
          f.sales_line("S1", "SWY", "PN-WY", qty="4", price="300")]
    loader.load(db, f.sales_result(so, sl), b.id, AS_OF)
    db.commit(); profit.recompute(db)
    return {"strong": g_strong, "weak": g_weak, "empty": g_empty}


def test_list_items_match_analyze_both_sorts(db, seeded):
    """两条旧路径的清单字段与逐池 analyze 逐字段相等（批量化语义不变的总锚点）。"""
    expected = {gid: pool.analyze(db, gid, as_of=AS_OF) for gid in seeded.values()}
    for sort in ("savings", "member_count"):
        out = pool.list_pools(db, as_of=AS_OF, sort=sort, page_size=50)
        assert out["total"] == 3 and len(out["items"]) == 3
        for item in out["items"]:
            d = expected[item["group_id"]]
            assert item["demand_qty"] == d["demand"]["total_qty"], (sort, item["group_id"])
            assert item["demand_revenue_ex_tax"] == d["demand"]["total_revenue_ex_tax"]
            assert item["theoretical_saving"] == d["savings"]["theoretical_max"]
            assert item["supply_available_upper"] == d["savings"]["supply_available_upper"]


def test_hand_computed_values_and_ranking(db, seeded):
    """不止与 analyze 互证，还与手算 fixture 对账；savings 全局排名次序不变。"""
    out = pool.list_pools(db, as_of=AS_OF, sort="savings", page_size=50)
    assert [i["group_id"] for i in out["items"]] == [
        seeded["strong"], seeded["weak"], seeded["empty"]]
    by = {i["group_id"]: i for i in out["items"]}
    strong = by[seeded["strong"]]
    assert strong["theoretical_saving"] == 500.0        # (150-100)*10
    assert strong["supply_available_upper"] == 500.0    # 标杆 2 张单→供应可得
    assert strong["demand_qty"] == 12.0
    weak = by[seeded["weak"]]
    assert weak["theoretical_saving"] == 200.0          # (150-100)*4
    assert weak["supply_available_upper"] == 0.0        # 标杆单样本→供应不稳
    assert weak["demand_qty"] == 4.0
    empty = by[seeded["empty"]]
    assert empty["theoretical_saving"] == 0.0 and empty["supply_available_upper"] == 0.0
    assert empty["demand_qty"] == 0.0 and empty["demand_revenue_ex_tax"] == 0.0


def test_supply_window_not_truncated_by_page_range(db):
    """页面选近 30 天时，60/90 天前的采购证据仍进标杆/供应（批量版沿用 analyze 的
    双窗口：采购看 [today-365, today]，销量按页面窗口）。"""
    x = _part(db, "PN-WWX", "BX"); y = _part(db, "PN-WWY", "BY")
    gid = _pool(db, "池窗", [x, y])
    b = SysImportBatch(filename="w.xlsx", file_type="purchase", file_hash="hbatchw")
    db.add(b); db.flush()
    po = {"P1": f.purchase_head("P1", on=date(2026, 4, 2), is_tax_inclusive=True),
          "P2": f.purchase_head("P2", on=date(2026, 3, 3), is_tax_inclusive=True)}
    pl = [f.purchase_line("P1", "LX1", "PN-WWX", qty="5", price="113"),
          f.purchase_line("P2", "LX2", "PN-WWX", qty="5", price="113"),
          f.purchase_line("P1", "LY", "PN-WWY", qty="5", price="169.5")]
    loader.load(db, f.purchase_result(po, pl), b.id, AS_OF)
    so = {"S1": f.sales_head("S1", on=date(2026, 5, 15))}   # 销量在页面窗口内
    sl = [f.sales_line("S1", "SLY", "PN-WWY", qty="10", price="300")]
    loader.load(db, f.sales_result(so, sl), b.id, AS_OF)
    db.commit(); profit.recompute(db)

    window = {"date_from": date(2026, 5, 2), "date_to": date(2026, 6, 1)}
    d = pool.analyze(db, gid, as_of=AS_OF, **window)
    for sort in ("savings", "member_count"):
        item = pool.list_pools(db, as_of=AS_OF, sort=sort, **window)["items"][0]
        assert item["theoretical_saving"] == d["savings"]["theoretical_max"] == 500.0
        assert item["supply_available_upper"] == 500.0    # 窗口外×2单→一年内供应稳定
        assert item["demand_qty"] == 10.0
        assert item["demand_revenue_ex_tax"] == d["demand"]["total_revenue_ex_tax"]


def test_query_count_constant_as_pools_grow(db, seeded):
    """核心验收：池数 3 → 10 后，两条旧路径的查询数严格相等（原每池 ~5-6 条 SQL）。"""
    baseline = {}
    for sort in ("savings", "member_count"):
        with count_queries() as c:
            pool.list_pools(db, as_of=AS_OF, sort=sort, page_size=50)
        baseline[sort] = c["n"]

    b = SysImportBatch(filename="g.xlsx", file_type="purchase", file_hash="hbatchg")
    db.add(b); db.flush()
    po, pl, so, sl = {}, [], {}, []
    for i in range(7):
        a = _part(db, f"PN-G{i}A", "BA"); bb = _part(db, f"PN-G{i}B", "BB")
        _pool(db, f"池G{i}", [a, bb])
        po[f"GP{i}"] = f.purchase_head(f"GP{i}", on=date(2026, 1, 5), is_tax_inclusive=True)
        pl.append(f.purchase_line(f"GP{i}", f"GLA{i}", f"PN-G{i}A", qty="5", price="113"))
        pl.append(f.purchase_line(f"GP{i}", f"GLB{i}", f"PN-G{i}B", qty="5", price="226"))
        so[f"GS{i}"] = f.sales_head(f"GS{i}", on=date(2026, 2, 1))
        sl.append(f.sales_line(f"GS{i}", f"GSL{i}", f"PN-G{i}B", qty="3", price="300"))
    loader.load(db, f.purchase_result(po, pl), b.id, AS_OF)
    loader.load(db, f.sales_result(so, sl), b.id, AS_OF)
    db.commit(); profit.recompute(db)

    for sort in ("savings", "member_count"):
        with count_queries() as c:
            out = pool.list_pools(db, as_of=AS_OF, sort=sort, page_size=50)
        assert out["total"] == 10
        assert c["n"] == baseline[sort], (
            f"sort={sort}: 池数 3→10 查询数变化 {baseline[sort]} → {c['n']}（疑似 N+1 回归）")

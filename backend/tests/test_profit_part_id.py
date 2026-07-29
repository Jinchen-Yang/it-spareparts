"""利润口径：part_id 分组与 pn 分组等价（未合并库零回归）+ 合并后成本归并（验收场景6）。"""
from collections import defaultdict
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.etl import loader
from app.models.sales import FSalesLine
from app.models.system import SysImportBatch
from app.services import cost, merge, profit
from tests import factories as f


@pytest.fixture()
def seeded(db):
    """三个型号交错的采购/销售时间线。"""
    b = SysImportBatch(filename="t.xlsx", file_type="purchase", file_hash="h4")
    db.add(b)
    db.flush()
    orders = {
        "P1": f.purchase_head("P1", on=date(2026, 1, 5)),
        "P2": f.purchase_head("P2", on=date(2026, 1, 20)),
    }
    lines = [
        f.purchase_line("P1", "PL1", "PN-X", qty="10", price="80"),
        f.purchase_line("P1", "PL2", "PN-Y", qty="5", price="200"),
        f.purchase_line("P2", "PL3", "PN-X", qty="10", price="100"),
    ]
    loader.load(db, f.purchase_result(orders, lines), b.id, date(2026, 6, 1))
    so = {
        "S1": f.sales_head("S1", on=date(2026, 1, 10)),
        "S2": f.sales_head("S2", on=date(2026, 2, 1)),
    }
    sl = [
        f.sales_line("S1", "SL1", "PN-X", qty="5", price="150"),
        f.sales_line("S2", "SL2", "PN-X", qty="12", price="150"),
        f.sales_line("S2", "SL3", "PN-Y", qty="2", price="300"),
        f.sales_line("S2", "SL4", "PN-Z", qty="1", price="500"),   # 无采购 → no_cost
    ]
    loader.load(db, f.sales_result(so, sl), b.id, date(2026, 6, 1))
    db.commit()
    return b


def _expected_by_pn_grouping(db):
    """旧口径（按 pn_std 文本分组）手工回放，作为等价性基准。"""
    rows = db.execute(select(FSalesLine.id, FSalesLine.pn_std)).all()
    pn_of = dict(rows)
    # fixture 的 is_tax_inclusive=None 按甲方约定视为未税原值；
    # 与 profit._load_purchase_events 同口径，保证等价性基准仍成立。
    def ex(price):
        return profit._ex_tax_purchase(Decimal(price), None)

    pur = defaultdict(list)
    pur["PN-X"] = [cost.PurchaseEvent(date(2026, 1, 5), Decimal(10), ex(80)),
                   cost.PurchaseEvent(date(2026, 1, 20), Decimal(10), ex(100))]
    pur["PN-Y"] = [cost.PurchaseEvent(date(2026, 1, 5), Decimal(5), ex(200))]
    fallback = {"PN-X": ex(100), "PN-Y": ex(200)}
    sales = defaultdict(list)
    dates = {"SL1": date(2026, 1, 10), "SL2": date(2026, 2, 1),
             "SL3": date(2026, 2, 1), "SL4": date(2026, 2, 1)}
    qtys = {"SL1": 5, "SL2": 12, "SL3": 2, "SL4": 1}
    for sid, raw in db.execute(select(FSalesLine.id, FSalesLine.raw_line_id)).all():
        sales[pn_of[sid]].append(cost.SaleEvent(dates[raw], sid, Decimal(qtys[raw])))
    out = {}
    for pn, sevents in sales.items():
        out.update(cost.replay(pur.get(pn, []), sevents, fallback.get(pn)))
    return out


def test_recompute_equivalent_to_pn_grouping(db, seeded):
    """未合并库（pn↔part 双射）：part_id 分组结果与 pn 分组逐行一致——P3 零回归证明。"""
    stats = profit.recompute(db)
    assert stats["sales_lines"] == 4
    expected = _expected_by_pn_grouping(db)
    for sid, mov, fifo, src in db.execute(
        select(FSalesLine.id, FSalesLine.cost_moving_avg,
               FSalesLine.cost_fifo, FSalesLine.cost_source)
    ).all():
        exp = expected.get(sid)
        if exp is None or (exp.moving_avg is None and exp.fifo is None):
            assert mov is None and fifo is None and src == "none"
        else:
            assert mov == exp.moving_avg, f"line {sid} moving_avg 不一致"
            assert fifo == exp.fifo, f"line {sid} fifo 不一致"
            assert src == exp.source


def test_recompute_deterministic(db, seeded):
    profit.recompute(db)
    snap1 = {r[0]: (r[1], r[2]) for r in db.execute(
        select(FSalesLine.id, FSalesLine.cost_moving_avg, FSalesLine.cost_fifo)).all()}
    profit.recompute(db)
    snap2 = {r[0]: (r[1], r[2]) for r in db.execute(
        select(FSalesLine.id, FSalesLine.cost_moving_avg, FSalesLine.cost_fifo)).all()}
    assert snap1 == snap2, "重算必须幂等可复现"


def test_merge_unifies_cost_stream(db, seeded):
    """PN-Z 无采购 → no_cost；把 PN-Z 并入 PN-X 后，Z 的销售吃到 X 的采购批次。"""
    profit.recompute(db)
    sl4 = db.scalar(select(FSalesLine).where(FSalesLine.raw_line_id == "SL4"))
    assert sl4.cost_source == "none"

    merge.merge_parts(db, "PN-Z", "PN-X", "同款不同写法", "tester")
    profit.recompute(db)
    db.expire_all()
    sl4 = db.scalar(select(FSalesLine).where(FSalesLine.raw_line_id == "SL4"))
    assert sl4.cost_source in ("computed", "fallback")
    assert sl4.cost_moving_avg is not None, "合并后成本流归并，原 no_cost 行获得成本"

    # part 维度聚合应只剩 PN-X / PN-Y 两行（Z 已归并）
    agg = profit.aggregate(db, "part", None, None, False)
    dims = {r["dimension"] for r in agg["rows"]}
    assert dims == {"PN-X", "PN-Y"}

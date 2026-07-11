"""型号盈亏排名：未税双成本法赚钱/亏损双榜 + 无成本行不进榜 + 采购/销售价统计。"""
from datetime import date

import pytest

from app.etl import loader
from app.models.system import SysImportBatch
from app.services import dashboard, profit
from tests import factories as f

AS_OF = date(2026, 6, 1)


@pytest.fixture()
def seeded(db):
    b = SysImportBatch(filename="t.xlsx", file_type="purchase", file_hash="hrank")
    db.add(b); db.flush()
    porders = {
        "P1": f.purchase_head("P1", on=date(2026, 1, 5), is_tax_inclusive=True),
    }
    plines = [
        f.purchase_line("P1", "PL1", "PN-A", qty="1", price="113"),   # ex 100
        f.purchase_line("P1", "PL2", "PN-C", qty="1", price="226"),   # ex 200
    ]
    loader.load(db, f.purchase_result(porders, plines), b.id, date(2026, 6, 1))
    sorders = {"S1": f.sales_head("S1", on=date(2026, 2, 1))}
    slines = [
        f.sales_line("S1", "SL1", "PN-A", qty="1", price="226"),   # rev 200, cost 100 → +100 赚
        f.sales_line("S1", "SL2", "PN-C", qty="1", price="113"),   # rev 100, cost 200 → -100 亏
        f.sales_line("S1", "SL3", "PN-B", qty="1", price="113"),   # 无采购 → no_cost
    ]
    loader.load(db, f.sales_result(sorders, slines), b.id, date(2026, 6, 1))
    db.commit()
    profit.recompute(db)
    return b


def test_profit_and_loss_boards(db, seeded):
    d = dashboard.part_ranking(db, None, None, as_of=AS_OF)
    assert [r["pn_std"] for r in d["profitable"]] == ["PN-A"]
    assert [r["pn_std"] for r in d["loss"]] == ["PN-C"]
    assert d["profitable"][0]["gross_profit_moving"] == 100.0
    assert d["loss"][0]["gross_profit_moving"] == -100.0
    # 无成本行 PN-B 既不进赚钱也不进亏损榜，单独计数
    assert d["counts"] == {"total_parts": 3, "with_cost": 2, "profitable": 1,
                           "loss": 1, "no_cost_parts": 1}


def test_price_stats_ex_tax(db, seeded):
    d = dashboard.part_ranking(db, None, None, as_of=AS_OF)
    a = d["profitable"][0]
    # 采购价未税：113/1.13=100（加权均价/中位/最低/最高一致，样本 1）
    assert a["purchase_price"]["wavg"] == 100.0
    assert a["purchase_price"]["median"] == 100.0
    assert a["purchase_price"]["samples"] == 1
    assert a["purchase_price"]["last_date"] == "2026-01-05"
    # 销售价未税：226/1.13=200
    assert a["sale_price"]["wavg"] == 200.0
    assert a["sale_price"]["median"] == 200.0
    # 覆盖率：PN-A 全部有成本 → 1.0
    assert a["cost_coverage"] == 1.0


def test_fifo_sort_switch(db, seeded):
    """cost_method=fifo：单批次下 fifo 与 moving 相同，双榜内容一致，window 记录口径。"""
    d = dashboard.part_ranking(db, None, None, cost_method="fifo", as_of=AS_OF)
    assert d["window"]["cost_method"] == "fifo"
    assert [r["pn_std"] for r in d["profitable"]] == ["PN-A"]

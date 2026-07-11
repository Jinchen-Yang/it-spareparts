"""经营趋势：销售/采购/毛利按日/月分桶（未税）+ 未来日期排除。"""
from datetime import date

import pytest

from app.etl import loader
from app.models.system import SysImportBatch
from app.services import dashboard, profit
from tests import factories as f

AS_OF = date(2026, 6, 1)


@pytest.fixture()
def seeded(db):
    b = SysImportBatch(filename="t.xlsx", file_type="purchase", file_hash="htrend")
    db.add(b); db.flush()
    porders = {
        "P1": f.purchase_head("P1", on=date(2026, 1, 5), is_tax_inclusive=True),
        "P2": f.purchase_head("P2", on=date(2026, 2, 10), is_tax_inclusive=True),
    }
    plines = [
        f.purchase_line("P1", "PL1", "PN-A", qty="10", price="113"),   # 1月 ex 1000
        f.purchase_line("P2", "PL2", "PN-A", qty="5", price="113"),    # 2月 ex 500
    ]
    loader.load(db, f.purchase_result(porders, plines), b.id, date(2026, 6, 1))
    sorders = {
        "S1": f.sales_head("S1", on=date(2026, 1, 20)),
        "S2": f.sales_head("S2", on=date(2026, 2, 15)),
        "S3": f.sales_head("S3", on=date(2026, 12, 2)),   # 未来
    }
    slines = [
        f.sales_line("S1", "SL1", "PN-A", qty="1", price="226"),   # 1月 rev 200 cost 100 gp 100
        f.sales_line("S2", "SL2", "PN-A", qty="1", price="226"),   # 2月 rev 200 gp 100
        f.sales_line("S3", "SL3", "PN-A", qty="1", price="226"),   # 未来，排除
    ]
    loader.load(db, f.sales_result(sorders, slines), b.id, date(2026, 6, 1))
    db.commit()
    profit.recompute(db)
    return b


def test_monthly_trend_excludes_future(db, seeded):
    d = dashboard.trend(db, None, None, granularity="month", as_of=AS_OF)
    assert d["granularity"] == "month"
    by = {r["period"]: r for r in d["series"]}
    assert set(by) == {"2026-01-01", "2026-02-01"}     # 12月未来单不成桶
    assert by["2026-01-01"]["purchase_ex_tax"] == 1000.0
    assert by["2026-01-01"]["sales_ex_tax"] == 200.0
    assert by["2026-01-01"]["gross_profit"] == 100.0
    assert by["2026-02-01"]["purchase_ex_tax"] == 500.0
    assert by["2026-02-01"]["sales_ex_tax"] == 200.0


def test_future_appears_when_as_of_moves(db, seeded):
    d = dashboard.trend(db, None, None, granularity="month", as_of=date(2026, 12, 31))
    periods = {r["period"] for r in d["series"]}
    assert "2026-12-01" in periods

"""老板侧销售订单列表：未税逐行毛利 + 状态/关键词筛选 + 采购拉通标记 + 未来单标记。"""
from datetime import date

import pytest

from app.etl import loader
from app.models.purchase import FPurchaseOrder
from app.models.system import SysImportBatch
from app.services import dashboard, profit
from tests import factories as f

AS_OF = date(2026, 6, 1)


@pytest.fixture()
def seeded(db):
    b = SysImportBatch(filename="t.xlsx", file_type="purchase", file_hash="hsales")
    db.add(b); db.flush()
    # 采购单 P1 关联销售单号 SO-1（拉通信号）
    porders = {"P1": f.purchase_head("P1", on=date(2026, 1, 5), is_tax_inclusive=True)}
    plines = [f.purchase_line("P1", "PL1", "PN-A", qty="10", price="113")]
    loader.load(db, f.purchase_result(porders, plines), b.id, date(2026, 6, 1))
    db.query(FPurchaseOrder).filter_by(order_no="P1").update({"linked_sales_order_no": "SO-1"})
    db.commit()

    sorders = {
        "S1": f.sales_head("S1", order_no="SO-1", on=date(2026, 2, 1)),
        "S2": f.sales_head("S2", order_no="SO-2", on=date(2026, 2, 5), data_status="已取消"),
        "S3": f.sales_head("S3", order_no="SO-3", on=date(2026, 12, 2)),   # 未来
    }
    slines = [
        f.sales_line("S1", "SL1", "PN-A", qty="1", price="226"),
        f.sales_line("S2", "SL2", "PN-A", qty="1", price="226"),
        f.sales_line("S3", "SL3", "PN-A", qty="1", price="226"),
    ]
    loader.load(db, f.sales_result(sorders, slines), b.id, date(2026, 6, 1))
    db.commit()
    profit.recompute(db)
    return b


def test_default_active_only_and_linkage(db, seeded):
    d = dashboard.sales_lines(db, as_of=AS_OF)
    # 默认仅已生效：SO-2(已取消)排除；SO-1、SO-3(未来但已生效)在内
    nos = {i["order_no"] for i in d["items"]}
    assert nos == {"SO-1", "SO-3"}
    so1 = next(i for i in d["items"] if i["order_no"] == "SO-1")
    assert so1["linked_purchase"] is True          # P1 经 linked_sales_order_no 关联
    assert so1["revenue_amount"] == 200.0          # 未税
    assert so1["unit_price_ex_tax"] == 200.0       # 226/1.13
    assert so1["gross_profit"] == 100.0
    so3 = next(i for i in d["items"] if i["order_no"] == "SO-3")
    assert so3["is_future"] is True


def test_status_all_includes_cancelled(db, seeded):
    d = dashboard.sales_lines(db, status="全部", as_of=AS_OF)
    assert {i["order_no"] for i in d["items"]} == {"SO-1", "SO-2", "SO-3"}


def test_sort_by_gross_profit(db, seeded):
    d = dashboard.sales_lines(db, status="全部", sort="gross_profit", order="desc", as_of=AS_OF)
    gps = [i["gross_profit"] for i in d["items"] if i["gross_profit"] is not None]
    assert gps == sorted(gps, reverse=True)

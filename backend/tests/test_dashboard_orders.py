"""订单拉通（复审 P1-4）：订单粒度（一单一行、多型号聚合）+ 拉通只认已生效采购单
+ 采购订单列表。"""
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
    b = SysImportBatch(filename="t.xlsx", file_type="purchase", file_hash="hord")
    db.add(b); db.flush()
    porders = {
        "P1": f.purchase_head("P1", on=date(2026, 1, 5), is_tax_inclusive=True),   # 已生效，关联 SO-1
        "P2": f.purchase_head("P2", on=date(2026, 1, 6), is_tax_inclusive=True, data_status="已取消"),  # 取消，关联 SO-2
    }
    plines = [
        f.purchase_line("P1", "PL1", "PN-A", qty="10", price="113"),
        f.purchase_line("P2", "PL2", "PN-B", qty="5", price="113"),
    ]
    loader.load(db, f.purchase_result(porders, plines), b.id, date(2026, 6, 1))
    db.query(FPurchaseOrder).filter_by(order_no="P1").update({"linked_sales_order_no": "SO-1"})
    db.query(FPurchaseOrder).filter_by(order_no="P2").update({"linked_sales_order_no": "SO-2"})
    db.commit()
    sorders = {
        "S1": f.sales_head("S1", order_no="SO-1", on=date(2026, 2, 1)),   # 两个型号 → 应聚合成 1 行
        "S2": f.sales_head("S2", order_no="SO-2", on=date(2026, 2, 5)),   # 关联的采购单已取消
    }
    slines = [
        f.sales_line("S1", "SL1", "PN-A", qty="1", price="226"),
        f.sales_line("S1", "SL2", "PN-B", qty="2", price="113"),
        f.sales_line("S2", "SL3", "PN-A", qty="1", price="226"),
    ]
    loader.load(db, f.sales_result(sorders, slines), b.id, date(2026, 6, 1))
    db.commit(); profit.recompute(db)
    return b


def test_sales_order_granularity(db, seeded):
    d = dashboard.sales_orders(db, status="全部", as_of=AS_OF)
    by = {i["order_no"]: i for i in d["items"]}
    # SO-1 两个型号聚合成一行（此前是两行）
    assert by["SO-1"]["part_count"] == 2
    assert by["SO-1"]["total_qty"] == 3.0            # 1 + 2
    assert by["SO-1"]["total_revenue"] == 400.0      # A:1*226/1.13=200 + B:2*113/1.13=200
    # 单据数 = 2（不是明细行数 3）
    assert d["total"] == 2


def test_linkage_only_active_purchase(db, seeded):
    d = dashboard.sales_orders(db, status="全部", as_of=AS_OF)
    by = {i["order_no"]: i for i in d["items"]}
    assert by["SO-1"]["linked_purchase"] is True     # P1 已生效
    assert by["SO-2"]["linked_purchase"] is False    # P2 已取消 → 不算拉通


def test_purchase_orders_list(db, seeded):
    d = dashboard.purchase_orders(db, status="全部", as_of=AS_OF)
    by = {i["order_no"]: i for i in d["items"]}
    assert set(by) == {"P1", "P2"}
    assert by["P1"]["total_ex_tax"] == 1000.0        # 10 * 113/1.13
    assert by["P1"]["linked_sales_order"] == "SO-1"
    assert by["P1"]["part_count"] == 1

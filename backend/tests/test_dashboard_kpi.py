"""老板看板 KPI：未税口径 + 成本覆盖率/未配成本营收（防毛利虚高）+ 未来日期排除 + 订单健康。"""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.etl import loader
from app.models.system import SysImportBatch
from app.services import dashboard, profit
from tests import factories as f

AS_OF = date(2026, 6, 1)   # 固定"今天"，使 2026-12-02 成为未来日期


@pytest.fixture()
def seeded(db):
    b = SysImportBatch(filename="t.xlsx", file_type="purchase", file_hash="hkpi")
    db.add(b); db.flush()
    # 采购：PN-A 含税单价 113（÷1.13=100 未税），10 个，已生效；另一张进行中单不计金额
    porders = {
        "P1": f.purchase_head("P1", on=date(2026, 1, 5), is_tax_inclusive=True),
        "P2": f.purchase_head("P2", on=date(2026, 3, 1), data_status="进行中"),
    }
    plines = [
        f.purchase_line("P1", "PL1", "PN-A", qty="10", price="113"),
        f.purchase_line("P2", "PL2", "PN-A", qty="5", price="113"),
    ]
    loader.load(db, f.purchase_result(porders, plines), b.id, date(2026, 6, 1))
    # 销售
    sorders = {
        "S1": f.sales_head("S1", on=date(2026, 2, 1)),                       # 备件销售·已配成本
        "S2": f.sales_head("S2", on=date(2026, 2, 5)),                       # 备件销售·无成本(PN-B无采购)
        "S3": f.sales_head("S3", on=date(2026, 12, 2)),                      # 未来日期(应排除)
        "S4": f.sales_head("S4", on=date(2026, 2, 3), business_type="整机销售"),  # 不计营收
        "S5": f.sales_head("S5", on=date(2026, 2, 10), data_status="已取消"),     # 取消单
    }
    slines = [
        f.sales_line("S1", "SL1", "PN-A", qty="1", price="226"),   # rev 200, cost 100
        f.sales_line("S2", "SL2", "PN-B", qty="1", price="113"),   # rev 100, no_cost
        f.sales_line("S3", "SL3", "PN-A", qty="1", price="226"),   # 未来，排除
        f.sales_line("S4", "SL4", "PN-A", qty="1", price="226"),   # 整机→excluded_revenue
        f.sales_line("S5", "SL5", "PN-A", qty="1", price="226"),   # 取消→不计
    ]
    loader.load(db, f.sales_result(sorders, slines), b.id, date(2026, 6, 1))
    db.commit()
    profit.recompute(db)
    return b


def test_kpi_amounts_ex_tax_and_coverage(db, seeded):
    k = dashboard.kpi(db, None, None, as_of=AS_OF)
    # 销售额未税：S1(200)+S2(100)=300（S3未来排除、S4不计营收、S5取消非已生效）
    assert k["sales_ex_tax"] == 300.0
    # 已配成本营收=S1的200；毛利=100；毛利率=100/200=0.5
    assert k["sales_costed_ex_tax"] == 200.0
    assert k["gross_profit"] == 100.0
    assert k["gross_margin"] == 0.5
    # 成本覆盖率=200/300；未配成本营收=100（S2）——防毛利虚高的两个数
    assert k["cost_coverage"] == round(200 / 300, 4)
    assert k["sales_uncosted_ex_tax"] == 100.0
    # 被排除营收=S4整机 200
    assert k["excluded_revenue"] == 200.0
    # 采购额未税：仅已生效 P1 = 10*113/1.13 = 1000（P2进行中不计）
    assert k["purchase_ex_tax"] == 1000.0


def test_kpi_order_health(db, seeded):
    k = dashboard.kpi(db, None, None, as_of=AS_OF)
    assert k["orders_future"] == 1          # S3
    assert k["orders_in_progress"] == 1     # P2
    assert k["orders_cancelled"] == 1       # S5
    # 复审 P1-5：S3 虽是已生效但日期在未来 → 只进 orders_future，不重复计入 orders_active
    # 已生效非未来 = S1/S2/S4(销售) + P1(采购) = 4（S3 被排除）
    assert k["orders_active"] == 4
    assert k["window"]["future_excluded"] is True


def test_kpi_future_sale_excluded_from_revenue(db, seeded):
    """把 as_of 推到 2026-12-31，S3 不再是未来 → 销售额应增加 S3 的 200。"""
    k = dashboard.kpi(db, None, None, as_of=date(2026, 12, 31))
    assert k["sales_ex_tax"] == 500.0       # 300 + S3(200)
    assert k["orders_future"] == 0

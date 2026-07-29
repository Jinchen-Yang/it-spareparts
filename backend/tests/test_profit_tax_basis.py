"""正式利润统一 13% 税口径：
销售含税 ÷1.13；采购仅明确含税时 ÷1.13，False/None 均按未税原值。
原始单据 tax_rate/0%/空税率不被覆盖（本测试只验计算字段，不改单据）。"""
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app import config
from app.etl import loader
from app.models.sales import FSalesLine
from app.models.system import SysImportBatch
from app.services import profit
from tests import factories as f


def _seed(db, *, purchase_inc):
    """一采一销、同型号：采购 10 个 @含税价 113；销售 1 个 @含税单价 226。
    purchase_inc 控制采购头 is_tax_inclusive。销售 unit_price 恒含税。"""
    b = SysImportBatch(filename="t.xlsx", file_type="purchase", file_hash="htax")
    db.add(b)
    db.flush()
    orders = {"P1": f.purchase_head("P1", on=date(2026, 1, 5), is_tax_inclusive=purchase_inc)}
    lines = [f.purchase_line("P1", "PL1", "PN-A", qty="10", price="113")]
    loader.load(db, f.purchase_result(orders, lines), b.id, date(2026, 6, 1))
    so = {"S1": f.sales_head("S1", on=date(2026, 2, 1))}
    sl = [f.sales_line("S1", "SL1", "PN-A", qty="1", price="226")]
    loader.load(db, f.sales_result(so, sl), b.id, date(2026, 6, 1))
    db.commit()
    return b


def _line(db):
    return db.scalar(select(FSalesLine).where(FSalesLine.raw_line_id == "SL1"))


def test_sale_and_inclusive_purchase_both_ex_tax_13pct(db):
    """采购含税单：成本 = 113/1.13 = 100；销售营收 = 226/1.13 = 200；毛利 = 100。"""
    _seed(db, purchase_inc=True)
    profit.recompute(db)
    ln = _line(db)
    assert ln.cost_moving_avg == Decimal("100")
    assert ln.revenue_amount == Decimal("200.00")
    assert ln.cost_amount == Decimal("100.00")
    assert ln.gross_profit == Decimal("100.00")


def test_unknown_tax_flag_defaults_to_exclusive(db):
    """采购 is_tax_inclusive=None 时，按甲方约定统一视为未税原值。"""
    _seed(db, purchase_inc=None)
    profit.recompute(db)
    assert _line(db).cost_moving_avg == Decimal("113")


def test_exclusive_purchase_price_kept_as_is(db):
    """采购明确不含税单：单价已是未税，成本取原值 113（不再 ÷1.13）；销售仍 ÷1.13。"""
    _seed(db, purchase_inc=False)
    profit.recompute(db)
    ln = _line(db)
    assert ln.cost_moving_avg == Decimal("113")
    assert ln.revenue_amount == Decimal("200.00")   # 销售恒含税，仍换未税
    assert ln.gross_profit == Decimal("87.00")       # 200 - 113


def test_legacy_tax_basis_setting_does_not_override_fixed_policy(db, monkeypatch):
    """旧 TAX_BASIS 设置不再改变统一 13% 计算事实。"""
    monkeypatch.setattr(config, "TAX_BASIS", "as_is")
    _seed(db, purchase_inc=True)
    profit.recompute(db)
    ln = _line(db)
    assert ln.cost_moving_avg == Decimal("100")
    assert ln.revenue_amount == Decimal("200.00")
    assert ln.gross_profit == Decimal("100.00")


def test_sales_revenue_midpoint_uses_postgresql_money_rounding(db):
    batch = SysImportBatch(
        filename="sales-midpoint.xlsx",
        file_type="sales",
        file_hash="sales-midpoint-half-up",
    )
    db.add(batch)
    db.flush()
    loader.load(
        db,
        f.sales_result(
            {"S-MID": f.sales_head("S-MID", on=date(2026, 2, 1))},
            [
                f.sales_line(
                    "S-MID",
                    "SL-MID",
                    "PN-MID",
                    qty="0.565",
                    price="0.01",
                ),
            ],
        ),
        batch.id,
        date(2026, 6, 1),
    )
    db.commit()

    profit.recompute(db)

    revenue = db.scalar(
        select(FSalesLine.revenue_amount).where(
            FSalesLine.raw_line_id == "SL-MID",
        )
    )
    assert revenue == Decimal("0.01")

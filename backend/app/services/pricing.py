"""含税/未税价格换算——财务口径**单一真值源**。

复审二轮 Standards：这些税价表达式此前散在 dashboard.py 的私有函数里、又被 pool.py 跨模块
引用，财务规则将来易多处漂移。收敛到本模块，看板/池/未来任何聚合都从这里取，改一处即全改。

口径与 profit._ex_tax_* 同源（config.PROFIT_VAT_RATE=13%）：
- 销售 unit_price 恒含税 → ÷1.13。
- 采购按头表 is_tax_inclusive 归一：明确不含税取原值，含税/未知 ÷1.13。
- TAX_BASIS != "ex_tax" 时一律不换算（取原值）。
"""
from decimal import Decimal

from sqlalchemy import case

from app import config
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.sales import FSalesLine

VAT1 = Decimal(1) + config.PROFIT_VAT_RATE


def sale_ex_unit():
    """销售未税单价：销售 unit_price 恒含税 → ÷1.13（TAX_BASIS!=ex_tax 时原值）。"""
    up = FSalesLine.unit_price
    return up / VAT1 if config.TAX_BASIS == "ex_tax" else up


def purchase_ex_unit():
    """采购未税单价：按头表 is_tax_inclusive 归一（含税/未知÷1.13、明确不含税原值）。"""
    up = FPurchaseLine.unit_price
    if config.TAX_BASIS != "ex_tax":
        return up
    return case((FPurchaseOrder.is_tax_inclusive.is_(False), up), else_=up / VAT1)


def purchase_ex_tax_expr():
    """采购行未税额表达式：unit_price*qty，按头表 is_tax_inclusive 归一。与 profit._ex_tax_purchase 同口径。"""
    return purchase_ex_unit() * FPurchaseLine.qty

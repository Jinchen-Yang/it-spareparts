"""含税/未税价格换算的 SQL 表达式入口。

复审二轮 Standards：这些税价表达式此前散在 dashboard.py 的私有函数里、又被 pool.py 跨模块
引用，财务规则将来易多处漂移。收敛到本模块，看板/池/未来任何聚合都从这里取，改一处即全改。

口径与 ``profit._ex_tax_*`` 同源（``tax_policy.TAX_FACTOR=1.13``）：
- 销售 unit_price 恒含税 → ÷1.13。
- 采购按头表 is_tax_inclusive 归一：只有明确含税才 ÷1.13，不含税/未知取原值。
展示设置不改变计算事实；未税底座始终按上述固定 13% 生成。
"""
from sqlalchemy import case

from app import tax_policy
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.sales import FSalesLine

VAT1 = tax_policy.TAX_FACTOR


def sale_ex_unit():
    """销售未税单价：销售 unit_price 恒含税 → ÷1.13。"""
    up = FSalesLine.unit_price
    return up / VAT1


def purchase_ex_unit():
    """采购未税单价：仅明确含税才 ÷1.13；False/NULL 均按未税原值。"""
    up = FPurchaseLine.unit_price
    return case((FPurchaseOrder.is_tax_inclusive.is_(True), up / VAT1), else_=up)


def purchase_ex_tax_expr():
    """采购行未税额表达式：unit_price*qty，按头表 is_tax_inclusive 归一。与 profit._ex_tax_purchase 同口径。"""
    return purchase_ex_unit() * FPurchaseLine.qty

"""互通池价格纪律的共享行口径。

这里只定义“哪些历史行是有效价格事实”，不定义利润成本池。采购价格纪律覆盖全部
已生效真实采购类型；利润引擎继续独立使用 ``COST_PURCHASE_TYPES``，两者不得混用。
订单状态与日期窗口由调用方统一通过 ``active_orders`` / resolve_window 处理。
"""
from sqlalchemy import and_

from app.models.purchase import FPurchaseLine
from app.models.sales import FSalesLine


def purchase_priced_condition():
    """全部采购类型中，单价和数量均为正的真实采购行。"""
    return and_(
        FPurchaseLine.unit_price.is_not(None), FPurchaseLine.unit_price > 0,
        FPurchaseLine.qty.is_not(None), FPurchaseLine.qty > 0,
    )


def sales_priced_condition():
    """计营收且单价、数量均为正的真实销售行。"""
    return and_(
        FSalesLine.counts_revenue.is_(True),
        FSalesLine.unit_price.is_not(None), FSalesLine.unit_price > 0,
        FSalesLine.qty.is_not(None), FSalesLine.qty > 0,
    )

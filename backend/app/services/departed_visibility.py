"""D-0063 离职销售隐藏集合：销售/采购分析**显示面**过滤专用。

隐藏集合 = 已删除（deleted_at 非空）且勾选隐藏（hide_documents）的账号的
`salesperson_name`，与单据上的销售姓名**精确匹配**（与行级过滤同款对齐）。

红线（D-0063 决定 2/4）：本模块**只**用于订单列表 / 导出 / 人员筛选候选 /
按销售分组行的过滤；汇总 KPI、趋势、直方图、成本移动加权、库存推导等
一切重算路径**不得**消费本模块——隐藏是显示口径，不改事实。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.sales import FSalesOrder
from app.models.system import SysUser


def hidden_salespersons(db: Session) -> set[str]:
    """隐藏中的销售姓名集合；空集 = 无离职隐藏（调用方零开销跳过过滤）。"""
    names = db.execute(
        select(SysUser.salesperson_name)
        .where(SysUser.deleted_at.is_not(None),
               SysUser.hide_documents.is_(True),
               SysUser.salesperson_name.is_not(None))
    ).scalars().all()
    return {name.strip() for name in names if name and name.strip()}


def hidden_sales_order_nos(db: Session) -> set[str] | None:
    """被隐藏销售单的 order_no 集合（供采购单按 linked_sales_order_no 排除）。

    无隐藏时返回 None，调用方据此跳过过滤；空集与 None 语义不同：
    None=不过滤，空集理论上不出现（有隐藏姓名而无任何单据时为空集，此时
    过滤不排除任何行，等价无操作）。
    """
    hidden = hidden_salespersons(db)
    if not hidden:
        return None
    nos = db.execute(
        select(FSalesOrder.order_no)
        .where(FSalesOrder.salesperson.in_(hidden),
               FSalesOrder.order_no.is_not(None))
    ).scalars().all()
    return set(nos)

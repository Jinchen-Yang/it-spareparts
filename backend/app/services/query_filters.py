"""跨 service 共享的查询口径 helper（单一真值源，防口径散落漂移）。

架构体检 2026-06-29 主线 A：`if config.ACTIVE_STATUS_ONLY: stmt.where(data_status=='已生效')`
此前在 profit / part_overview / purchase_analysis / inventory 等散落 14 处、各写一遍，
将来改"已生效"措辞或调开关要搜遍全仓。收敛到此处。
"""
from app import config


def active_orders(stmt, order_model):
    """按"已生效"过滤订单查询（受 config.ACTIVE_STATUS_ONLY 开关控制；关则不过滤）。

    order_model 需有 data_status 列（FPurchaseOrder / FSalesOrder）。仅用于
    "无条件按生效过滤"的站点；带 status 入参的条件过滤（如 recent_purchases）不适用。
    """
    if config.ACTIVE_STATUS_ONLY:
        return stmt.where(order_model.data_status == config.ACTIVE_STATUS)
    return stmt

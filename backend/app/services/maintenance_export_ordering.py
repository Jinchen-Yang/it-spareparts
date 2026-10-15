"""D-33：只复用 WBDD 下载排序，不改变各出口的筛选集合或网页顺序。"""

from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder


def wbdd_export_order_by(*, include_lines: bool = True) -> tuple:
    """先按业务日期排单，同号异身份仍连续；唯一行 ID 稳定打平。"""
    orders = (
        FMaintenanceOrder.order_date.asc().nullslast(),
        FMaintenanceOrder.order_no.asc().nullslast(),
        FMaintenanceOrder.id.asc(),
    )
    if not include_lines:
        return orders
    return (*orders, FMaintenanceLine.line_no.asc().nullslast(),
            FMaintenanceLine.id.asc())

"""Effective assigned WBDD read model shared by workbooks and row queries.

Returns a composable SQL statement, without pagination, execution, or writes.
Callers supply already-authorized project scope; this is not an authorization API.
"""
from datetime import date

from sqlalchemy import select

from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment


def assigned_lines_query(
    *,
    project_id: str | None,
    window: tuple[date, date] | None,
    allowed_project_ids: set[str] | None = None,
    export_order: bool = False,
):
    """需求单明细 + 其项目归属（未归属单不进这两张表——它们还没有项目口径）。

    2026-08-19（#267 读侧修复 1）：墓碑整单作废与行级软作废都在此过滤——
    项目总表 03、02 概览、主页全局行级表三处共用本入口。
    2026-08-21（客户反馈）：order_date 倒序——最新日期排前面；行键/行级哈希
    与行序解耦，Excel 回传不受行序影响。
    #259 修正：line_no 可空，同单同日的行在前三键上会打平；行 id 作尾键，
    否则 03 行级分页切片跨页会重复/漏行（打平行的顺序由堆序决定，不稳定）。
    D-33：下载显式选择完整业务日期升序，网页仍沿用上述倒序。
    """
    from app.services import maintenance_demands
    from app.services.query_filters import active_orders
    from app.services.maintenance_export_ordering import wbdd_export_order_by

    ordering = wbdd_export_order_by() if export_order else (
        FMaintenanceOrder.order_date.desc(), FMaintenanceOrder.order_no,
        FMaintenanceLine.line_no, FMaintenanceLine.id,
    )

    stmt = (
        select(FMaintenanceLine, FMaintenanceOrder,
               MaintenanceSourceOrderAssignment.project_id)
        .join(FMaintenanceOrder, FMaintenanceOrder.id == FMaintenanceLine.order_id)
        .join(MaintenanceSourceOrderAssignment,
              (MaintenanceSourceOrderAssignment.source_order_id
               == FMaintenanceOrder.raw_order_id)
              & MaintenanceSourceOrderAssignment.is_active.is_(True))
        .where(FMaintenanceLine.is_active.is_(True),
               maintenance_demands.active_demand_condition())
        .order_by(*ordering)
    )
    # Source workflow status and the system tombstone are independent. Keep
    # inactive source headers out of every effective project/export view even
    # when the tombstone cutover remains disabled.
    stmt = active_orders(stmt, FMaintenanceOrder)
    if project_id is not None:
        stmt = stmt.where(MaintenanceSourceOrderAssignment.project_id == project_id)
    if allowed_project_ids is not None:
        # Scope helper contract: None = full scope; empty set = no visible rows.
        stmt = stmt.where(
            MaintenanceSourceOrderAssignment.project_id.in_(allowed_project_ids)
        )
    if window is not None:
        stmt = stmt.where(FMaintenanceOrder.order_date >= window[0],
                          FMaintenanceOrder.order_date <= window[1])
    return stmt

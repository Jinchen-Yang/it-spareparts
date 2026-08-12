"""Read-only projection: purchase orders linked to a maintenance project.

Linkage path:
    采购订单 (FPurchaseOrder.linked_maintenance_order_no)
    → 维保需求单 (FMaintenanceOrder.raw_order_id)
    → 项目 (FMaintenanceOrder.project_std = MaintenanceProject.display_name)
"""

from collections import defaultdict
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder
from app.models.maintenance_project import MaintenanceProject
from app.models.purchase import FPurchaseLine, FPurchaseOrder


def _project_display_name(db: Session, project_id: str) -> str | None:
    return db.scalar(
        select(MaintenanceProject.display_name).where(
            MaintenanceProject.project_id == project_id
        )
    )


def get_project_procurement_chain(
    db: Session,
    project_id: str,
) -> list[dict]:
    """Return purchase orders → demand orders for *project_id*.

    Only orders with a stable maintenance linkage are included.  Orders
    that appear in multiple projects, have no project_std match, or are
    cancelled are silently excluded (the UI labels them as "not found").
    """
    display_name = _project_display_name(db, project_id)
    if not display_name:
        return []

    # Find all demand orders for this project
    demand_order_ids = list(
        db.scalars(
            select(FMaintenanceOrder.raw_order_id).where(
                FMaintenanceOrder.project_std == display_name,
            )
        ).all()
    )
    if not demand_order_ids:
        return []

    # Find purchase orders linked to those demand orders
    purchase_orders = (
        db.execute(
            select(
                FPurchaseOrder.raw_order_id,
                FPurchaseOrder.order_no,
                FPurchaseOrder.order_date,
                FPurchaseOrder.purchaser,
                FPurchaseOrder.linked_maintenance_order_no,
            ).where(
                FPurchaseOrder.linked_maintenance_order_no.in_(demand_order_ids),
            )
        )
        .mappings()
        .all()
    )

    if not purchase_orders:
        return []

    po_raw_ids = [po.raw_order_id for po in purchase_orders]
    po_lines = (
        db.execute(
            select(
                FPurchaseLine.raw_line_id,
                FPurchaseLine.order_id,
                FPurchaseLine.pn_std,
                FPurchaseLine.description,
                FPurchaseLine.qty,
                FPurchaseLine.unit_price,
            ).where(
                FPurchaseLine.order_id.in_(
                    select(FPurchaseOrder.id).where(
                        FPurchaseOrder.raw_order_id.in_(po_raw_ids)
                    )
                ),
            )
        )
        .mappings()
        .all()
    )

    # Group lines by order
    lines_by_order: dict[str, list[dict]] = defaultdict(list)
    for line in po_lines:
        lines_by_order[line.order_id].append({
            "pn": line.pn_std,
            "description": line.description,
            "qty": float(line.qty) if line.qty is not None else None,
            "unit_price": float(line.unit_price) if line.unit_price is not None else None,
        })

    # Get the FMaintenanceOrder info for the linked demand
    demand_info: dict[str, dict] = {}
    demand_rows = db.execute(
        select(
            FMaintenanceOrder.raw_order_id,
            FMaintenanceOrder.order_no,
            FMaintenanceOrder.order_date,
        ).where(FMaintenanceOrder.raw_order_id.in_(demand_order_ids))
    ).mappings().all()
    for d in demand_rows:
        demand_info[d.raw_order_id] = {
            "order_no": d.order_no,
            "order_date": d.order_date.isoformat() if d.order_date else None,
        }

    result = []
    for po in purchase_orders:
        po_id = db.scalar(
            select(FPurchaseOrder.id).where(
                FPurchaseOrder.raw_order_id == po.raw_order_id
            )
        )
        linked_demand = demand_info.get(po.linked_maintenance_order_no, {})
        result.append({
            "purchase_order_no": po.order_no,
            "purchase_date": po.order_date.isoformat() if po.order_date else None,
            "purchaser": po.purchaser,
            "demand_order_no": linked_demand.get("order_no"),
            "demand_date": linked_demand.get("order_date"),
            "line_count": len(lines_by_order.get(po_id, [])),
            "lines": lines_by_order.get(po_id, []),
        })

    return result

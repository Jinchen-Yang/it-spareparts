"""Read-only projection: purchase orders linked to a maintenance project.

Linkage path (stable assignment only — never name-based guessing):
    采购订单 (FPurchaseOrder.linked_maintenance_order_no)
    → 维保需求单 (FMaintenanceOrder.raw_order_id)
    → 项目 (MaintenanceSourceOrderAssignment, is_active)
"""

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.maintenance import FMaintenanceOrder
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.models.purchase import FPurchaseLine, FPurchaseOrder


def _money(value) -> str | None:
    """Fixed-point serialization; frontend money() already handles strings."""
    if value is None:
        return None
    return format(value, "f")


def _demand_ids_by_assignment(db: Session, project_id: str) -> set[str]:
    return set(
        db.scalars(
            select(MaintenanceSourceOrderAssignment.source_order_id).where(
                MaintenanceSourceOrderAssignment.project_id == project_id,
                MaintenanceSourceOrderAssignment.is_active.is_(True),
            )
        ).all()
    )


def get_project_procurement_chain(
    db: Session,
    project_id: str,
) -> list[dict]:
    """Return purchase orders → demand orders for *project_id*.

    Only demand orders stably linked via an active source assignment are
    included.  Anything unassigned, ambiguous or cancelled is excluded and
    must be surfaced by the admin reconciliation queue — the panel never
    guesses by project name.
    """
    demand_ids = _demand_ids_by_assignment(db, project_id)
    if not demand_ids:
        return []

    purchase_rows = (
        db.execute(
            select(
                FPurchaseOrder.id,
                FPurchaseOrder.order_no,
                FPurchaseOrder.order_date,
                FPurchaseOrder.purchaser,
                FPurchaseOrder.linked_maintenance_order_no,
                FPurchaseOrder.data_status,
            ).where(
                FPurchaseOrder.linked_maintenance_order_no.in_(demand_ids),
            )
        )
        .mappings()
        .all()
    )
    if not purchase_rows:
        return []

    # Established active-status filter（config.ACTIVE_STATUS = 已生效）：
    # 已作废/未生效订单不是业务证据，不进入项目采购面板。
    from app import config
    purchase_rows = [
        row for row in purchase_rows
        if row.data_status == config.ACTIVE_STATUS
    ]
    if not purchase_rows:
        return []

    order_ids = [row.id for row in purchase_rows]
    line_rows = (
        db.execute(
            select(
                FPurchaseLine.order_id,
                FPurchaseLine.pn_std,
                FPurchaseLine.description,
                FPurchaseLine.qty,
                FPurchaseLine.unit_price,
            ).where(FPurchaseLine.order_id.in_(order_ids))
        )
        .mappings()
        .all()
    )
    lines_by_order: dict[int, list[dict]] = defaultdict(list)
    for line in line_rows:
        lines_by_order[line.order_id].append({
            "pn": line.pn_std,
            "description": line.description,
            "qty": _money(line.qty),
            "unit_price": _money(line.unit_price),
        })

    demand_info: dict[str, dict] = {}
    demand_rows = db.execute(
        select(
            FMaintenanceOrder.raw_order_id,
            FMaintenanceOrder.order_no,
            FMaintenanceOrder.order_date,
        ).where(FMaintenanceOrder.raw_order_id.in_(demand_ids))
    ).mappings().all()
    for d in demand_rows:
        demand_info[d.raw_order_id] = {
            "order_no": d.order_no,
            "order_date": d.order_date.isoformat() if d.order_date else None,
        }

    result = []
    for po in purchase_rows:
        linked_demand = demand_info.get(po.linked_maintenance_order_no, {})
        result.append({
            "purchase_order_no": po.order_no,
            "purchase_date": po.order_date.isoformat() if po.order_date else None,
            "purchaser": po.purchaser,
            "demand_order_no": linked_demand.get("order_no"),
            "demand_date": linked_demand.get("order_date"),
            "line_count": len(lines_by_order.get(po.id, [])),
            "lines": lines_by_order.get(po.id, []),
        })

    return result

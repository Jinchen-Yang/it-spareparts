"""Read the existing active demand/project assignment; not authorization.

This deliberately does not add source-status, tombstone or line filters.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.maintenance import FMaintenanceOrder
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment


def demand_has_active_project_assignment(
    db: Session, *, project_id: str, demand_order_no: str,
) -> bool:
    return db.execute(
        select(FMaintenanceOrder.id)
        .join(
            MaintenanceSourceOrderAssignment,
            (MaintenanceSourceOrderAssignment.source_order_id
             == FMaintenanceOrder.raw_order_id)
            & (MaintenanceSourceOrderAssignment.is_active.is_(True)),
        )
        .where(
            FMaintenanceOrder.order_no == demand_order_no,
            MaintenanceSourceOrderAssignment.project_id == project_id,
        )
        .limit(1)
    ).first() is not None

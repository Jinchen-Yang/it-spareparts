"""Read-only projection: purchase orders linked to a maintenance project.

Linkage path (stable assignment only — never name-based guessing):
    采购订单 (FPurchaseOrder.linked_maintenance_order_no)
    → 维保需求单 (FMaintenanceOrder.raw_order_id)
    → 项目 (MaintenanceSourceOrderAssignment, is_active)
"""

from collections import defaultdict

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.maintenance import FMaintenanceOrder
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.models.purchase import FPurchaseLine, FPurchaseOrder

DEFAULT_PAGE_SIZE = 20


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
    """Return purchase orders → demand orders for *project_id*（全量，旧协议）。

    Only demand orders stably linked via an active source assignment are
    included.  Anything unassigned, ambiguous or cancelled is excluded and
    must be surfaced by the admin reconciliation queue — the panel never
    guesses by project name.
    """
    return list_project_procurement(db, project_id)["rows"]


def list_project_procurement(
    db: Session,
    project_id: str,
    *,
    source_order_id: str | None = None,
    page: int | None = None,
    page_size: int | None = None,
) -> dict:
    """采购链分页读模型（#259）：``{"rows", "total", "page", "page_size"}``。

    - ``source_order_id``：只看挂在这张需求单（WBDD raw id）上的采购单；仍然
      只认稳定归属——不在本项目归属内的需求单即使传了也是空。
    - ``page`` 省略 = 全量返回（与 :func:`get_project_procurement_chain` 相同），
      此时 ``page_size`` 被忽略、回显 None；给了 ``page`` 才回显真正切片用的页长
      （省略页长时是 :data:`DEFAULT_PAGE_SIZE`，不是 None）。``total`` 永远是
      过滤后的真实总数，不是本页行数。
    - 行序固定（采购日期倒序 → 采购单号 → id），否则分页跨页会重复/漏行。
    """
    from app import config

    page_size = (page_size or DEFAULT_PAGE_SIZE) if page is not None else None
    demand_ids = _demand_ids_by_assignment(db, project_id)
    if source_order_id is not None:
        demand_ids &= {source_order_id}
    if not demand_ids:
        return {"rows": [], "total": 0, "page": page, "page_size": page_size}

    # Established active-status filter（config.ACTIVE_STATUS = 已生效）：
    # 已作废/未生效订单不是业务证据，不进入项目采购面板。
    scope = (
        FPurchaseOrder.linked_maintenance_order_no.in_(demand_ids),
        FPurchaseOrder.data_status == config.ACTIVE_STATUS,
    )
    total = int(db.execute(
        select(func.count()).select_from(FPurchaseOrder).where(*scope)
    ).scalar_one())
    stmt = (
        select(
            FPurchaseOrder.id,
            FPurchaseOrder.order_no,
            FPurchaseOrder.order_date,
            FPurchaseOrder.purchaser,
            FPurchaseOrder.linked_maintenance_order_no,
            FPurchaseOrder.data_status,
        )
        .where(*scope)
        .order_by(FPurchaseOrder.order_date.desc().nullslast(),
                  FPurchaseOrder.order_no, FPurchaseOrder.id)
    )
    if page is not None:
        stmt = stmt.offset((page - 1) * page_size).limit(page_size)
    purchase_rows = db.execute(stmt).mappings().all()
    if not purchase_rows:
        return {"rows": [], "total": total, "page": page, "page_size": page_size}

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
        ).where(FMaintenanceOrder.raw_order_id.in_(
            {row.linked_maintenance_order_no for row in purchase_rows}))
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
            # 需求单 raw id：前端按选中需求单收敛时与看板 source_order_id 同一把键
            "demand_source_order_id": po.linked_maintenance_order_no,
            "demand_order_no": linked_demand.get("order_no"),
            "demand_date": linked_demand.get("order_date"),
            "line_count": len(lines_by_order.get(po.id, [])),
            "lines": lines_by_order.get(po.id, []),
        })

    return {"rows": result, "total": total, "page": page, "page_size": page_size}

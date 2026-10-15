"""maintenance-return-request-summary-v1：整单下单量与实际收货的只读核对。

有效明细和收货先独立聚合，再结合当前归属。调用者负责身份授权；
None 仅表示已确认的全域权限，空范围永远不扩大。这里不提供写入容量保证。
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder
from app.models.maintenance_doc_import import MaintenanceRkdReturnLine
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.services.query_filters import active_beta_maintenance_orders

MAX_BATCH_SIZE = 500
CONTRACT_VERSION = "v1.0-draft"
_ZERO = Decimal("0.000")


class ReturnSummaryError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__({
            "invalid_summary_request": "返还汇总参数无效",
            "summary_batch_too_large": "返还汇总每批最多 500 张需求单",
            "historical_as_of_not_supported": "当前返还汇总不支持历史截点",
            "summary_snapshot_required": "返还汇总需要只读一致性快照事务",
            "summary_unavailable": "返还汇总暂不可用，请重试",
        }[code])

    @property
    def status_code(self) -> int:
        return 503 if self.code in {"summary_snapshot_required", "summary_unavailable"} else 422


@dataclass(frozen=True, slots=True)
class DemandReturnSummary:
    source_order_id: str
    order_no: str | None
    project_id: str | None
    effective: bool
    demand_qty: Decimal | None
    received_qty: Decimal | None
    pending_qty: Decimal | None
    remaining_qty: Decimal | None
    over_received_qty: Decimal | None
    return_status: str | None
    basis_state: str
    reason_codes: tuple[str, ...]
    undated_receipt_qty: Decimal | None

    def to_dict(self) -> dict:
        values = asdict(self)
        for key in ("demand_qty", "received_qty", "pending_qty", "remaining_qty",
                    "over_received_qty", "undated_receipt_qty"):
            value = values[key]
            values[key] = format(value, ".3f") if value is not None else None
        values["reason_codes"] = list(self.reason_codes)
        return values


@dataclass(frozen=True, slots=True)
class ReturnRequestSummarySnapshot:
    as_of: datetime
    orders: tuple[DemandReturnSummary, ...]
    unassigned_by_project: dict[str, Decimal]

    def to_dict(self) -> dict:
        unknown = sum(item.pending_qty is None for item in self.orders)
        known = sum((item.pending_qty for item in self.orders if item.pending_qty is not None), _ZERO)
        return {
            "contract_version": CONTRACT_VERSION,
            "scope": "current_cumulative_whole_orders",
            "as_of": self.as_of.isoformat().replace("+00:00", "Z"),
            "orders": [item.to_dict() for item in self.orders],
            "unassigned_by_project": [
                {"project_id": project_id, "received_qty": format(qty, ".3f")}
                for project_id, qty in sorted(self.unassigned_by_project.items())
            ],
            "summary": {"known_pending_qty": format(known, ".3f"),
                        "unknown_order_count": unknown, "incomplete": bool(unknown)},
        }


def _ids(values: Collection[str] | None, *, max_length: int, nullable: bool = False):
    if values is None and nullable:
        return None
    if not isinstance(values, Collection) or isinstance(values, (str, bytes, dict)):
        raise ReturnSummaryError("invalid_summary_request")
    if any(not isinstance(item, str) or not item.strip() or len(item) > max_length for item in values):
        raise ReturnSummaryError("invalid_summary_request")
    return sorted(set(values))


def _summary(row) -> DemandReturnSummary:
    reasons = []
    count = row["line_count"] or 0
    demand = row["demand_qty"]
    if not count:
        reasons.append("no_active_lines")
        demand = None
    elif row["qty_count"] != count:
        reasons.append("missing_demand_qty")
        demand = None
    if row["negative_count"]:
        reasons.append("negative_demand_qty")
        demand = None
    if demand is not None and not demand.is_finite():
        reasons.append("invalid_demand_qty")
        demand = None
    if demand == _ZERO:
        reasons.append("zero_demand_qty")
    effective = bool(row["header_effective"] and row["project_id"] and row["project_active"])
    if not row["header_effective"]:
        reasons.append("inactive_order")
    if not row["project_id"]:
        reasons.append("unassigned_order")
    elif not row["project_active"]:
        reasons.append("inactive_project")
    received = row["received_qty"] or _ZERO
    undated = row["undated_qty"] or _ZERO
    conflict = bool(row["ownership_conflict"])
    if conflict:
        reasons.append("ownership_conflict")
        received = undated = None
    elif undated:
        reasons.append("undated_receipts")
    basis = ("ownership_conflict" if conflict else "inactive" if not effective
             else "incomplete" if demand is None or demand <= _ZERO else "complete")
    complete = basis == "complete"
    pending = max(demand - received, _ZERO) if complete else None
    return DemandReturnSummary(
        source_order_id=row["source_order_id"], order_no=row["order_no"],
        project_id=row["project_id"], effective=effective, demand_qty=demand,
        received_qty=received, pending_qty=pending, remaining_qty=pending,
        over_received_qty=(max(received - demand, _ZERO)
                           if demand is not None and received is not None else None),
        return_status=("returned" if received >= demand else "not_returned") if complete else None,
        basis_state=basis, reason_codes=tuple(reasons), undated_receipt_qty=undated,
    )


def summarize_demand_returns(
    db: Session, *, source_order_ids: Collection[str],
    allowed_project_ids: Collection[str] | None, as_of: date | None = None,
) -> dict[str, DemandReturnSummary]:
    """One aggregate SELECT per <=500 unique IDs; never flush, lock or commit."""
    if as_of is not None:
        raise ReturnSummaryError("historical_as_of_not_supported")
    ids = _ids(source_order_ids, max_length=64)
    allowed = _ids(allowed_project_ids, max_length=36, nullable=True)
    if len(ids) > MAX_BATCH_SIZE:
        raise ReturnSummaryError("summary_batch_too_large")
    if not ids or allowed == []:
        return {}

    active = active_beta_maintenance_orders(
        select(FMaintenanceOrder.id).where(FMaintenanceOrder.data_status == "已生效"),
        FMaintenanceOrder,
    ).correlate(None)
    orders = select(
        FMaintenanceOrder.id.label("order_id"),
        FMaintenanceOrder.raw_order_id.label("source_order_id"), FMaintenanceOrder.order_no,
        FMaintenanceOrder.id.in_(active).label("header_effective"),
        MaintenanceSourceOrderAssignment.project_id,
        MaintenanceProject.is_active.label("project_active"),
    ).outerjoin(MaintenanceSourceOrderAssignment, and_(
        MaintenanceSourceOrderAssignment.source_order_id == FMaintenanceOrder.raw_order_id,
        MaintenanceSourceOrderAssignment.is_active.is_(True),
    )).outerjoin(MaintenanceProject,
                 MaintenanceProject.project_id == MaintenanceSourceOrderAssignment.project_id,
    ).where(FMaintenanceOrder.raw_order_id.in_(ids))
    if allowed is not None:
        orders = orders.where(MaintenanceSourceOrderAssignment.project_id.in_(allowed))
    orders = orders.cte("return_summary_orders")
    demand = select(
        FMaintenanceLine.order_id,
        func.count().label("line_count"), func.count(FMaintenanceLine.qty).label("qty_count"),
        func.count().filter(FMaintenanceLine.qty < 0).label("negative_count"),
        func.sum(FMaintenanceLine.qty).label("demand_qty"),
    ).join(orders, FMaintenanceLine.order_id == orders.c.order_id).where(
        FMaintenanceLine.is_active.is_(True),
    ).group_by(FMaintenanceLine.order_id).cte("return_summary_demand")
    receipts = select(
        MaintenanceRkdReturnLine.source_order_id,
        func.sum(MaintenanceRkdReturnLine.qty).label("received_qty"),
        func.sum(case((MaintenanceRkdReturnLine.occurred_at.is_(None),
                       MaintenanceRkdReturnLine.qty), else_=_ZERO)).label("undated_qty"),
        func.bool_or(or_(orders.c.project_id.is_(None),
                        MaintenanceRkdReturnLine.project_id != orders.c.project_id,
                        )).label("ownership_conflict"),
    ).join(orders, MaintenanceRkdReturnLine.source_order_id == orders.c.source_order_id).where(
        MaintenanceRkdReturnLine.line_status == "active",
    ).group_by(MaintenanceRkdReturnLine.source_order_id).cte("return_summary_receipts")
    stmt = select(
        orders, demand.c.line_count, demand.c.qty_count, demand.c.negative_count, demand.c.demand_qty,
        receipts.c.received_qty, receipts.c.undated_qty, receipts.c.ownership_conflict,
    ).outerjoin(demand, demand.c.order_id == orders.c.order_id).outerjoin(
        receipts, receipts.c.source_order_id == orders.c.source_order_id,
    ).order_by(orders.c.source_order_id)
    try:
        with db.no_autoflush:
            return {row["source_order_id"]: _summary(row) for row in db.execute(stmt).mappings()}
    except SQLAlchemyError as exc:
        raise ReturnSummaryError("summary_unavailable") from exc


def get_return_requests_outstanding_summary(
    db: Session, *, source_order_ids: Collection[str],
    allowed_project_ids: Collection[str] | None, project_ids: Collection[str] | None,
) -> ReturnRequestSummarySnapshot:
    """Batch the complete caller-selected set in its existing read-only RR transaction.

    The caller must select IDs in that same transaction. We neither discover
    more orders nor change the connection's isolation level/transaction state.
    Project filters restrict both orders and the separate unassigned buckets.
    """
    ids = _ids(source_order_ids, max_length=64)
    allowed = _ids(allowed_project_ids, max_length=36, nullable=True)
    projects = _ids(project_ids, max_length=36, nullable=True)
    selected = (projects if allowed is None else allowed if projects is None
                else sorted(set(projects) & set(allowed)))
    try:
        with db.no_autoflush:
            isolation, readonly, observed_at = db.execute(select(
                func.current_setting("transaction_isolation"),
                func.current_setting("transaction_read_only"), func.transaction_timestamp(),
            )).one()
            if isolation != "repeatable read" or readonly != "on":
                raise ReturnSummaryError("summary_snapshot_required")
            orders = []
            for start in range(0, len(ids), MAX_BATCH_SIZE):
                orders.extend(summarize_demand_returns(
                    db, source_order_ids=ids[start:start + MAX_BATCH_SIZE],
                    allowed_project_ids=selected,
                ).values())
            unassigned = {}
            if selected != []:
                stmt = select(
                    MaintenanceRkdReturnLine.project_id, func.sum(MaintenanceRkdReturnLine.qty),
                ).where(
                    MaintenanceRkdReturnLine.line_status == "active",
                    MaintenanceRkdReturnLine.source_order_id.is_(None),
                ).group_by(MaintenanceRkdReturnLine.project_id)
                if selected is not None:
                    stmt = stmt.where(MaintenanceRkdReturnLine.project_id.in_(selected))
                unassigned = dict(db.execute(stmt).all())
            return ReturnRequestSummarySnapshot(observed_at, tuple(orders), unassigned)
    except SQLAlchemyError as exc:
        raise ReturnSummaryError("summary_unavailable") from exc

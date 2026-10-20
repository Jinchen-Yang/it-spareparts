"""Frozen d41e3d6 in-memory candidate implementation, for SQL equivalence tests only."""
from decimal import Decimal
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from app.models.dimensions import DimPart
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder
from app.models.maintenance_doc_import import MaintenanceRkdReturnLine
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.services.query_filters import active_beta_maintenance_orders
from app.services.maintenance_return_request_summary import MAX_BATCH_SIZE, summarize_demand_returns
from app.services.maintenance_return_receipts import _get_project, _qty, ReturnReceiptNotFound


def legacy_memory_candidates(
    db: Session, *, project_id: str, page: int, page_size: int, q: str | None = None,
    part_id: int | None = None, capacity_only: bool = False,
    receipt_id: str | None = None,
) -> dict:
    """Receipt selectors expose only effective WBDD identities in this project."""
    _get_project(db, project_id)
    stmt = active_beta_maintenance_orders(
        select(FMaintenanceOrder.raw_order_id, FMaintenanceOrder.order_no,
               FMaintenanceOrder.order_date)
        .join(MaintenanceSourceOrderAssignment,
              MaintenanceSourceOrderAssignment.source_order_id == FMaintenanceOrder.raw_order_id)
        .where(MaintenanceSourceOrderAssignment.project_id == project_id,
               MaintenanceSourceOrderAssignment.is_active.is_(True),
               FMaintenanceOrder.data_status == "已生效"),
        FMaintenanceOrder,
    )
    if q and q.strip():
        # 2026-10-13 口径：q 匹配「单号 或 单内任一活跃明细行的 PN 文本」——
        # 需求单候选框里直接输 PN 也要能搜出单（原话：输入 pn 自行匹配维保订单）。
        # PN 文本三路兜底：行 pn_std / 行 pn_raw / 型号主数据当前 pn_std（治理
        # 合并或改名后行内旧 PN 过期，按当前主数据仍可搜到）。仍用 EXISTS 不
        # join 外层，同单多行命中不重复，count/分页口径不变。
        text_q = q.strip()
        stmt = stmt.where(or_(
            FMaintenanceOrder.order_no.icontains(text_q, autoescape=True),
            select(FMaintenanceLine.id)
            .join(DimPart, DimPart.id == FMaintenanceLine.part_id)
            .where(
                FMaintenanceLine.order_id == FMaintenanceOrder.id,
                FMaintenanceLine.is_active.is_(True),
                or_(
                    FMaintenanceLine.pn_std.icontains(text_q, autoescape=True),
                    FMaintenanceLine.pn_raw.icontains(text_q, autoescape=True),
                    DimPart.pn_std.icontains(text_q, autoescape=True),
                ),
            ).exists(),
        ))
    if part_id is not None:
        # 按 PN 建议需求单（2026-10-12）：EXISTS 相关子查询只判断「单内存在该备件
        # 的活跃明细行」，不 join——同单多行同 PN 天然不重复，count/分页口径不变，
        # 且能吃 ix_ml_order_active 索引。
        stmt = stmt.where(
            select(FMaintenanceLine.id).where(
                FMaintenanceLine.order_id == FMaintenanceOrder.id,
                FMaintenanceLine.is_active.is_(True),
                FMaintenanceLine.part_id == part_id,
            ).exists()
        )
    ordering = (FMaintenanceOrder.order_no, FMaintenanceOrder.raw_order_id)
    if not capacity_only:
        total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        rows = db.execute(stmt.order_by(*ordering)
                          .offset((page - 1) * page_size).limit(page_size)).all()
        return {"rows": [{"source_order_id": row.raw_order_id, "order_no": row.order_no,
                           "order_date": row.order_date.isoformat() if row.order_date else None}
                          for row in rows],
                "total": total, "page": page, "page_size": page_size}

    current = None
    if receipt_id:
        current = db.scalar(select(MaintenanceRkdReturnLine).where(
            MaintenanceRkdReturnLine.rkd_line_id == receipt_id,
            MaintenanceRkdReturnLine.project_id == project_id,
            MaintenanceRkdReturnLine.line_status == "active",
        ))
        if current is None:
            raise ReturnReceiptNotFound("返还记录不存在")
    rows = db.execute(stmt.order_by(*ordering)).all()
    # Keep the historical identity even when inactive, reassigned or outside
    # the search/PN filter. Only the receipt's authorized identity is exposed;
    # the scoped summary below still withholds another project's quantities.
    if current and current.source_order_id not in {row.raw_order_id for row in rows}:
        own_order = db.execute(select(FMaintenanceOrder.raw_order_id, FMaintenanceOrder.order_no,
                                      FMaintenanceOrder.order_date)
            .where(FMaintenanceOrder.raw_order_id == current.source_order_id)).first()
        if own_order:
            rows.append(own_order)
    summaries = {}
    ids = [row.raw_order_id for row in rows]
    for start in range(0, len(ids), MAX_BATCH_SIZE):
        summaries.update(summarize_demand_returns(
            db, source_order_ids=ids[start:start + MAX_BATCH_SIZE],
            allowed_project_ids=[project_id],
        ))
    candidates = []
    for row in rows:
        summary = summaries.get(row.raw_order_id)
        is_current = bool(current and current.source_order_id == row.raw_order_id)
        if not is_current and (summary is None or summary.basis_state != "complete"
                               or summary.remaining_qty <= 0):
            continue
        maximum = None
        if summary and summary.basis_state == "complete":
            maximum = max(summary.demand_qty - summary.received_qty
                          + (current.qty if is_current else Decimal(0)), Decimal(0))
        if is_current:
            maximum = max(maximum or Decimal(0), current.qty) if current.qty.is_finite() else None
        candidates.append({
            "source_order_id": row.raw_order_id, "order_no": row.order_no,
            "order_date": row.order_date.isoformat() if row.order_date else None,
            "demand_qty": _qty(summary.demand_qty) if summary and summary.demand_qty is not None else None,
            "received_qty": _qty(summary.received_qty) if summary and summary.received_qty is not None else None,
            "remaining_qty": _qty(summary.remaining_qty) if summary and summary.remaining_qty is not None else None,
            "max_qty": _qty(maximum) if maximum is not None else None,
            "basis_state": summary.basis_state if summary else "incomplete",
            "is_current": is_current,
        })
    # The edit selector must see its current order on the first page, even if
    # its display number would otherwise sort after hundreds of alternatives.
    candidates.sort(key=lambda item: not item["is_current"])
    return {"rows": candidates[(page - 1) * page_size:page * page_size],
            "total": len(candidates), "page": page, "page_size": page_size}

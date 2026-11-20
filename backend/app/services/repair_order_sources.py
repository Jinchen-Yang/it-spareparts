"""Query and reserve maintenance return quantities for a repair order."""

from sqlalchemy import BigInteger, String, cast, column, func, or_, select, table
from sqlalchemy.orm import Session

from app import permissions
from app.api.maintenance_project_scope import resolve_visible_project_ids
from app.models.dimensions import DimPart
from app.models.maintenance_doc_import import MaintenanceRkdReturnLine as Receipt
from app.security import UserContext
from app.services.maintenance_repair import (
    BAD_CONDITIONS,
    RepairConflict,
    RepairForbidden,
    eligible_qty,
    lock_parts,
)
from app.services.maintenance_return_receipts import lock_receipt_context, lock_receipt_projects

# Both the old result ledger and repair-order reservations contribute to this view.
deductions = table(
    "v_repair_receipt_deductions",
    column("receipt_id", String),
    column("legacy_qty", BigInteger),
    column("order_qty", BigInteger),
)


def _available_source(row: Receipt, part: DimPart | None) -> int:
    if part is None or part.status != "active" or row.receipt_kind != "part" or row.review_required:
        return 0
    return eligible_qty(row)


def _candidate(row: Receipt, part: DimPart, available: int) -> dict:
    return {
        "receiptId": row.rkd_line_id,
        "version": row.version,
        "headNo": row.head_no,
        "occurredAt": row.occurred_at.isoformat() if row.occurred_at else None,
        "pn": part.pn_std,
        "description": (part.description or row.description or "")[:256],
        "condition": row.test_result,
        "availableQty": available,
    }


def _require_receipt_page(ctx: UserContext) -> None:
    if not permissions.page_permission_allowed(
        role=ctx.role, permission_map=ctx.permissions or {}, page_key="page_maintenance"
    ):
        raise RepairForbidden("无权访问维保备件返还")


def return_candidates(db: Session, ctx: UserContext, *, q: str | None, page: int, page_size: int) -> dict:
    _require_receipt_page(ctx)
    scope = resolve_visible_project_ids(db, ctx)
    if scope == set():
        return {"items": [], "total": 0}

    balance = Receipt.qty - func.coalesce(deductions.c.legacy_qty, 0) - func.coalesce(deductions.c.order_qty, 0)
    query = (
        select(Receipt, DimPart, cast(balance, BigInteger).label("available"))
        .join(DimPart, Receipt.part_id == DimPart.id)
        .outerjoin(deductions, deductions.c.receipt_id == Receipt.rkd_line_id)
        .where(
            Receipt.line_status == "active",
            Receipt.receipt_kind == "part",
            Receipt.review_required.is_(False),
            Receipt.test_result.in_(BAD_CONDITIONS),
            DimPart.status == "active",
            Receipt.qty >= 1,
            Receipt.qty <= 2147483647,
            Receipt.qty == func.trunc(Receipt.qty),
            balance > 0,
        )
    )
    if scope is not None:
        query = query.where(Receipt.project_id.in_(scope))
    if q and (term := q.strip()):
        query = query.where(or_(
            Receipt.head_no.icontains(term, autoescape=True),
            Receipt.pn.icontains(term, autoescape=True),
            DimPart.pn_std.icontains(term, autoescape=True),
            DimPart.description.icontains(term, autoescape=True),
            Receipt.description.icontains(term, autoescape=True),
        ))
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.execute(
        query.order_by(Receipt.occurred_at.desc().nulls_last(), Receipt.rkd_line_id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [_candidate(row, part, available) for row, part, available in rows], "total": total or 0}


def reserve_return_sources(
    db: Session,
    ctx: UserContext,
    expected_versions: dict[str, int | None],
    quantities: dict[str, int],
    existing_quantities: dict[str, int],
) -> dict[str, dict]:
    """Validate the proposed final order against current balances while holding source locks.

    Existing quantities belong to the already-locked order. They can be reused by
    that order and still count against other repair orders and the old ledger.
    """
    ids = set(quantities) | set(existing_quantities)
    if not ids:
        return {}
    increasing_ids = {
        receipt_id for receipt_id, requested in quantities.items()
        if requested > existing_quantities.get(receipt_id, 0)
    }
    if increasing_ids:
        _require_receipt_page(ctx)
    lock_receipt_context(db)
    identities = {
        row.rkd_line_id: (row.part_id, row.project_id)
        for row in db.execute(
            select(Receipt.rkd_line_id, Receipt.part_id, Receipt.project_id)
            .where(Receipt.rkd_line_id.in_(ids))
        )
    }
    if set(identities) != ids:
        raise RepairConflict("选中的返还明细已变化，请刷新后重试")
    part_ids = {part for part, _ in identities.values() if part is not None}
    lock_parts(db, part_ids)
    lock_receipt_projects(db, {project for _, project in identities.values()})
    rows = db.scalars(
        select(Receipt)
        .where(Receipt.rkd_line_id.in_(ids))
        .order_by(Receipt.rkd_line_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    if len(rows) != len(ids) or any(
        identities[row.rkd_line_id] != (row.part_id, row.project_id) for row in rows
    ):
        raise RepairConflict("选中的返还明细已变化，请刷新后重试")

    # 释放原单占用沿用同一套锁，只有增加占用需要重新选件鉴权。
    if not increasing_ids:
        return {}
    scope = resolve_visible_project_ids(db, ctx)
    increasing_part_ids = {identities[receipt_id][0] for receipt_id in increasing_ids}
    parts = {
        part.id: part for part in db.scalars(
            select(DimPart).where(DimPart.id.in_(increasing_part_ids)).order_by(DimPart.id).with_for_update(read=True)
        )
    }
    used = dict(db.execute(
        select(deductions.c.receipt_id, deductions.c.legacy_qty + deductions.c.order_qty)
        .where(deductions.c.receipt_id.in_(increasing_ids))
    ).all())
    sources = {}
    for row in rows:
        receipt_id = row.rkd_line_id
        if receipt_id not in increasing_ids:
            continue
        if scope is not None and row.project_id not in scope:
            raise RepairForbidden("无权选用该维保项目的返还明细")
        version = expected_versions.get(receipt_id)
        if version is None:
            raise RepairConflict("追加送修数量须使用当前返还明细版本，请刷新后重试")
        if row.version != version:
            raise RepairConflict("返还明细已更新，请刷新左侧候选后重试")
        part = parts.get(row.part_id)
        existing = existing_quantities.get(receipt_id, 0)
        available = _available_source(row, part) - used.get(receipt_id, 0) + existing
        if part is None or quantities[receipt_id] > available:
            raise RepairConflict("返还明细的可用数量不足或已不可送修，请刷新后重试")
        sources[receipt_id] = _candidate(row, part, available)
    return sources

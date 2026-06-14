"""库存服务（§7.4/§7.6）：快照列表 + 人工修正 + 出入流水动态在库与对账。

在库口径（§7.6）：
- 快照 inventory.source_qty 是源系统某时点真实库存，退化为「对账基准」；
- 永续在库由 inventory_movement 按 (part_id, warehouse) 时间回放求得（compute_onhand），
  盘点(is_absolute)重置结存、直发(direction=0)不计；
- reconcile 把两者并列比差异，缺一方则标 ledger_only / snapshot_only，兜住「流水不全」。
"""
from datetime import date
from decimal import Decimal

from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from app import config, security
from app.models.dimensions import DimPart
from app.models.inventory import Inventory, InventoryMovement
from app.models.system import SysAuditLog

_QTY_TOL = Decimal("0.001")


def _d(x):
    return float(x) if isinstance(x, Decimal) else x


def _jsonable(d: dict) -> dict:
    """审计 JSONB 安全化：date → isoformat 字符串。"""
    return {k: (v.isoformat() if isinstance(v, date) else v) for k, v in d.items()}


def _display_qty(inv: Inventory) -> Decimal:
    return inv.manual_qty if inv.is_qty_overridden and inv.manual_qty is not None else inv.source_qty


def _row(inv: Inventory) -> dict:
    return {
        "id": inv.id, "part_id": inv.part_id, "pn_std": inv.pn_std, "warehouse": inv.warehouse,
        "display_qty": _d(_display_qty(inv)),
        "source_qty": _d(inv.source_qty), "manual_qty": _d(inv.manual_qty),
        "is_qty_overridden": inv.is_qty_overridden, "safety_stock": _d(inv.safety_stock),
        "description": inv.description, "brand": inv.brand, "unit": inv.unit,
        "unit_cost": _d(inv.unit_cost), "inventory_value": _d(inv.inventory_value),
        "snapshot_date": inv.snapshot_date,
    }


def list_inventory(db: Session, warehouse: str | None, q: str | None,
                   page: int, page_size: int,
                   user_ctx: security.UserContext | None = None) -> dict:
    stmt = select(Inventory)
    if warehouse:
        stmt = stmt.where(Inventory.warehouse == warehouse)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Inventory.pn_std.ilike(like), Inventory.description.ilike(like)))
    if user_ctx is not None:
        stmt = security.apply_data_scope(stmt, user_ctx)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.execute(
        stmt.order_by(Inventory.pn_std, Inventory.warehouse)
        .offset((page - 1) * page_size).limit(page_size)
    ).scalars().all()
    # 附动态在库（流水回放）与对账差异：仅算本页涉及的 part，避免全表回放
    onhand = compute_onhand(db, part_ids={r.part_id for r in rows}) if rows else {}
    items = []
    for inv in rows:
        row = _row(inv)
        computed = onhand.get((inv.part_id, inv.warehouse))
        row["computed_qty"] = _d(computed) if computed is not None else None
        row["ledger_diff"] = (_d(computed - inv.source_qty)
                              if computed is not None and inv.source_qty is not None else None)
        items.append(row)
    return {"total": total, "page": page, "page_size": page_size, "items": items}


def compute_onhand(db: Session, warehouse: str | None = None,
                   part_ids: set[int] | None = None,
                   as_of: date | None = None) -> dict[tuple[int, str], Decimal]:
    """按 (part_id, warehouse) 时间回放出入流水求在库（永续库存核心，§7.6）。

    回放顺序：movement_date 升序（NULL 视为最早）、同日按 id。规则：
    - is_absolute（盘点）→ 把结存重置为 qty；
    - 其余 → 结存 += direction * qty（direction=0 的直发/通知不动结存）。
    as_of 给定则只计该日（含）之前的流水（点时库存）；part_ids 限定范围。
    """
    if part_ids is not None and not part_ids:
        return {}
    stmt = select(
        InventoryMovement.part_id, InventoryMovement.warehouse,
        InventoryMovement.direction, InventoryMovement.qty, InventoryMovement.is_absolute,
    ).order_by(
        InventoryMovement.part_id, InventoryMovement.warehouse,
        InventoryMovement.movement_date.asc().nullsfirst(), InventoryMovement.id.asc(),
    )
    if warehouse:
        stmt = stmt.where(InventoryMovement.warehouse == warehouse)
    if part_ids is not None:
        stmt = stmt.where(InventoryMovement.part_id.in_(part_ids))
    if as_of is not None:
        stmt = stmt.where(or_(InventoryMovement.movement_date <= as_of,
                              InventoryMovement.movement_date.is_(None)))
    out: dict[tuple[int, str], Decimal] = {}
    for part_id, wh, direction, qty, is_abs in db.execute(stmt):
        key = (part_id, wh)
        cur = out.get(key, Decimal(0))
        out[key] = qty if is_abs else cur + direction * qty
    return out


def _movement_dict(m: InventoryMovement, balance: Decimal | None = None) -> dict:
    return {
        "id": m.id, "raw_movement_id": m.raw_movement_id, "pn_std": m.pn_std,
        "warehouse": m.warehouse, "movement_date": m.movement_date,
        "doc_type": m.doc_type, "doc_type_raw": m.doc_type_raw, "doc_no": m.doc_no,
        "direction": m.direction, "qty": _d(m.qty), "is_absolute": m.is_absolute,
        "signed_qty": (0.0 if m.direction == 0 else _d(m.direction * m.qty)),
        "unit_price": _d(m.unit_price), "counterpart_warehouse": m.counterpart_warehouse,
        "ledger_kind": m.ledger_kind, "snapshot_balance": _d(m.snapshot_balance),
        "balance": _d(balance) if balance is not None else None,
    }


def part_ledger(db: Session, part_id: int, warehouse: str | None = None) -> list[dict]:
    """单个 part 的完整出入流水（按时间升序）+ 逐行动态结存——库存页点开看明细用。"""
    stmt = select(InventoryMovement).where(InventoryMovement.part_id == part_id)
    if warehouse:
        stmt = stmt.where(InventoryMovement.warehouse == warehouse)
    stmt = stmt.order_by(
        InventoryMovement.warehouse,
        InventoryMovement.movement_date.asc().nullsfirst(), InventoryMovement.id.asc(),
    )
    running: dict[str, Decimal] = {}
    out = []
    for m in db.execute(stmt).scalars():
        bal = m.qty if m.is_absolute else running.get(m.warehouse, Decimal(0)) + m.direction * m.qty
        running[m.warehouse] = bal
        out.append(_movement_dict(m, bal))
    return out


def list_movements(db: Session, warehouse: str | None, q: str | None,
                   doc_type: str | None, page: int, page_size: int,
                   ledger_kind: str | None = None) -> dict:
    """全局出入流水分页（按时间倒序），可按仓库/型号/单据类型/备件整机筛选。"""
    stmt = select(InventoryMovement)
    if warehouse:
        stmt = stmt.where(InventoryMovement.warehouse == warehouse)
    if doc_type:
        stmt = stmt.where(InventoryMovement.doc_type == doc_type)
    if ledger_kind:
        stmt = stmt.where(InventoryMovement.ledger_kind == ledger_kind)
    if q:
        stmt = stmt.where(InventoryMovement.pn_std.ilike(f"%{q.strip()}%"))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.execute(
        stmt.order_by(InventoryMovement.movement_date.desc().nullslast(),
                      InventoryMovement.id.desc())
        .offset((page - 1) * page_size).limit(page_size)
    ).scalars().all()
    return {"total": total, "page": page, "page_size": page_size,
            "items": [_movement_dict(m) for m in rows]}


def reconcile(db: Session, warehouse: str | None = None,
              only_diff: bool = True, as_of: date | None = None) -> dict:
    """对账：流水回放在库 vs 快照 source_qty，按 (part_id, warehouse) 比对。

    status：match=一致 / diff=有差异 / ledger_only=只有流水无快照 / snapshot_only=只有快照无流水。
    缺一方按 0 计差异。only_diff=True 时隐藏完全一致的行。
    """
    computed = compute_onhand(db, warehouse=warehouse, as_of=as_of)

    snap_stmt = (
        select(Inventory.part_id, Inventory.warehouse, func.sum(Inventory.source_qty))
        .group_by(Inventory.part_id, Inventory.warehouse)
    )
    if warehouse:
        snap_stmt = snap_stmt.where(Inventory.warehouse == warehouse)
    snapshot = {(p, w): q for p, w, q in db.execute(snap_stmt)}

    keys = set(computed) | set(snapshot)
    part_ids = {p for p, _ in keys}
    pn_by_id = dict(db.execute(
        select(DimPart.id, DimPart.pn_std).where(DimPart.id.in_(part_ids))
    ).all()) if part_ids else {}

    rows = []
    n_match = n_diff = 0
    for (part_id, wh) in keys:
        c = computed.get((part_id, wh))
        s = snapshot.get((part_id, wh))
        cval = c if c is not None else Decimal(0)
        sval = s if s is not None else Decimal(0)
        diff = cval - sval
        if s is None:
            stat = "ledger_only"
        elif c is None:
            stat = "snapshot_only"
        elif abs(diff) <= _QTY_TOL:
            stat = "match"
        else:
            stat = "diff"
        if stat == "match":
            n_match += 1
        else:
            n_diff += 1
        if only_diff and stat == "match":
            continue
        rows.append({
            "part_id": part_id, "pn_std": pn_by_id.get(part_id), "warehouse": wh,
            "computed_qty": _d(c) if c is not None else None,
            "snapshot_qty": _d(s) if s is not None else None,
            "diff": _d(diff), "status": stat,
        })
    rows.sort(key=lambda r: (abs(r["diff"] or 0)), reverse=True)
    return {"rows": rows, "summary": {"match": n_match, "diff": n_diff, "total": n_match + n_diff}}


def backfill_costs(db: Session) -> dict:
    """按商品加权平均(不含税,口径同利润COGS)回填 inventory.unit_cost / inventory_value。

    单位成本 = Σ(采购量×不含税单价) / Σ采购量，仅取计入成本的采购类型、已生效、单价>0。
    库存金额 = 展示数量(人工修正优先) × 单位成本。无采购记录的商品保持 NULL（界面显示"未计算"）。
    整改 P3：按 part_id 关联（合并后源/目标采购历史归并，与利润口径一致）。
    """
    ex = "/ (1 + COALESCE(po.tax_rate, 0))" if config.TAX_BASIS == "ex_tax" else ""
    active = "AND po.data_status = '已生效'" if config.ACTIVE_STATUS_ONLY else ""
    sql = text(f"""
        WITH cost AS (
            SELECT pl.part_id,
                   SUM(pl.qty * pl.unit_price {ex}) / NULLIF(SUM(pl.qty), 0) AS uc
            FROM f_purchase_line pl
            JOIN f_purchase_order po ON pl.order_id = po.id
            WHERE pl.unit_price > 0 AND pl.qty > 0
              AND po.source_type = ANY(:types) {active}
            GROUP BY pl.part_id
        )
        UPDATE inventory i SET
            unit_cost = round(c.uc, 2),
            inventory_value = round(c.uc * (CASE WHEN i.is_qty_overridden AND i.manual_qty IS NOT NULL
                                                 THEN i.manual_qty ELSE i.source_qty END), 2)
        FROM cost c WHERE c.part_id = i.part_id
    """)
    res = db.execute(sql, {"types": config.COST_PURCHASE_TYPES})
    db.commit()
    filled = db.scalar(select(func.count()).select_from(Inventory).where(Inventory.unit_cost.is_not(None)))
    total = db.scalar(select(func.count()).select_from(Inventory))
    return {"updated": res.rowcount, "filled": filled, "total": total,
            "no_cost": total - filled}


def warehouses(db: Session) -> list[str]:
    return [w for (w,) in db.execute(
        select(Inventory.warehouse).distinct().order_by(Inventory.warehouse)
    ).all()]


def update_inventory(db: Session, inv_id: int, manual_qty: Decimal | None,
                     safety_stock: Decimal | None, clear_override: bool,
                     reason: str | None, operated_by: str | None) -> dict | None:
    """人工修正：写 manual_qty/safety_stock，不动 source_qty；写审计；重算库存金额。"""
    inv = db.get(Inventory, inv_id)
    if inv is None:
        return None
    before = _row(inv)

    if clear_override:
        inv.manual_qty = None
        inv.is_qty_overridden = False
    elif manual_qty is not None:
        inv.manual_qty = manual_qty
        inv.is_qty_overridden = True
    if safety_stock is not None:
        inv.safety_stock = safety_stock

    # 重算库存金额（display_qty × unit_cost）
    if inv.unit_cost is not None:
        inv.inventory_value = (_display_qty(inv) * inv.unit_cost).quantize(Decimal("0.01"))

    db.flush()
    after = _row(inv)
    db.add(SysAuditLog(entity_type="inventory", entity_id=inv.id, action="update",
                       before_json=_jsonable(before), after_json=_jsonable(after), reason=reason,
                       operated_by=operated_by))
    db.commit()
    return after

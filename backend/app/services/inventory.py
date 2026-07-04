"""库存服务（§7.4）：列表 + 人工修正（写审计、不碰 source_qty）。"""
from datetime import date
from decimal import Decimal
from functools import reduce

from sqlalchemy import case, func, or_, select, text
from sqlalchemy.orm import Session

from app import config, security
from app.models.dimensions import DimPart
from app.models.inventory import Inventory
from app.models.system import SysAuditLog
from app.services.query_filters import col_matches_any, keyword_groups_or_substr


def _d(x):
    return float(x) if isinstance(x, Decimal) else x


def _jsonable(d: dict) -> dict:
    """审计 JSONB 安全化：date → isoformat 字符串。"""
    return {k: (v.isoformat() if isinstance(v, date) else v) for k, v in d.items()}


def _display_qty(inv: Inventory) -> Decimal:
    return inv.manual_qty if inv.is_qty_overridden and inv.manual_qty is not None else inv.source_qty


def _row(inv: Inventory) -> dict:
    return {
        "id": inv.id, "pn_std": inv.pn_std, "warehouse": inv.warehouse,
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
    if q and q.strip():
        # 分词模糊（大小写不敏感 ILIKE + 规格变体，与动态库存/采购同源）：'8TB SATA' 词序无关、
        # 跨 pn_std/description 命中即可，不再要求整段连续子串（旧整串匹配模糊度过低）。
        for g in keyword_groups_or_substr(q):
            stmt = stmt.where(or_(
                col_matches_any(Inventory.pn_std, g),
                col_matches_any(Inventory.description, g),
            ))
    if user_ctx is not None:
        stmt = security.apply_data_scope(stmt, user_ctx)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.execute(
        stmt.order_by(Inventory.pn_std, Inventory.warehouse)
        .offset((page - 1) * page_size).limit(page_size)
    ).scalars().all()
    return {"total": total, "page": page, "page_size": page_size,
            "items": [_row(inv) for inv in rows]}


def backfill_costs(db: Session) -> dict:
    """按商品加权平均(不含税,口径同利润COGS)回填 inventory.unit_cost / inventory_value。

    单位成本 = Σ(采购量×不含税单价) / Σ采购量，仅取计入成本的采购类型、已生效、单价>0。
    库存金额 = 展示数量(人工修正优先) × 单位成本。无采购记录的商品保持 NULL（界面显示"未计算"）。
    整改 P3：按 part_id 关联（合并后源/目标采购历史归并，与利润口径一致）。
    """
    ex = "/ (1 + COALESCE(po.tax_rate, 0))" if config.TAX_BASIS == "ex_tax" else ""
    active = f"AND po.data_status = '{config.ACTIVE_STATUS}'" if config.ACTIVE_STATUS_ONLY else ""
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


# ============================================================
# 锚定动态库存（甲方 2026-07-04）：最近库存快照做期初 + 快照日之后单据流水。
# 动态可用 = 期初 + 采购入 − 销售出 − 维保出（退货冲抵）。截止今天（未来日期脏单不计）。
# 期初来源可插拔：8 月盘点结果按普通库存文件导入后，锚点自动前移、期初随之变准。
# 仓库维度：采购单无仓库字段 → 只算型号级；分仓展示用快照行作参考（甲方选定口径）。
# ============================================================

def dynamic_stock_map(db: Session, part_ids: list[int] | None = None) -> dict[int, dict]:
    """按 part 计算锚定动态库存。返回 {part_id: {dynamic_qty, anchor_qty, anchor_date,
    in_qty, out_sales, out_maint}}；无快照的新型号期初=0、流水全算。"""
    from sqlalchemy import case

    from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder
    from app.models.purchase import FPurchaseLine, FPurchaseOrder
    from app.models.sales import FSalesLine, FSalesOrder
    from app.services.query_filters import active_orders

    if part_ids is not None and not part_ids:
        return {}
    today = date.today()
    display = case((Inventory.is_qty_overridden, Inventory.manual_qty),
                   else_=Inventory.source_qty)
    anchor_q = (
        select(Inventory.part_id.label("pid"),
               func.sum(display).label("aq"),
               func.max(Inventory.snapshot_date).label("ad"))
        .group_by(Inventory.part_id)
    )
    if part_ids is not None:
        anchor_q = anchor_q.where(Inventory.part_id.in_(part_ids))
    anchor_sq = anchor_q.subquery()

    def _blank() -> dict:
        return {"anchor_qty": Decimal(0), "anchor_date": None, "in_qty": Decimal(0),
                "out_sales": Decimal(0), "out_maint": Decimal(0)}

    out: dict[int, dict] = {}
    for pid, aq, ad in db.execute(select(anchor_sq.c.pid, anchor_sq.c.aq, anchor_sq.c.ad)):
        rec = _blank()
        rec["anchor_qty"], rec["anchor_date"] = aq or Decimal(0), ad
        out[pid] = rec

    def _flow(line, order, qty_expr, key):
        stmt = (
            select(line.part_id, func.coalesce(func.sum(qty_expr), 0))
            .join(order, line.order_id == order.id)
            .outerjoin(anchor_sq, anchor_sq.c.pid == line.part_id)
            .where(order.order_date.is_not(None), order.order_date <= today,
                   or_(anchor_sq.c.ad.is_(None), order.order_date > anchor_sq.c.ad))
            .group_by(line.part_id)
        )
        stmt = active_orders(stmt, order)
        if part_ids is not None:
            stmt = stmt.where(line.part_id.in_(part_ids))
        for pid, qv in db.execute(stmt):
            out.setdefault(pid, _blank())[key] = qv or Decimal(0)

    _flow(FPurchaseLine, FPurchaseOrder, func.coalesce(FPurchaseLine.qty, 0), "in_qty")
    _flow(FSalesLine, FSalesOrder, func.coalesce(FSalesLine.qty, 0), "out_sales")
    _flow(FMaintenanceLine, FMaintenanceOrder,
          func.coalesce(FMaintenanceLine.qty, 0) - func.coalesce(FMaintenanceLine.return_qty, 0),
          "out_maint")

    for rec in out.values():
        rec["dynamic_qty"] = (rec["anchor_qty"] + rec["in_qty"]
                              - rec["out_sales"] - rec["out_maint"])
    return out


def list_dynamic(db: Session, q: str | None, page: int, page_size: int,
                 user_ctx: security.UserContext | None = None) -> dict:
    """动态库存列表（型号级为主）：动态可用/期初(锚点日)/之后入出，分仓快照行作参考。"""
    smap = dynamic_stock_map(db)
    ids = list(smap)
    # 关键词搜索：分词（大小写不敏感 ILIKE + 规格变体归一，与型号查询/采购同一 keyword_term_groups
    # 口径）→ 逐词组统计命中数（pn_std/description/brand 任一命中即算该词命中）→ **部分命中也召回**，
    # 按（命中词数 desc, 动态库存 desc）排序=匹配率优先。前端据 match_hits/match_terms 显示「命中 X/N词」。
    hit_map: dict[int, int] = {}
    n_terms = 0
    groups = keyword_groups_or_substr(q)
    if q and q.strip() and ids and groups:
        n_terms = len(groups)
        hit_exprs = []
        for g in groups:
            cond = or_(
                col_matches_any(DimPart.pn_std, g),
                col_matches_any(DimPart.description, g),
                col_matches_any(DimPart.brand, g),
            )
            hit_exprs.append(case((cond, 1), else_=0))
        hits = reduce(lambda a, b: a + b, hit_exprs)
        rows = db.execute(
            select(DimPart.id, hits.label("h")).where(DimPart.id.in_(ids), hits > 0)
        ).all()
        hit_map = {i: int(h) for i, h in rows}
        ids = list(hit_map)

    # 有搜索时匹配率优先（命中词数 desc），无搜索时按动态库存 desc
    ids.sort(key=lambda i: (-hit_map.get(i, 0), -(smap[i]["dynamic_qty"]), i))
    total = len(ids)
    page = max(page, 1)
    page_ids = ids[(page - 1) * page_size: page * page_size]

    parts = {}
    if page_ids:
        parts = {p.id: p for p in db.execute(
            select(DimPart).where(DimPart.id.in_(page_ids))).scalars()}
    wh: dict[int, list] = {}
    if page_ids:
        for inv in db.execute(select(Inventory).where(Inventory.part_id.in_(page_ids))
                              .order_by(Inventory.warehouse)).scalars():
            wh.setdefault(inv.part_id, []).append({
                "id": inv.id, "warehouse": inv.warehouse, "qty": _d(_display_qty(inv)),
                "source_qty": _d(inv.source_qty), "manual_qty": _d(inv.manual_qty),
                "is_qty_overridden": inv.is_qty_overridden,
                "safety_stock": _d(inv.safety_stock),
                "unit_cost": _d(inv.unit_cost), "inventory_value": _d(inv.inventory_value),
                "snapshot_date": inv.snapshot_date.isoformat() if inv.snapshot_date else None,
            })

    items = []
    for i in page_ids:
        p, s = parts.get(i), smap[i]
        if p is None:
            continue
        items.append({
            "part_id": i, "pn_std": p.pn_std, "description": p.description, "brand": p.brand,
            "dynamic_qty": _d(s["dynamic_qty"]), "anchor_qty": _d(s["anchor_qty"]),
            "anchor_date": s["anchor_date"].isoformat() if s["anchor_date"] else None,
            "in_qty": _d(s["in_qty"]), "out_sales": _d(s["out_sales"]),
            "out_maint": _d(s["out_maint"]),
            "warehouses": wh.get(i, []),
            # 匹配率（仅搜索时）：命中词数 / 总词数，前端显示「命中 X/N词」并可据此排序展示
            "match_hits": hit_map.get(i) if n_terms else None,
            "match_terms": n_terms or None,
        })
    return {"total": total, "page": page, "page_size": page_size, "items": items,
            "match_terms": n_terms or None}

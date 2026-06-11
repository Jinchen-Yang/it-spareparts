"""型号全景查询服务（§9）。

业务查询默认按 ACTIVE_STATUS_ONLY 过滤 data_status='已生效'。
利润概览本期给基础聚合（avg_cost/avg_sale_price/total_qty_sold）；
avg_margin 待 Step 6 成本匹配后才有意义，暂返回 None。
"""
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app import config, security
from app.models.dimensions import DimCustomer, DimPart, DimSupplier
from app.models.inquiry import FPartInquiry
from app.models.inventory import Inventory, PartSubstitute
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.sales import FSalesLine, FSalesOrder

_ACTIVE = "已生效"


def _d(x) -> float | None:
    return float(x) if isinstance(x, Decimal) else x


def search_parts(db: Session, q: str | None, page: int, page_size: int,
                 user_ctx: security.UserContext | None = None) -> dict:
    stmt = select(DimPart)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(DimPart.pn_std.ilike(like), DimPart.description.ilike(like)))
    if user_ctx is not None:
        stmt = security.apply_data_scope(stmt, user_ctx)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.execute(
        stmt.order_by(DimPart.pn_std).offset((page - 1) * page_size).limit(page_size)
    ).scalars().all()
    return {
        "total": total, "page": page, "page_size": page_size,
        "items": [{
            "pn_std": p.pn_std, "description": p.description, "brand": p.brand,
            "category_major": p.category_major, "needs_review": p.needs_review,
        } for p in rows],
    }


def _purchases_query(pn_std: str, user_ctx: security.UserContext | None = None):
    # 显示完整采购历史(含维保等),source_type 让用户辨识"哪些计入成本"
    stmt = (
        select(FPurchaseOrder.order_no, FPurchaseOrder.order_date,
                DimSupplier.name_normalized, FPurchaseLine.qty, FPurchaseLine.unit_price,
                FPurchaseOrder.source_type)
        .join(FPurchaseOrder, FPurchaseLine.order_id == FPurchaseOrder.id)
        .join(DimSupplier, FPurchaseOrder.supplier_id == DimSupplier.id, isouter=True)
        .where(FPurchaseLine.pn_std == pn_std)
    )
    if config.ACTIVE_STATUS_ONLY:
        stmt = stmt.where(FPurchaseOrder.data_status == _ACTIVE)
    if user_ctx is not None:
        stmt = security.apply_data_scope(stmt, user_ctx)
    return stmt.order_by(FPurchaseOrder.order_date.desc().nullslast())


def _sales_query(pn_std: str, user_ctx: security.UserContext | None = None):
    # 带 salesperson 以便 sales 角色行级匿名化（保留行情价、抹掉同事客户名）
    stmt = (
        select(FSalesOrder.order_no, FSalesOrder.order_date,
                DimCustomer.name_normalized, FSalesLine.qty, FSalesLine.unit_price,
                FSalesOrder.salesperson)
        .join(FSalesOrder, FSalesLine.order_id == FSalesOrder.id)
        .join(DimCustomer, FSalesOrder.customer_id == DimCustomer.id, isouter=True)
        .where(FSalesLine.pn_std == pn_std)
    )
    if config.ACTIVE_STATUS_ONLY:
        stmt = stmt.where(FSalesOrder.data_status == _ACTIVE)
    if user_ctx is not None:
        stmt = security.apply_data_scope(stmt, user_ctx)
    return stmt.order_by(FSalesOrder.order_date.desc().nullslast())


def _paginate(db: Session, base_stmt, page: int, page_size: int, mapper) -> dict:
    total = db.scalar(select(func.count()).select_from(base_stmt.subquery()))
    rows = db.execute(base_stmt.offset((page - 1) * page_size).limit(page_size)).all()
    return {"total": total, "page": page, "page_size": page_size,
            "items": [mapper(r) for r in rows]}


def _purchase_row(r):
    return {"order_no": r[0], "order_date": r[1], "supplier": r[2],
            "qty": _d(r[3]), "unit_price": _d(r[4]), "source_type": r[5]}


def _sales_row(r):
    return {"order_no": r[0], "order_date": r[1], "customer": r[2],
            "qty": _d(r[3]), "unit_price": _d(r[4]), "salesperson": r[5]}


def list_purchases(db: Session, pn_std: str, page: int, page_size: int,
                   user_ctx: security.UserContext | None = None) -> dict:
    return _paginate(db, _purchases_query(pn_std, user_ctx), page, page_size, _purchase_row)


def list_sales(db: Session, pn_std: str, page: int, page_size: int,
               user_ctx: security.UserContext | None = None) -> dict:
    out = _paginate(db, _sales_query(pn_std, user_ctx), page, page_size, _sales_row)
    out["items"] = security.anonymize_sales_rows(out["items"], user_ctx)  # 防恶性竞争
    return out


def _profit_summary(db: Session, pn_std: str) -> dict:
    # 加权平均采购成本（active, unit_price>0,按成本口径过滤——维保等不入)
    pc = (
        select(func.sum(FPurchaseLine.qty * FPurchaseLine.unit_price), func.sum(FPurchaseLine.qty))
        .join(FPurchaseOrder, FPurchaseLine.order_id == FPurchaseOrder.id)
        .where(FPurchaseLine.pn_std == pn_std, FPurchaseLine.unit_price > 0,
               FPurchaseOrder.source_type.in_(config.COST_PURCHASE_TYPES))
    )
    if config.ACTIVE_STATUS_ONLY:
        pc = pc.where(FPurchaseOrder.data_status == _ACTIVE)
    amt, qty = db.execute(pc).one()
    avg_cost = (amt / qty) if amt and qty else None

    sc = (
        select(func.sum(FSalesLine.qty * FSalesLine.unit_price), func.sum(FSalesLine.qty))
        .join(FSalesOrder, FSalesLine.order_id == FSalesOrder.id)
        .where(FSalesLine.pn_std == pn_std)
    )
    if config.ACTIVE_STATUS_ONLY:
        sc = sc.where(FSalesOrder.data_status == _ACTIVE)
    samt, sqty = db.execute(sc).one()
    avg_sale = (samt / sqty) if samt and sqty else None

    # 两种成本法的单位成本与毛利率（基于 recompute 落库的逐行成本，数量加权）
    cc = (
        select(
            func.sum(FSalesLine.cost_moving_avg * FSalesLine.qty),
            func.sum(FSalesLine.cost_fifo * FSalesLine.qty),
            func.sum(FSalesLine.qty).filter(FSalesLine.cost_moving_avg.is_not(None)),
            func.sum(FSalesLine.revenue_amount).filter(FSalesLine.cost_moving_avg.is_not(None)),
        )
        .join(FSalesOrder, FSalesLine.order_id == FSalesOrder.id)
        .where(FSalesLine.pn_std == pn_std, FSalesLine.counts_revenue.is_(True))
    )
    if config.ACTIVE_STATUS_ONLY:
        cc = cc.where(FSalesOrder.data_status == _ACTIVE)
    cma, cff, cqty, crev = db.execute(cc).one()
    avg_cost_moving = (cma / cqty) if cma and cqty else None
    avg_cost_fifo = (cff / cqty) if cff and cqty else None

    def _mgn(cost):
        return round(float((crev - cost) / crev), 4) if cost is not None and crev else None

    return {
        "avg_purchase_cost": _d(avg_cost.quantize(Decimal("0.01"))) if avg_cost is not None else None,
        "avg_sale_price": _d(avg_sale.quantize(Decimal("0.01"))) if avg_sale is not None else None,
        "avg_cost_moving": _d(avg_cost_moving.quantize(Decimal("0.01"))) if avg_cost_moving is not None else None,
        "avg_cost_fifo": _d(avg_cost_fifo.quantize(Decimal("0.01"))) if avg_cost_fifo is not None else None,
        "avg_margin_moving": _mgn(cma),
        "avg_margin_fifo": _mgn(cff),
        "total_qty_sold": _d(sqty) if sqty is not None else 0,
    }


def _inventory(db: Session, pn_std: str) -> list[dict]:
    rows = db.execute(select(Inventory).where(Inventory.pn_std == pn_std)).scalars().all()
    out = []
    for inv in rows:
        display = inv.manual_qty if inv.is_qty_overridden and inv.manual_qty is not None else inv.source_qty
        out.append({
            "warehouse": inv.warehouse, "display_qty": _d(display),
            "source_qty": _d(inv.source_qty), "manual_qty": _d(inv.manual_qty),
            "unit_cost": _d(inv.unit_cost), "inventory_value": _d(inv.inventory_value),
        })
    return out


def _substitutes(db: Session, part_id: int | None) -> list[dict]:
    if part_id is None:
        return []
    rows = db.execute(
        select(PartSubstitute).where(
            or_(PartSubstitute.part_id_a == part_id, PartSubstitute.part_id_b == part_id)
        )
    ).scalars().all()
    out = []
    for s in rows:
        other_id = s.part_id_b if s.part_id_a == part_id else s.part_id_a
        other = db.get(DimPart, other_id)
        if other:
            out.append({"pn_std": other.pn_std, "description": other.description, "source": s.source})
    return out


def _sales_velocity(db: Session, pn_std: str) -> dict:
    """近 90 天销售速率（二期采购场景："进 50 个合理吗"需要知道卖多快）。"""
    since = date.today() - timedelta(days=90)
    stmt = (
        select(func.sum(FSalesLine.qty).filter(FSalesOrder.order_date >= since),
               func.max(FSalesOrder.order_date))
        .select_from(FSalesLine)
        .join(FSalesOrder, FSalesLine.order_id == FSalesOrder.id)
        .where(FSalesLine.pn_std == pn_std)
    )
    if config.ACTIVE_STATUS_ONLY:
        stmt = stmt.where(FSalesOrder.data_status == _ACTIVE)
    qty90, last_date = db.execute(stmt).one()
    qty90 = qty90 or Decimal(0)
    return {
        "qty_sold_90d": _d(qty90),
        "monthly_avg_90d": _d((qty90 / 3).quantize(Decimal("0.1"))),
        "last_sale_date": last_date,
    }


def quick_pricing(db: Session, pn_std: str) -> dict:
    """轻量定价摘要（整机拆解/批量询价场景）：近N天采购价窗口 / 近90天均售价 / 库存合计。

    比 get_overview 轻一个量级，供 lookup_prices_bulk 每行调用。
    采购价只取计入成本的采购类型（与成本口径一致，排除维保等）。
    """
    lp = (
        select(FPurchaseOrder.order_date, FPurchaseLine.unit_price, FPurchaseOrder.source_type)
        .join(FPurchaseOrder, FPurchaseLine.order_id == FPurchaseOrder.id)
        .where(FPurchaseLine.pn_std == pn_std,
               FPurchaseLine.unit_price.is_not(None), FPurchaseLine.unit_price > 0,
               FPurchaseOrder.source_type.in_(config.COST_PURCHASE_TYPES))
    )
    if config.ACTIVE_STATUS_ONLY:
        lp = lp.where(FPurchaseOrder.data_status == _ACTIVE)
    last = db.execute(lp.order_by(FPurchaseOrder.order_date.desc().nullslast()).limit(1)).first()

    # 近 N 天采购价窗口（客户要"最近15天采购价"）：均/低/高/笔数
    win_since = date.today() - timedelta(days=config.RECENT_PURCHASE_DAYS)
    rp = (
        select(func.avg(FPurchaseLine.unit_price), func.min(FPurchaseLine.unit_price),
               func.max(FPurchaseLine.unit_price), func.count())
        .join(FPurchaseOrder, FPurchaseLine.order_id == FPurchaseOrder.id)
        .where(FPurchaseLine.pn_std == pn_std,
               FPurchaseLine.unit_price.is_not(None), FPurchaseLine.unit_price > 0,
               FPurchaseOrder.source_type.in_(config.COST_PURCHASE_TYPES),
               FPurchaseOrder.order_date >= win_since)
    )
    if config.ACTIVE_STATUS_ONLY:
        rp = rp.where(FPurchaseOrder.data_status == _ACTIVE)
    r_avg, r_min, r_max, r_cnt = db.execute(rp).one()

    since = date.today() - timedelta(days=90)
    sp = (
        select(func.sum(FSalesLine.qty * FSalesLine.unit_price), func.sum(FSalesLine.qty))
        .select_from(FSalesLine)
        .join(FSalesOrder, FSalesLine.order_id == FSalesOrder.id)
        .where(FSalesLine.pn_std == pn_std, FSalesLine.unit_price > 0,
               FSalesOrder.order_date >= since)
    )
    if config.ACTIVE_STATUS_ONLY:
        sp = sp.where(FSalesOrder.data_status == _ACTIVE)
    samt, sqty = db.execute(sp).one()

    inv_rows = db.execute(select(Inventory).where(Inventory.pn_std == pn_std)).scalars().all()
    stock = sum(
        (inv.manual_qty if inv.is_qty_overridden and inv.manual_qty is not None else inv.source_qty)
        or Decimal(0) for inv in inv_rows
    )
    return {
        "last_purchase_price": _d(last[1]) if last else None,
        "last_purchase_date": last[0] if last else None,
        "last_purchase_type": last[2] if last else None,
        # 近 N 天采购价窗口；窗口内无采购时各值为 None，回退看 last_purchase_*
        "recent_purchase_days": config.RECENT_PURCHASE_DAYS,
        "recent_purchase_avg": _d(r_avg.quantize(Decimal("0.01"))) if r_avg else None,
        "recent_purchase_min": _d(r_min) if r_min else None,
        "recent_purchase_max": _d(r_max) if r_max else None,
        "recent_purchase_count": r_cnt or 0,
        "avg_sale_price_90d": _d((samt / sqty).quantize(Decimal("0.01"))) if samt and sqty else None,
        "stock_total": _d(stock),
    }


def _inquiry_ref(db: Session, pn_std: str) -> dict:
    row = db.execute(
        select(func.min(FPartInquiry.money), func.max(FPartInquiry.money),
                func.count()).where(FPartInquiry.pn_std == pn_std)
    ).one()
    last = db.scalar(
        select(FPartInquiry.money).where(FPartInquiry.pn_std == pn_std)
        .order_by(FPartInquiry.apply_time.desc().nullslast()).limit(1)
    )
    return {"min_money": _d(row[0]), "max_money": _d(row[1]),
            "last_money": _d(last), "count": row[2]}


def get_overview(db: Session, pn_std: str,
                 user_ctx: security.UserContext | None = None) -> dict | None:
    part = db.scalar(select(DimPart).where(DimPart.pn_std == pn_std))
    if part is None:
        return None
    return {
        "part": {
            "pn_std": part.pn_std, "description": part.description, "brand": part.brand,
            "category_major": part.category_major, "category_minor": part.category_minor,
            "unit": part.unit, "needs_review": part.needs_review,
        },
        "purchases_recent": _paginate(db, _purchases_query(pn_std, user_ctx), 1, 20, _purchase_row)["items"],
        # 防恶性竞争：sales 角色保留行情价、抹掉同事的客户名（本人成交保留）
        "sales_recent": security.anonymize_sales_rows(
            _paginate(db, _sales_query(pn_std, user_ctx), 1, 20, _sales_row)["items"], user_ctx),
        "inventory": _inventory(db, pn_std),
        "substitutes": _substitutes(db, part.id),
        "profit_summary": _profit_summary(db, pn_std),
        "inquiry_ref": _inquiry_ref(db, pn_std),
        "sales_velocity": _sales_velocity(db, pn_std),
    }

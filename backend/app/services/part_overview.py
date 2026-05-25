"""型号全景查询服务（§9）。

业务查询默认按 ACTIVE_STATUS_ONLY 过滤 data_status='已生效'。
利润概览本期给基础聚合（avg_cost/avg_sale_price/total_qty_sold）；
avg_margin 待 Step 6 成本匹配后才有意义，暂返回 None。
"""
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
    stmt = (
        select(FPurchaseOrder.order_no, FPurchaseOrder.order_date,
                DimSupplier.name_normalized, FPurchaseLine.qty, FPurchaseLine.unit_price)
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
    stmt = (
        select(FSalesOrder.order_no, FSalesOrder.order_date,
                DimCustomer.name_normalized, FSalesLine.qty, FSalesLine.unit_price)
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
            "qty": _d(r[3]), "unit_price": _d(r[4])}


def _sales_row(r):
    return {"order_no": r[0], "order_date": r[1], "customer": r[2],
            "qty": _d(r[3]), "unit_price": _d(r[4])}


def list_purchases(db: Session, pn_std: str, page: int, page_size: int,
                   user_ctx: security.UserContext | None = None) -> dict:
    return _paginate(db, _purchases_query(pn_std, user_ctx), page, page_size, _purchase_row)


def list_sales(db: Session, pn_std: str, page: int, page_size: int,
               user_ctx: security.UserContext | None = None) -> dict:
    return _paginate(db, _sales_query(pn_std, user_ctx), page, page_size, _sales_row)


def _profit_summary(db: Session, pn_std: str) -> dict:
    # 加权平均采购成本（active, unit_price>0）
    pc = (
        select(func.sum(FPurchaseLine.qty * FPurchaseLine.unit_price), func.sum(FPurchaseLine.qty))
        .join(FPurchaseOrder, FPurchaseLine.order_id == FPurchaseOrder.id)
        .where(FPurchaseLine.pn_std == pn_std, FPurchaseLine.unit_price > 0)
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

    # 平均毛利率：取 recompute 落库的 gross_margin（计营收行）的均值
    mq = select(func.avg(FSalesLine.gross_margin)).join(
        FSalesOrder, FSalesLine.order_id == FSalesOrder.id
    ).where(FSalesLine.pn_std == pn_std, FSalesLine.counts_revenue.is_(True),
            FSalesLine.gross_margin.is_not(None))
    if config.ACTIVE_STATUS_ONLY:
        mq = mq.where(FSalesOrder.data_status == _ACTIVE)
    avg_margin = db.scalar(mq)

    return {
        "avg_cost": _d(avg_cost.quantize(Decimal("0.01"))) if avg_cost is not None else None,
        "avg_sale_price": _d(avg_sale.quantize(Decimal("0.01"))) if avg_sale is not None else None,
        "avg_margin": round(float(avg_margin), 4) if avg_margin is not None else None,
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
        "sales_recent": _paginate(db, _sales_query(pn_std, user_ctx), 1, 20, _sales_row)["items"],
        "inventory": _inventory(db, pn_std),
        "substitutes": _substitutes(db, part.id),
        "profit_summary": _profit_summary(db, pn_std),
        "inquiry_ref": _inquiry_ref(db, pn_std),
    }

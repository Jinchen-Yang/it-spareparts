"""全局采购记录查询（合同重点：销售/采购直接看最近买了什么、什么价）。

与 part_overview 的单型号采购史互补：这里是跨型号的时间线视图。
口径与全站一致：ACTIVE_STATUS_ONLY 过滤已生效；型号展示用 dim_part 的
canonical pn_std（part_id 主口径——合并后的历史行自动显示存活型号）。
"""
from datetime import date, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app import config, security
from app.models import DimPart, DimSupplier, FPurchaseLine, FPurchaseOrder, PartAlias

_ACTIVE = "已生效"
MAX_PAGE_SIZE = 200
MAX_DAYS = 3660  # 上限十年：days 参数防呆


def recent_purchases(db: Session, user_ctx: security.UserContext | None = None,
                     q: str | None = None, days: int = 30,
                     supplier: str | None = None,
                     page: int = 1, page_size: int = 50) -> dict:
    days = max(1, min(int(days or 30), MAX_DAYS))
    page = max(1, page)
    page_size = max(1, min(page_size, MAX_PAGE_SIZE))

    stmt = (
        select(
            FPurchaseLine.id.label("line_id"),
            FPurchaseOrder.order_no,
            FPurchaseOrder.order_date,
            FPurchaseOrder.purchaser,
            FPurchaseOrder.source_type,
            DimSupplier.name_normalized.label("supplier"),
            DimPart.pn_std,                       # canonical（合并后为存活型号）
            DimPart.needs_review,
            FPurchaseLine.description,
            FPurchaseLine.brand,
            FPurchaseLine.qty,
            FPurchaseLine.unit_price,
            FPurchaseLine.line_amount,
        )
        .join(FPurchaseOrder, FPurchaseLine.order_id == FPurchaseOrder.id)
        .join(DimPart, FPurchaseLine.part_id == DimPart.id)
        .join(DimSupplier, FPurchaseOrder.supplier_id == DimSupplier.id, isouter=True)
        .where(FPurchaseOrder.order_date >= date.today() - timedelta(days=days))
    )
    if config.ACTIVE_STATUS_ONLY:
        stmt = stmt.where(FPurchaseOrder.data_status == _ACTIVE)
    if q and q.strip():
        like = f"%{q.strip()}%"
        # 别名召回：用户拿单据上的原文 PN（含已合并的旧 PN、被去 V 码的原始
        # 写法）来查也要命中——part_alias.part_id 合并时已重指存活商品，
        # 经它过滤仍是 part_id 主口径（绝不直接 ILIKE 事实表 pn 文本作键）
        alias_hit = (
            select(PartAlias.part_id)
            .where(PartAlias.pn_raw.ilike(like), PartAlias.part_id.is_not(None))
        )
        stmt = stmt.where(or_(
            DimPart.pn_std.ilike(like),
            FPurchaseLine.description.ilike(like),
            FPurchaseLine.brand.ilike(like),
            FPurchaseLine.part_id.in_(alias_hit),
        ))
    if supplier and supplier.strip():
        stmt = stmt.where(DimSupplier.name_normalized.ilike(f"%{supplier.strip()}%"))
    if user_ctx is not None:
        stmt = security.apply_data_scope(stmt, user_ctx)

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.execute(
        stmt.order_by(FPurchaseOrder.order_date.desc().nullslast(),
                      FPurchaseLine.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).mappings().all()

    return {
        "total": total, "page": page, "page_size": page_size, "days": days,
        "items": [dict(r) for r in rows],
    }

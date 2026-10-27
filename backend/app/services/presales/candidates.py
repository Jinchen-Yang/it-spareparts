"""售前表单候选；在限制结果条数前应用动作、字段和行级权限。"""

from sqlalchemy import false, func, or_, select

from app.models.dimensions import DimCustomer, DimSupplier
from app.models.presales import PresalesInquiry
from app.models.sales import FSalesOrder
from app.security import is_field_hidden, is_scoped_sales
from app.services.presales import common
from app.services.workflow import work_items
from app.services.workflow.registry import Forbidden


def _query(model, q):
    name = func.coalesce(func.nullif(model.name_normalized, ""), model.name_raw).label("name")
    query = select(model.id, name)
    if q.strip():
        pattern = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        query = query.where(or_(model.name_raw.ilike(pattern, escape="\\"),
                                model.name_normalized.ilike(pattern, escape="\\")))
    return query.order_by(name, model.id)


def customers(db, ctx, q="", *, limit=20):
    common.require(ctx, "page_presales")
    common.require(ctx, "action_presales_inquire")
    if is_field_hidden(ctx, "customer_id"):
        raise Forbidden("无客户信息权限")
    query = _query(DimCustomer, q)
    if is_scoped_sales(ctx):
        user, _ = common.resolve_actor(db, ctx)
        # 销售分析没有客户归属表；保守复用销售名历史，以及本人询价用过的客户。
        historical = false()
        if ctx.salesperson_name:
            historical = select(FSalesOrder.id).where(
                FSalesOrder.customer_id == DimCustomer.id,
                FSalesOrder.salesperson == ctx.salesperson_name,
            ).exists()
        own_inquiry = select(PresalesInquiry.id).where(
            PresalesInquiry.customer_id == DimCustomer.id,
            PresalesInquiry.created_by == user.id,
        ).exists()
        query = query.where(or_(historical, own_inquiry))
    return [dict(row._mapping) for row in db.execute(query.limit(limit))]


def suppliers(db, ctx, q="", *, limit=20):
    common.require(ctx, "page_presales")
    if not any(work_items.has_permission(ctx, key) for key in
               ("action_presales_quote", "action_presales_offer")):
        raise Forbidden("无此操作权限")
    if is_field_hidden(ctx, "supplier_id"):
        raise Forbidden("无供应商信息权限")
    return [dict(row._mapping) for row in db.execute(_query(DimSupplier, q).limit(limit))]

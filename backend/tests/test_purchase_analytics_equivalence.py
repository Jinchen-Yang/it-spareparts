"""采购分析查询重构的等价性测试 + EXPLAIN 计划守护（性能修复 2026-10-17）。

修复动机：生产只读 EXPLAIN（reviews/client-req-20261016/explain-prod-pr3ab.md）显示
关联销售单全表 ``DISTINCT ON`` 去重（外排 5.2MB ≈ 0.9–1.0s）把 /purchases/analytics/
orders 全年区间拖到 ≈2.7s（金臣验收线：一整年区间 >2s 即缺陷）。修法：
- orders：排序键全在采购单侧 → count/分页先不连 f_sales_order，页内再按 order_no
  DISTINCT ON 补关联销售员/客户（与旧去重规则同一条：id 最小）；销售人员/客户
  筛选或按销售/客户下钻必须在分页前过滤 → 去重输入被当前窗口采购单的 order_no
  半连接限定（不用全表 DISTINCT ON，也不用 LATERAL 逐单探测——复审实测后者在
  po×明细行连接后按行数放大，客户下钻在本机基准（=生产规模）退化 2.2 倍）。
- summary：销售/客户不参与时该外连接是死重（列不被引用、去重后 1:1），直接不连。
- options 销售人员：DISTINCT ON 的输入用「已生效采购单引用的 order_no」半连接限定。
- order_detail：按 order_no 直取 id 最小一条。

**接口响应结构（字段名、类型、嵌套、分页/排序语义、回显字段）一字不改**是本修复的
硬约束（下游 PR-4a 前端已按现有结构开发）。证明方式：把旧实现（a636dbf）的五个查询
函数原样保留为下方 ``_ref_*`` 参考函数（只存在于测试），在同一批夹具上与新版逐场景
对拍完整 JSON 输出相等；另用 EXPLAIN（不 ANALYZE，先例 test_maintenance_project_
foundation）守护计划形态防回退。

客户候选参照独立按规格读表并在 Python 去重、匹配与分页，不复用实现的新构件。
其余参考函数与新版共用模块内未改动的表达式/条件构件（_order_amount_exprs、_base_where、
_group_dim 等），差异只在查询装配——这正是要证明等价的部分。
"""
import copy
import re
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import String, and_, case, func, or_, select, text
from sqlalchemy import event
from sqlalchemy.orm import Session

from app import config, permissions, security, tax_policy
from app.business_time import business_today
from app.db import engine
from app.models import (DimCustomer, DimPart, FPurchaseLine, FPurchaseOrder,
                        FSalesOrder)
from app.security import UserContext
from app.services import purchase_analytics
from app.services.purchase_analytics import (NO_LINKED_LABEL, UNCATEGORIZED_LABEL,
                                             EMPTY_LABEL, _base_where,
                                             _batch_line_join, _category_dim,
                                             _category_match, _effective_rate, _f,
                                             _filters_echo, _group_dim,
                                             _is_int_str, _line_amount_exprs,
                                             _nominal_side_expr, _norm_categories,
                                             _order_amount_exprs, _round2,
                                             _validate_common, line_amounts_reference,
                                             order_amounts_reference,
                                             order_nominal_side_reference)
from tests.test_purchase_analytics_api import _batch, _customer, _line, _part, _po, _sales

D = Decimal

# =======================================================================
# 参考实现：a636dbf 的旧查询装配（逐字拷贝，仅函数名加 _ref_ 前缀、内部
# _sales_link 改调 _ref_sales_link）。不要按新思路"改进"这里的任何一行——
# 它是等价性对拍的基准；仅 options 客户候选按后续规格独立补齐。
# =======================================================================


def _ref_sales_link():
    """关联销售单去重子查询：``f_sales_order.order_no`` 无唯一约束（当前 0 重复），
    DISTINCT ON 按 order_no 取 id 最小的一条（确定性），防一对多扩行。关联是身份
    查找，不过滤销售单状态。"""
    return (
        select(
            FSalesOrder.order_no.label("order_no"),
            FSalesOrder.salesperson.label("salesperson"),
            FSalesOrder.customer_id.label("customer_id"),
        )
        .distinct(FSalesOrder.order_no)
        .order_by(FSalesOrder.order_no, FSalesOrder.id)
    ).subquery()


def _ref_options(db: Session, user_ctx=None, *, customer_q="",
                 customer_page=1, customer_page_size=200,
                 selected_customer_ids=None) -> dict:
    """筛选取值（全部来自已生效数据）：类型原值 / 采购员 / 关联销售人员 / 分类（含未分类）。"""
    po = FPurchaseOrder
    active = po.data_status == config.ACTIVE_STATUS
    source_types = [r for r in db.execute(
        select(func.distinct(po.source_type_raw)).where(
            active, po.source_type_raw.is_not(None), po.source_type_raw != ""
        ).order_by(po.source_type_raw)).scalars()]
    purchasers = [r for r in db.execute(
        select(func.distinct(po.purchaser)).where(
            active, po.purchaser.is_not(None), po.purchaser != ""
        ).order_by(po.purchaser)).scalars()]
    link = _ref_sales_link()
    salespersons = [r for r in db.execute(
        select(func.distinct(link.c.salesperson))
        .select_from(po).outerjoin(link, po.linked_sales_order_no == link.c.order_no)
        .where(active, link.c.salesperson.is_not(None), link.c.salesperson != "")
        .order_by(link.c.salesperson)).scalars()]
    pl = FPurchaseLine
    dp = DimPart
    cat_rows = db.execute(
        select(func.distinct(_category_dim(dp)))
        .select_from(po)
        .join(pl, _batch_line_join(po, pl))
        .join(dp, pl.part_id == dp.id)
        .where(active)
    ).scalars().all()
    categories = sorted(c for c in cat_rows if c != UNCATEGORIZED_LABEL)
    if UNCATEGORIZED_LABEL in cat_rows:
        categories.append(UNCATEGORIZED_LABEL)
    # 客户参照按规格独立计算：简单读表，Python 取最小销售行、去重和分页；名称排序沿用数据库 collation。
    customers, selected_customers, customers_has_more = [], [], False
    if user_ctx is None or not security.is_field_hidden(user_ctx, "customer_id"):
        referenced = set(db.scalars(select(po.linked_sales_order_no).where(active)))
        first_sales = {}
        for sale in db.scalars(select(FSalesOrder).order_by(FSalesOrder.id)):
            first_sales.setdefault(sale.order_no, sale.customer_id)
        ids = {cid for no, cid in first_sales.items() if no in referenced}
        candidates = [(c.name_raw, c.id) for c in db.scalars(
            select(DimCustomer).order_by(DimCustomer.name_raw, DimCustomer.id))
            if c.id in ids]
        selected_customers = [
            {"customer_id": cid, "customer": name} for name, cid in candidates
            if cid in (selected_customer_ids or [])]
        matching = [(name, cid) for name, cid in candidates
                    if customer_q.strip().lower() in name.lower()]
        start = (customer_page - 1) * customer_page_size
        customers_has_more = len(matching) > start + customer_page_size
        customers = [{"customer_id": cid, "customer": name}
                     for name, cid in matching[start:start + customer_page_size]]
    data = {
        "customers": customers,
        "customers_has_more": customers_has_more,
        "selected_customers": selected_customers,
        "source_types": source_types,
        "purchasers": purchasers,
        "salespersons": salespersons,
        "categories": categories,
        "uncategorized_label": UNCATEGORIZED_LABEL,
        "no_linked_label": NO_LINKED_LABEL,
    }
    if user_ctx is not None:
        data = security.apply_data_scope(data, user_ctx)
    return data


def _share(nominal, total) -> float | None:
    """名义占比参照（与服务端 _share 同语义）：分母 0/NULL → None，绝不把空当 0。"""
    if not total or nominal is None:
        return None
    return round(float(Decimal(nominal) / Decimal(total)), 4)


def _ref_summary(db: Session, user_ctx=None, *, date_from: date | None = None,
                 date_to: date | None = None, source_type: list[str] | None = None,
                 purchaser: list[str] | None = None, salesperson: list[str] | None = None,
                 customer_id: list[int] | None = None, category: list[str] | None = None,
                 group_by: str = "source_type") -> dict:
    """KPI + 分组汇总。分类筛选（或按分类分组）→ 行级口径（拆行只计匹配行）；
    其余 → 单头口径（实付取单头「采购金额」原值）。三套金额一次全部返回。

    行级口径下主查询逐行展开，行数直接 ``count(pl.id)``；单头口径下若把行 LEFT JOIN
    进主查询，单头金额会被行数放大，因此行数始终用独立语句按同一筛选/分组统计。
    """
    _validate_common(date_from, date_to)
    if group_by not in purchase_analytics.GROUP_BY_KEYS:
        raise purchase_analytics.FilterValidationError(f"不支持的分组维度：{group_by}")
    po, pl, dp = FPurchaseOrder, FPurchaseLine, DimPart
    link = _ref_sales_link()
    category = _norm_categories(category)
    line_mode = bool(category) or group_by == "category"
    cat = _category_match(category)
    where = _base_where(po, link, date_from=date_from, date_to=date_to,
                        source_type=source_type, purchaser=purchaser,
                        salesperson=salesperson, customer_id=customer_id)
    if line_mode and cat is not None:
        where.append(cat)

    if line_mode:
        ex_e, inc_e = _line_amount_exprs(po, pl)
        paid_e = pl.line_amount
        side = _nominal_side_expr(po)
        measures = [
            func.count(func.distinct(po.id)).label("orders"),
            func.count(pl.id).label("lines"),
            func.sum(inc_e).label("amount_inc"),
            func.sum(ex_e).label("amount_ex"),
            func.sum(paid_e).label("amount_paid"),
            func.sum(case((side == "inc", inc_e), else_=0)).label("nominal_inc"),
            func.sum(case((side == "ex", ex_e), else_=0)).label("nominal_ex"),
        ]
    else:
        ex_e, inc_e, paid_e, side = _order_amount_exprs(po)
        measures = [
            func.count(func.distinct(po.id)).label("orders"),
            func.sum(inc_e).label("amount_inc"),
            func.sum(ex_e).label("amount_ex"),
            func.sum(paid_e).label("amount_paid"),
            func.sum(case((side == "inc", inc_e), else_=0)).label("nominal_inc"),
            func.sum(case((side == "ex", ex_e), else_=0)).label("nominal_ex"),
        ]

    def _apply_from(stmt):
        """统一 FROM 链：行级口径逐行展开（单头批次行 + 分类维），否则纯单头。"""
        if line_mode:
            stmt = (stmt.select_from(po)
                    .join(pl, _batch_line_join(po, pl))
                    .join(dp, pl.part_id == dp.id))
        else:
            stmt = stmt.select_from(po)
        return stmt.outerjoin(link, po.linked_sales_order_no == link.c.order_no)

    def _lines_stmt(dim=None):
        """行数独立统计（仅单头口径调用；此时无分类筛选，无需 join dim_part）。"""
        stmt = select(
            *([dim.label("key")] if dim is not None else []),
            func.count(pl.id).label("lines"),
        ).select_from(po).join(pl, _batch_line_join(po, pl)) \
            .outerjoin(link, po.linked_sales_order_no == link.c.order_no).where(*where)
        if dim is not None:
            stmt = stmt.group_by(dim)
        return stmt

    kpi_row = db.execute(_apply_from(select(*measures)).where(*where)).one()
    kpi_lines = kpi_row.lines if line_mode else (db.execute(_lines_stmt()).scalar() or 0)

    dim = _group_dim(group_by, po, link, dp)
    groups_ranking_restricted = security.is_field_hidden(user_ctx, "amount_paid")
    if groups_ranking_restricted:
        grp_order = (func.count(func.distinct(po.id)).desc(), dim.asc().nullslast())
    else:
        grp_order = (func.sum(paid_e).desc().nullslast(), dim)
    grp_rows = db.execute(
        _apply_from(select(dim.label("key"), *measures)).where(*where).group_by(dim)
        .order_by(*grp_order)
    ).all()
    lines_by_key: dict[str, int] = {}
    if not line_mode:
        lines_by_key = {r.key: r.lines for r in db.execute(_lines_stmt(dim)).all()}

    customer_names = {}
    if group_by == "customer":
        ids = [int(r.key) for r in grp_rows
               if r.key not in (NO_LINKED_LABEL, EMPTY_LABEL) and _is_int_str(r.key)]
        if ids:
            customer_names = dict(db.execute(
                select(DimCustomer.id, DimCustomer.name_normalized)
                .where(DimCustomer.id.in_(ids))).all())

    groups = []
    for r in grp_rows:
        item = {
            "key": r.key,
            "label": r.key,
            "orders": r.orders,
            "lines": r.lines if line_mode else lines_by_key.get(r.key, 0),
            "amount_inc": _f(_round2(r.amount_inc)),
            "amount_ex": _f(_round2(r.amount_ex)),
            "amount_paid": _f(_round2(r.amount_paid)),
            # 名义占比与 KPI 同分子分母口径（金臣 2026-10-18 名义侧裁定）
            "nominal_inc_share": _share(r.nominal_inc, r.amount_inc),
            "nominal_ex_share": _share(r.nominal_ex, r.amount_ex),
        }
        if group_by == "customer":
            # 客户名只放登记过的脱敏键（customer），label 不承载客户名
            item["label"] = NO_LINKED_LABEL if r.key == NO_LINKED_LABEL else None
            item["customer"] = customer_names.get(int(r.key)) if _is_int_str(r.key) else None
        groups.append(item)

    return {
        "basis_mode": "line" if line_mode else "order",
        "filters_echo": _filters_echo(
            date_from=date_from, date_to=date_to, source_type=source_type,
            purchaser=purchaser, salesperson=salesperson, customer_id=customer_id,
            category=category, group_by=group_by),
        "as_of": business_today().isoformat(),
        "kpi": {
            "orders": kpi_row.orders,
            "lines": kpi_lines,
            "amount_inc": _f(_round2(kpi_row.amount_inc)),
            "amount_ex": _f(_round2(kpi_row.amount_ex)),
            "amount_paid": _f(_round2(kpi_row.amount_paid)),
            "nominal_inc_share": _share(kpi_row.nominal_inc, kpi_row.amount_inc),
            "nominal_ex_share": _share(kpi_row.nominal_ex, kpi_row.amount_ex),
        },
        "groups": groups,
        "groups_ranking_restricted": groups_ranking_restricted,
    }


def _ref_orders(db: Session, user_ctx=None, *, date_from: date | None = None,
                date_to: date | None = None, source_type: list[str] | None = None,
                purchaser: list[str] | None = None, salesperson: list[str] | None = None,
                customer_id: list[int] | None = None, category: list[str] | None = None,
                group_by: str | None = None, group_key: str | None = None,
                sort: str = "order_date", order: str = "desc",
                page: int = 1, page_size: int = 50) -> dict:
    """分组下钻订单列表（分页 + 排序，同额/同日按 id 倒序稳定翻页）。

    group_by + group_key：把上一层分组值折成对应维度精确过滤（哨兵值
    「（无关联销售单）」「(空)」「未分类」与 month/day 文本键同一真值）；
    category 分组折成分类筛选（保持行级口径与上层分组一致）。
    """
    _validate_common(date_from, date_to)
    if sort not in purchase_analytics.SORT_KEYS:
        raise purchase_analytics.FilterValidationError(f"不支持的排序键：{sort}")
    if order not in ("asc", "desc"):
        raise purchase_analytics.FilterValidationError(f"不支持的排序方向：{order}")
    if group_key is not None and not group_by:
        raise purchase_analytics.FilterValidationError("group_key 需与 group_by 同传")
    if group_by is not None and group_by not in purchase_analytics.GROUP_BY_KEYS:
        raise purchase_analytics.FilterValidationError(f"不支持的分组维度：{group_by}")
    page = max(1, page)
    page_size = max(1, min(page_size, purchase_analytics.MAX_PAGE_SIZE))
    requested_sort = sort
    ranking_restricted = sort != "order_date" and security.is_field_hidden(
        user_ctx, "amount_paid")
    if ranking_restricted:
        sort = "order_date"

    po, pl, dp = FPurchaseOrder, FPurchaseLine, DimPart
    link = _ref_sales_link()
    category = _norm_categories(category)
    line_mode = bool(category) or group_by == "category"
    cat = _category_match(category)
    where = _base_where(po, link, date_from=date_from, date_to=date_to,
                        source_type=source_type, purchaser=purchaser,
                        salesperson=salesperson, customer_id=customer_id)
    if cat is not None:
        where.append(cat)
    if group_key and group_by:
        where.append(_group_dim(group_by, po, link, dp) == group_key)

    po_cols = (po.id, po.order_no, po.order_date, po.source_type_raw, po.purchaser,
               link.c.salesperson, link.c.customer_id,
               DimCustomer.name_normalized.label("customer_name"))
    if line_mode:
        ex_e, inc_e = _line_amount_exprs(po, pl)
        paid_e = pl.line_amount
        side = _nominal_side_expr(po)
        base = (
            select(*po_cols, side.label("nominal_side"), func.count(pl.id).label("lines"),
                   func.sum(inc_e).label("amount_inc"), func.sum(ex_e).label("amount_ex"),
                   func.sum(paid_e).label("amount_paid"))
            .select_from(po)
            .join(pl, _batch_line_join(po, pl))
            .join(dp, pl.part_id == dp.id)
            .outerjoin(link, po.linked_sales_order_no == link.c.order_no)
            .outerjoin(DimCustomer, link.c.customer_id == DimCustomer.id)
            .where(*where)
            .group_by(po.id, po.order_no, po.order_date, po.source_type_raw, po.purchaser,
                      link.c.salesperson, link.c.customer_id, DimCustomer.name_normalized)
        )
    else:
        ex_e, inc_e, paid_e, side = _order_amount_exprs(po)
        base = (
            select(*po_cols, side.label("nominal_side"), func.count(pl.id).label("lines"),
                   inc_e.label("amount_inc"), ex_e.label("amount_ex"), paid_e.label("amount_paid"))
            .select_from(po)
            .outerjoin(pl, _batch_line_join(po, pl))
            .outerjoin(link, po.linked_sales_order_no == link.c.order_no)
            .outerjoin(DimCustomer, link.c.customer_id == DimCustomer.id)
            .where(*where)
            .group_by(po.id, po.order_no, po.order_date, po.source_type_raw, po.purchaser,
                      link.c.salesperson, link.c.customer_id, DimCustomer.name_normalized)
        )

    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    if line_mode:
        sort_expr = {
            "order_date": po.order_date,
            "amount_inc": func.sum(inc_e),
            "amount_ex": func.sum(ex_e),
            "amount_paid": func.sum(paid_e),
        }[sort]
    else:
        sort_expr = {
            "order_date": po.order_date,
            "amount_inc": inc_e,
            "amount_ex": ex_e,
            "amount_paid": paid_e,
        }[sort]
    direction = sort_expr.desc().nullslast() if order == "desc" else sort_expr.asc().nullslast()
    stmt = base.order_by(direction, po.id.desc()).limit(page_size).offset((page - 1) * page_size)
    items = [{
        "order_id": r.id, "order_no": r.order_no,
        "order_date": r.order_date.isoformat() if r.order_date else None,
        "source_type": r.source_type_raw, "purchaser": r.purchaser,
        "salesperson": r.salesperson, "customer_id": r.customer_id,
        "customer": r.customer_name, "lines": r.lines,
        "nominal_side": r.nominal_side,
        "amount_inc": _f(_round2(r.amount_inc)),
        "amount_ex": _f(_round2(r.amount_ex)),
        "amount_paid": _f(_round2(r.amount_paid)),
    } for r in db.execute(stmt)]
    return {
        "total": total, "page": page, "page_size": page_size,
        "sort": requested_sort, "order": order,
        "effective_sort": sort, "ranking_restricted": ranking_restricted,
        "basis_mode": "line" if line_mode else "order",
        "filters_echo": _filters_echo(
            date_from=date_from, date_to=date_to, source_type=source_type,
            purchaser=purchaser, salesperson=salesperson, customer_id=customer_id,
            category=category, group_by=group_by, group_key=group_key),
        "as_of": business_today().isoformat(),
        "items": items,
    }


def _ref_order_detail(db: Session, order_id: int, user_ctx=None, *,
                      category: list[str] | None = None) -> dict | None:
    """采购模块自己的订单明细（不依赖 page_boss_board）：单头三口径 + **单头批次行**。

    残留旧批次行不出现在这里（D-39 补充决策）；订单不存在或不是已生效 → None。
    category 传入时展示行按分类过滤，并附 ``category_matched`` 行级汇总（行实付 =
    匹配行 line_amount 之和，与列表/分组同一行级口径）。
    """
    po = db.get(FPurchaseOrder, order_id)
    if po is None or po.data_status != config.ACTIVE_STATUS:
        return None
    category = _norm_categories(category)
    ex, inc = order_amounts_reference(
        po.amount_ex_tax, po.amount_inc_tax, po.tax_rate, po.is_tax_inclusive)

    link_row = None
    # 2026-10-18 编码检察官 #398：与列表同一真值，只滤 NULL（linked='' 命中 order_no=''）
    if po.linked_sales_order_no is not None:
        link = _ref_sales_link()
        link_row = db.execute(
            select(link.c.salesperson, link.c.customer_id, DimCustomer.name_normalized)
            .select_from(link.outerjoin(DimCustomer, link.c.customer_id == DimCustomer.id))
            .where(link.c.order_no == po.linked_sales_order_no)
        ).first()

    pl, dp = FPurchaseLine, DimPart
    cat = _category_match(category)
    stmt = (
        select(pl.id, pl.line_no, pl.part_id, dp.pn_std, pl.description, pl.brand,
               pl.qty, pl.unit_price, pl.line_amount, _category_dim(dp).label("category"))
        .select_from(pl)
        .join(dp, pl.part_id == dp.id)
        .where(pl.order_id == po.id, pl.import_batch_id == po.import_batch_id)
    )
    if cat is not None:
        stmt = stmt.where(cat)
    stmt = stmt.order_by(pl.line_no.asc().nullslast(), pl.id)
    lines = []
    matched_paid = matched_inc = matched_ex = Decimal(0)
    for r in db.execute(stmt):
        l_ex, l_inc = line_amounts_reference(r.line_amount, po.tax_rate, po.is_tax_inclusive)
        if r.line_amount is not None:
            matched_paid += r.line_amount
            if l_inc is not None:
                matched_inc += l_inc
            if l_ex is not None:
                matched_ex += l_ex
        lines.append({
            "line_id": r.id, "line_no": r.line_no, "part_id": r.part_id,
            "pn_std": r.pn_std, "description": r.description, "brand": r.brand,
            "qty": _f(r.qty), "unit_price": _f(r.unit_price),
            "line_amount": _f(r.line_amount),
            "category": r.category,
            "line_amount_inc": _f(l_inc),
            "line_amount_ex": _f(l_ex),
        })
    data = {
        "order_id": po.id, "order_no": po.order_no,
        "order_date": po.order_date.isoformat() if po.order_date else None,
        "source_type": po.source_type_raw, "purchaser": po.purchaser,
        "data_status": po.data_status,
        "is_tax_inclusive": po.is_tax_inclusive,
        "tax_rate": _f(po.tax_rate),
        "tax_rate_effective": _f(_effective_rate(po.tax_rate)),
        "nominal_side": order_nominal_side_reference(
            po.tax_rate, po.is_tax_inclusive, po.amount_ex_tax, po.amount_inc_tax),
        "amount_ex": _f(ex), "amount_inc": _f(inc),
        "amount_paid": _f(po.amount_inc_tax),
        "linked_sales_order_no": po.linked_sales_order_no,
        "salesperson": link_row.salesperson if link_row else None,
        "customer_id": link_row.customer_id if link_row else None,
        "customer": link_row.name_normalized if link_row else None,
        "lines_count": len(lines),
        "lines": lines,
    }
    if cat is not None:
        data["category_matched"] = {
            "lines": len(lines),
            "amount_inc": _f(_round2(matched_inc)),
            "amount_ex": _f(_round2(matched_ex)),
            "amount_paid": _f(_round2(matched_paid)),
        }
    return data


# =======================================================================
# 夹具：在 11 单残留形态之上补齐等价性对拍所需形态
# =======================================================================


@pytest.fixture()
def eq(db):
    """对拍数据集（返回命名空间）。已生效采购单共 12 张（2026-03～05），覆盖：

    - 残留形态：poA（同行号新旧批次 + 旧批次多行号 + **3 批次混合**：ob2 行也不
      计入）、poC（同行号残留）、poD（单头批次无任何行，计 0 行）、poE（已取消）
    - 关联销售单去重：XS-002 两张同销售员不同客户（客户取 id 小）；XS-003 第二张
      销售员不同（销售员/筛选取值必须取 id 小的「销售小王」，不得取「销售小赵」）；
      XS-005 min-id 行销售员为 NULL、第二张有值（options 取值必须排除「销售小钱」、
      分组落「(空)」）；XS-006 已取消销售单（关联不过滤销售单状态）
    - 悬空关联：poI 的 linked 指向不存在的 XS-404 → 「（无关联销售单）」
    - 空串关联：s_blank/s_blank_dup 同为 order_no=''（min-id 去重取「销售空号」）+
      poK.linked='' —— 等值关联 ''='' 命中，页内补值（IN 列表）与分页前关联/
      旧外连接必须同真值（守护 is not None 过滤 + 空串键去重方向）
    - 分类形态：' 电器件 '（btrim 同真值）、''（未分类哨兵）、None（未分类）
    - 名义侧（2026-10-18 裁定）：poL 含税单未标税率 → nominal_side="ex"
      （nominal_ex 分子非零）；poB/poJ 未标税率不含税单 → "inc"；poF 税率 0 与
      NULL 同义 → "inc"；其余已标税率 → None
    - 历史缺口（2026-10-18 对抗复审）：poM 不含税单缺不含税额、只录入含税额 →
      单头口径实际推算的是不含税侧 → nominal_side="ex"（缺口翻转形态）；
      行级口径仍按标记结构 → "inc"
    """
    nb, ob1, ob2 = _batch(db, "new"), _batch(db, "old1"), _batch(db, "old2")
    p1 = _part(db, "PN-DQ", "电器件")
    p2 = _part(db, "PN-FL", None)
    p3 = _part(db, "PN-JX", "机械件")
    p4 = _part(db, "PN-PAD", " 电器件 ")     # 首尾空格：分组/筛选同一 btrim 真值
    p5 = _part(db, "PN-EMP", "")             # 空串 → 未分类哨兵
    pres = _part(db, "PN-RES", "残留类")
    c1 = _customer(db, "华东电力")
    c2 = _customer(db, "华南钢铁")
    c3 = _customer(db, "华南钢铁二期")
    c4 = _customer(db, "北方机械")
    s1 = _sales(db, "XS-001-A", "XS-001", "销售小王", c1, nb)
    s2 = _sales(db, "XS-002-A", "XS-002", "销售小李", c2, nb)
    s2_dup = _sales(db, "XS-002-B", "XS-002", "销售小李", c3, nb)   # 同销售员不同客户
    s3 = _sales(db, "XS-003-A", "XS-003", "销售小王", c1, nb)
    s3_dup = _sales(db, "XS-003-B", "XS-003", "销售小赵", c2, nb)   # 同单号不同销售员
    s5 = _sales(db, "XS-005-A", "XS-005", None, c1, nb)             # min-id 行销售员为空
    s5_dup = _sales(db, "XS-005-B", "XS-005", "销售小钱", c2, nb)   # 第二张才有销售员
    s6 = FSalesOrder(raw_order_id="XS-006-A", order_no="XS-006", salesperson="销售小孙",
                     customer_id=c4.id, data_status="已取消", import_batch_id=nb.id)
    db.add(s6)
    # order_no=''：等值关联 ''='' 可命中（ETL clean_str 不会产出，但库无 CHECK 约束，
    # 手工修复/旁路写入可达）——页内补值与分页前关联/旧外连接必须给出同一答案。
    # 第二张 '' 销售单钉死空串键上的 min-id 去重方向（取「销售空号」而非 B）
    s_blank = _sales(db, "XS-BLANK-A", "", "销售空号", c1, nb)
    s_blank_dup = _sales(db, "XS-BLANK-B", "", "销售空号B", c2, nb)
    db.flush()
    assert s2_dup.id > s2.id and s3_dup.id > s3.id and s5_dup.id > s5.id
    assert s_blank_dup.id > s_blank.id

    poa = _po(db, "PA", nb, on=date(2026, 3, 5), source_type="销售订单", purchaser="采购员甲",
              linked="XS-001", is_inc=True, rate=D("0.13"), amount_ex=D("1000"),
              amount_inc=D("1130"))
    _line(db, "PA-L1", poa, p1, nb, line_no=1, amount=D("500"))
    _line(db, "PA-L2", poa, p2, nb, line_no=2, amount=D("630"))
    _line(db, "PA-OLD1-L1", poa, p1, ob1, line_no=1, amount=D("999"))   # 残留：同行号
    _line(db, "PA-OLD1-L3", poa, pres, ob1, line_no=3, amount=D("500"))  # 残留：多行号
    _line(db, "PA-OLD2-L7", poa, p3, ob2, line_no=7, amount=D("777"))   # 3 批次混合

    pob = _po(db, "PB", nb, on=date(2026, 3, 20), source_type="指定采购", purchaser=None,
              is_inc=False, rate=None, amount_ex=D("2000"), amount_inc=None)
    _line(db, "PB-L1", pob, p3, nb, line_no=1, amount=D("800"))
    _line(db, "PB-L2", pob, p1, nb, line_no=2, amount=D("1200"))

    poc = _po(db, "PC", nb, on=date(2026, 4, 1), source_type="销售订单", purchaser="采购员乙",
              linked="XS-002", is_inc=True, rate=D("0.06"), amount_ex=None,
              amount_inc=D("1060"))
    _line(db, "PC-L1", poc, p1, nb, line_no=1, amount=D("1060"))
    _line(db, "PC-OLD1-L1", poc, p1, ob1, line_no=1, amount=D("400"))   # 残留：同行号

    pod = _po(db, "PD", nb, on=date(2026, 4, 10), source_type="维保需求", purchaser="采购员乙",
              is_inc=None, rate=None, amount_ex=D("500"), amount_inc=D("500"))
    _line(db, "PD-OLD1-L1", pod, p1, ob1, line_no=1, amount=D("500"))   # 只有旧批次行

    poe = _po(db, "PE", nb, on=date(2026, 3, 1), source_type="销售订单", purchaser="采购员甲",
              is_inc=True, rate=D("0.13"), amount_ex=D("100"), amount_inc=D("113"),
              status="已取消")
    _line(db, "PE-L1", poe, p1, nb, line_no=1, amount=D("113"))

    pof = _po(db, "PF", nb, on=date(2026, 5, 15), source_type="销售订单", purchaser="采购员甲",
              linked="XS-001", is_inc=False, rate=D("0"), amount_ex=D("1000"),
              amount_inc=D("1130"))
    _line(db, "PF-L1", pof, p2, nb, line_no=1, amount=D("1000"))

    pog = _po(db, "PG", nb, on=date(2026, 5, 20), source_type="销售订单", purchaser="采购员丙",
              linked="XS-003", is_inc=False, rate=D("0.006"), amount_ex=D("1000"),
              amount_inc=D("1006"))
    _line(db, "PG-L1", pog, p3, nb, line_no=1, amount=D("1000"))

    poh = _po(db, "PH", nb, on=date(2026, 4, 20), source_type="批量采购", purchaser="采购员甲",
              linked="XS-005", is_inc=True, rate=D("0.13"), amount_ex=D("2000"),
              amount_inc=D("2260"))
    _line(db, "PH-L1", poh, p4, nb, line_no=1, amount=D("300"))
    _line(db, "PH-L2", poh, p5, nb, line_no=2, amount=D("500"))

    poi = _po(db, "PI", nb, on=date(2026, 5, 2), source_type="销售订单", purchaser="采购员乙",
              linked="XS-404", is_inc=False, rate=D("0.13"), amount_ex=D("400"),
              amount_inc=D("452"))
    _line(db, "PI-L1", poi, p1, nb, line_no=1, amount=D("452"))

    poj = _po(db, "PJ", nb, on=date(2026, 3, 25), source_type="销售订单", purchaser="采购员丙",
              linked="XS-006", is_inc=False, rate=None, amount_ex=D("600"),
              amount_inc=D("678"))
    _line(db, "PJ-L1", poj, p1, nb, line_no=1, amount=D("678"))

    pok = _po(db, "PK", nb, on=date(2026, 4, 25), source_type="批量采购", purchaser="采购员乙",
              linked="", is_inc=True, rate=D("0.13"), amount_ex=D("700"),
              amount_inc=D("791"))
    _line(db, "PK-L1", pok, p3, nb, line_no=1, amount=D("791"))
    # 名义侧裁定（2026-10-18）：含税单未标税率 → 名义侧 = 不含税（"ex"），
    # nominal_ex 分子因此非零（此前唯一缺口：夹具没有 is_inc=True 且未标税率的单）
    pol = _po(db, "PL", nb, on=date(2026, 5, 25), source_type="批量采购", purchaser="采购员甲",
              is_inc=True, rate=None, amount_ex=None, amount_inc=D("565"))
    _line(db, "PL-L1", pol, p3, nb, line_no=1, amount=D("565"))
    pom = _po(db, "PM", nb, on=date(2026, 5, 28), source_type="批量采购", purchaser="采购员乙",
              is_inc=False, rate=None, amount_ex=None, amount_inc=D("452"))
    _line(db, "PM-L1", pom, p3, nb, line_no=1, amount=D("452"))

    db.commit()

    class _EQ:
        pass

    d = _EQ()
    d.db, d.nb, d.ob1, d.ob2 = db, nb, ob1, ob2
    d.c1, d.c2, d.c3, d.c4 = c1, c2, c3, c4
    d.poa, d.pob, d.poc, d.pod, d.poe = poa, pob, poc, pod, poe
    d.pof, d.pog, d.poh, d.poi, d.poj, d.pok, d.pol, d.pom = (
        pof, pog, poh, poi, poj, pok, pol, pom)
    d.active_pos = [poa, pob, poc, pod, pof, pog, poh, poi, poj, pok, pol, pom]
    d.all_pos = d.active_pos + [poe]
    return d


def _masked_ctx(username="eq-masked") -> UserContext:
    """data_purchase_cost=False 的 boss 上下文（触发 orders/summary 的结构性排序保护）。"""
    perms = permissions.effective("boss", None)
    perms["data_purchase_cost"] = False
    return UserContext(user_id=username, role="boss", permissions=perms)


def _assert_orders_eq(db, ctx, label, **params):
    ref = _ref_orders(db, copy.deepcopy(ctx), **params)
    new = purchase_analytics.orders(db, copy.deepcopy(ctx), **params)
    assert new == ref, f"{label}\nparams={params!r}\n旧={ref!r}\n新={new!r}"


def _assert_summary_eq(db, ctx, label, **params):
    ref = _ref_summary(db, copy.deepcopy(ctx), **params)
    new = purchase_analytics.summary(db, copy.deepcopy(ctx), **params)
    assert new == ref, f"{label}\nparams={params!r}\n旧={ref!r}\n新={new!r}"


# =======================================================================
# 对拍：orders
# =======================================================================

Y = date(2026, 1, 1)
Y_END = date(2026, 12, 31)


def test_orders_equivalence_sorts(eq):
    """排序每个键 asc/desc；同额（poA/poF 实付 1130）按 id 倒序稳定翻页。"""
    for sort in ("order_date", "amount_inc", "amount_ex", "amount_paid"):
        for order in ("asc", "desc"):
            for page_size in (3, 9, 200):
                _assert_orders_eq(eq.db, None, f"sort={sort}/{order}/ps={page_size}",
                                  date_from=Y, date_to=Y_END, sort=sort, order=order,
                                  page_size=page_size)


def test_orders_equivalence_pagination_boundaries(eq):
    """分页边界：第一页/最后一页不满页/恰好整页/超出范围空页/page_size=1。"""
    for page_size in (1, 3, 4, 9, 200):
        for page in (1, 2, 3, 4, 10):
            _assert_orders_eq(eq.db, None, f"page={page}/ps={page_size}",
                              date_from=Y, date_to=Y_END, page=page, page_size=page_size)
    # 翻第 2/3 页的页内补值路径 + asc 排序
    for page in (2, 3):
        _assert_orders_eq(eq.db, None, f"multi-page/page={page}",
                          date_from=Y, date_to=Y_END, page=page, page_size=4,
                          sort="amount_paid", order="asc")


def test_orders_equivalence_filters(eq):
    """分类筛选与销售人员/客户筛选（含只在 dup 行出现的销售员 → 空结果）。"""
    d = eq
    cases = [
        {"source_type": ["销售订单", "指定采购"]},
        {"purchaser": ["采购员甲"]},
        {"purchaser": ["采购员乙", "采购员丙"]},
        {"salesperson": ["销售小王"]},
        {"salesperson": ["销售小赵"]},                 # 只在 XS-003 的 dup 行 → 必须空
        {"salesperson": ["销售空号B"]},                # 只在空串键 '' 的 dup 行 → 必须空（min-id 去重方向）
        {"salesperson": ["销售小王", "销售小李"]},
        # NULL 字面量入筛选列表：SQL x IN (NULL) 恒非真 → 两侧恒空集，只守护
        # 不炸 + 回显一致（并非「筛出 min-id 行销售员为空的单」，那是 EMPTY_LABEL 下钻）
        {"salesperson": [None]},
        {"customer_id": [d.c1.id]},
        {"customer_id": [d.c2.id]},                    # XS-002 去重取 min-id 行客户
        {"customer_id": [d.c3.id]},                    # 只在 dup 行 → 必须空
        {"customer_id": [d.c4.id]},                    # 已取消销售单的关联客户
        {"customer_id": [999999]},
        {"category": ["电器件"]},
        {"category": [" 电器件 "]},                     # 归一后同值
        {"category": [UNCATEGORIZED_LABEL]},
        {"category": ["电器件", UNCATEGORIZED_LABEL, ""]},
        {"category": ["机械件"]},
        {"date_from": date(2026, 3, 15), "date_to": date(2026, 4, 30)},
        {"date_from": date(2026, 6, 1), "date_to": date(2026, 6, 30)},   # 空窗口
        {"source_type": ["销售订单"], "salesperson": ["销售小王"], "category": ["电器件"]},
        {"customer_id": [d.c1.id], "category": [UNCATEGORIZED_LABEL]},
    ]
    for kw in cases:
        _assert_orders_eq(d.db, None, "filter",
                          **{"date_from": Y, "date_to": Y_END, "page_size": 200, **kw})


def test_orders_equivalence_drilldowns(eq):
    """每个分组维度的下钻（含哨兵值）与行级口径下钻排序。"""
    d = eq
    cases = [
        {"group_by": "source_type", "group_key": "销售订单"},
        {"group_by": "source_type", "group_key": "批量采购"},
        {"group_by": "purchaser", "group_key": EMPTY_LABEL},
        {"group_by": "salesperson", "group_key": "销售小王"},
        {"group_by": "salesperson", "group_key": "销售小李"},
        {"group_by": "salesperson", "group_key": EMPTY_LABEL},          # min-id 行销售员为空
        {"group_by": "salesperson", "group_key": NO_LINKED_LABEL},
        {"group_by": "customer", "group_key": str(d.c1.id)},
        {"group_by": "customer", "group_key": str(d.c2.id)},
        {"group_by": "customer", "group_key": NO_LINKED_LABEL},
        {"group_by": "month", "group_key": "2026-03"},
        {"group_by": "month", "group_key": "2026-04"},
        {"group_by": "day", "group_key": "2026-05-15"},
        {"group_by": "category", "group_key": "电器件"},
        {"group_by": "category", "group_key": UNCATEGORIZED_LABEL},
        # 行级口径下钻 + 金额排序（聚合列排序）
        {"group_by": "category", "group_key": "电器件", "sort": "amount_paid"},
        {"group_by": "category", "group_key": "机械件", "sort": "amount_inc", "order": "asc"},
        # 下钻与既有分类筛选取交集
        {"group_by": "category", "group_key": "电器件", "category": ["电器件", "机械件"]},
        # 无 group_key 的分组入参（不折过滤，但 category 维仍翻行级口径）
        {"group_by": "salesperson"},
        {"group_by": "category"},
        {"group_by": "customer"},
        # 下钻 × 筛选交叉（needs_link 双条件、行级口径 + 金额排序）
        {"group_by": "salesperson", "group_key": "销售小王", "salesperson": ["销售小王"],
         "category": ["电器件"], "sort": "amount_paid"},
        {"group_by": "salesperson", "group_key": "销售小王", "customer_id": [d.c1.id]},
        {"group_by": "salesperson", "group_key": EMPTY_LABEL, "page_size": 3},
        {"group_by": "customer", "group_key": str(d.c1.id), "salesperson": [None]},
    ]
    for kw in cases:
        _assert_orders_eq(d.db, None, "drilldown",
                          **{"date_from": Y, "date_to": Y_END, "page_size": 200, **kw})


def test_orders_equivalence_linked_deep_pages(eq):
    """分页前关联路径（销售/客户筛选或下钻 → needs_link=True）深翻页：LIMIT/OFFSET
    真正生效的组合。此前 needs_link=True 场景全部 page=1 且 page_size ≥ 筛后全集，
    对该分支的分页装订零判别力（复审确认缺口）。夹具中销售小王 3 单、客户 c1
    名下 4 单（含 poK 空串关联）。"""
    d = eq
    cases = [
        {"salesperson": ["销售小王"], "page_size": 2, "page": 2},
        {"salesperson": ["销售小王"], "page_size": 2, "page": 3},    # 超出 → 空页
        {"group_by": "customer", "group_key": str(d.c1.id), "page_size": 1, "page": 3},
        {"group_by": "customer", "group_key": str(d.c1.id), "page_size": 2, "page": 3},
        {"group_by": "salesperson", "group_key": "销售小王", "page_size": 1, "page": 2},
        # 行级口径下钻 + 关联筛选 + 深页 + 金额排序（非空末页）
        {"customer_id": [d.c1.id], "group_by": "category", "group_key": "电器件",
         "page_size": 1, "page": 2, "sort": "amount_paid"},
        # 空串关联（poK）在分页前关联路径同样取到 s_blank 的销售员/客户
        {"salesperson": ["销售空号"], "page_size": 1, "page": 1},
    ]
    for kw in cases:
        _assert_orders_eq(d.db, None, "linked-deep-page",
                          **{"date_from": Y, "date_to": Y_END, **kw})


def test_orders_equivalence_masked_ranking(eq):
    """金额被遮蔽时的排序回退（ranking_restricted/effective_sort 回显一致）。"""
    ctx = _masked_ctx()
    for kw in (
        {"sort": "amount_paid"},
        {"sort": "amount_inc", "order": "asc"},
        {"group_by": "salesperson", "group_key": "销售小王", "sort": "amount_paid"},
        {"group_by": "category", "group_key": "电器件", "sort": "amount_ex"},
        {"group_by": "customer", "group_key": str(eq.c2.id), "customer_id": [eq.c2.id],
         "sort": "amount_inc"},
    ):
        _assert_orders_eq(eq.db, ctx, "masked", date_from=Y, date_to=Y_END, **kw)
    # 无遮蔽账号对照组（同参数仍按金额排）
    _assert_orders_eq(eq.db, None, "unmasked", date_from=Y, date_to=Y_END,
                      sort="amount_paid")


# =======================================================================
# 对拍：summary / options / order_detail
# =======================================================================

def test_summary_equivalence(eq):
    d = eq
    for group_by in purchase_analytics.GROUP_BY_KEYS:
        _assert_summary_eq(d.db, None, f"group_by={group_by}",
                           date_from=Y, date_to=Y_END, group_by=group_by)
    cases = [
        {"group_by": "salesperson", "salesperson": ["销售小王"]},
        {"group_by": "salesperson", "salesperson": ["销售小赵"]},
        {"group_by": "customer", "customer_id": [d.c1.id]},
        {"group_by": "customer", "customer_id": [d.c3.id]},
        {"group_by": "category", "category": ["电器件"]},
        {"group_by": "category", "category": [UNCATEGORIZED_LABEL]},
        {"group_by": "month", "category": ["机械件"], "salesperson": ["销售小王"]},
        {"group_by": "source_type", "date_from": date(2026, 4, 1), "date_to": date(2026, 4, 30)},
        {"group_by": "source_type", "salesperson": [None]},
        # line_mode × 分组维走 group_link（KPI/行数语句不带关联）的交叉组合
        {"group_by": "salesperson", "category": ["电器件"]},
        {"group_by": "customer", "category": ["机械件"], "customer_id": [d.c1.id]},
        {"group_by": "customer", "category": [UNCATEGORIZED_LABEL],
         "salesperson": ["销售小王"], "source_type": ["销售订单"]},
    ]
    for kw in cases:
        _assert_summary_eq(d.db, None, "summary",
                           **{"date_from": Y, "date_to": Y_END, **kw})


def test_summary_equivalence_masked(eq):
    ctx = _masked_ctx()
    for group_by in ("month", "salesperson", "customer", "category"):
        _assert_summary_eq(eq.db, ctx, "masked", date_from=Y, date_to=Y_END,
                           group_by=group_by)


def test_options_equivalence(eq):
    assert purchase_analytics.options(eq.db) == _ref_options(eq.db)
    # 金额遮蔽上下文（options 不含金额列，理论无差异；守护结构性排序保护的传递）
    assert purchase_analytics.options(eq.db, _masked_ctx()) == _ref_options(eq.db, _masked_ctx())


def test_order_detail_equivalence(eq):
    """每张单（含已取消/不存在）× 分类过滤变体；category_matched 嵌套块一致。"""
    for po in eq.all_pos + [type("X", (), {"id": 99999999})()]:
        for cat in (None, ["电器件"], [UNCATEGORIZED_LABEL], ["机械件", "电器件"], [""]):
            ref = _ref_order_detail(eq.db, po.id, None, category=cat)
            new = purchase_analytics.order_detail(eq.db, po.id, None, category=cat)
            assert new == ref, (po.id, cat, ref, new)



def test_order_detail_blank_linked_matches_list(eq):
    """linked_sales_order_no='' 的单：明细与列表给出同一关联销售员/客户（min-id「销售空号」）。"""
    listed = purchase_analytics.orders(eq.db, None, page_size=200)
    row = next(i for i in listed["items"] if i["order_id"] == eq.pok.id)
    detail = purchase_analytics.order_detail(eq.db, eq.pok.id, None)
    assert row["salesperson"] == "销售空号"
    assert (detail["salesperson"], detail["customer_id"], detail["customer"]) == (
        row["salesperson"], row["customer_id"], row["customer"])

# =======================================================================
# EXPLAIN 计划守护（不 ANALYZE；先例 test_maintenance_project_foundation）
# =======================================================================

# 半连接锚定断言：旧形态的 ``LEFT OUTER JOIN (SELECT`` 自带 "IN (SELECT" 子串，
# 纯子串断言对旧形态误绿（复审实证）——锚定「IN (SELECT 后同一子查询内紧跟
# f_purchase_order.」才是有界形态的编译期特征。
_BOUNDED_DEDUP_RE = re.compile(r"IN \(SELECT\s+f_purchase_order\.")


def _capture_selects(fn):
    captured = []

    def _hook(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith(("SELECT", "WITH")):
            captured.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", _hook)
    try:
        fn()
    finally:
        event.remove(engine, "before_cursor_execute", _hook)
    return captured


def _explain(db, statement, parameters) -> str:
    return "\n".join(db.connection().exec_driver_sql(
        f"EXPLAIN (FORMAT TEXT) {statement}", parameters).scalars())


def _dedup_on_order_no_subtree(plan: str) -> list[str]:
    """取 DISTINCT ON (f_sales_order.order_no) 的 Unique 节点子树。

    外层 ``SELECT DISTINCT salesperson`` 也会产生 Unique 节点，且其子树包含
    整棵内层树——按「第一个 Unique」取子树会把外层当内层，断言随之误绿
    （复审实证）。识别标志：内层去重的排序键是
    ``Sort Key: f_sales_order.order_no, f_sales_order.id``；取**最后一个**
    匹配的 Unique（最内层），其子树才是去重的真实输入。
    """
    lines = plan.splitlines()
    found: list[str] = []
    for i, line in enumerate(lines):
        if "Unique" not in line:
            continue
        indent = len(line) - len(line.lstrip())
        sub = []
        for deeper in lines[i + 1:]:
            if len(deeper) - len(deeper.lstrip()) > indent:
                sub.append(deeper)
            else:
                break
        if any("Sort Key: f_sales_order.order_no" in l for l in sub):
            found = sub
    return found


def _analyze_facts(db):
    db.execute(text("ANALYZE f_purchase_order"))
    db.execute(text("ANALYZE f_purchase_line"))
    db.execute(text("ANALYZE f_sales_order"))
    db.execute(text("ANALYZE dim_part"))
    db.execute(text("ANALYZE dim_customer"))


def test_orders_plan_no_sales_filter_never_touches_sales_table(db, eq):
    """性能守护（核心）：无销售/客户筛选时 count 与分页语句完全不碰 f_sales_order
    ——关联值由页内 order_no 限定的 DISTINCT ON 补齐。防回退到全表 DISTINCT ON。"""
    _analyze_facts(db)
    captured = _capture_selects(lambda: purchase_analytics.orders(
        db, None, date_from=Y, date_to=Y_END, sort="amount_paid", page_size=3))
    assert captured
    count_stmts = [c for c in captured if "count(*)" in c[0]]
    page_stmts = [c for c in captured if "LIMIT" in c[0]]
    assert len(count_stmts) == 1 and len(page_stmts) == 1
    for statement, parameters in count_stmts + page_stmts:
        plan = _explain(db, statement, parameters)
        assert "f_sales_order" not in statement, statement
        assert "f_sales_order" not in plan, (statement, plan)
    # 页内补值语句允许碰 f_sales_order，但必须被页内 order_no IN 限定
    enrich = [c for c in captured if "DISTINCT ON (f_sales_order.order_no)" in c[0]]
    assert len(enrich) == 1
    assert " IN (" in enrich[0][0], enrich[0][0]


def test_orders_plan_salesperson_filter_dedup_input_bounded(db, eq):
    """性能守护：销售员筛选需在分页前关联（去重子查询参与过滤），但 DISTINCT ON
    的输入必须被当前窗口采购单的 order_no 半连接限定——不是对 f_sales_order
    全表排序去重（旧形态），也不是 LATERAL 逐单探测（复审实测客户下钻退化）。"""
    _analyze_facts(db)
    captured = _capture_selects(lambda: purchase_analytics.orders(
        db, None, date_from=Y, date_to=Y_END, salesperson=["销售小王"], sort="amount_paid"))
    mains = [c for c in captured if "count(*)" in c[0] or "LIMIT" in c[0]]
    assert len(mains) == 2
    for statement, parameters in mains:
        assert _BOUNDED_DEDUP_RE.search(statement), statement   # 编译期结构：半连接限定
        plan = _explain(db, statement, parameters)
        assert "f_sales_order" in plan, (statement, plan)
        sub = _dedup_on_order_no_subtree(plan)
        assert sub, plan
        # 去重输入的子树里必须出现 f_purchase_order（旧全表形态是裸表扫描排序）
        assert any("f_purchase_order" in l for l in sub), (statement, plan)


def test_orders_plan_customer_drilldown_dedup_input_bounded(db, eq):
    """性能守护：按客户分组下钻同样必须有界去重（IN 半连接限定输入）。"""
    _analyze_facts(db)
    captured = _capture_selects(lambda: purchase_analytics.orders(
        db, None, date_from=Y, date_to=Y_END, group_by="customer",
        group_key=str(eq.c1.id)))
    mains = [c for c in captured if "count(*)" in c[0] or "LIMIT" in c[0]]
    assert len(mains) == 2
    for statement, parameters in mains:
        assert _BOUNDED_DEDUP_RE.search(statement), statement
        plan = _explain(db, statement, parameters)
        assert "f_sales_order" in plan, (statement, plan)
        sub = _dedup_on_order_no_subtree(plan)
        assert sub, plan
        assert any("f_purchase_order" in l for l in sub), (statement, plan)


def test_options_plan_salespersons_input_bounded_by_purchase_orders(db, eq):
    """性能守护：options 销售人员的 DISTINCT ON 输入被「已生效采购单 order_no」
    半连接限定——内层去重 Unique（排序键 f_sales_order.order_no, id）的子树里
    必须出现与 f_purchase_order 的 Join（旧形态是对 f_sales_order 裸全表
    [Seq] Scan + Sort 去重，去重输入不含任何 Join）。语句文本断言
    ``_BOUNDED_DEDUP_RE`` 兜底（编译期结构，不随计划形状漂移）。"""
    # 此守护现在也覆盖客户候选共享的有界去重。
    _analyze_facts(db)
    captured = _capture_selects(lambda: purchase_analytics.options(db))
    stmts = [c for c in captured if "DISTINCT ON (f_sales_order.order_no)" in c[0]]
    assert len(stmts) == 1
    statement, parameters = stmts[0]
    assert _BOUNDED_DEDUP_RE.search(statement), statement
    plan = _explain(db, statement, parameters)
    sub = _dedup_on_order_no_subtree(plan)
    assert sub, plan
    # 去重输入必须先与采购单侧连接（半连接的任意计划形态；注意 PG 的
    # Nested Loop 节点文本不含 "Join" 子串，三种连接节点都要认）
    assert any("Join" in l or "Nested Loop" in l or "hashed SubPlan" in l
               for l in sub), plan
    assert any("f_purchase_order" in l for l in sub), plan


def test_summary_plan_skips_sales_table_unless_link_participates(db, eq):
    """性能守护：summary 无销售/客户参与时全部语句不碰 f_sales_order；销售员
    分组时参与语句的有界去重必须被采购单半连接限定（非全表 DISTINCT ON）。"""
    _analyze_facts(db)
    plain = _capture_selects(lambda: purchase_analytics.summary(
        db, None, date_from=Y, date_to=Y_END, group_by="source_type"))
    assert plain
    for statement, parameters in plain:
        assert "f_sales_order" not in statement, statement
        assert "f_sales_order" not in _explain(db, statement, parameters), statement
    linked = _capture_selects(lambda: purchase_analytics.summary(
        db, None, date_from=Y, date_to=Y_END, group_by="salesperson"))
    for statement, parameters in linked:
        if "f_sales_order" in statement:
            assert _BOUNDED_DEDUP_RE.search(statement), statement
            plan = _explain(db, statement, parameters)
            sub = _dedup_on_order_no_subtree(plan)
            assert sub, (statement, plan)
            assert any("f_purchase_order" in l for l in sub), (statement, plan)


def test_options_statement_count_constant_across_customer_paging(db, eq):
    counts = set()
    for size in (1, 50, 200):
        for page in (1, 3):
            for query in ("", "华"):
                for selected in (None, [eq.c1.id], list(range(1, 201))):
                    captured = _capture_selects(lambda: purchase_analytics.options(
                        db, customer_page_size=size, customer_page=page,
                        customer_q=query, selected_customer_ids=selected))
                    counts.add(len(captured))
                    dedup = [sql for sql, _ in captured
                             if "DISTINCT ON (f_sales_order.order_no)" in sql]
                    assert len(dedup) == 1
                    assert dedup[0].count("DISTINCT ON (f_sales_order.order_no)") == 1
    assert len(counts) == 1, counts


def test_options_customer_equivalence(db, eq):
    batch = _batch(db, "customer-options")
    names = ["Alpha%_\\One", "alpha plain", "literal%", "literal_", "literal\\", "ALPHA other", "alpha fourth"]
    added = []
    for i, name in enumerate(names):
        customer = _customer(db, name)
        added.append(customer.id)
        no = f"OPTIONS-{i}"
        _sales(db, no, no, "候选销售", customer, batch)
        _po(db, no, batch, on=Y, source_type="销售订单", linked=no)
    # 同 order_no 后续行的客户不得进入候选。
    excluded = _customer(db, "excluded duplicate-order customer")
    _sales(db, "OPTIONS-DUP", "OPTIONS-0", "后续销售", excluded, batch)
    db.flush()
    cases = [{}, *[{"customer_q": q} for q in ("%", "_", "\\", "aLpHa")],
             {"customer_q": "aLpHa", "customer_page_size": 4},  # 恰好 size
             {"customer_q": "aLpHa", "customer_page_size": 3},  # size + 1
             {"customer_q": "aLpHa", "customer_page_size": 3, "customer_page": 2},
             {"customer_page_size": 1, "customer_page": 2},
             {"customer_q": "no match", "selected_customer_ids": [added[0], eq.c1.id, 999999]}]
    hidden = _masked_ctx()
    hidden.permissions["data_customer"] = False
    assert security.is_field_hidden(hidden, "customer_id")
    for ctx in (None, hidden):
        for params in cases:
            actual = purchase_analytics.options(db, ctx, **params)
            assert actual == _ref_options(db, ctx, **params), params
            if ctx is not None:
                assert actual["customers"] == actual["selected_customers"] == []
                assert actual["customers_has_more"] is False
    result = purchase_analytics.options(db, customer_q="aLpHa", customer_page_size=3)
    assert len(result["customers"]) == 3 and result["customers_has_more"] is True
    assert purchase_analytics.options(db, customer_q="aLpHa", customer_page_size=4)["customers_has_more"] is False
    captured = _capture_selects(lambda: purchase_analytics.options(
        db, hidden, customer_q="alpha", selected_customer_ids=added))
    assert all("dim_customer" not in sql and "customer_id" not in sql for sql, _ in captured)

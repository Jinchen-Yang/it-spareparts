"""销售分析页聚合服务（D-41 §1，PR-3b）。

只读分析，挂在销售单模块下的独立「销售分析」页（仅 admin/boss，页面键
``page_sales_analysis``）：看所选区间有多少销售单、多少钱，并按金额倒序追踪大单。
不改动老板看板 ``/dashboard/sales`` 与 OrdersBlock（决策 2026-10-17 Q3）。

口径（单一真值源，本模块不另起炉灶）：
- 订单范围 = 已生效（``query_filters.active_orders``，与老板看板一致）。日期为闭区间
  ``[date_from, date_to]``，不传 = 全部；**不做**看板 KPI 的"未来日期排除"——
  区间由用户显式选择，"全部"就是全部（见 orders/summary 的 as_of 仅作展示）。
- 三金额并存（D-41 §2）：不含税 ``amount_ex_tax`` / 含税订单金额 ``amount_inc_tax``
  （可空：没有值返回 null，绝不当 0、不用推算值冒充）/ 营收 ``revenue`` = 订单内
  计营收行（``f_sales_line.counts_revenue``，由导入/利润链路按
  ``config.REVENUE_BUSINESS_TYPES`` 落库）的 ``revenue_amount`` 之和——与老板看板
  ``/dashboard/sales`` 同口径，本模块只消费不重算。
- 对账（D-40 §4-§5）：每单挂最新快照（``sales_reconciliation.latest_snapshots_by_
  raw_order_id``：source_modified_at DESC NULLS LAST → import_batch_id DESC → id DESC）；
  ``include_returns`` 只影响 ``reconciled_display``（含 = 已对账 + 退货已对账，
  默认不含 = 只看已对账），不写死扣减。无快照 → has_data=False、金额全 null。
- 脱敏：对账字段键名（reconciled_amount / pending_amount / return_reconciled_amount /
  order_amount_at_snapshot / reconciled_as_of / reconciled_display）逐字登记在
  ``config.FIELD_GROUPS["sales_reconciliation"]``（data_reconciliation 组），API 层经
  ``apply_field_visibility`` 统一置空；``amount_ex_tax``/``amount_inc_tax``/``customer``
  沿用既有 purchase_cost / customer_info 组的保守遮蔽，营收口径键保持公开。
"""
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import tax_policy
from app.models.dimensions import DimCustomer
from app.models.sales import FSalesLine, FSalesOrder, FSalesReconciliationSnapshot
from app.services.query_filters import active_orders
from app.services.sales_reconciliation import latest_snapshots_by_raw_order_id

# 大单列表排序键（D-41 §2：三金额任一可倒序）。固定 DESC NULLS LAST + id DESC 破并列。
ORDER_SORTS = ("amount_ex_tax", "amount_inc_tax", "revenue")


def _f(x) -> float | None:
    return float(tax_policy.round_money(x)) if x is not None else None


def _clean_multi(values: list[str] | None) -> list[str]:
    """多选筛选值清洗：去空白、去空串、保序去重（重复值对 IN 无意义，静默收敛）。"""
    return list(dict.fromkeys(v.strip() for v in (values or []) if v and v.strip()))


def _order_query(
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    business_types: list[str] | None = None,
    customer: str | None = None,
    salesperson: str | None = None,
):
    """销售分析订单基查询：一张销售订单一行（LEFT JOIN 明细聚营收，无明细行也保留）。

    返回 (stmt, sort_columns)：sort_columns 供 orders() 按 D-41 三金额键排序；
    summary() 把 stmt 包成子查询做合计，保证 KPI = 列表合计（同一口径同一 SQL）。
    """
    so, sl = FSalesOrder, FSalesLine
    revenue = (
        func.sum(sl.revenue_amount).filter(sl.counts_revenue.is_(True)).label("revenue")
    )
    stmt = (
        select(
            so.id, so.order_no, so.order_date, so.salesperson, so.business_type,
            so.data_status, so.raw_order_id, so.amount_ex_tax, so.amount_inc_tax,
            DimCustomer.name_normalized.label("customer"),
            revenue,
        )
        .join(sl, sl.order_id == so.id, isouter=True)
        .join(DimCustomer, so.customer_id == DimCustomer.id, isouter=True)
    )
    stmt = active_orders(stmt, so)
    if date_from is not None:
        stmt = stmt.where(so.order_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(so.order_date <= date_to)
    types = _clean_multi(business_types)
    if types:
        stmt = stmt.where(so.business_type.in_(types))
    if customer and customer.strip():
        stmt = stmt.where(DimCustomer.name_normalized.ilike(f"%{customer.strip()}%"))
    if salesperson and salesperson.strip():
        stmt = stmt.where(so.salesperson.ilike(f"%{salesperson.strip()}%"))
    stmt = stmt.group_by(so.id, DimCustomer.name_normalized)
    return stmt, {
        "amount_ex_tax": so.amount_ex_tax,
        "amount_inc_tax": so.amount_inc_tax,
        "revenue": revenue,
    }


def business_type_options(db: Session) -> dict:
    """业务类型筛选候选取值与计数（D-41 §4：原列「业务类型#」的 14 个原值，不映射）。

    只数已生效单；计数为全时间口径（与生产分布对账用），NULL 归入 value=None 一组
    不丢弃。排序：计数降序、同计数按值升序——确定序，分页/快照测试可复现。
    """
    so = FSalesOrder
    stmt = active_orders(select(so.business_type, func.count().label("orders")), so)
    stmt = stmt.group_by(so.business_type).order_by(
        func.count().desc(), so.business_type.asc().nulls_last()
    )
    return {
        "business_types": [
            {"value": r.business_type, "orders": r.orders} for r in db.execute(stmt)
        ]
    }


def summary(
    db: Session,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    business_types: list[str] | None = None,
    customer: str | None = None,
    salesperson: str | None = None,
    as_of: date | None = None,
) -> dict:
    """所选区间 KPI：订单数 + 三金额合计（D-41 验收：金额与列表/SQL 一致）。

    金额合计跳过 NULL 并单独报告 amount_inc_tax_missing（缺失单数）——NULL 不当 0、
    也不用推算值冒充（D-41 §3）。无订单时合计为 None 而非 0（与 total=None 语义一致，
    区分"查无数据"与"合计为零"）。
    """
    today = as_of or date.today()
    base, _ = _order_query(
        date_from=date_from, date_to=date_to, business_types=business_types,
        customer=customer, salesperson=salesperson,
    )
    sub = base.subquery()
    agg = db.execute(
        select(
            func.count().label("orders"),
            func.sum(sub.c.amount_ex_tax).label("amount_ex_tax_total"),
            func.sum(sub.c.amount_inc_tax).label("amount_inc_tax_total"),
            func.count()
            .filter(sub.c.amount_inc_tax.is_(None))
            .label("amount_inc_tax_missing"),
            func.sum(sub.c.revenue).label("revenue_total"),
        ).select_from(sub)
    ).one()
    return {
        "window": {
            "date_from": date_from.isoformat() if date_from else None,
            "date_to": date_to.isoformat() if date_to else None,
        },
        "filters_echo": {
            "business_type": _clean_multi(business_types),
            "customer": customer,
            "salesperson": salesperson,
        },
        "as_of": today.isoformat(),
        "kpi": {
            "orders": agg.orders,
            "amount_ex_tax_total": _f(agg.amount_ex_tax_total),
            "amount_inc_tax_total": _f(agg.amount_inc_tax_total),
            "amount_inc_tax_missing": agg.amount_inc_tax_missing,
            "revenue_total": _f(agg.revenue_total),
        },
    }


def _reconciliation_block(
    snap: FSalesReconciliationSnapshot | None, *, include_returns: bool
) -> dict:
    """单条最新快照 → 展示块（D-40 §4-§5）。

    reconciled_display：不含退货 = 已对账；含 = 已对账 + 退货已对账（不写死扣减）。
    参与求和的分量按"缺失即不存在"处理（任一有值就给该侧合计，全缺失 → None），
    绝不把空单元格补成 0。
    """
    if snap is None:
        return {
            "has_data": False,
            "reconciled_as_of": None,
            "order_amount_at_snapshot": None,
            "reconciled_amount": None,
            "pending_amount": None,
            "return_reconciled_amount": None,
            "reconciled_display": None,
        }
    parts = [snap.reconciled_amount]
    if include_returns:
        parts.append(snap.return_reconciled_amount)
    present = [p for p in parts if p is not None]
    return {
        "has_data": True,
        "reconciled_as_of": (
            snap.source_modified_at.isoformat() if snap.source_modified_at else None
        ),
        "order_amount_at_snapshot": _f(snap.order_amount),
        "reconciled_amount": _f(snap.reconciled_amount),
        "pending_amount": _f(snap.pending_amount),
        "return_reconciled_amount": _f(snap.return_reconciled_amount),
        "reconciled_display": _f(sum(present)) if present else None,
    }


def orders(
    db: Session,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    business_types: list[str] | None = None,
    customer: str | None = None,
    salesperson: str | None = None,
    sort: str = "amount_ex_tax",
    page: int = 1,
    page_size: int = 50,
    include_returns: bool = False,
    as_of: date | None = None,
) -> dict:
    """大单列表：按所选金额键倒序 + 分页（D-41 §2「可按任一已显示金额倒序排列」）。

    排序固定 ``<key> DESC NULLS LAST, id DESC``（PG DESC 默认 NULLS FIRST，必须显式
    NULLS LAST 才能让缺失值沉底）；并列金额按 id 倒序破并列，分页翻页不丢行/重行。
    """
    today = as_of or date.today()
    if sort not in ORDER_SORTS:
        sort = "amount_ex_tax"
    base, sort_cols = _order_query(
        date_from=date_from, date_to=date_to, business_types=business_types,
        customer=customer, salesperson=salesperson,
    )
    total = db.execute(select(func.count()).select_from(base.subquery())).scalar() or 0
    key = sort_cols[sort].desc().nulls_last()
    stmt = (
        base.order_by(key, FSalesOrder.id.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    )
    rows = db.execute(stmt).all()
    snaps = latest_snapshots_by_raw_order_id(db, [r.raw_order_id for r in rows])
    items = []
    for r in rows:
        items.append({
            "order_id": r.id,
            "order_no": r.order_no,
            "order_date": r.order_date.isoformat() if r.order_date else None,
            "is_future": bool(r.order_date and r.order_date > today),
            "salesperson": r.salesperson,
            "customer": r.customer,
            "business_type": r.business_type,
            "data_status": r.data_status,
            "amount_ex_tax": _f(r.amount_ex_tax),
            "amount_inc_tax": _f(r.amount_inc_tax),
            "revenue": _f(r.revenue),
            "reconciliation": _reconciliation_block(
                snaps.get(r.raw_order_id), include_returns=include_returns
            ),
        })
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "as_of": today.isoformat(),
        "sort": sort,
        "include_returns": include_returns,
        "items": items,
    }


def order_detail(
    db: Session,
    order_id: int,
    *,
    include_returns: bool = False,
    as_of: date | None = None,
) -> dict | None:
    """订单下钻：三金额 + 最新对账块 + 对账历史（每个快照一条，D-40 §3 只追加）。

    与列表同口径（已生效；非已生效单 404，由 API 层转换）。历史按最新值排序键的
    反序（旧→新：source_modified_at ASC NULLS FIRST → 批次 → id），末条与
    reconciliation 块一致。"""

    today = as_of or date.today()
    base, _ = _order_query()
    row = db.execute(base.where(FSalesOrder.id == order_id)).one_or_none()
    if row is None:
        return None
    history_rows = db.scalars(
        select(FSalesReconciliationSnapshot)
        .where(FSalesReconciliationSnapshot.raw_order_id == row.raw_order_id)
        .order_by(
            FSalesReconciliationSnapshot.source_modified_at.asc().nulls_first(),
            FSalesReconciliationSnapshot.import_batch_id.asc(),
            FSalesReconciliationSnapshot.id.asc(),
        )
    ).all()
    history = [
        {
            "import_batch_id": snap.import_batch_id,
            "source": snap.source,
            "created_at": (
                snap.created_at.isoformat() if snap.created_at else None
            ),
            "reconciled_as_of": (
                snap.source_modified_at.isoformat()
                if snap.source_modified_at
                else None
            ),
            "order_amount_at_snapshot": _f(snap.order_amount),
            "reconciled_amount": _f(snap.reconciled_amount),
            "pending_amount": _f(snap.pending_amount),
            "return_reconciled_amount": _f(snap.return_reconciled_amount),
        }
        for snap in history_rows
    ]
    latest = history_rows[-1] if history_rows else None
    return {
        "order_id": row.id,
        "order_no": row.order_no,
        "order_date": row.order_date.isoformat() if row.order_date else None,
        "is_future": bool(row.order_date and row.order_date > today),
        "salesperson": row.salesperson,
        "customer": row.customer,
        "business_type": row.business_type,
        "data_status": row.data_status,
        "amount_ex_tax": _f(row.amount_ex_tax),
        "amount_inc_tax": _f(row.amount_inc_tax),
        "revenue": _f(row.revenue),
        "as_of": today.isoformat(),
        "reconciliation": _reconciliation_block(
            latest, include_returns=include_returns
        ),
        "reconciliation_history": history,
    }

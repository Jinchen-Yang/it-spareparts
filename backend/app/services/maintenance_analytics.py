"""维保数据分析看板：PN 维度的成本排名与损坏频率（2026-08-21，用户需求）。

口径约定：
- 消耗口径 = WBDD 需求单明细（f_maintenance_line）的有效数量 qty−return_qty；
  标准三过滤：行 is_active、active_orders()（生效/墓碑）、order_date ∈ 窗口。
- 成本口径 = recompute 回填的 cost_amount_inc_tax/ex_tax（缺价行不按 0，
  missing_lines 单列——铁律 5）。
- 损坏佐证 = RKD 坏件返还量（maintenance_rkd_return_line.qty，按 PN 对齐
  occurred_at 窗口）；坏返率 = 坏件量 / 有效消耗量，分母为零不显示。
- 聚合只引用 AGGREGATE_SOURCE_COLUMNS 白名单列（铁律 3）：单头 order_no/
  order_date + 行 qty/return_qty + 成本回填列；挂靠 join 只用于项目计数。
- 权限：无 data_purchase_cost → 成本列整体 restricted()（键集与 ready 一致），
  成本排序拒绝（422），不静默降级（boss-board 同款）。
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.business_time import business_today
from app.models.dimensions import DimPart
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder
from app.models.maintenance import MaintenanceManualCostOverride
from app.models.maintenance_doc_import import MaintenanceRkdReturnLine
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.services import maintenance_cost_quality, query_filters
from app.services.maintenance_boss_board import not_imported, ready, restricted, wbdd_imported

RANGES = ("ytd", "12m", "all", "custom")
SORTS = (
    "cost_inc", "cost_ex", "qty", "return_qty", "effective_qty",
    "occurrences", "order_count", "project_count", "monthly_avg",
    "bad_qty", "bad_rate", "missing_lines", "cost_share", "pn",
)
COST_SORTS = {"cost_inc", "cost_ex"}


class AnalyticsValidationError(ValueError):
    """参数非法（422 语义）。"""


class AnalyticsSortNotPermitted(ValueError):
    """无成本权限按成本排序（422 语义，不静默降级）。"""


def resolve_window(range_: str, date_from: date | None,
                   date_to: date | None) -> tuple[date | date | None, date | date | None]:
    """ytd / 12m / all / custom → (start, end)。all 两端为 None（全量）。"""
    today = business_today()
    if range_ == "ytd":
        return today.replace(month=1, day=1), today
    if range_ == "12m":
        return today - timedelta(days=365), today
    if range_ == "all":
        return None, None
    if range_ == "custom":
        if date_from is None or date_to is None:
            raise AnalyticsValidationError("自定义窗口必须同时给 date_from 与 date_to")
        if date_from > date_to:
            raise AnalyticsValidationError("起始日期不能晚于结束日期")
        return date_from, date_to
    raise AnalyticsValidationError(f"时间窗必须是 {'/'.join(RANGES)}")


def _window_months(start: date | None, end: date | None) -> Decimal | None:
    """窗口月数（月均数量分母）；all 窗口无起点返回 None（不显示月均）。"""
    if start is None or end is None:
        return None
    months = (end.year - start.year) * 12 + (end.month - start.month) + 1
    return Decimal(max(months, 1))


def pn_ranking(
    db: Session,
    *,
    range_: str = "ytd",
    date_from: date | None = None,
    date_to: date | None = None,
    q: str | None = None,
    sort: str = "cost_inc",
    page: int = 1,
    page_size: int = 20,
    can_cost: bool,
    allowed_project_ids: set[str] | None = None,
) -> dict:
    """PN 排名聚合。

    allowed_project_ids 非 None（行键 own_maintenance_projects_only 开）时，
    行集与坏件佐证都收敛到该范围：未归属行（assignment 为 NULL）一并排除，
    不得经排名/汇总泄露他人项目（含 total 口径）。
    """
    start, end = resolve_window(range_, date_from, date_to)
    months = _window_months(start, end)

    cost_inc, _actual_inc, _estimated_inc, missing_inc = (
        maintenance_cost_quality.sql_normalized_line_cost(
            source_column=FMaintenanceLine.cost_source,
            tax_basis_column=FMaintenanceLine.cost_tax_basis,
            legacy_amount_column=FMaintenanceLine.cost_amount,
            normalized_amount_column=FMaintenanceLine.cost_amount_inc_tax,
            normalized_basis="inc",
            anomaly_flags_column=FMaintenanceLine.anomaly_flags,
            qty_column=FMaintenanceLine.qty,
            return_qty_column=FMaintenanceLine.return_qty,
            manual_unit_cost_column=MaintenanceManualCostOverride.unit_cost_inc_tax,
            manual_active_column=MaintenanceManualCostOverride.active,
        )
    )
    cost_ex, _actual_ex, _estimated_ex, _missing_ex = (
        maintenance_cost_quality.sql_normalized_line_cost(
            source_column=FMaintenanceLine.cost_source,
            tax_basis_column=FMaintenanceLine.cost_tax_basis,
            legacy_amount_column=FMaintenanceLine.cost_amount,
            normalized_amount_column=FMaintenanceLine.cost_amount_ex_tax,
            normalized_basis="ex",
            anomaly_flags_column=FMaintenanceLine.anomaly_flags,
            qty_column=FMaintenanceLine.qty,
            return_qty_column=FMaintenanceLine.return_qty,
            manual_unit_cost_column=MaintenanceManualCostOverride.unit_cost_ex_tax,
            manual_active_column=MaintenanceManualCostOverride.active,
        )
    )

    # ---- 主聚合：WBDD 明细按 PN 分组（单查询，无 N+1） ----
    stmt = (
        select(
            FMaintenanceLine.part_id,
            func.max(FMaintenanceLine.pn_std).label("pn_std"),
            func.max(DimPart.description).label("description"),
            func.count(FMaintenanceLine.id).label("occurrences"),
            func.count(func.distinct(FMaintenanceOrder.order_no)).label("order_count"),
            func.count(func.distinct(
                MaintenanceSourceOrderAssignment.project_id)).label("project_count"),
            func.coalesce(func.sum(FMaintenanceLine.qty), Decimal("0")).label("qty"),
            func.coalesce(func.sum(FMaintenanceLine.return_qty), Decimal("0")).label("return_qty"),
            func.sum(cost_inc).label("cost_inc"),
            func.sum(cost_ex).label("cost_ex"),
            func.count().filter(missing_inc).label("missing_lines"),
            func.min(FMaintenanceOrder.order_date).label("first_date"),
            func.max(FMaintenanceOrder.order_date).label("last_date"),
        )
        .select_from(FMaintenanceLine)
        .join(FMaintenanceOrder, FMaintenanceOrder.id == FMaintenanceLine.order_id)
        .outerjoin(DimPart, DimPart.id == FMaintenanceLine.part_id)
        .outerjoin(
            MaintenanceManualCostOverride,
            (MaintenanceManualCostOverride.line_id == FMaintenanceLine.id)
            & MaintenanceManualCostOverride.active.is_(True),
        )
        .outerjoin(
            MaintenanceSourceOrderAssignment,
            (MaintenanceSourceOrderAssignment.source_order_id
             == FMaintenanceOrder.raw_order_id)
            & MaintenanceSourceOrderAssignment.is_active.is_(True))
        .where(FMaintenanceLine.is_active.is_(True))
        .group_by(FMaintenanceLine.part_id)
    )
    stmt = query_filters.active_orders(stmt, FMaintenanceOrder)
    if allowed_project_ids is not None:
        # outerjoin 上的 where 让未归属行（NULL）自然落选——范围账号看不到无主行
        stmt = stmt.where(
            MaintenanceSourceOrderAssignment.project_id.in_(
                allowed_project_ids or {""}))
    if start is not None:
        stmt = stmt.where(FMaintenanceOrder.order_date >= start)
    if end is not None:
        stmt = stmt.where(FMaintenanceOrder.order_date <= end)
    rows = db.execute(stmt).all()

    # ---- 坏件佐证：RKD 坏件返还按 part_id 聚合 ----
    rkd_stmt = select(
        MaintenanceRkdReturnLine.part_id,
        func.upper(MaintenanceRkdReturnLine.pn),
        func.coalesce(func.sum(MaintenanceRkdReturnLine.qty), Decimal("0")),
    ).group_by(MaintenanceRkdReturnLine.part_id,
               func.upper(MaintenanceRkdReturnLine.pn))
    if allowed_project_ids is not None:
        rkd_stmt = rkd_stmt.where(
            MaintenanceRkdReturnLine.project_id.in_(allowed_project_ids or {""}))
    rkd = db.execute(rkd_stmt).all()
    bad_by_part = {p: q for p, _pn, q in rkd if p is not None}
    bad_by_pn = {pn.upper(): q for _p, pn, q in rkd}

    # ---- 关键词过滤（PN/描述包含，大小写不敏感） ----
    term = (q or "").strip().upper()
    items = []
    for r in rows:
        pn = (r.pn_std or "").strip()
        if term and term not in pn.upper() and term not in (r.description or "").upper():
            continue
        effective = (r.qty or Decimal("0")) - (r.return_qty or Decimal("0"))
        # part_id 优先，缺 part_id 的 RKD 行按 PN 大写文本回退（boss_facts 同口径）
        bad_qty = (bad_by_part.get(r.part_id)
                   or bad_by_pn.get(pn.upper())
                   or Decimal("0"))
        items.append({
            "part_id": r.part_id,
            "pn": pn,
            "description": r.description,
            "occurrences": int(r.occurrences),
            "order_count": int(r.order_count),
            "project_count": int(r.project_count),
            "qty": r.qty,
            "return_qty": r.return_qty,
            "effective_qty": effective,
            "cost_inc": (
                Decimal(r.cost_inc).quantize(Decimal("0.01"))
                if r.cost_inc is not None else None
            ),
            "cost_ex": (
                Decimal(r.cost_ex).quantize(Decimal("0.01"))
                if r.cost_ex is not None else None
            ),
            "missing_lines": int(r.missing_lines),
            "bad_return_qty": bad_qty,
            "first_date": r.first_date.isoformat() if r.first_date else None,
            "last_date": r.last_date.isoformat() if r.last_date else None,
        })

    # ---- 汇总（过滤后全集上计算，占比分母用全集） ----
    total_cost_inc = sum((i["cost_inc"] or Decimal("0")) for i in items)
    total_cost_ex = sum((i["cost_ex"] or Decimal("0")) for i in items)
    total_effective = sum(i["effective_qty"] for i in items)
    total_bad = sum(i["bad_return_qty"] for i in items)

    # 先加工派生指标（排序键依赖），再排序，最后赋名次
    for i in items:
        i["cost_share_pct"] = (
            float(((i["cost_inc"] or Decimal("0")) / total_cost_inc * 100).quantize(Decimal("0.1")))
            if total_cost_inc else None)
        i["monthly_avg_qty"] = (
            float((i["effective_qty"] / months).quantize(Decimal("0.1")))
            if months is not None else None)
        i["bad_return_rate_pct"] = (
            float(((i["bad_return_qty"] / i["effective_qty"]) * 100).quantize(Decimal("0.1")))
            if i["effective_qty"] > 0 and i["bad_return_qty"] > 0 else None)

    def sort_key(i):
        return {
            "cost_inc": (i["cost_inc"] or Decimal("0"), i["effective_qty"]),
            "cost_ex": (i["cost_ex"] or Decimal("0"), i["effective_qty"]),
            "qty": (i["qty"] or Decimal("0"), i["occurrences"]),
            "return_qty": (i["return_qty"] or Decimal("0"),),
            "effective_qty": (i["effective_qty"], i["cost_inc"] or Decimal("0")),
            "occurrences": (i["occurrences"], i["effective_qty"]),
            "order_count": (i["order_count"], i["effective_qty"]),
            "project_count": (i["project_count"], i["effective_qty"]),
            "monthly_avg": (i["monthly_avg_qty"] or 0, i["effective_qty"]),
            "bad_qty": (i["bad_return_qty"], i["effective_qty"]),
            "bad_rate": (i["bad_return_rate_pct"] or 0, i["bad_return_qty"]),
            "missing_lines": (i["missing_lines"],),
            "cost_share": (i["cost_share_pct"] or 0,),
            "pn": (i["pn"],),
        }[sort]

    items.sort(key=sort_key, reverse=(sort != "pn"))  # PN 按字母升序更自然
    for rank, i in enumerate(items, 1):
        i["rank"] = rank

    total = len(items)
    page_items = items[(page - 1) * page_size: page * page_size]

    # ---- 权限信封：成本列整体三态（键集一致，无侧信道） ----
    if can_cost:
        for i in page_items:
            i["cost_inc"] = ready(str(i["cost_inc"]) if i["cost_inc"] is not None else None)
            i["cost_ex"] = ready(str(i["cost_ex"]) if i["cost_ex"] is not None else None)
    else:
        for i in page_items:
            i["cost_inc"] = restricted()
            i["cost_ex"] = restricted()

    wbdd_ready = wbdd_imported(db)
    cost_total = (ready(str(total_cost_inc)) if can_cost
                  else (not_imported() if not wbdd_ready else restricted()))

    return {
        "rows": page_items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "window": {
            "range": range_,
            "date_from": start.isoformat() if start else None,
            "date_to": end.isoformat() if end else None,
            "months": int(months) if months is not None else None,
        },
        "summary": {
            "part_count": total,
            "total_cost_inc": cost_total,
            "total_cost_ex": ready(str(total_cost_ex)) if can_cost else restricted(),
            "total_effective_qty": str(total_effective),
            "total_bad_return_qty": str(total_bad),
            "wbdd_ready": wbdd_ready,
        },
        "sort": sort,
    }

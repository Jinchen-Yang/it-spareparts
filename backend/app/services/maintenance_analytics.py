"""维保数据分析看板：PN 维度的成本排名与损坏频率（2026-08-21，用户需求）。

口径约定：
- 消耗口径 = WBDD 需求单明细（f_maintenance_line）的有效数量 qty−return_qty；
  标准三过滤：行 is_active、active_orders()（生效/墓碑）、order_date ∈ 窗口。
- 成本口径 = recompute 回填的 cost_amount_inc_tax/ex_tax（缺价行不按 0，
  missing_lines 单列——铁律 5）。
- 返还与实际领用取同项目、权限与业务日期窗口的有效事实；返还率分母为
  已确认现场领用数量。需求数量/退货仍只用于原有需求成本与消耗展示。
- 聚合只引用 AGGREGATE_SOURCE_COLUMNS 白名单列（铁律 3）：单头 order_no/
  order_date + 行 qty/return_qty + 成本回填列；当前挂靠用于项目计数及业务类型筛选。
- 权限：无 data_purchase_cost → 成本列整体 restricted()（键集与 ready 一致），
  成本排序拒绝（422），不静默降级（boss-board 同款）。
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import Date, and_, case, func, or_, select
from sqlalchemy.orm import Session

from app.business_time import business_today
from app.models.dimensions import DimPart
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder
from app.models.maintenance import MaintenanceManualCostOverride
from app.models.maintenance_doc_import import MaintenanceRkdReturnLine
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.services import maintenance_cost_quality, query_filters
from app.services.maintenance_return_metrics import pn_totals, rate_pct
from app.services.maintenance_boss_board import (
    BUSINESS_TYPE_CODES,
    BUSINESS_TYPE_LABELS,
    _business_type_clause,
    not_imported,
    ready,
    restricted,
    wbdd_imported,
)

RANGES = ("ytd", "12m", "all", "custom")
SORTS = (
    "cost_inc",
    "cost_ex",
    "qty",
    "return_qty",
    "effective_qty",
    "occurrences",
    "order_count",
    "project_count",
    "monthly_avg",
    "bad_qty",
    "bad_rate",
    "issued_qty",
    "receipt_qty",
    "receipt_rate",
    "missing_lines",
    "cost_share",
    "pn",
)
COST_SORTS = {"cost_inc", "cost_ex"}
# 需求类型码 ↔ 库内字面量（API 只收 repair/stock 码，服务层收字面量）
DEMAND_TYPE_LITERALS = {"repair": "报修供货", "stock": "补库供货"}


class AnalyticsValidationError(ValueError):
    """参数非法（422 语义）。"""


class AnalyticsSortNotPermitted(ValueError):
    """无成本权限按成本排序（422 语义，不静默降级）。"""


def parse_csv_items(
    spec: str | None, *, max_items: int, max_len: int, field: str, trim: bool = True
) -> list[str] | None:
    """CSV 入参 → 去空/去重后的列表；全空返回 None，超限 422 语义。

    trim=False 用于仓库这类自由文本：筛选走库内原值精确 IN，
    去空白会让「 广州仓 」这类历史带空格值选不中自己（filter_options 同口径）。
    """
    if spec is None:
        return None
    items: list[str] = []
    for raw in spec.split(","):
        item = raw.strip() if trim else raw
        if not item:
            continue
        if len(item) > max_len:
            raise AnalyticsValidationError(f"{field} 单项长度不能超过 {max_len}")
        if item not in items:
            items.append(item)
    if not items:
        return None
    if len(items) > max_items:
        raise AnalyticsValidationError(f"{field} 最多 {max_items} 项")
    return items


def resolve_window(
    range_: str, date_from: date | None, date_to: date | None
) -> tuple[date | date | None, date | date | None]:
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


def _cost_source_clause(cost_sources: list[str] | None):
    """取价来源四分类 → 行级 where 子句（前端 costSourceCategory 同口径）。

    linked=direct；manual=manual；missing=NULL/none/空串（前端 !source 判空同口径）；
    estimated=其余非空来源（window/purchase_history/pool_*/月均/销售参考等）。
    """
    if not cost_sources:
        return None
    clauses = []
    for code in cost_sources:
        if code == "linked":
            clauses.append(FMaintenanceLine.cost_source == "direct")
        elif code == "manual":
            clauses.append(FMaintenanceLine.cost_source == "manual")
        elif code == "missing":
            clauses.append(
                or_(
                    FMaintenanceLine.cost_source.is_(None),
                    FMaintenanceLine.cost_source.in_(("none", "")),
                )
            )
        elif code == "estimated":
            clauses.append(
                and_(
                    FMaintenanceLine.cost_source.is_not(None),
                    FMaintenanceLine.cost_source.not_in(
                        ("direct", "manual", "none", "")
                    ),
                )
            )
    if not clauses:
        return None
    return or_(*clauses) if len(clauses) > 1 else clauses[0]


def _effective_salesperson():
    """有效销售 SQL 表达式（v1.35，两查询共用）。

    项目主档优先：有活跃挂靠项目时，人工改过（salesperson_override_active，
    含清空）→ 主档值即权威，不回退订单源旧值；未改过且主档空 → 回退订单源；
    无活跃挂靠 → 订单源。两侧都 btrim 去空白，NULL/空白并成同一档（JSON null）。
    """
    master = func.nullif(func.btrim(MaintenanceProject.salesperson), "")
    source = func.nullif(func.btrim(FMaintenanceOrder.salesperson), "")
    return case(
        (MaintenanceProject.project_id.is_(None), source),
        (MaintenanceProject.salesperson_override_active.is_(True), master),
        else_=func.coalesce(master, source),
    )


def pn_ranking(
    db: Session,
    *,
    range_: str = "ytd",
    date_from: date | None = None,
    date_to: date | None = None,
    q: str | None = None,
    business_type: str = "all",
    project_ids: list[str] | None = None,
    customer: str | None = None,
    salesperson: str | None = None,
    order_no: str | None = None,
    demand_types: list[str] | None = None,
    warehouses: list[str] | None = None,
    cost_sources: list[str] | None = None,
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
    业务类型按当前项目分类，在 PN 聚合前取交集；具体分类必须有真实项目，
    未归属只进入 all（含六档全选），不得混入 unlabeled。
    历史五档 URL（无 refit）是显式子集：只筛那五档，不等于 all。
    全字段筛选（项目/客户/销售/单号/需求类型/仓库/取价来源）同样在聚合前
    落到 where：行集、total、summary、成本占比与分页随之收敛；project_ids
    与 allowed_project_ids 取交集，绝不放大行键范围。
    销售口径（v1.35 有效销售）：有活跃挂靠 → 项目主档销售（人工改过即权威，
    清空即未标注，不回退订单源旧值）；主档空且未改过 → 回退订单源；无项目 →
    订单源。sp 筛选与 spend_trend 的 by_salesperson 同口径，禁止两页签分叉。
    """
    start, end = resolve_window(range_, date_from, date_to)
    months = _window_months(start, end)
    business_type_clause = _business_type_clause(business_type)

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
            func.count(
                func.distinct(MaintenanceSourceOrderAssignment.project_id)
            ).label("project_count"),
            func.coalesce(func.sum(FMaintenanceLine.qty), Decimal("0")).label("qty"),
            func.coalesce(func.sum(FMaintenanceLine.return_qty), Decimal("0")).label(
                "return_qty"
            ),
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
            (
                MaintenanceSourceOrderAssignment.source_order_id
                == FMaintenanceOrder.raw_order_id
            )
            & MaintenanceSourceOrderAssignment.is_active.is_(True),
        )
        # v1.35 有效销售：主聚合 outerjoin 项目主档，sp 筛选读主档销售
        .outerjoin(
            MaintenanceProject,
            MaintenanceProject.project_id
            == MaintenanceSourceOrderAssignment.project_id,
        )
        .where(FMaintenanceLine.is_active.is_(True))
        .group_by(FMaintenanceLine.part_id)
    )
    stmt = query_filters.active_orders(stmt, FMaintenanceOrder)
    if business_type_clause is not None:
        # EXISTS 显式要求项目存在，避免 outer join 的 NULL 被归入「未标注」。
        # 显式只关联挂靠：主聚合已 outerjoin 项目主档，auto-correlate 会连
        # 项目一起收走，子查询会丢 FROM（与 _spend_filters 同款）。
        stmt = stmt.where(
            select(1)
            .where(
                MaintenanceProject.project_id
                == MaintenanceSourceOrderAssignment.project_id,
                business_type_clause,
            )
            .correlate(MaintenanceSourceOrderAssignment)
            .exists()
        )
    if allowed_project_ids is not None:
        # outerjoin 上的 where 让未归属行（NULL）自然落选——范围账号看不到无主行
        stmt = stmt.where(
            MaintenanceSourceOrderAssignment.project_id.in_(allowed_project_ids or {""})
        )
    if project_ids is not None:
        # 与 allowed_project_ids 是 AND 关系：显式项目筛选绝不放大行键范围
        stmt = stmt.where(
            MaintenanceSourceOrderAssignment.project_id.in_(project_ids or {""})
        )
    if customer:
        stmt = stmt.where(
            FMaintenanceOrder.end_customer.icontains(customer, autoescape=True)
        )
    if salesperson:
        # v1.35 有效销售口径：匹配 _effective_salesperson（项目主档优先）
        stmt = stmt.where(
            _effective_salesperson().icontains(salesperson, autoescape=True)
        )
    if order_no:
        stmt = stmt.where(
            FMaintenanceOrder.order_no.icontains(order_no, autoescape=True)
        )
    if demand_types:
        stmt = stmt.where(FMaintenanceOrder.demand_type.in_(demand_types))
    if warehouses:
        stmt = stmt.where(FMaintenanceOrder.warehouse.in_(warehouses))
    cost_source_clause = _cost_source_clause(cost_sources)
    if cost_source_clause is not None:
        stmt = stmt.where(cost_source_clause)
    # Demand filters match linked facts; occurrence dates are handled separately.
    fact_source_ids = None
    demand_part_scope = None
    if customer or salesperson or order_no or demand_types or warehouses or cost_source_clause is not None:
        fact_source_ids = set(db.scalars(
            stmt.with_only_columns(FMaintenanceOrder.raw_order_id)
            .group_by(None).distinct()
        ))
    if cost_source_clause is not None:
        demand_part_scope = {
            (pid, oid, part_id)
            for pid, oid, part_id in db.execute(
                stmt.with_only_columns(MaintenanceSourceOrderAssignment.project_id,
                                       FMaintenanceOrder.raw_order_id,
                                       FMaintenanceLine.part_id)
                .group_by(None).distinct()
            ) if pid is not None and part_id is not None
        }
    if start is not None:
        stmt = stmt.where(FMaintenanceOrder.order_date >= start)
    if end is not None:
        stmt = stmt.where(FMaintenanceOrder.order_date <= end)
    rows = db.execute(stmt).all()

    # Actual issues and receipts share project permissions and business dates.
    fact_project_ids = None if project_ids is None else set(project_ids)
    if allowed_project_ids is not None:
        fact_project_ids = (set(allowed_project_ids) if fact_project_ids is None
                            else fact_project_ids & set(allowed_project_ids))
    if business_type_clause is not None:
        classified = set(db.scalars(select(MaintenanceProject.project_id)
                                   .where(business_type_clause)))
        fact_project_ids = (classified if fact_project_ids is None
                            else fact_project_ids & classified)
    facts = pn_totals(db, project_ids=fact_project_ids, date_from=start, date_to=end,
                      source_order_ids=fact_source_ids, demand_part_scope=demand_part_scope)
    by_part = {f["part_id"]: f for f in facts if f["part_id"] is not None}
    by_pn = {}
    ambiguous_pns = set()
    for fact in facts:
        key = fact["pn"].strip().upper()
        if key in by_pn:
            ambiguous_pns.add(key)
        by_pn[key] = fact
    for key in ambiguous_pns:
        del by_pn[key]
    matched_facts: set[int] = set()

    # ---- 关键词过滤（PN/描述包含，大小写不敏感） ----
    term = (q or "").strip().upper()
    items = []
    for r in rows:
        pn = (r.pn_std or "").strip()
        # Match identity before filtering text: a renamed PN keeps its demand
        # quantities/costs when searched by its current canonical name.
        fact = (by_part.get(r.part_id) if r.part_id is not None
                else by_pn.get(pn.upper())) or {}
        search_values = (pn, r.description, fact.get("pn"), fact.get("description"))
        if term and not any(term in (value or "").upper() for value in search_values):
            continue
        effective = (r.qty or Decimal("0")) - (r.return_qty or Decimal("0"))
        if fact:
            matched_facts.add(id(fact))
        bad_qty = fact.get("bad_returned_qty", Decimal("0"))
        items.append(
            {
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
                    if r.cost_inc is not None
                    else None
                ),
                "cost_ex": (
                    Decimal(r.cost_ex).quantize(Decimal("0.01"))
                    if r.cost_ex is not None
                    else None
                ),
                "missing_lines": int(r.missing_lines),
                "bad_return_qty": bad_qty,
                "issued_qty": fact.get("issued_qty", Decimal("0")),
                "receipt_qty": fact.get("returned_qty", Decimal("0")),
                "first_date": r.first_date.isoformat() if r.first_date else None,
                "last_date": r.last_date.isoformat() if r.last_date else None,
            }
        )

    # Facts without demand rows remain visible, with no fabricated demand cost.
    for fact in facts:
        if id(fact) in matched_facts:
            continue
        if term and term not in fact["pn"].upper() and term not in (fact["description"] or "").upper():
            continue
        items.append({
            "part_id": fact["part_id"], "pn": fact["pn"],
            "description": fact["description"], "occurrences": 0,
            "order_count": 0, "project_count": len(fact["project_ids"]),
            "qty": Decimal("0"), "return_qty": Decimal("0"),
            "effective_qty": Decimal("0"), "cost_inc": None, "cost_ex": None,
            "missing_lines": 0, "bad_return_qty": fact["bad_returned_qty"],
            "issued_qty": fact["issued_qty"], "receipt_qty": fact["returned_qty"],
            "first_date": None, "last_date": None,
        })

    # ---- 汇总（过滤后全集上计算，占比分母用全集） ----
    total_cost_inc = sum((i["cost_inc"] or Decimal("0")) for i in items)
    total_cost_ex = sum((i["cost_ex"] or Decimal("0")) for i in items)
    total_effective = sum(i["effective_qty"] for i in items)
    total_bad = sum(i["bad_return_qty"] for i in items)

    # 先加工派生指标（排序键依赖），再排序，最后赋名次
    for i in items:
        i["cost_share_pct"] = (
            float(
                ((i["cost_inc"] or Decimal("0")) / total_cost_inc * 100).quantize(
                    Decimal("0.1")
                )
            )
            if total_cost_inc
            else None
        )
        i["monthly_avg_qty"] = (
            float((i["effective_qty"] / months).quantize(Decimal("0.1")))
            if months is not None
            else None
        )
        i["bad_return_rate_pct"] = rate_pct(i["bad_return_qty"], i["issued_qty"])
        i["receipt_return_rate_pct"] = rate_pct(i["receipt_qty"], i["issued_qty"])

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
            "issued_qty": (i["issued_qty"], i["receipt_qty"]),
            "receipt_qty": (i["receipt_qty"], i["issued_qty"]),
            "receipt_rate": (i["receipt_return_rate_pct"] is not None,
                             i["receipt_return_rate_pct"] or 0, i["receipt_qty"]),
            "missing_lines": (i["missing_lines"],),
            "cost_share": (i["cost_share_pct"] or 0,),
            "pn": (i["pn"],),
        }[sort]

    items.sort(key=sort_key, reverse=(sort != "pn"))  # PN 按字母升序更自然
    for rank, i in enumerate(items, 1):
        i["rank"] = rank

    total = len(items)
    page_items = items[(page - 1) * page_size : page * page_size]

    # ---- 权限信封：成本列整体三态（键集一致，无侧信道） ----
    if can_cost:
        for i in page_items:
            i["cost_inc"] = ready(
                str(i["cost_inc"]) if i["cost_inc"] is not None else None
            )
            i["cost_ex"] = ready(
                str(i["cost_ex"]) if i["cost_ex"] is not None else None
            )
    else:
        for i in page_items:
            i["cost_inc"] = restricted()
            i["cost_ex"] = restricted()

    wbdd_ready = wbdd_imported(db)
    cost_total = (
        ready(str(total_cost_inc))
        if can_cost
        else (not_imported() if not wbdd_ready else restricted())
    )

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
            "total_issued_qty": str(sum((i["issued_qty"] for i in items), Decimal("0"))),
            "total_receipt_qty": str(sum((i["receipt_qty"] for i in items), Decimal("0"))),
            "wbdd_ready": wbdd_ready,
        },
        "sort": sort,
    }


def filter_options(db: Session, *, allowed_project_ids: set[str] | None = None) -> dict:
    """筛选器候选项：有效订单的非空仓库去重排序（最多 200）。

    allowed_project_ids 非 None（行键开）时用 EXISTS 收敛到可见项目，
    范围账号不得看到范围外仓库。
    """
    # 返回值必须是库内原值：warehouses 筛选走精确 IN，去空白会让候选项选不中自己
    stmt = (
        select(FMaintenanceOrder.warehouse)
        .where(
            FMaintenanceOrder.warehouse.is_not(None),
            func.btrim(FMaintenanceOrder.warehouse) != "",
        )
        .distinct()
        .order_by(FMaintenanceOrder.warehouse)
        .limit(200)
    )
    stmt = query_filters.active_orders(stmt, FMaintenanceOrder)
    if allowed_project_ids is not None:
        stmt = stmt.where(
            select(1)
            .where(
                MaintenanceSourceOrderAssignment.source_order_id
                == FMaintenanceOrder.raw_order_id,
                MaintenanceSourceOrderAssignment.is_active.is_(True),
                MaintenanceSourceOrderAssignment.project_id.in_(
                    allowed_project_ids or {""}
                ),
            )
            .exists()
        )
    return {"warehouses": [w for w in db.scalars(stmt).all() if w]}


# ---------------------------------------------------------------- 开支统计


GRANULARITIES = ("day", "week", "month", "year")
# 开支统计档位：boss-board 六档（四类标准 + other/unlabeled）+ 无活跃项目档。
# 标准字面量从 BUSINESS_TYPE_LABELS 动态展开，未来新增业务类型自动进入拆分。
_BUSINESS_TYPE_SPLIT_CODES: tuple[str, ...] = (*BUSINESS_TYPE_CODES, "unassigned")
_BUSINESS_TYPE_SPLIT_LABELS: dict[str, str] = {
    **BUSINESS_TYPE_LABELS,
    "other": "非维保",
    "unlabeled": "未标注",
    "unassigned": "未归属",
}
# 销售明细上限：销售人数可能很多，超出部分对看板只增噪声
_SALESPERSON_LIMIT = 200
# 按项目汇总上限：项目数可能很多，超出部分对看板只增噪声
_PROJECT_LIMIT = 200


def _cost_envelope(value, *, can_cost: bool, wbdd_ready: bool) -> dict:
    """成本三态信封：WBDD 未导入 → not_imported（绝不渲染 0）；有权 → ready；否则 restricted。"""
    if not wbdd_ready:
        return not_imported()
    if can_cost:
        return ready(str(Decimal(value or 0).quantize(Decimal("0.01"))))
    return restricted()


def _cost_share_pct(value: Decimal, total: Decimal, *, can_cost: bool) -> float | None:
    """成本占比（%，0.1 精度）；无成本权限或总额为 0 时 null（不泄露成本序）。"""
    if not can_cost or not total:
        return None
    return float((value / total * 100).quantize(Decimal("0.1")))


def _business_type_split_case():
    """行 → 开支统计档位的 SQL CASE（与 business_type_code 同语义）。

    顺序即判定优先级：无活跃挂靠/项目不存在 → unassigned；项目类型 NULL/空白 →
    unlabeled；命中标准字面量 → 对应码；其余非空文本 → other。
    """
    trimmed = func.btrim(MaintenanceProject.business_type)
    return case(
        (
            or_(
                MaintenanceSourceOrderAssignment.assignment_id.is_(None),
                MaintenanceProject.project_id.is_(None),
            ),
            "unassigned",
        ),
        (or_(MaintenanceProject.business_type.is_(None), trimmed == ""), "unlabeled"),
        *[(trimmed == label, code) for code, label in BUSINESS_TYPE_LABELS.items()],
        else_="other",
    )


def _spend_base(stmt):
    """开支统计三条分组查询（主聚合/销售/项目）共用的 FROM 与基础行过滤。

    v1.35 起统一 outerjoin 项目主档：有效销售表达式与 by_project 都要读它。
    """
    return (
        stmt.select_from(FMaintenanceLine)
        .join(FMaintenanceOrder, FMaintenanceOrder.id == FMaintenanceLine.order_id)
        .outerjoin(
            MaintenanceManualCostOverride,
            (MaintenanceManualCostOverride.line_id == FMaintenanceLine.id)
            & MaintenanceManualCostOverride.active.is_(True),
        )
        .outerjoin(
            MaintenanceSourceOrderAssignment,
            (
                MaintenanceSourceOrderAssignment.source_order_id
                == FMaintenanceOrder.raw_order_id
            )
            & MaintenanceSourceOrderAssignment.is_active.is_(True),
        )
        .outerjoin(
            MaintenanceProject,
            MaintenanceProject.project_id
            == MaintenanceSourceOrderAssignment.project_id,
        )
        .where(
            FMaintenanceLine.is_active.is_(True),
            FMaintenanceOrder.order_date.is_not(None),
        )
    )


def _spend_filters(
    stmt,
    *,
    business_type_clause,
    allowed_project_ids: set[str] | None,
    project_ids: list[str] | None,
    customer: str | None,
    salesperson: str | None,
    order_no: str | None,
    demand_types: list[str] | None,
    warehouses: list[str] | None,
    cost_sources: list[str] | None,
    start: date | None,
    end: date | None,
):
    """pn_ranking 同款筛选子句（主聚合/销售/项目三条分组查询共用，口径单一）。

    无日期行无法上时间轴：调用方在各查询都先加 order_date IS NOT NULL。
    sp 走 v1.35 有效销售口径（_effective_salesperson，项目主档优先），
    调用方必须已 outerjoin 项目主档。
    """
    if business_type_clause is not None:
        # EXISTS 显式要求项目存在，避免 outer join 的 NULL 被归入「未标注」。
        # 显式只关联挂靠：主聚合已 outerjoin 项目，auto-correlate 会连项目一起收走。
        stmt = stmt.where(
            select(1)
            .where(
                MaintenanceProject.project_id
                == MaintenanceSourceOrderAssignment.project_id,
                business_type_clause,
            )
            .correlate(MaintenanceSourceOrderAssignment)
            .exists()
        )
    if allowed_project_ids is not None:
        # outerjoin 上的 where 让未归属行（NULL）自然落选——范围账号看不到无主行
        stmt = stmt.where(
            MaintenanceSourceOrderAssignment.project_id.in_(allowed_project_ids or {""})
        )
    if project_ids is not None:
        # 与 allowed_project_ids 是 AND 关系：显式项目筛选绝不放大行键范围
        stmt = stmt.where(
            MaintenanceSourceOrderAssignment.project_id.in_(project_ids or {""})
        )
    if customer:
        stmt = stmt.where(
            FMaintenanceOrder.end_customer.icontains(customer, autoescape=True)
        )
    if salesperson:
        # v1.35 有效销售口径：匹配 _effective_salesperson（项目主档优先）
        stmt = stmt.where(
            _effective_salesperson().icontains(salesperson, autoescape=True)
        )
    if order_no:
        stmt = stmt.where(
            FMaintenanceOrder.order_no.icontains(order_no, autoescape=True)
        )
    if demand_types:
        stmt = stmt.where(FMaintenanceOrder.demand_type.in_(demand_types))
    if warehouses:
        stmt = stmt.where(FMaintenanceOrder.warehouse.in_(warehouses))
    cost_source_clause = _cost_source_clause(cost_sources)
    if cost_source_clause is not None:
        stmt = stmt.where(cost_source_clause)
    if start is not None:
        stmt = stmt.where(FMaintenanceOrder.order_date >= start)
    if end is not None:
        stmt = stmt.where(FMaintenanceOrder.order_date <= end)
    return stmt


def spend_trend(
    db: Session,
    *,
    range_: str = "ytd",
    date_from: date | None = None,
    date_to: date | None = None,
    granularity: str = "month",
    business_type: str = "all",
    project_ids: list[str] | None = None,
    customer: str | None = None,
    salesperson: str | None = None,
    order_no: str | None = None,
    demand_types: list[str] | None = None,
    warehouses: list[str] | None = None,
    cost_sources: list[str] | None = None,
    can_cost: bool,
    allowed_project_ids: set[str] | None = None,
) -> dict:
    """开支统计：按期（日/周/月/年）× 业务类型 × 销售 × 项目拆分已知成本。

    口径与 pn_ranking 完全一致：行 is_active、active_orders()，成本只认
    sql_normalized_line_cost 的已知成本（缺价行不进 SUM，missing_lines 单列，
    绝不按 0 摊入）。业务类型按需求单**当前活跃挂靠项目**分类：无活跃挂靠或项目
    不存在 → unassigned；项目类型 NULL/空白 → unlabeled；标准字面量 → 对应码；
    其余非空文本 → other。显式分类筛选（含六档全选）不混入未归属，未归属只进
    默认 all。
    销售口径（v1.35 有效销售）：有活跃挂靠 → 项目主档销售（人工改过即权威，
    清空即未标注，不回退订单源旧值）；主档空且未改过 → 回退订单源；无项目 →
    订单源。by_salesperson 分组与 sp 筛选都用该口径（见 _effective_salesperson）。
    by_project（v1.35）：按当前活跃挂靠项目分组，未归属单列一行（display_name
    「未归属（无项目）」），与主聚合同筛选同口径，上限 200 行。
    allowed_project_ids 非 None（own_maintenance_projects_only 开）时行集收敛到
    该范围：未归属行一并排除，不得经分桶/销售/项目/汇总泄露他人项目。
    分桶 = date_trunc(granularity, order_date)::date（周桶从周一开始）；无日期行
    无法上时间轴，主聚合与销售/项目汇总一并排除。
    """
    if granularity not in GRANULARITIES:
        raise AnalyticsValidationError(f"粒度必须是 {'/'.join(GRANULARITIES)}")
    start, end = resolve_window(range_, date_from, date_to)
    business_type_clause = _business_type_clause(business_type)
    wbdd_ready = wbdd_imported(db)

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

    def apply_filters(statement):
        return _spend_filters(
            statement,
            business_type_clause=business_type_clause,
            allowed_project_ids=allowed_project_ids,
            project_ids=project_ids,
            customer=customer,
            salesperson=salesperson,
            order_no=order_no,
            demand_types=demand_types,
            warehouses=warehouses,
            cost_sources=cost_sources,
            start=start,
            end=end,
        )

    # ---- 主聚合：按 (时间桶, 业务类型档) 分组，Python 侧透视 ----
    bucket = func.date_trunc(granularity, FMaintenanceOrder.order_date).cast(Date)
    split = _business_type_split_case()
    stmt = _spend_base(
        select(
            bucket.label("bucket"),
            split.label("business_type"),
            func.count(func.distinct(FMaintenanceOrder.order_no)).label("order_count"),
            func.coalesce(func.sum(FMaintenanceLine.qty), Decimal("0")).label("qty"),
            func.coalesce(func.sum(FMaintenanceLine.return_qty), Decimal("0")).label(
                "return_qty"
            ),
            func.sum(cost_inc).label("cost_inc"),
            func.sum(cost_ex).label("cost_ex"),
            func.count().filter(missing_inc).label("missing_lines"),
        )
    ).group_by(bucket, split)
    stmt = query_filters.active_orders(stmt, FMaintenanceOrder)
    stmt = apply_filters(stmt)
    rows = db.execute(stmt).all()

    # 桶/分类两级累加：SQL SUM 对缺价行返回 NULL，这里按「无已知成本」记 0，
    # 缺口由 missing_lines 单列提示；组内有已知成本时其金额照常累加。
    pivot: dict[date, dict] = {}
    for r in rows:
        agg = pivot.setdefault(
            r.bucket,
            {
                "order_count": 0,
                "qty": Decimal("0"),
                "return_qty": Decimal("0"),
                "cost_inc": Decimal("0"),
                "cost_ex": Decimal("0"),
                "missing_lines": 0,
                "categories": {},
            },
        )
        values = {
            "order_count": int(r.order_count or 0),
            "qty": Decimal(r.qty or 0),
            "return_qty": Decimal(r.return_qty or 0),
            "cost_inc": Decimal(r.cost_inc or 0),
            "cost_ex": Decimal(r.cost_ex or 0),
            "missing_lines": int(r.missing_lines or 0),
        }
        agg["order_count"] += values["order_count"]
        agg["qty"] += values["qty"]
        agg["return_qty"] += values["return_qty"]
        agg["cost_inc"] += values["cost_inc"]
        agg["cost_ex"] += values["cost_ex"]
        agg["missing_lines"] += values["missing_lines"]
        agg["categories"][r.business_type] = values

    buckets = []
    for bucket_date in sorted(pivot):  # 桶升序（周桶键=周一）
        agg = pivot[bucket_date]
        buckets.append(
            {
                "bucket": bucket_date.isoformat(),
                "order_count": agg["order_count"],
                "qty": str(agg["qty"]),
                "effective_qty": str(agg["qty"] - agg["return_qty"]),
                "missing_lines": agg["missing_lines"],
                "cost_inc": _cost_envelope(
                    agg["cost_inc"], can_cost=can_cost, wbdd_ready=wbdd_ready
                ),
                "cost_ex": _cost_envelope(
                    agg["cost_ex"], can_cost=can_cost, wbdd_ready=wbdd_ready
                ),
                # 桶内只给有数据的档位（每档一个含税成本信封），零数据档位省略
                "by_business_type": {
                    code: _cost_envelope(
                        agg["categories"][code]["cost_inc"],
                        can_cost=can_cost,
                        wbdd_ready=wbdd_ready,
                    )
                    for code in _BUSINESS_TYPE_SPLIT_CODES
                    if code in agg["categories"]
                },
            }
        )

    totals: dict[str, dict] = {}
    for agg in pivot.values():
        for code, values in agg["categories"].items():
            total = totals.setdefault(
                code,
                {
                    "order_count": 0,
                    "qty": Decimal("0"),
                    "return_qty": Decimal("0"),
                    "cost_inc": Decimal("0"),
                    "cost_ex": Decimal("0"),
                    "missing_lines": 0,
                },
            )
            total["order_count"] += values["order_count"]
            total["qty"] += values["qty"]
            total["return_qty"] += values["return_qty"]
            total["cost_inc"] += values["cost_inc"]
            total["cost_ex"] += values["cost_ex"]
            total["missing_lines"] += values["missing_lines"]

    raw_rows = [
        {"code": code, **totals[code]}
        for code in _BUSINESS_TYPE_SPLIT_CODES
        if code in totals
    ]
    total_cost_inc = sum((t["cost_inc"] for t in raw_rows), Decimal("0"))
    if can_cost and total_cost_inc:
        raw_rows.sort(key=lambda t: (-t["cost_inc"], -t["qty"], t["code"]))
    else:
        # 无成本权限或全部缺价时按数量排序：成本序本身会泄露受限金额
        raw_rows.sort(key=lambda t: (-t["qty"], t["code"]))
    by_business_type = [
        {
            "code": t["code"],
            "label": _BUSINESS_TYPE_SPLIT_LABELS.get(t["code"], t["code"]),
            "order_count": t["order_count"],
            "qty": str(t["qty"]),
            "effective_qty": str(t["qty"] - t["return_qty"]),
            "missing_lines": t["missing_lines"],
            "cost_inc": _cost_envelope(
                t["cost_inc"], can_cost=can_cost, wbdd_ready=wbdd_ready
            ),
            "cost_ex": _cost_envelope(
                t["cost_ex"], can_cost=can_cost, wbdd_ready=wbdd_ready
            ),
            "cost_share_pct": _cost_share_pct(
                t["cost_inc"], total_cost_inc, can_cost=can_cost
            ),
        }
        for t in raw_rows
    ]

    # ---- 销售汇总：单独一次分组查询（同筛选同口径） ----
    # v1.35 有效销售（_effective_salesperson）：项目主档优先；NULL 与空白
    # （含纯空格）并成同一档，JSON 里统一为 null，不产生重复的「无人」行。
    effective_sp = _effective_salesperson()
    sp_stmt = _spend_base(
        select(
            effective_sp.label("salesperson"),
            func.count(func.distinct(FMaintenanceOrder.order_no)).label("order_count"),
            func.coalesce(func.sum(FMaintenanceLine.qty), Decimal("0")).label("qty"),
            func.coalesce(func.sum(FMaintenanceLine.return_qty), Decimal("0")).label(
                "return_qty"
            ),
            func.sum(cost_inc).label("cost_inc"),
            func.sum(cost_ex).label("cost_ex"),
        )
    ).group_by(effective_sp)
    sp_stmt = query_filters.active_orders(sp_stmt, FMaintenanceOrder)
    sp_stmt = apply_filters(sp_stmt)
    people = [
        {
            "salesperson": r.salesperson,
            "order_count": int(r.order_count or 0),
            "qty": Decimal(r.qty or 0),
            "return_qty": Decimal(r.return_qty or 0),
            "cost_inc": Decimal(r.cost_inc or 0),
            "cost_ex": Decimal(r.cost_ex or 0),
        }
        for r in db.execute(sp_stmt).all()
    ]
    if can_cost and total_cost_inc:
        people.sort(key=lambda t: (-t["cost_inc"], t["salesperson"] or ""))
    else:
        people.sort(key=lambda t: (-t["qty"], t["salesperson"] or ""))
    by_salesperson = [
        {
            "salesperson": t["salesperson"],
            "order_count": t["order_count"],
            "qty": str(t["qty"]),
            "effective_qty": str(t["qty"] - t["return_qty"]),
            "cost_inc": _cost_envelope(
                t["cost_inc"], can_cost=can_cost, wbdd_ready=wbdd_ready
            ),
            "cost_ex": _cost_envelope(
                t["cost_ex"], can_cost=can_cost, wbdd_ready=wbdd_ready
            ),
            "cost_share_pct": _cost_share_pct(
                t["cost_inc"], total_cost_inc, can_cost=can_cost
            ),
        }
        for t in people[:_SALESPERSON_LIMIT]
    ]

    # ---- 按项目汇总：单独一次分组查询（同筛选同口径，v1.35） ----
    # 按当前活跃挂靠的 assignment.project_id 分组；无活跃挂靠归 project_id=NULL
    # 行，display_name 渲染「未归属（无项目）」，业务类型档为 unassigned。
    proj_stmt = _spend_base(
        select(
            MaintenanceSourceOrderAssignment.project_id.label("project_id"),
            func.max(MaintenanceProject.display_name).label("display_name"),
            func.max(_business_type_split_case()).label("business_type"),
            func.count(func.distinct(FMaintenanceOrder.order_no)).label("order_count"),
            func.coalesce(func.sum(FMaintenanceLine.qty), Decimal("0")).label("qty"),
            func.coalesce(func.sum(FMaintenanceLine.return_qty), Decimal("0")).label(
                "return_qty"
            ),
            func.sum(cost_inc).label("cost_inc"),
            func.sum(cost_ex).label("cost_ex"),
        )
    ).group_by(MaintenanceSourceOrderAssignment.project_id)
    proj_stmt = query_filters.active_orders(proj_stmt, FMaintenanceOrder)
    proj_stmt = apply_filters(proj_stmt)
    project_rows = [
        {
            "project_id": r.project_id,
            "display_name": r.display_name,
            "business_type_code": r.business_type,
            "order_count": int(r.order_count or 0),
            "qty": Decimal(r.qty or 0),
            "return_qty": Decimal(r.return_qty or 0),
            "cost_inc": Decimal(r.cost_inc or 0),
            "cost_ex": Decimal(r.cost_ex or 0),
        }
        for r in db.execute(proj_stmt).all()
    ]
    if can_cost and total_cost_inc:
        project_rows.sort(key=lambda t: (-t["cost_inc"], t["display_name"] or ""))
    else:
        # 无成本权限或全部缺价时按数量排序：成本序本身会泄露受限金额
        project_rows.sort(key=lambda t: (-t["qty"], t["display_name"] or ""))
    by_project = [
        {
            "project_id": t["project_id"],
            "display_name": t["display_name"] or "未归属（无项目）",
            "business_type_code": t["business_type_code"],
            "business_type_label": _BUSINESS_TYPE_SPLIT_LABELS.get(
                t["business_type_code"], t["business_type_code"]
            ),
            "order_count": t["order_count"],
            "qty": str(t["qty"]),
            "effective_qty": str(t["qty"] - t["return_qty"]),
            "cost_inc": _cost_envelope(
                t["cost_inc"], can_cost=can_cost, wbdd_ready=wbdd_ready
            ),
            "cost_ex": _cost_envelope(
                t["cost_ex"], can_cost=can_cost, wbdd_ready=wbdd_ready
            ),
            "cost_share_pct": _cost_share_pct(
                t["cost_inc"], total_cost_inc, can_cost=can_cost
            ),
        }
        for t in project_rows[:_PROJECT_LIMIT]
    ]

    return {
        "granularity": granularity,
        "window": {
            "range": range_,
            "date_from": start.isoformat() if start else None,
            "date_to": end.isoformat() if end else None,
        },
        "buckets": buckets,
        "by_business_type": by_business_type,
        "by_salesperson": by_salesperson,
        "by_project": by_project,
        "summary": {
            "bucket_count": len(buckets),
            "order_count": sum((t["order_count"] for t in raw_rows), 0),
            "qty": str(sum((t["qty"] for t in raw_rows), Decimal("0"))),
            "effective_qty": str(
                sum((t["qty"] - t["return_qty"] for t in raw_rows), Decimal("0"))
            ),
            "missing_lines": sum((t["missing_lines"] for t in raw_rows), 0),
            "total_cost_inc": _cost_envelope(
                total_cost_inc, can_cost=can_cost, wbdd_ready=wbdd_ready
            ),
            "total_cost_ex": _cost_envelope(
                sum((t["cost_ex"] for t in raw_rows), Decimal("0")),
                can_cost=can_cost,
                wbdd_ready=wbdd_ready,
            ),
            "wbdd_ready": wbdd_ready,
        },
    }

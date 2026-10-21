"""期间下单与每日已计消耗（M01/M03，R34 / D-0024）——独立小模块，只读。

不修改 ``maintenance_dashboard`` 主服务（R33 四视图）与任何业务写路径；
查询前后业务事实（成本金额、库存、领用行）保持不变。

- M01 期间下单：制单日期（``f_maintenance_order.order_date``）入期的有效需求单
  （已生效 + 未墓碑 + 活跃项目挂靠，沿 T4 ``summarize_demand_returns`` 口径）
  的有效行 ``Σcoalesce(qty,0)``，另列去重单数；**不扣退货、不扣免返**。
  无制单日期的有效单不入期间、不冒充入某天，按项目/单位独立
  ``undated_orders`` 桶报告（B-F12）。
- M03 每日每项目已计消耗：实际领用（映射有效 + confirmed/corrected + 行有效）
  数量全计；金额只读既有 ``cost_amount_ex_tax/inc_tax`` 已知小计，另报
  未计成本量／覆盖率／来源标注。**不回算、不覆盖成本、不减估值**；
  成本证据可能含估算，原取价证据可能晚于领用日（既有成本链，不为 R34 重算）。
- 单位（M12/B-F09）：数量与分母按 ``dim_part.unit`` 分桶（空/缺 = 待核桶），
  混合单位 → 平面数量/覆盖率 null + ``unit_breakdown``；金额是同币种小计，
  允许跨单位相加。**金额缺失语义（B-F11）**：聚合桶内没有任何已计成本行时
  ``cost_amount_*`` 键不存在（不是 0.00 冒充已知）；真实 0 元已计行输出
  "0.00" 保留。

金额键（cost_amount/unit_cost/coverage/sources）只在 ``include_amounts=True``
时出现；无金额权的调用方拿到纯数量视图（不是 0 值冒充）。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, case, func, select
from sqlalchemy.orm import Session

from app.models.dimensions import DimPart
from app.models.maintenance import (
    FMaintenanceLine,
    FMaintenanceOrder,
    MaintenanceDemandTombstone,
)
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_project_operations import (
    MaintenanceSiteIssue,
    MaintenanceSiteIssueLine,
)
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.services.maintenance_dashboard_units import (
    QtyBuckets,
    normalize_unit,
    sorted_unit_keys,
)

_ZERO = Decimal("0")
_QUANT = Decimal("0.001")
_MONEY = Decimal("0.01")
_COST_NOTE = (
    "只读既有已计成本（可能含估算，原取价证据可能晚于领用日）；"
    "不回算、不减返件估值、不覆盖成本链"
)
_UNDATED_NOTE = "无制单日期下单未计入期间（不猜日期）"


def _qty(value: Decimal) -> str:
    return format(value.quantize(_QUANT), "f")


def _money(value: Decimal) -> str:
    return format(value.quantize(_MONEY), "f")


def _qty_field(_field: str, value: Decimal) -> str:
    return _qty(value)


def _flat(value: Decimal | None) -> str | None:
    return None if value is None else _qty(value)


def _coverage(costed: Decimal, issued: Decimal) -> float | None:
    if issued <= 0:
        return None
    return float((costed / issued * 100).quantize(Decimal("0.1")))


def _order_conditions(start, end, *, undated: bool = False) -> list:
    """有效需求单口径；``start=None``（range=all）下界不设限。

    ``undated=True``（B-F12）：同一有效性（已生效 + 未墓碑 + 活跃挂靠）但
    ``order_date IS NULL`` 的单——无日期不入期间，单列报告。
    """
    tombstoned = select(1).where(
        MaintenanceDemandTombstone.source_order_id == FMaintenanceOrder.raw_order_id,
        MaintenanceDemandTombstone.restored_at.is_(None),
    ).exists()
    conditions = [
        FMaintenanceOrder.data_status == "已生效",
        ~tombstoned,
        MaintenanceSourceOrderAssignment.is_active.is_(True),
    ]
    if undated:
        return [*conditions, FMaintenanceOrder.order_date.is_(None)]
    conditions.extend([
        FMaintenanceOrder.order_date.is_not(None),
        FMaintenanceOrder.order_date <= end,
    ])
    if start is not None:
        conditions.append(FMaintenanceOrder.order_date >= start)
    return conditions


_ORDER_JOINS = (
    dict(
        target=MaintenanceSourceOrderAssignment,
        onclause=and_(
            MaintenanceSourceOrderAssignment.source_order_id
            == FMaintenanceOrder.raw_order_id,
            MaintenanceSourceOrderAssignment.is_active.is_(True),
        ),
    ),
    dict(
        target=FMaintenanceLine,
        onclause=FMaintenanceLine.order_id == FMaintenanceOrder.id,
    ),
)


def period_orders(
    db: Session,
    *,
    start,
    end,
    projects: dict[str, MaintenanceProject],
) -> dict:
    """M01 期间下单：项目行 + 合计卡（数量域，无金额键）。

    ``projects`` 为调用方已按授权交集收敛的项目宇宙：空 dict → 空结果，
    绝不扩全量。qty 用原始行数量（不扣退货/免返）；单数按需求单去重。
    数量按单位分桶（B-F09）；无日期单按项目单列（B-F12）。
    """

    def _empty() -> dict:
        return {
            "cards": {
                "order_qty": _qty(_ZERO), "unit": None, "unit_breakdown": [],
                "order_count": 0,
                "undated_orders": {
                    "order_qty": _qty(_ZERO), "order_count": 0,
                    "unit_breakdown": [],
                    "note": _UNDATED_NOTE,
                },
            },
            "rows": [],
        }

    if not projects:
        return _empty()
    project_filter = MaintenanceSourceOrderAssignment.project_id.in_(projects)
    count_rows = db.execute(
        select(
            MaintenanceSourceOrderAssignment.project_id.label("project_id"),
            func.count(func.distinct(FMaintenanceOrder.id)).label("order_count"),
        )
        .select_from(FMaintenanceOrder)
        .join(**_ORDER_JOINS[0])
        .join(**_ORDER_JOINS[1])
        .where(
            *_order_conditions(start, end),
            FMaintenanceLine.is_active.is_(True),
            project_filter,
        )
        .group_by(MaintenanceSourceOrderAssignment.project_id)
    ).all()
    counts = {row.project_id: int(row.order_count) for row in count_rows}
    unit_rows = db.execute(
        select(
            MaintenanceSourceOrderAssignment.project_id.label("project_id"),
            DimPart.unit.label("unit"),
            func.coalesce(func.sum(FMaintenanceLine.qty), _ZERO).label("order_qty"),
        )
        .select_from(FMaintenanceOrder)
        .join(**_ORDER_JOINS[0])
        .join(**_ORDER_JOINS[1])
        .outerjoin(DimPart, DimPart.id == FMaintenanceLine.part_id)
        .where(
            *_order_conditions(start, end),
            FMaintenanceLine.is_active.is_(True),
            project_filter,
        )
        .group_by(
            MaintenanceSourceOrderAssignment.project_id, DimPart.unit
        )
    ).all()
    undated_unit_rows = db.execute(
        select(
            MaintenanceSourceOrderAssignment.project_id.label("project_id"),
            DimPart.unit.label("unit"),
            func.coalesce(func.sum(FMaintenanceLine.qty), _ZERO).label("order_qty"),
        )
        .select_from(FMaintenanceOrder)
        .join(**_ORDER_JOINS[0])
        .join(**_ORDER_JOINS[1])
        .outerjoin(DimPart, DimPart.id == FMaintenanceLine.part_id)
        .where(
            *_order_conditions(start, end, undated=True),
            FMaintenanceLine.is_active.is_(True),
            project_filter,
        )
        .group_by(
            MaintenanceSourceOrderAssignment.project_id, DimPart.unit
        )
    ).all()
    undated_count_rows = db.execute(
        select(
            MaintenanceSourceOrderAssignment.project_id.label("project_id"),
            func.count(func.distinct(FMaintenanceOrder.id)).label("order_count"),
        )
        .select_from(FMaintenanceOrder)
        .join(**_ORDER_JOINS[0])
        .join(**_ORDER_JOINS[1])
        .where(
            *_order_conditions(start, end, undated=True),
            FMaintenanceLine.is_active.is_(True),
            project_filter,
        )
        .group_by(MaintenanceSourceOrderAssignment.project_id)
    ).all()
    undated_counts = {
        row.project_id: int(row.order_count) for row in undated_count_rows
    }

    def _fill(rows_raw) -> dict[str, QtyBuckets]:
        buckets: dict[str, QtyBuckets] = {}
        for row in rows_raw:
            bucket = buckets.setdefault(row.project_id, QtyBuckets("order_qty"))
            bucket.add(normalize_unit(row.unit), order_qty=Decimal(row.order_qty))
        return buckets

    dated_by_project = _fill(unit_rows)
    undated_by_project = _fill(undated_unit_rows)
    total_buckets = QtyBuckets("order_qty")
    undated_total = QtyBuckets("order_qty")
    total_count = 0
    undated_total_count = 0
    rows = []
    for project_id, project in projects.items():
        buckets = dated_by_project.get(project_id, QtyBuckets("order_qty"))
        undated_buckets = undated_by_project.get(
            project_id, QtyBuckets("order_qty")
        )
        count = counts.get(project_id, 0)
        undated_count = undated_counts.get(project_id, 0)
        for unit, fields in buckets.items().items():
            total_buckets.add(unit, order_qty=fields["order_qty"])
        for unit, fields in undated_buckets.items().items():
            undated_total.add(unit, order_qty=fields["order_qty"])
        total_count += count
        undated_total_count += undated_count
        if (
            buckets.total("order_qty") == 0 and count == 0
            and undated_buckets.total("order_qty") == 0 and undated_count == 0
        ):
            continue
        rows.append({
            "project_id": project_id,
            "project_code": project.project_code,
            "display_name": project.display_name,
            "business_type": project.business_type,
            "order_qty": _flat(buckets.flat("order_qty")),
            "unit": buckets.single_unit(),
            "unit_breakdown": buckets.breakdown(
                "order_qty", formatter=_qty_field
            ),
            "order_count": count,
            "undated_order_qty": _flat(undated_buckets.flat("order_qty")),
            "undated_order_count": undated_count,
            # 无日期桶也按单位（静态审查补充：混合不并算，导出按单位展开）。
            "undated_unit_breakdown": undated_buckets.breakdown(
                "order_qty", formatter=_qty_field
            ),
        })
    return {
        "cards": {
            "order_qty": _flat(total_buckets.flat("order_qty")),
            "unit": total_buckets.single_unit(),
            "unit_breakdown": total_buckets.breakdown(
                "order_qty", formatter=_qty_field
            ),
            "order_count": total_count,
            "undated_orders": {
                "order_qty": _flat(undated_total.flat("order_qty")),
                "order_count": undated_total_count,
                # v3：无日期卡同形单位桶（混合平面 null 时 UI 可消费）。
                "unit_breakdown": undated_total.breakdown(
                    "order_qty", formatter=_qty_field
                ),
                "note": _UNDATED_NOTE,
            },
        },
        "rows": rows,
    }


def _issue_conditions(start, end, projects) -> list:
    """M03 领用有效口径：映射有效 + confirmed/corrected + 行有效 + 期间。

    ``start=None``（range=all）下界不设限。
    """
    conditions = [
        MaintenanceSiteIssue.status_mapping_state == "mapped",
        MaintenanceSiteIssue.normalized_status.in_(("confirmed", "corrected")),
        MaintenanceSiteIssueLine.is_active.is_(True),
        MaintenanceSiteIssue.issue_date <= end,
        MaintenanceSiteIssue.project_id.in_(projects),
    ]
    if start is not None:
        conditions.append(MaintenanceSiteIssue.issue_date >= start)
    return conditions


def daily_consumption(
    db: Session,
    *,
    start,
    end,
    projects: dict[str, MaintenanceProject],
    include_amounts: bool,
) -> dict:
    """M03 每日每项目已计消耗：日×项目行 + 合计卡（B-F09 单位分桶）。

    无金额权时只有数量键；金额/覆盖/来源键整个不出现。
    金额键仅当聚合桶内有 ≥1 条已计行才出现（B-F11，未知不冒充 0.00）。
    """
    cards: dict[str, Any] = {
        "issued_qty": _qty(_ZERO), "unit": None, "unit_breakdown": [],
    }
    if include_amounts:
        cards["uncosted_qty"] = _qty(_ZERO)
        cards["coverage_pct"] = None
        cards["sources"] = []
        cards["note"] = _COST_NOTE
    if not projects:
        return {"cards": cards, "rows": []}

    costed = MaintenanceSiteIssueLine.cost_amount_inc_tax.is_not(None)
    base = (
        select(
            MaintenanceSiteIssue.issue_date.label("issue_date"),
            MaintenanceSiteIssue.project_id.label("project_id"),
            DimPart.unit.label("unit"),
            func.sum(MaintenanceSiteIssueLine.quantity).label("issued_qty"),
            func.sum(case(
                (costed, MaintenanceSiteIssueLine.quantity), else_=_ZERO,
            )).label("costed_qty"),
            func.sum(case(
                (costed, MaintenanceSiteIssueLine.cost_amount_ex_tax), else_=_ZERO,
            )).label("amount_ex"),
            func.sum(case(
                (costed, MaintenanceSiteIssueLine.cost_amount_inc_tax), else_=_ZERO,
            )).label("amount_inc"),
            func.bool_or(costed).label("has_costed"),
            func.count().label("line_count"),
        )
        .select_from(MaintenanceSiteIssueLine)
        .join(
            MaintenanceSiteIssue,
            MaintenanceSiteIssue.issue_id == MaintenanceSiteIssueLine.issue_id,
        )
        .outerjoin(DimPart, DimPart.id == MaintenanceSiteIssueLine.part_id)
        .where(*_issue_conditions(start, end, list(projects)))
        .group_by(
            MaintenanceSiteIssue.issue_date,
            MaintenanceSiteIssue.project_id,
            DimPart.unit,
        )
    )
    raw_rows = db.execute(base).all()

    sources_by_cell: dict[tuple[date, str, str | None], set[str]] = defaultdict(set)
    if include_amounts:
        source_rows = db.execute(
            select(
                MaintenanceSiteIssue.issue_date.label("issue_date"),
                MaintenanceSiteIssue.project_id.label("project_id"),
                DimPart.unit.label("unit"),
                MaintenanceSiteIssueLine.cost_source.label("source"),
            )
            .distinct()
            .select_from(MaintenanceSiteIssueLine)
            .join(
                MaintenanceSiteIssue,
                MaintenanceSiteIssue.issue_id == MaintenanceSiteIssueLine.issue_id,
            )
            .outerjoin(DimPart, DimPart.id == MaintenanceSiteIssueLine.part_id)
            .where(*_issue_conditions(start, end, list(projects)), costed)
        ).all()
        for row in source_rows:
            if row.source:
                sources_by_cell[
                    (row.issue_date, row.project_id, normalize_unit(row.unit))
                ].add(row.source)

    # （日 × 项目）→ 单位桶：数量分桶不混算；金额跨单位同币种小计。
    cell_units: dict[tuple[date, str], dict[str | None, dict[str, Any]]] = (
        defaultdict(dict)
    )
    for row in raw_rows:
        unit = normalize_unit(row.unit)
        bucket = cell_units[(row.issue_date, row.project_id)].setdefault(unit, {
            "issued": _ZERO, "costed": _ZERO,
            "amount_ex": _ZERO, "amount_inc": _ZERO,
            "has_costed": False, "line_count": 0,
        })
        bucket["issued"] += Decimal(row.issued_qty or 0)
        bucket["costed"] += Decimal(row.costed_qty or 0)
        bucket["amount_ex"] += Decimal(row.amount_ex or 0)
        bucket["amount_inc"] += Decimal(row.amount_inc or 0)
        bucket["has_costed"] = bucket["has_costed"] or bool(row.has_costed)
        bucket["line_count"] += int(row.line_count)

    rows = []
    window_units: dict[str | None, dict[str, Any]] = {}
    for (issue_day, project_id), units in cell_units.items():
        project = projects.get(project_id)
        mixed = len(units) > 1
        entry: dict[str, Any] = {
            "issue_date": issue_day.isoformat(),
            "project_id": project_id,
            "project_code": project.project_code if project else None,
            "display_name": project.display_name if project else project_id,
            "issued_qty": _flat(
                sum((b["issued"] for b in units.values()), _ZERO)
                if not mixed else None
            ),
            "unit": next(iter(units)) if not mixed else None,
            "unit_breakdown": [
                {
                    "unit": unit,
                    "issued_qty": _qty(units[unit]["issued"]),
                    **({
                        "costed_qty": _qty(units[unit]["costed"]),
                        "uncosted_qty": _qty(
                            units[unit]["issued"] - units[unit]["costed"]
                        ),
                        "coverage_pct": _coverage(
                            units[unit]["costed"], units[unit]["issued"]
                        ),
                        **({
                            "cost_amount_ex_tax": _money(units[unit]["amount_ex"]),
                            "cost_amount_inc_tax": _money(units[unit]["amount_inc"]),
                        } if include_amounts and units[unit]["has_costed"] else {}),
                    } if include_amounts else {}),
                }
                for unit in sorted_unit_keys(units.keys())
            ],
            "line_count": sum((b["line_count"] for b in units.values()), 0),
        }
        if include_amounts:
            cell_costed = any(b["has_costed"] for b in units.values())
            cell_issued = sum((b["issued"] for b in units.values()), _ZERO)
            cell_costed_qty = sum((b["costed"] for b in units.values()), _ZERO)
            entry.update({
                "costed_qty": _flat(cell_costed_qty if not mixed else None),
                "uncosted_qty": _flat(
                    (cell_issued - cell_costed_qty) if not mixed else None
                ),
                "coverage_pct": (
                    _coverage(cell_costed_qty, cell_issued)
                    if not mixed else None
                ),
                "sources": sorted({
                    source
                    for unit in units
                    for source in sources_by_cell.get(
                        (issue_day, project_id, unit), ()
                    )
                }),
            })
            # B-F11：无任何已计行 → 金额键不存在（未知 ≠ 0.00）；
            # 金额为跨单位同币种小计（真实 0 元已计行保留 "0.00"）。
            if cell_costed:
                entry["cost_amount_ex_tax"] = _money(
                    sum((b["amount_ex"] for b in units.values()), _ZERO)
                )
                entry["cost_amount_inc_tax"] = _money(
                    sum((b["amount_inc"] for b in units.values()), _ZERO)
                )
        rows.append(entry)
        for unit, bucket in units.items():
            window = window_units.setdefault(unit, {
                "issued": _ZERO, "costed": _ZERO,
                "amount_ex": _ZERO, "amount_inc": _ZERO,
                "has_costed": False,
            })
            window["issued"] += bucket["issued"]
            window["costed"] += bucket["costed"]
            window["amount_ex"] += bucket["amount_ex"]
            window["amount_inc"] += bucket["amount_inc"]
            window["has_costed"] = window["has_costed"] or bucket["has_costed"]
    rows.sort(key=lambda item: (item["issue_date"], item["project_id"]), reverse=True)

    window_mixed = len(window_units) > 1
    cards["issued_qty"] = _flat(
        sum((b["issued"] for b in window_units.values()), _ZERO)
        if not window_mixed else None
    )
    # 空窗口（范围内无领用行）：window_units 为空 dict——next(iter({})) 会
    # StopIteration→500（B3 独立验收反例）；合法空 shape=unit None/空桶，
    # 与空范围早退分支同形（issued_qty 下面重算为 "0.000"）。
    cards["unit"] = (
        next(iter(window_units))
        if window_units and not window_mixed else None
    )
    if include_amounts:
        window_issued = sum((b["issued"] for b in window_units.values()), _ZERO)
        window_costed = sum((b["costed"] for b in window_units.values()), _ZERO)
        cards["uncosted_qty"] = _flat(
            (window_issued - window_costed) if not window_mixed else None
        )
        cards["coverage_pct"] = (
            _coverage(window_costed, window_issued)
            if not window_mixed else None
        )
        cards["sources"] = sorted({
            source for (_d, _p, unit), sources in sources_by_cell.items()
            if unit in window_units for source in sources
        })
        cards["unit_breakdown"] = [
            {
                "unit": unit,
                "issued_qty": _qty(window_units[unit]["issued"]),
                "costed_qty": _qty(window_units[unit]["costed"]),
                "uncosted_qty": _qty(
                    window_units[unit]["issued"] - window_units[unit]["costed"]
                ),
                "coverage_pct": _coverage(
                    window_units[unit]["costed"], window_units[unit]["issued"]
                ),
            }
            for unit in sorted_unit_keys(window_units.keys())
        ]
        if any(b["has_costed"] for b in window_units.values()):
            cards["cost_amount_ex_tax"] = _money(
                sum((b["amount_ex"] for b in window_units.values()), _ZERO)
            )
            cards["cost_amount_inc_tax"] = _money(
                sum((b["amount_inc"] for b in window_units.values()), _ZERO)
            )
    else:
        cards["unit_breakdown"] = [
            {
                "unit": unit,
                "issued_qty": _qty(window_units[unit]["issued"]),
            }
            for unit in sorted_unit_keys(window_units.keys())
        ]
    return {"cards": cards, "rows": rows}


def consumption_issue_rows(
    db: Session,
    *,
    start,
    end,
    projects: dict[str, MaintenanceProject],
    include_amounts: bool,
    issue_date=None,
    project_id: str | None = None,
) -> list[dict]:
    """领用明细（日×项目下钻）：行级数量与（有权时）成本证据。

    行级 ``unit``（主数据主单位，空 = 待核）为数量域字段，全角色可见。
    """
    if not projects:
        return []
    conditions = _issue_conditions(start, end, list(projects))
    if issue_date is not None:
        conditions.append(MaintenanceSiteIssue.issue_date == issue_date)
    if project_id is not None:
        conditions.append(MaintenanceSiteIssue.project_id == project_id)
    lines = db.execute(
        select(
            MaintenanceSiteIssue.issue_id,
            MaintenanceSiteIssue.issue_no,
            MaintenanceSiteIssue.issue_date,
            MaintenanceSiteIssue.project_id,
            MaintenanceSiteIssueLine.issue_line_id,
            MaintenanceSiteIssueLine.part_id,
            MaintenanceSiteIssueLine.pn,
            MaintenanceSiteIssueLine.quantity,
            MaintenanceSiteIssueLine.cost_source,
            MaintenanceSiteIssueLine.cost_amount_ex_tax,
            MaintenanceSiteIssueLine.cost_amount_inc_tax,
            MaintenanceSiteIssueLine.unit_cost_ex_tax,
            MaintenanceSiteIssueLine.unit_cost_inc_tax,
            MaintenanceSiteIssueLine.reference_side,
            MaintenanceSiteIssueLine.algorithm_version,
            DimPart.unit.label("unit"),
        )
        .select_from(MaintenanceSiteIssueLine)
        .join(
            MaintenanceSiteIssue,
            MaintenanceSiteIssue.issue_id == MaintenanceSiteIssueLine.issue_id,
        )
        .outerjoin(DimPart, DimPart.id == MaintenanceSiteIssueLine.part_id)
        .where(*conditions)
        .order_by(
            MaintenanceSiteIssue.issue_date.desc(),
            MaintenanceSiteIssue.project_id,
            MaintenanceSiteIssueLine.issue_line_id,
        )
    ).all()
    rows = []
    for line in lines:
        project = projects.get(line.project_id)
        entry: dict[str, Any] = {
            "issue_line_id": line.issue_line_id,
            "issue_id": line.issue_id,
            "issue_no": line.issue_no,
            "issue_date": line.issue_date.isoformat(),
            "project_id": line.project_id,
            "project_code": project.project_code if project else None,
            "display_name": (
                project.display_name if project else line.project_id
            ),
            "part_id": line.part_id,
            "pn": line.pn,
            "qty": _qty(Decimal(line.quantity)),
            "unit": normalize_unit(line.unit),
        }
        if include_amounts:
            entry["cost_source"] = line.cost_source
            entry["algorithm_version"] = line.algorithm_version
            entry["reference_side"] = line.reference_side
            if line.cost_amount_ex_tax is not None:
                entry["cost_amount_ex_tax"] = _money(Decimal(line.cost_amount_ex_tax))
                entry["cost_amount_inc_tax"] = _money(
                    Decimal(line.cost_amount_inc_tax)
                )
                entry["unit_cost_ex_tax"] = _money(Decimal(line.unit_cost_ex_tax))
                entry["unit_cost_inc_tax"] = _money(Decimal(line.unit_cost_inc_tax))
        rows.append(entry)
    return rows

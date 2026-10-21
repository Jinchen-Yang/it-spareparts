"""期间下单与每日已计消耗（M01/M03，R34 / D-0024）——独立小模块，只读。

不修改 ``maintenance_dashboard`` 主服务（R33 四视图）与任何业务写路径；
查询前后业务事实（成本金额、库存、领用行）保持不变。

- M01 期间下单：制单日期（``f_maintenance_order.order_date``）入期的有效需求单
  （已生效 + 未墓碑 + 活跃项目挂靠，沿 T4 ``summarize_demand_returns`` 口径）
  的有效行 ``Σcoalesce(qty,0)``，另列去重单数；**不扣退货、不扣免返**。
- M03 每日每项目已计消耗：实际领用（映射有效 + confirmed/corrected + 行有效）
  数量全计；金额只读既有 ``cost_amount_ex_tax/inc_tax`` 已知小计，另报
  未计成本量／覆盖率／来源标注。**不回算、不覆盖成本、不减估值**；
  成本证据可能含估算，原取价证据可能晚于领用日（既有成本链，不为 R34 重算）。

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

_ZERO = Decimal("0")
_QUANT = Decimal("0.001")
_MONEY = Decimal("0.01")
_COST_NOTE = (
    "只读既有已计成本（可能含估算，原取价证据可能晚于领用日）；"
    "不回算、不减返件估值、不覆盖成本链"
)


def _qty(value: Decimal) -> str:
    return format(value.quantize(_QUANT), "f")


def _money(value: Decimal) -> str:
    return format(value.quantize(_MONEY), "f")


def _order_conditions(start, end) -> list:
    """有效需求单口径；``start=None``（range=all）下界不设限。"""
    tombstoned = select(1).where(
        MaintenanceDemandTombstone.source_order_id == FMaintenanceOrder.raw_order_id,
        MaintenanceDemandTombstone.restored_at.is_(None),
    ).exists()
    conditions = [
        FMaintenanceOrder.data_status == "已生效",
        ~tombstoned,
        FMaintenanceOrder.order_date.is_not(None),
        FMaintenanceOrder.order_date <= end,
        MaintenanceSourceOrderAssignment.is_active.is_(True),
    ]
    if start is not None:
        conditions.append(FMaintenanceOrder.order_date >= start)
    return conditions


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
    """
    if not projects:
        return {"cards": {"order_qty": _qty(_ZERO), "order_count": 0}, "rows": []}
    rows_raw = db.execute(
        select(
            MaintenanceSourceOrderAssignment.project_id.label("project_id"),
            func.coalesce(func.sum(FMaintenanceLine.qty), _ZERO).label("order_qty"),
            func.count(func.distinct(FMaintenanceOrder.id)).label("order_count"),
        )
        .select_from(FMaintenanceOrder)
        .join(
            MaintenanceSourceOrderAssignment,
            and_(
                MaintenanceSourceOrderAssignment.source_order_id
                == FMaintenanceOrder.raw_order_id,
                MaintenanceSourceOrderAssignment.is_active.is_(True),
                MaintenanceSourceOrderAssignment.project_id.in_(projects),
            ),
        )
        .join(FMaintenanceLine, FMaintenanceLine.order_id == FMaintenanceOrder.id)
        .where(*_order_conditions(start, end), FMaintenanceLine.is_active.is_(True))
        .group_by(MaintenanceSourceOrderAssignment.project_id)
    ).all()
    by_project = {row.project_id: row for row in rows_raw}
    rows = []
    total_qty = _ZERO
    total_orders = 0
    for project_id, project in projects.items():
        row = by_project.get(project_id)
        qty = Decimal(row.order_qty) if row is not None else _ZERO
        count = int(row.order_count) if row is not None else 0
        total_qty += qty
        total_orders += count
        if qty == 0 and count == 0:
            continue
        rows.append({
            "project_id": project_id,
            "project_code": project.project_code,
            "display_name": project.display_name,
            "business_type": project.business_type,
            "order_qty": _qty(qty),
            "order_count": count,
        })
    return {
        "cards": {"order_qty": _qty(total_qty), "order_count": total_orders},
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
    """M03 每日每项目已计消耗：日×项目行 + 合计卡。

    无金额权时只有数量键；金额/覆盖/来源键整个不出现。
    """
    cards: dict[str, Any] = {}
    rows: list[dict] = []
    if not projects:
        cards["issued_qty"] = _qty(_ZERO)
        if include_amounts:
            cards.update({
                "cost_amount_ex_tax": _money(_ZERO),
                "cost_amount_inc_tax": _money(_ZERO),
                "uncosted_qty": _qty(_ZERO),
                "coverage_pct": None,
                "sources": [],
                "note": _COST_NOTE,
            })
        return {"cards": cards, "rows": rows}

    costed = MaintenanceSiteIssueLine.cost_amount_inc_tax.is_not(None)
    base = (
        select(
            MaintenanceSiteIssue.issue_date.label("issue_date"),
            MaintenanceSiteIssue.project_id.label("project_id"),
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
            func.count().label("line_count"),
        )
        .select_from(MaintenanceSiteIssueLine)
        .join(
            MaintenanceSiteIssue,
            MaintenanceSiteIssue.issue_id == MaintenanceSiteIssueLine.issue_id,
        )
        .where(*_issue_conditions(start, end, list(projects)))
        .group_by(MaintenanceSiteIssue.issue_date, MaintenanceSiteIssue.project_id)
    )
    raw_rows = db.execute(base).all()

    sources_by_cell: dict[tuple[date, str], set[str]] = defaultdict(set)
    if include_amounts:
        source_rows = db.execute(
            select(
                MaintenanceSiteIssue.issue_date.label("issue_date"),
                MaintenanceSiteIssue.project_id.label("project_id"),
                MaintenanceSiteIssueLine.cost_source.label("source"),
            )
            .distinct()
            .select_from(MaintenanceSiteIssueLine)
            .join(
                MaintenanceSiteIssue,
                MaintenanceSiteIssue.issue_id == MaintenanceSiteIssueLine.issue_id,
            )
            .where(*_issue_conditions(start, end, list(projects)), costed)
        ).all()
        for row in source_rows:
            if row.source:
                sources_by_cell[(row.issue_date, row.project_id)].add(row.source)

    total_qty = total_ex = total_inc = total_uncosted = _ZERO
    all_sources: set[str] = set()
    for row in raw_rows:
        project = projects.get(row.project_id)
        project_code = project.project_code if project else None
        display_name = project.display_name if project else row.project_id
        issued = Decimal(row.issued_qty or 0)
        costed_qty = Decimal(row.costed_qty or 0)
        amount_ex = Decimal(row.amount_ex or 0)
        amount_inc = Decimal(row.amount_inc or 0)
        uncosted = issued - costed_qty
        total_qty += issued
        total_ex += amount_ex
        total_inc += amount_inc
        total_uncosted += uncosted
        cell_sources = sorted(sources_by_cell.get((row.issue_date, row.project_id), ()))
        all_sources.update(cell_sources)
        entry: dict[str, Any] = {
            "issue_date": row.issue_date.isoformat(),
            "project_id": row.project_id,
            "project_code": project_code,
            "display_name": display_name,
            "issued_qty": _qty(issued),
            "line_count": int(row.line_count),
        }
        if include_amounts:
            entry.update({
                "cost_amount_ex_tax": _money(amount_ex),
                "cost_amount_inc_tax": _money(amount_inc),
                "costed_qty": _qty(costed_qty),
                "uncosted_qty": _qty(uncosted),
                "coverage_pct": (
                    float((costed_qty / issued * 100).quantize(Decimal("0.1")))
                    if issued > 0 else None
                ),
                "sources": cell_sources,
            })
        rows.append(entry)
    rows.sort(key=lambda item: (item["issue_date"], item["project_id"]), reverse=True)

    cards["issued_qty"] = _qty(total_qty)
    if include_amounts:
        cards.update({
            "cost_amount_ex_tax": _money(total_ex),
            "cost_amount_inc_tax": _money(total_inc),
            "uncosted_qty": _qty(total_uncosted),
            "coverage_pct": (
                float(((total_qty - total_uncosted) / total_qty * 100)
                      .quantize(Decimal("0.1")))
                if total_qty > 0 else None
            ),
            "sources": sorted(all_sources),
            "note": _COST_NOTE,
        })
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
    """领用明细（日×项目下钻）：行级数量与（有权时）成本证据。"""
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
        )
        .select_from(MaintenanceSiteIssueLine)
        .join(
            MaintenanceSiteIssue,
            MaintenanceSiteIssue.issue_id == MaintenanceSiteIssueLine.issue_id,
        )
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

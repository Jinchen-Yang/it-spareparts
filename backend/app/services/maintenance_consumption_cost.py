"""Reproducible unit-cost resolution for confirmed site consumption.

The waterfall is intentionally strict and deterministic:
maint demand line (exact link / same order / latest same PN) -> direct purchase
line -> all valid purchase samples in ±7 days -> all valid sales samples in
±7 days -> manual fill -> unresolved.  Returns are outside the scope of this
fact chain and therefore never offset consumption.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Iterable

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app import config
from app import tax_policy
from app.models.maintenance_project_operations import MaintenanceSiteIssueLine
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.sales import FSalesLine, FSalesOrder


ALGORITHM_VERSION = "site-issue-cost-v2.1"
# v2: 维保需求单价格成为最强证据层；v2.1（2026-08-24 修复）：单价改净数量口径
# （cost_amount_ex_tax ÷ (qty−return_qty)，消除部分退货压价）+ 匹配优先精确行/
# 同单（领用单号=WBDD 单号），同项目同 PN 取最新一单只作兜底。
_CENT = Decimal("0.01")
_MONEY_MAX_EXCLUSIVE = Decimal("1000000000000")
_QUANTITY_MAX_EXCLUSIVE = Decimal("100000000000")


class CostResolutionError(ValueError):
    """A resolved monetary value cannot be represented by Numeric(14,2)."""


def _amount(value: Decimal, *, label: str = "成本单价") -> Decimal:
    if not value.is_finite():
        raise CostResolutionError(f"现场领用{label}超出允许范围")
    try:
        normalized = value.quantize(_CENT, rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise CostResolutionError(f"现场领用{label}超出允许范围") from exc
    if (
        not normalized.is_finite()
        or normalized < 0
        or normalized >= _MONEY_MAX_EXCLUSIVE
    ):
        raise CostResolutionError(f"现场领用{label}超出允许范围")
    return normalized


def _valid(qty: Decimal | None, unit_price: Decimal | None) -> bool:
    if qty is None or unit_price is None:
        return False
    normalized_qty = Decimal(qty)
    normalized_price = Decimal(unit_price)
    return (
        normalized_qty.is_finite()
        and normalized_price.is_finite()
        and normalized_qty > 0
        and normalized_price > 0
        and normalized_qty < _QUANTITY_MAX_EXCLUSIVE
        and normalized_price < _MONEY_MAX_EXCLUSIVE
    )


def _weighted(samples: list[dict]) -> Decimal | None:
    valid = [
        sample
        for sample in samples
        if _valid(sample["quantity"], sample["unit_price_ex_tax"])
    ]
    total_qty = sum(
        (Decimal(sample["quantity"]) for sample in valid), start=Decimal("0")
    )
    if total_qty <= 0:
        return None
    return _amount(
        sum(
            (
                Decimal(sample["quantity"]) * Decimal(sample["unit_price_ex_tax"])
                for sample in valid
            ),
            start=Decimal("0"),
        )
        / total_qty
    )


def _direct_purchase(
    db: Session, line: MaintenanceSiteIssueLine, *, issue_date: date
) -> tuple[Decimal, list[dict]] | None:
    if line.linked_purchase_line_id is None:
        return None
    sample = db.execute(
        select(
            FPurchaseLine.id,
            FPurchaseLine.part_id,
            FPurchaseLine.qty,
            FPurchaseLine.unit_price,
            FPurchaseOrder.is_tax_inclusive,
            FPurchaseOrder.order_no,
            FPurchaseOrder.order_date,
        )
        .join(FPurchaseOrder, FPurchaseOrder.id == FPurchaseLine.order_id)
        .where(
            FPurchaseLine.id == line.linked_purchase_line_id,
            FPurchaseOrder.data_status == config.ACTIVE_STATUS,
        )
    ).one_or_none()
    if (
        sample is None
        or sample.part_id != line.part_id
        or not _valid(sample.qty, sample.unit_price)
    ):
        return None
    unit_ex = (
        tax_policy.ex_from_inc(sample.unit_price)
        if sample.is_tax_inclusive is True
        else _amount(Decimal(sample.unit_price))
    )
    return unit_ex, [
        {
            "sample_id": f"purchase:{sample.id}",
            "document_no": sample.order_no,
            "document_date": sample.order_date.isoformat()
            if sample.order_date
            else None,
            "distance_days": (
                abs((sample.order_date - issue_date).days)
                if sample.order_date
                else None
            ),
            "quantity": format(sample.qty, "f"),
            "unit_price_raw": format(sample.unit_price, "f"),
            "unit_price_ex_tax": format(unit_ex, "f"),
            "tax_conversion": "divide_1.13"
            if sample.is_tax_inclusive is True
            else "none",
        }
    ]


def _purchase_window(
    db: Session, *, part_id: int, issue_date: date, from_date: date, to_date: date
) -> tuple[Decimal, list[dict]] | None:
    raw_samples = list(
        db.execute(
            select(
                FPurchaseLine.id,
                FPurchaseLine.qty,
                FPurchaseLine.unit_price,
                FPurchaseOrder.is_tax_inclusive,
                FPurchaseOrder.order_no,
                FPurchaseOrder.order_date,
            )
            .join(FPurchaseOrder, FPurchaseOrder.id == FPurchaseLine.order_id)
            .where(
                FPurchaseLine.part_id == part_id,
                FPurchaseOrder.data_status == config.ACTIVE_STATUS,
                FPurchaseOrder.order_date >= from_date,
                FPurchaseOrder.order_date <= to_date,
                FPurchaseLine.qty > 0,
                FPurchaseLine.unit_price > 0,
            )
            .order_by(FPurchaseOrder.order_date, FPurchaseLine.id)
        )
    )
    samples = [
        {
            "sample_id": f"purchase:{row.id}",
            "document_no": row.order_no,
            "document_date": row.order_date.isoformat(),
            "distance_days": abs((row.order_date - issue_date).days),
            "quantity": format(row.qty, "f"),
            "unit_price_raw": format(row.unit_price, "f"),
            "unit_price_ex_tax": format(
                Decimal(row.unit_price) / tax_policy.TAX_FACTOR
                if row.is_tax_inclusive is True
                else _amount(Decimal(row.unit_price)),
                "f",
            ),
            "tax_conversion": "divide_1.13" if row.is_tax_inclusive is True else "none",
        }
        for row in raw_samples
        if _valid(row.qty, row.unit_price)
    ]
    value = _weighted(samples)
    if value is None:
        return None
    return value, samples


def _sales_window(
    db: Session, *, part_id: int, issue_date: date, from_date: date, to_date: date
) -> tuple[Decimal, list[dict]] | None:
    raw_samples = list(
        db.execute(
            select(
                FSalesLine.id,
                FSalesLine.qty,
                FSalesLine.unit_price,
                FSalesOrder.order_no,
                FSalesOrder.order_date,
            )
            .join(FSalesOrder, FSalesOrder.id == FSalesLine.order_id)
            .where(
                FSalesLine.part_id == part_id,
                FSalesOrder.data_status == config.ACTIVE_STATUS,
                FSalesOrder.order_date >= from_date,
                FSalesOrder.order_date <= to_date,
                FSalesLine.qty > 0,
                FSalesLine.unit_price > 0,
            )
            .order_by(FSalesOrder.order_date, FSalesLine.id)
        )
    )
    samples = [
        {
            "sample_id": f"sales:{row.id}",
            "document_no": row.order_no,
            "document_date": row.order_date.isoformat(),
            "distance_days": abs((row.order_date - issue_date).days),
            "quantity": format(row.qty, "f"),
            "unit_price_raw": format(row.unit_price, "f"),
            "unit_price_ex_tax": format(
                Decimal(row.unit_price) / tax_policy.TAX_FACTOR,
                "f",
            ),
            "tax_conversion": "divide_1.13",
        }
        for row in raw_samples
        if _valid(row.qty, row.unit_price)
    ]
    value = _weighted(samples)
    if value is None:
        return None
    return value, samples


def _purchase_sample(row, *, issue_date: date) -> dict:
    unit_ex = (
        Decimal(row.unit_price) / tax_policy.TAX_FACTOR
        if row.is_tax_inclusive is True
        else _amount(Decimal(row.unit_price))
    )
    return {
        "sample_id": f"purchase:{row.id}",
        "document_no": row.order_no,
        "document_date": row.order_date.isoformat() if row.order_date else None,
        "distance_days": (
            abs((row.order_date - issue_date).days) if row.order_date else None
        ),
        "quantity": format(row.qty, "f"),
        "unit_price_raw": format(row.unit_price, "f"),
        "unit_price_ex_tax": format(unit_ex, "f"),
        "tax_conversion": ("divide_1.13" if row.is_tax_inclusive is True else "none"),
    }


def _sales_sample(row, *, issue_date: date) -> dict:
    return {
        "sample_id": f"sales:{row.id}",
        "document_no": row.order_no,
        "document_date": row.order_date.isoformat(),
        "distance_days": abs((row.order_date - issue_date).days),
        "quantity": format(row.qty, "f"),
        "unit_price_raw": format(row.unit_price, "f"),
        "unit_price_ex_tax": format(
            Decimal(row.unit_price) / tax_policy.TAX_FACTOR,
            "f",
        ),
        "tax_conversion": "divide_1.13",
    }


def _apply_resolution(
    line: MaintenanceSiteIssueLine,
    *,
    issue_date: date,
    source: str | None,
    side: str | None,
    unit_cost: Decimal | None,
    samples: list[dict],
) -> MaintenanceSiteIssueLine:
    from_date = issue_date - timedelta(days=7)
    to_date = issue_date + timedelta(days=7)
    line.cost_source = source
    line.price_basis = "ex_tax"
    line.reference_side = side
    line.reference_samples = samples
    line.reference_sample_ids = [sample["sample_id"] for sample in samples]
    line.reference_sample_count = len(samples)
    line.reference_window_from = (
        from_date if source in {"purchase_window", "sales_window"} else None
    )
    line.reference_window_to = (
        to_date if source in {"purchase_window", "sales_window"} else None
    )
    line.algorithm_version = ALGORITHM_VERSION
    line.tax_rate_used = tax_policy.TAX_RATE
    line.manual_unit_cost_inc_tax = (
        _amount(
            tax_policy.inc_from_ex(Decimal(line.manual_unit_cost)),
            label="人工含税单价",
        )
        if line.manual_unit_cost is not None
        else None
    )
    if unit_cost is None:
        line.unit_cost = None
        line.cost_amount = None
        line.unit_cost_ex_tax = None
        line.unit_cost_inc_tax = None
        line.cost_amount_ex_tax = None
        line.cost_amount_inc_tax = None
        return line

    unit_cost_ex_tax = _amount(Decimal(unit_cost), label="未税成本单价")
    unit_cost_inc_tax = _amount(
        tax_policy.inc_from_ex(unit_cost_ex_tax),
        label="含税成本单价",
    )
    cost_amount_ex_tax = _amount(
        Decimal(line.quantity) * unit_cost_ex_tax,
        label="未税成本金额",
    )
    cost_amount_inc_tax = _amount(
        Decimal(line.quantity) * unit_cost_inc_tax,
        label="含税成本金额",
    )
    # Backward-compatible aliases are deliberately pinned to the ex-tax facts.
    line.unit_cost = unit_cost_ex_tax
    line.cost_amount = cost_amount_ex_tax
    line.unit_cost_ex_tax = unit_cost_ex_tax
    line.unit_cost_inc_tax = unit_cost_inc_tax
    line.cost_amount_ex_tax = cost_amount_ex_tax
    line.cost_amount_inc_tax = cost_amount_inc_tax
    return line


def _demand_price_maps(
    db: Session,
    *,
    project_ids: set[str],
    part_ids: set[int],
) -> tuple[
    dict[str, tuple[int, Decimal]],
    dict[tuple[str, str, int], Decimal],
    dict[tuple[str, int], Decimal],
]:
    """需求单价格层的三级匹配索引（2026-08-24 修复版）。

    单价 = cost_amount_ex_tax ÷ (qty − return_qty)：分子本身是退货冲抵后的
    金额，分母必须同步用净数量，否则部分退货行会把领用单价系统性压低；
    整行退净（净数量 ≤ 0）的行没有单价可提供，直接不入选。

    返回 (raw_line_id→(part_id, 单价), (project_id, order_no, part_id)→单价,
    (project_id, part_id)→最新一单单价)。消费侧按 精确行 → 同单同 PN →
    同项目同 PN 最新 的优先级取值。
    """
    by_raw_line: dict[str, tuple[int, Decimal]] = {}
    by_order: dict[tuple[str, str, int], Decimal] = {}
    by_part: dict[tuple[str, int], Decimal] = {}
    if not project_ids or not part_ids:
        return by_raw_line, by_order, by_part
    from app.models.maintenance_project import MaintenanceProject  # noqa: F401
    from app.models.maintenance_source_assignment import (
        MaintenanceSourceOrderAssignment,
    )

    demand_rows = db.execute(
        select(
            MaintenanceSourceOrderAssignment.project_id,
            FMaintenanceLine.raw_line_id,
            FMaintenanceLine.part_id,
            FMaintenanceLine.cost_amount_ex_tax,
            FMaintenanceLine.qty,
            FMaintenanceLine.return_qty,
            FMaintenanceOrder.order_no,
        )
        .select_from(FMaintenanceLine)
        .join(FMaintenanceOrder,
              FMaintenanceOrder.id == FMaintenanceLine.order_id)
        .join(MaintenanceSourceOrderAssignment, and_(
            MaintenanceSourceOrderAssignment.source_order_id
            == FMaintenanceOrder.raw_order_id,
            MaintenanceSourceOrderAssignment.is_active.is_(True),
        ))
        .where(
            MaintenanceSourceOrderAssignment.project_id.in_(project_ids),
            FMaintenanceLine.part_id.in_(part_ids),
            FMaintenanceLine.is_active.is_(True),
            FMaintenanceLine.cost_amount_ex_tax.isnot(None),
        )
        .order_by(FMaintenanceOrder.order_date.desc(), FMaintenanceLine.id.desc())
    ).all()
    for pid, raw_line_id, part_id, amount_ex, qty, return_qty, order_no in demand_rows:
        net_qty = Decimal(qty or 0) - Decimal(return_qty or 0)
        if net_qty <= 0:
            continue
        price = Decimal(amount_ex) / net_qty
        if not _valid(net_qty, price):
            continue
        normalized = _amount(price)
        by_raw_line.setdefault(raw_line_id, (part_id, normalized))
        by_order.setdefault((pid, order_no, part_id), normalized)
        by_part.setdefault((pid, part_id), normalized)
    return by_raw_line, by_order, by_part


def _pick_demand_unit(
    *,
    line: MaintenanceSiteIssueLine,
    project_id: str | None,
    issue_no: str | None,
    maps: tuple[
        dict[str, tuple[int, Decimal]],
        dict[tuple[str, str, int], Decimal],
        dict[tuple[str, int], Decimal],
    ],
) -> tuple[Decimal, str] | None:
    """单条领用行的需求单价格挑选：精确行 → 同单同 PN → 同项目最新一单。"""
    by_raw_line, by_order, by_part = maps
    if not project_id:
        return None
    exact = by_raw_line.get(getattr(line, "source_line_id", None) or "")
    if exact is not None and exact[0] == line.part_id:
        return exact[1], "维保需求单精确行（领用行关联的需求行）"
    same_order = by_order.get((project_id, issue_no or "", line.part_id))
    if same_order is not None:
        return same_order, f"维保需求单同单同 PN（{issue_no}）"
    fallback = by_part.get((project_id, line.part_id))
    if fallback is not None:
        return fallback, "维保需求单同 PN 最新一单（兜底）"
    return None


def resolve_lines(
    db: Session,
    *,
    lines: Iterable[tuple[date, MaintenanceSiteIssueLine]],
    as_of: date | None = None,
) -> list[MaintenanceSiteIssueLine]:
    """Resolve one bounded batch with three evidence reads, never per-line SQL.

    ``as_of`` freezes the evidence horizon for reproducible migration snapshots.
    Normal operating callers omit it and retain the full before/after-seven-day
    waterfall.
    """

    entries = list(lines)
    if not entries:
        return []
    part_ids = {line.part_id for _issue_date, line in entries}

    # 2026-08-23：维保领用的权威价格 = 该项目维保需求单（WBDD）同 PN 的
    # 已回填成本（它本身已过 direct/estimate 成本回填，是业务事实源）。
    # 作为最强证据层，优先于采购价窗口（用户口径：PN 价格从需求单提取）。
    # 2026-08-24 修复：净数量分母 + 精确行/同单优先（见 _demand_price_maps）。
    from app.models.maintenance_project_operations import MaintenanceSiteIssue

    # 迁移源测试用 SimpleNamespace 模拟行（无 issue_id）——拿不到项目就
    # 跳过需求单层，走原瀑布
    issue_ids = {line.issue_id for _issue_date, line in entries
                 if getattr(line, "issue_id", None) is not None}
    issue_project: dict[str, str] = {}
    issue_no_by_id: dict[str, str] = {}
    for issue_id, project_id, issue_no in db.execute(
        select(
            MaintenanceSiteIssue.issue_id,
            MaintenanceSiteIssue.project_id,
            MaintenanceSiteIssue.issue_no,
        ).where(MaintenanceSiteIssue.issue_id.in_(issue_ids))
    ):
        issue_project[issue_id] = project_id
        issue_no_by_id[issue_id] = issue_no
    project_ids = set(issue_project.values())
    demand_maps = _demand_price_maps(
        db, project_ids=project_ids, part_ids=part_ids)
    linked_ids = {
        line.linked_purchase_line_id
        for _issue_date, line in entries
        if line.linked_purchase_line_id is not None
    }
    from_date = min(issue_date for issue_date, _line in entries) - timedelta(days=7)
    to_date = max(issue_date for issue_date, _line in entries) + timedelta(days=7)
    if as_of is not None:
        to_date = min(to_date, as_of)

    direct_by_id = {}
    if linked_ids:
        direct_by_id = {
            row.id: row
            for row in db.execute(
                select(
                    FPurchaseLine.id,
                    FPurchaseLine.part_id,
                    FPurchaseLine.qty,
                    FPurchaseLine.unit_price,
                    FPurchaseOrder.is_tax_inclusive,
                    FPurchaseOrder.order_no,
                    FPurchaseOrder.order_date,
                )
                .join(FPurchaseOrder, FPurchaseOrder.id == FPurchaseLine.order_id)
                .where(
                    FPurchaseLine.id.in_(linked_ids),
                    FPurchaseOrder.data_status == config.ACTIVE_STATUS,
                    *(
                        (FPurchaseOrder.order_date <= as_of,)
                        if as_of is not None
                        else ()
                    ),
                )
            )
        }

    purchase_by_part: dict[int, list] = defaultdict(list)
    for row in db.execute(
        select(
            FPurchaseLine.id,
            FPurchaseLine.part_id,
            FPurchaseLine.qty,
            FPurchaseLine.unit_price,
            FPurchaseOrder.is_tax_inclusive,
            FPurchaseOrder.order_no,
            FPurchaseOrder.order_date,
        )
        .join(FPurchaseOrder, FPurchaseOrder.id == FPurchaseLine.order_id)
        .where(
            FPurchaseLine.part_id.in_(part_ids),
            FPurchaseOrder.data_status == config.ACTIVE_STATUS,
            FPurchaseOrder.order_date >= from_date,
            FPurchaseOrder.order_date <= to_date,
            FPurchaseLine.qty > 0,
            FPurchaseLine.unit_price > 0,
        )
        .order_by(FPurchaseOrder.order_date, FPurchaseLine.id)
    ):
        if _valid(row.qty, row.unit_price):
            purchase_by_part[row.part_id].append(row)

    sales_by_part: dict[int, list] = defaultdict(list)
    for row in db.execute(
        select(
            FSalesLine.id,
            FSalesLine.part_id,
            FSalesLine.qty,
            FSalesLine.unit_price,
            FSalesOrder.order_no,
            FSalesOrder.order_date,
        )
        .join(FSalesOrder, FSalesOrder.id == FSalesLine.order_id)
        .where(
            FSalesLine.part_id.in_(part_ids),
            FSalesOrder.data_status == config.ACTIVE_STATUS,
            FSalesOrder.order_date >= from_date,
            FSalesOrder.order_date <= to_date,
            FSalesLine.qty > 0,
            FSalesLine.unit_price > 0,
        )
        .order_by(FSalesOrder.order_date, FSalesLine.id)
    ):
        if _valid(row.qty, row.unit_price):
            sales_by_part[row.part_id].append(row)

    resolved: list[MaintenanceSiteIssueLine] = []
    for issue_date, line in entries:
        source: str | None = None
        side: str | None = None
        unit_cost: Decimal | None = None
        samples: list[dict] = []
        demand_pick = _pick_demand_unit(
            line=line,
            project_id=issue_project.get(getattr(line, "issue_id", None)),
            issue_no=issue_no_by_id.get(getattr(line, "issue_id", None)),
            maps=demand_maps,
        )
        if demand_pick is not None:
            demand_unit, demand_basis = demand_pick
            source, side = "maint_demand", "maint"
            unit_cost = demand_unit
            samples = [{
                "sample_id": f"wbdd:{line.part_id}",
                "basis": demand_basis,
                "unit_price_ex_tax": format(demand_unit, "f"),
            }]
        elif (
            direct := direct_by_id.get(line.linked_purchase_line_id)
        ) is not None and (
            direct.part_id == line.part_id
            and _valid(direct.qty, direct.unit_price)
        ):
            source, side = "direct_purchase", "purchase"
            unit_cost = (
                tax_policy.ex_from_inc(direct.unit_price)
                if direct.is_tax_inclusive is True
                else _amount(Decimal(direct.unit_price))
            )
            samples = [_purchase_sample(direct, issue_date=issue_date)]
            samples[0]["unit_price_ex_tax"] = format(unit_cost, "f")
        else:
            window_from = issue_date - timedelta(days=7)
            window_to = issue_date + timedelta(days=7)
            purchase_samples = [
                _purchase_sample(row, issue_date=issue_date)
                for row in purchase_by_part[line.part_id]
                if window_from <= row.order_date <= window_to
            ]
            purchase_cost = _weighted(purchase_samples)
            if purchase_cost is not None:
                source, side = "purchase_window", "purchase"
                unit_cost, samples = purchase_cost, purchase_samples
            else:
                sales_samples = [
                    _sales_sample(row, issue_date=issue_date)
                    for row in sales_by_part[line.part_id]
                    if window_from <= row.order_date <= window_to
                ]
                sales_cost = _weighted(sales_samples)
                if sales_cost is not None:
                    source, side = "sales_window", "sales"
                    unit_cost, samples = sales_cost, sales_samples
                elif line.manual_unit_cost is not None:
                    source, side = "manual", "manual"
                    unit_cost = _amount(Decimal(line.manual_unit_cost))
        resolved.append(
            _apply_resolution(
                line,
                issue_date=issue_date,
                source=source,
                side=side,
                unit_cost=unit_cost,
                samples=samples,
            )
        )
    return resolved


def _demand_unit_for_line(
    db: Session, line: MaintenanceSiteIssueLine
) -> tuple[Decimal, str] | None:
    """单条领用行的需求单价格（批量路径之外的独立入口）。

    独立成函数便于单测按 _direct_purchase/_purchase_window 同款打桩。
    """
    from app.models.maintenance_project_operations import MaintenanceSiteIssue

    if getattr(line, "issue_id", None) is None:
        return None
    issue_header = db.execute(
        select(
            MaintenanceSiteIssue.project_id,
            MaintenanceSiteIssue.issue_no,
        ).where(MaintenanceSiteIssue.issue_id == line.issue_id)
    ).first()
    if issue_header is None:
        return None
    return _pick_demand_unit(
        line=line,
        project_id=issue_header[0],
        issue_no=issue_header[1],
        maps=_demand_price_maps(
            db, project_ids={issue_header[0]}, part_ids={line.part_id}),
    )


def resolve_line(
    db: Session,
    *,
    issue_date: date,
    line: MaintenanceSiteIssueLine,
) -> MaintenanceSiteIssueLine:
    """Recompute and persist one line's evidence; strong evidence beats manual.

    2026-08-24 修复：单行路径补上需求单价格层（此前只有批量 resolve_lines
    有，fill_manual_cost 等补价入口会算出与确认路径不同的价格）。
    """
    from_date = issue_date - timedelta(days=7)
    to_date = issue_date + timedelta(days=7)
    source: str | None = None
    side: str | None = None
    unit_cost: Decimal | None = None
    samples: list[dict] = []

    demand_pick = _demand_unit_for_line(db, line)
    if demand_pick is not None:
        source, side = "maint_demand", "maint"
        unit_cost = demand_pick[0]
        samples = [{
            "sample_id": f"wbdd:{line.part_id}",
            "basis": demand_pick[1],
            "unit_price_ex_tax": format(demand_pick[0], "f"),
        }]
    else:
        direct = _direct_purchase(db, line, issue_date=issue_date)
        if direct is not None:
            source, side = "direct_purchase", "purchase"
            unit_cost, samples = direct
        else:
            purchase = _purchase_window(
                db,
                part_id=line.part_id,
                issue_date=issue_date,
                from_date=from_date,
                to_date=to_date,
            )
            if purchase is not None:
                source, side = "purchase_window", "purchase"
                unit_cost, samples = purchase
            else:
                sales = _sales_window(
                    db,
                    part_id=line.part_id,
                    issue_date=issue_date,
                    from_date=from_date,
                    to_date=to_date,
                )
                if sales is not None:
                    source, side = "sales_window", "sales"
                    unit_cost, samples = sales
                elif line.manual_unit_cost is not None:
                    source, side = "manual", "manual"
                    unit_cost = _amount(Decimal(line.manual_unit_cost))

    return _apply_resolution(
        line,
        issue_date=issue_date,
        source=source,
        side=side,
        unit_cost=unit_cost,
        samples=samples,
    )

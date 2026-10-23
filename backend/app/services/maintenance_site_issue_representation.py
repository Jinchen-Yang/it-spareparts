"""Existing internal fact/cost representation; not an authorized HTTP DTO.

Callers must apply their own field visibility policy before exposing this data.
"""

from decimal import Decimal

from app.models.maintenance_project_operations import MaintenanceSiteIssueLine


_COST_EVIDENCE_META = {
    None: ("missing", False, "待补价格"),
    "maint_demand": (
        "maintenance_demand_evidence",
        False,
        "关联维保需求单价",
    ),
    "direct_purchase": ("purchase_evidence", False, "关联采购单价"),
    "purchase_window": (
        "purchase_evidence",
        False,
        "采购前后 7 天数量加权",
    ),
    "sales_window": (
        "sales_estimate",
        True,
        "估算（销售前后 7 天数量加权）",
    ),
    "manual": ("manual_confirmed", False, "人工确认单价"),
}


def _money(value: Decimal | None) -> str | None:
    return format(value, ".2f") if value is not None else None


def _rate(value: Decimal) -> str:
    """Serialize fixed rates identically before and after a database refresh."""

    return format(value.normalize(), "f")


def _qty(value: Decimal) -> str:
    return format(value, ".3f")


def cost_evidence_metadata(source: str | None) -> dict:
    """Return stable business semantics for a resolved cost source."""

    kind, is_estimate, label = _COST_EVIDENCE_META.get(
        source,
        ("unknown", True, "未知成本证据"),
    )
    return {
        "cost_evidence_kind": kind,
        "cost_is_estimate": is_estimate,
        "cost_source_label": label,
    }


def site_issue_line_dict(row: MaintenanceSiteIssueLine) -> dict:
    return {
        "issue_line_id": row.issue_line_id,
        "line_no": row.line_no,
        "part_id": row.part_id,
        "pn": row.pn,
        "quantity": _qty(row.quantity),
        "delivery_line_id": row.delivery_line_id,
        "source_order_id": row.source_order_id,
        "source_line_id": row.source_line_id,
        "serial_number": row.serial_number,
        "no_return": row.no_return,
        "remark": row.remark,
        "demand_order_no": row.demand_order_no,
        "linked_purchase_line_id": row.linked_purchase_line_id,
        "manual_unit_cost": _money(row.manual_unit_cost),
        "manual_unit_cost_inc_tax": _money(row.manual_unit_cost_inc_tax),
        "manual_evidence": row.manual_evidence,
        "unit_cost": _money(row.unit_cost),
        "cost_amount": _money(row.cost_amount),
        "unit_cost_ex_tax": _money(row.unit_cost_ex_tax),
        "unit_cost_inc_tax": _money(row.unit_cost_inc_tax),
        "cost_amount_ex_tax": _money(row.cost_amount_ex_tax),
        "cost_amount_inc_tax": _money(row.cost_amount_inc_tax),
        "tax_rate_used": _rate(row.tax_rate_used),
        "cost_source": row.cost_source,
        **cost_evidence_metadata(row.cost_source),
        "price_basis": row.price_basis,
        "reference_side": row.reference_side,
        "reference_sample_ids": row.reference_sample_ids,
        "reference_sample_count": row.reference_sample_count,
        "reference_samples": row.reference_samples,
        "reference_window_from": (
            row.reference_window_from.isoformat() if row.reference_window_from else None
        ),
        "reference_window_to": (
            row.reference_window_to.isoformat() if row.reference_window_to else None
        ),
        "algorithm_version": row.algorithm_version,
        "version": row.version,
    }

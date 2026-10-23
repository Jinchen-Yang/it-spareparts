"""Manual workbook cost evidence and line amount writes shared by V1 and V2.

These helpers use the caller's Session and never commit. Line-before-override
locking and automatic-cost precedence remain the existing workbook contract.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.maintenance import FMaintenanceLine, MaintenanceManualCostOverride
from app.services.maintenance_workbook_plans import CostRefill, WorkbookError


def _merge_manual_cost_to_line(
    db: Session, refill: "CostRefill", *, operated_by: str,
) -> bool:
    """人工成本合并到主表——写 override 表（审计/回滚）并同步 f_maintenance_line：
    unit_cost_ex_tax/inc_tax + cost_source='manual' + cost_amount 字段。
    面板/看板/概览读主表即得人工值，无需各处 merge。

    仅当主表当前无自动成本（cost_source IN (NULL,'none')）时才合并到主表——已有
    direct/window 等自动证据的行不被人工值覆盖；'manual' 行允许修正人工价。
    override 表始终写入审计。
    """
    if refill.line_id is None:
        raise WorkbookError("line_not_found", "备件行标识无效，请重新下载")
    # Every caller uses the same row hierarchy.  Locking the parent line first
    # also serializes the otherwise-unlockable "no override row yet" state, so
    # concurrent first fills cannot both INSERT the unique line_id.
    line = db.scalar(
        select(FMaintenanceLine)
        .where(FMaintenanceLine.id == refill.line_id)
        .with_for_update()
    )
    if line is None:
        raise WorkbookError("line_not_found", "备件行已不存在，请重新下载")
    existing = db.scalar(
        select(MaintenanceManualCostOverride)
        .where(MaintenanceManualCostOverride.line_id == refill.line_id)
        .with_for_update()
    )
    changed = False
    if refill.unit_cost_ex_tax is not None:
        if existing is None:
            db.add(MaintenanceManualCostOverride(
                line_id=refill.line_id,
                unit_cost_ex_tax=refill.unit_cost_ex_tax,
                unit_cost_inc_tax=refill.unit_cost_inc_tax,
                reason=refill.reason, active=True, version=1,
                updated_by=operated_by))
            changed = True
        else:
            override_changed = (
                existing.unit_cost_ex_tax != refill.unit_cost_ex_tax
                or existing.unit_cost_inc_tax != refill.unit_cost_inc_tax
                or existing.reason != refill.reason
                or not existing.active
            )
            if override_changed:
                existing.unit_cost_ex_tax = refill.unit_cost_ex_tax
                existing.unit_cost_inc_tax = refill.unit_cost_inc_tax
                existing.reason = refill.reason
                existing.active = True
                existing.version += 1
                existing.updated_by = operated_by
                changed = True
        if line.cost_source in (None, "none", "manual"):
            cost_fields = (
                "unit_cost_ex_tax", "unit_cost_inc_tax",
                "cost_amount_ex_tax", "cost_amount_inc_tax",
                "cost_source", "cost_tax_basis", "unit_cost", "cost_amount",
            )
            before_cost = tuple(getattr(line, field) for field in cost_fields)
            # Match maintenance_cost.recompute's manual evidence contract:
            # the legacy amount is the ex-tax side, not an optional cache.
            # Cost-quality readers require this evidence before using either
            # normalized tax amount.
            line.cost_source = "manual"
            line.cost_tax_basis = "ex"
            _recompute_line_amounts(line, unit_cost_ex_tax=refill.unit_cost_ex_tax,
                                    unit_cost_inc_tax=refill.unit_cost_inc_tax)
            changed = before_cost != tuple(
                getattr(line, field) for field in cost_fields
            ) or changed
    elif refill.reason is not None:
        if existing is not None and existing.reason != refill.reason:
            existing.reason = refill.reason
            existing.version += 1
            existing.updated_by = operated_by
            changed = True
    return changed


def _recompute_line_amounts(line: FMaintenanceLine, *,
                            unit_cost_ex_tax: Decimal | None = None,
                            unit_cost_inc_tax: Decimal | None = None) -> None:
    """数量/退货数量/单价变更后重算两套成本金额（有效数量×单价）。
    无成本的行（none/NULL）保持 NULL——不知道≠0（铁律 5）。"""
    if unit_cost_ex_tax is not None:
        line.unit_cost_ex_tax = unit_cost_ex_tax
    if unit_cost_inc_tax is not None:
        line.unit_cost_inc_tax = unit_cost_inc_tax
    effective_qty = max((line.qty or Decimal(0)) - (line.return_qty or Decimal(0)),
                        Decimal(0))
    if line.unit_cost_ex_tax is not None:
        line.cost_amount_ex_tax = (line.unit_cost_ex_tax * effective_qty).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP)
    if line.unit_cost_inc_tax is not None:
        line.cost_amount_inc_tax = (line.unit_cost_inc_tax * effective_qty).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP)
    # Quantity-only edits must keep a materialized manual fact coherent too.
    # A partial historical price can carry a stale amount on its missing side;
    # do not turn that residue into valid legacy evidence. Real manual fills
    # provide both normalized prices. Keep other sources and dirty bases as-is.
    if (line.cost_source == "manual" and line.cost_tax_basis in ("ex", "inc")
            and line.unit_cost_ex_tax is not None and line.unit_cost_inc_tax is not None):
        line.unit_cost = getattr(line, f"unit_cost_{line.cost_tax_basis}_tax")
        line.cost_amount = getattr(line, f"cost_amount_{line.cost_tax_basis}_tax")

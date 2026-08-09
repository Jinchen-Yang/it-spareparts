"""Build server-owned, deterministic source snapshots for maintenance cutover."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_project_operations import (
    MaintenanceProjectExpenseAttribution,
    MaintenanceSiteIssue,
    MaintenanceSiteIssueLine,
)
from app.services.maintenance_migration_controls import canonical_hash


class MaintenanceMigrationSourceError(ValueError):
    """The requested server-side snapshot cannot be built safely."""


def _money_text(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


def _qty_text(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


def _required_text(value: Any, label: str, *, max_length: int) -> str:
    clean = str(value or "").strip()
    if not clean or len(clean) > max_length:
        raise MaintenanceMigrationSourceError(f"{label}无效")
    return clean


def _scalar_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _canonical_decimal_text(value: Any, label: str) -> str:
    try:
        number = Decimal(_scalar_text(value))
    except (InvalidOperation, ValueError) as exc:
        raise MaintenanceMigrationSourceError(f"{label}无效") from exc
    if not number.is_finite():
        raise MaintenanceMigrationSourceError(f"{label}无效")
    normalized = number.normalize()
    return "0" if not normalized else format(normalized, "f")


def _site_issue_rows(
    db: Session,
    *,
    project_id: str,
) -> list[dict[str, Any]]:
    rows = db.execute(
        select(MaintenanceSiteIssue, MaintenanceSiteIssueLine)
        .join(
            MaintenanceSiteIssueLine,
            MaintenanceSiteIssueLine.issue_id == MaintenanceSiteIssue.issue_id,
        )
        .where(MaintenanceSiteIssue.project_id == project_id)
        .order_by(
            MaintenanceSiteIssue.issue_date,
            MaintenanceSiteIssue.issue_id,
            MaintenanceSiteIssueLine.line_no,
        )
    ).all()
    return [
        {
            "issue_line_id": line.issue_line_id,
            "issue_id": issue.issue_id,
            "issue_no": issue.issue_no,
            "issue_date": issue.issue_date.isoformat(),
            "pn": line.pn,
            "quantity": _qty_text(line.quantity),
            "workflow_status": issue.normalized_status,
            "stable_identity": issue.source in {"direct_api", "workbook"},
            "cost_amount_ex_tax": _money_text(line.cost_amount_ex_tax),
            "cost_amount_inc_tax": _money_text(line.cost_amount_inc_tax),
            "issue_version": issue.version,
            "line_version": line.version,
            "source": issue.source,
            "import_batch_id": issue.import_batch_id,
        }
        for issue, line in rows
    ]


def _expense_rows(db: Session, *, project_id: str) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(MaintenanceProjectExpenseAttribution)
        .where(MaintenanceProjectExpenseAttribution.project_id == project_id)
        .order_by(
            MaintenanceProjectExpenseAttribution.expense_date,
            MaintenanceProjectExpenseAttribution.expense_id,
        )
    ).all()
    return [
        {
            "expense_id": row.expense_id,
            "expense_ref": row.expense_ref,
            "expense_date": row.expense_date.isoformat(),
            "normalized_status": row.normalized_status,
            "status_mapping_state": row.status_mapping_state,
            "amount_ex_tax": _money_text(row.amount_ex_tax),
            "amount_inc_tax": _money_text(row.amount_inc_tax),
            "version": row.version,
        }
        for row in rows
    ]


def build_project_source_payload(
    db: Session,
    *,
    project_id: str,
    cutover_date: date,
    historical_mode: str,
    historical_baseline: Mapping[str, Any] | None,
    opening_balances: Sequence[Mapping[str, Any]],
    inventory_movements: Sequence[Mapping[str, Any]] = (),
    warehouse_source_ready: bool,
) -> dict[str, Any]:
    """Read operational facts from the database; caller supplies only reviewed candidates.

    ``inventory_movements`` is deliberately an explicit integration boundary.  Until the
    canonical warehouse adapter slice provides it, the snapshot carries a blocker and can
    never be approved.
    """

    clean_project_id = _required_text(project_id, "项目稳定编号", max_length=36)
    project = db.scalar(
        select(MaintenanceProject).where(
            MaintenanceProject.project_id == clean_project_id
        )
    )
    if project is None:
        raise MaintenanceMigrationSourceError("维保项目不存在")
    if historical_mode not in {"approved_cost_baseline", "stable_site_issues"}:
        raise MaintenanceMigrationSourceError("历史成本模式无效")

    issue_rows = _site_issue_rows(db, project_id=clean_project_id)
    historical_rows = [
        row
        for row in issue_rows
        if date.fromisoformat(row["issue_date"]) < cutover_date
    ]
    post_cutover_rows = [
        row
        for row in issue_rows
        if date.fromisoformat(row["issue_date"]) >= cutover_date
    ]
    if historical_mode == "approved_cost_baseline":
        historical_rows = []

    source_blockers: list[dict[str, str]] = []
    if not warehouse_source_ready:
        source_blockers.append(
            {
                "code": "warehouse_source_not_ready",
                "detail": "权威仓库单据适配尚未接入，不能核定切换日后库存变动",
            }
        )

    normalized_opening = [
        {
            "balance_key": _required_text(
                row.get("balance_key"), "库存期初稳定键", max_length=256
            ),
            "pn": str(row.get("pn") or "").strip() or None,
            "quantity": _canonical_decimal_text(row.get("quantity"), "库存期初数量"),
            "evidence_hash": str(row.get("evidence_hash") or "").strip().lower(),
            "approved": row.get("approved") is True,
        }
        for row in opening_balances
    ]
    normalized_baseline = None
    if historical_baseline is not None:
        normalized_baseline = {
            "amount_ex_tax": _canonical_decimal_text(
                historical_baseline.get("amount_ex_tax"), "历史基线未税金额"
            ),
            "amount_inc_tax": _canonical_decimal_text(
                historical_baseline.get("amount_inc_tax"), "历史基线含税金额"
            ),
            "evidence_hash": str(historical_baseline.get("evidence_hash") or "")
            .strip()
            .lower(),
            "approved": historical_baseline.get("approved") is True,
        }

    payload: dict[str, Any] = {
        "project_id": clean_project_id,
        "cutover_date": cutover_date.isoformat(),
        "historical_mode": historical_mode,
        "historical_baseline": normalized_baseline,
        "historical_site_issues": historical_rows,
        "post_cutover_site_issues": post_cutover_rows,
        "approved_expenses": _expense_rows(db, project_id=clean_project_id),
        "opening_balances": normalized_opening,
        "inventory_movements": [dict(row) for row in inventory_movements],
        "return_offsets": [],
        "source_blockers": source_blockers,
        "source_coverage": {
            "warehouse_source_ready": warehouse_source_ready,
            "project_version": project.version,
        },
    }
    # Named reconciliation changes only approval state, not the underlying source
    # snapshot.  Excluding those two booleans lets reconcile verify that every
    # operational fact and candidate value is unchanged while still advancing state.
    hash_payload = dict(payload)
    if normalized_baseline is not None:
        hash_payload["historical_baseline"] = {
            key: value
            for key, value in normalized_baseline.items()
            if key != "approved"
        }
    hash_payload["opening_balances"] = [
        {key: value for key, value in row.items() if key != "approved"}
        for row in normalized_opening
    ]
    payload["source_snapshot_hash"] = canonical_hash(hash_payload)
    return payload

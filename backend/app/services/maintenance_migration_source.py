"""Build server-owned, deterministic source snapshots for maintenance cutover."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from sqlalchemy import bindparam, select, text
from sqlalchemy.orm import Session

from app.business_time import business_today
from app.models.dimensions import DimPart
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_project_operations import (
    MaintenanceProjectExpenseAttribution,
    MaintenanceProjectWorkbookState,
    MaintenanceSiteIssue,
    MaintenanceSiteIssueLine,
)
from app.services.maintenance_migration_controls import canonical_hash
from app.services.maintenance_project_operations import get_or_create_workbook_state


class MaintenanceMigrationSourceError(ValueError):
    """The requested server-side snapshot cannot be built safely."""


_COST_EVIDENCE_META = {
    None: ("missing", False, "待补价格"),
    "direct_purchase": ("purchase_evidence", False, "关联采购单价"),
    "purchase_window": ("purchase_evidence", False, "采购前后 7 天数量加权"),
    "sales_window": ("sales_estimate", True, "估算（销售前后 7 天数量加权）"),
    "manual": ("manual_confirmed", False, "人工确认单价"),
}
_SAMPLE_EVIDENCE_FIELDS = (
    "sample_id",
    "document_no",
    "document_date",
    "distance_days",
    "quantity",
    "unit_price_raw",
    "unit_price_ex_tax",
    "tax_conversion",
)
_MAX_SITE_ISSUE_LINES_PER_PROJECT = 200_000
_MAX_EXPENSE_ROWS_PER_PROJECT = 200_000
_MAX_REFERENCE_SAMPLES_PER_LINE = 10_000


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


def _table_has_columns(db: Session, table_name: str, columns: set[str]) -> bool:
    if db.scalar(text("SELECT to_regclass(:name)"), {"name": table_name}) is None:
        return False
    found = set(
        db.scalars(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :name"
            ),
            {"name": table_name},
        )
    )
    return columns <= found


def lock_optional_linkage_snapshot(db: Session) -> None:
    """Freeze merged #201/#207 link rows for the rest of this transaction.

    Project row locking serializes assignment creation/reassignment and operating
    fact writes, but #201 unassignment and a future delivery adapter can update
    their own link tables without taking that project row.  SHARE table locks
    close that remaining READ COMMITTED mixed-snapshot window.  Missing sibling
    contracts stay an explicit fail-closed state in the loaders below.
    """

    table_contracts = {
        "maintenance_source_order_assignment": {
            "assignment_id",
            "source_order_id",
            "project_id",
            "is_active",
            "version",
        },
        "maintenance_site_issue_delivery_source": {
            "delivery_line_id",
            "project_id",
            "source_order_id",
            "source_line_id",
            "part_id",
            "pn",
            "mapping_state",
            "mapping_version",
            "is_active",
        },
    }
    if _table_has_columns(
        db,
        "maintenance_source_order_assignment",
        table_contracts["maintenance_source_order_assignment"],
    ):
        db.execute(text("LOCK TABLE maintenance_source_order_assignment IN SHARE MODE"))
    if _table_has_columns(
        db,
        "maintenance_site_issue_delivery_source",
        table_contracts["maintenance_site_issue_delivery_source"],
    ):
        db.execute(
            text("LOCK TABLE maintenance_site_issue_delivery_source IN SHARE MODE")
        )


def lock_project_source_snapshot(
    db: Session, *, project_id: str
) -> tuple[MaintenanceProject, MaintenanceProjectWorkbookState]:
    """Freeze one project's mutable facts in the writers' canonical lock order.

    Operating-fact writers lock workbook state and then the project.  Source-order
    reassignment locks the project.  Holding both until the migration transaction
    commits prevents a mixed read while a manifest is rebuilt or signed.
    """

    project_exists = db.scalar(
        select(MaintenanceProject.project_id).where(
            MaintenanceProject.project_id == project_id
        )
    )
    if project_exists is None:
        raise MaintenanceMigrationSourceError("维保项目不存在")
    state = get_or_create_workbook_state(db, project_id=project_id, lock=True)
    project = db.scalar(
        select(MaintenanceProject)
        .where(MaintenanceProject.project_id == project_id)
        .with_for_update(read=True)
    )
    if project is None:
        raise MaintenanceMigrationSourceError("维保项目不存在")
    if not project.is_active:
        raise MaintenanceMigrationSourceError("维保项目已归档，不能进入迁移切换")
    return project, state


def lock_project_source_snapshots(db: Session, *, project_ids: Sequence[str]) -> None:
    """Lock every project before taking any shared cross-project linkage lock.

    This order prevents a multi-project run from holding the assignment table in
    SHARE mode while waiting for a later project row whose assignment writer is
    itself waiting for a table write lock.
    """

    for project_id in sorted(set(project_ids)):
        lock_project_source_snapshot(db, project_id=project_id)
    lock_optional_linkage_snapshot(db)


def _current_assignment_map(
    db: Session, source_order_ids: set[str]
) -> tuple[dict[str, list[dict[str, Any]]], bool]:
    required = {
        "assignment_id",
        "source_order_id",
        "project_id",
        "is_active",
        "version",
    }
    if not _table_has_columns(db, "maintenance_source_order_assignment", required):
        return {}, False
    if not source_order_ids:
        return {}, True
    statement = text(
        "SELECT assignment.assignment_id, assignment.source_order_id, "
        "assignment.project_id, assignment.version "
        "FROM maintenance_source_order_assignment AS assignment "
        "JOIN maintenance_project AS project "
        "  ON project.project_id = assignment.project_id "
        "WHERE assignment.is_active IS TRUE "
        "  AND project.is_active IS TRUE "
        "  AND assignment.source_order_id IN :source_ids "
        "ORDER BY assignment.source_order_id, assignment.assignment_id"
    ).bindparams(bindparam("source_ids", expanding=True))
    output: dict[str, list[dict[str, Any]]] = {}
    for assignment_id, source_order_id, project_id, version in db.execute(
        statement, {"source_ids": sorted(source_order_ids)}
    ):
        output.setdefault(str(source_order_id), []).append(
            {
                "assignment_id": str(assignment_id),
                "project_id": str(project_id),
                "version": int(version),
            }
        )
    return output, True


def _delivery_source_map(
    db: Session, delivery_line_ids: set[str]
) -> tuple[dict[str, dict[str, Any]], bool]:
    required = {
        "delivery_line_id",
        "project_id",
        "source_order_id",
        "source_line_id",
        "part_id",
        "pn",
        "mapping_state",
        "mapping_version",
        "is_active",
    }
    if not _table_has_columns(db, "maintenance_site_issue_delivery_source", required):
        return {}, False
    if not delivery_line_ids:
        return {}, True
    statement = text(
        "SELECT delivery_line_id, project_id, source_order_id, source_line_id, "
        "part_id, pn, mapping_state, mapping_version, is_active "
        "FROM maintenance_site_issue_delivery_source "
        "WHERE delivery_line_id IN :delivery_line_ids "
        "ORDER BY delivery_line_id"
    ).bindparams(bindparam("delivery_line_ids", expanding=True))
    return {
        str(row.delivery_line_id): {
            "project_id": str(row.project_id),
            "source_order_id": str(row.source_order_id),
            "source_line_id": str(row.source_line_id),
            "part_id": int(row.part_id),
            "pn": str(row.pn),
            "mapping_state": str(row.mapping_state),
            "mapping_version": str(row.mapping_version),
            "is_active": bool(row.is_active),
        }
        for row in db.execute(
            statement, {"delivery_line_ids": sorted(delivery_line_ids)}
        )
    }, True


def _sample_evidence(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [
        {field: sample.get(field) for field in _SAMPLE_EVIDENCE_FIELDS}
        for sample in value
        if isinstance(sample, Mapping)
    ]


def _cost_evidence(line: MaintenanceSiteIssueLine) -> dict[str, Any]:
    cost_source = getattr(line, "cost_source", None)
    kind, is_estimate, label = _COST_EVIDENCE_META.get(
        cost_source, ("unknown", True, "未知成本证据")
    )
    raw_samples = getattr(line, "reference_samples", None) or []
    if not isinstance(raw_samples, list) or len(raw_samples) > (
        _MAX_REFERENCE_SAMPLES_PER_LINE
    ):
        raise MaintenanceMigrationSourceError("现场领用成本样本证据超出安全上限")
    samples = _sample_evidence(raw_samples)
    sample_ids = [
        str(value).strip()
        for value in (getattr(line, "reference_sample_ids", None) or [])
    ]
    evidence_sample_ids = [
        str(sample.get("sample_id") or "").strip() for sample in samples
    ]
    sample_count = int(getattr(line, "reference_sample_count", 0) or 0)
    if (
        len(samples) != len(raw_samples)
        or sample_count != len(samples)
        or sample_count != len(sample_ids)
        or any(not value for value in sample_ids)
        or len(set(sample_ids)) != len(sample_ids)
        or evidence_sample_ids != sample_ids
    ):
        raise MaintenanceMigrationSourceError("现场领用成本样本证据不完整")
    return {
        "cost_source": cost_source,
        "cost_evidence_kind": kind,
        "cost_is_estimate": is_estimate,
        "cost_source_label": label,
        "price_basis": getattr(line, "price_basis", None),
        "linked_purchase_line_id": getattr(line, "linked_purchase_line_id", None),
        "manual_evidence": getattr(line, "manual_evidence", None),
        "reference_side": getattr(line, "reference_side", None),
        "reference_sample_ids": sample_ids,
        "reference_sample_count": sample_count,
        "reference_samples": samples,
        "reference_window_from": (
            getattr(line, "reference_window_from", None).isoformat()
            if getattr(line, "reference_window_from", None)
            else None
        ),
        "reference_window_to": (
            getattr(line, "reference_window_to", None).isoformat()
            if getattr(line, "reference_window_to", None)
            else None
        ),
        "algorithm_version": getattr(line, "algorithm_version", None),
    }


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
        .limit(_MAX_SITE_ISSUE_LINES_PER_PROJECT + 1)
    ).all()
    if len(rows) > _MAX_SITE_ISSUE_LINES_PER_PROJECT:
        raise MaintenanceMigrationSourceError("单项目现场领用明细超出迁移安全上限")
    source_order_ids = {
        str(getattr(line, "source_order_id", "") or "").strip()
        for issue, line in rows
        if issue.source == "site_issue_v2"
        and str(getattr(line, "source_order_id", "") or "").strip()
    }
    delivery_line_ids = {
        str(getattr(line, "delivery_line_id", "") or "").strip()
        for issue, line in rows
        if issue.source == "site_issue_v2"
        and str(getattr(line, "delivery_line_id", "") or "").strip()
    }
    assignments, assignment_contract_ready = _current_assignment_map(
        db, source_order_ids
    )
    delivery_sources, delivery_contract_ready = _delivery_source_map(
        db, delivery_line_ids
    )
    output: list[dict[str, Any]] = []
    for issue, line in rows:
        source_order_id = (
            str(getattr(line, "source_order_id", "") or "").strip() or None
        )
        source_line_id = str(getattr(line, "source_line_id", "") or "").strip() or None
        delivery_line_id = (
            str(getattr(line, "delivery_line_id", "") or "").strip() or None
        )
        assignment_rows = assignments.get(source_order_id or "", [])
        assignment = assignment_rows[0] if len(assignment_rows) == 1 else None
        delivery_source = delivery_sources.get(delivery_line_id or "")
        stable_identity = issue.source in {"direct_api", "workbook"}
        link_state = "legacy_stable" if stable_identity else "not_stable"
        if issue.source == "site_issue_v2":
            stable_identity = bool(
                assignment_contract_ready
                and delivery_contract_ready
                and assignment is not None
                and assignment["project_id"] == project_id
                and delivery_source is not None
                and delivery_source["is_active"]
                and delivery_source["mapping_state"] == "ready"
                and delivery_source["project_id"] == project_id
                and delivery_source["source_order_id"] == source_order_id
                and delivery_source["source_line_id"] == source_line_id
                and delivery_source["part_id"] == line.part_id
                and delivery_source["pn"] == line.pn
            )
            link_state = (
                "ready" if stable_identity else "assignment_or_delivery_mismatch"
            )
        output.append(
            {
                "issue_line_id": line.issue_line_id,
                "issue_id": issue.issue_id,
                "issue_no": issue.issue_no,
                "issue_date": issue.issue_date.isoformat(),
                "part_id": line.part_id,
                "pn": line.pn,
                "sn": (
                    getattr(line, "serial_number", None) or getattr(line, "sn", None)
                ),
                "quantity": _qty_text(line.quantity),
                "workflow_status": issue.normalized_status,
                "stable_identity": stable_identity,
                "link_state": link_state,
                "delivery_line_id": delivery_line_id,
                "source_order_id": source_order_id,
                "source_line_id": source_line_id,
                "source_assignment_id": assignment.get("assignment_id")
                if assignment
                else None,
                "source_assignment_version": assignment.get("version")
                if assignment
                else None,
                "delivery_mapping_version": (
                    delivery_source.get("mapping_version") if delivery_source else None
                ),
                "cost_amount_ex_tax": _money_text(line.cost_amount_ex_tax),
                "cost_amount_inc_tax": _money_text(line.cost_amount_inc_tax),
                **_cost_evidence(line),
                "issue_version": issue.version,
                "line_version": line.version,
                "source": issue.source,
                "import_batch_id": issue.import_batch_id,
            }
        )
    return output


def _expense_rows(db: Session, *, project_id: str) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(MaintenanceProjectExpenseAttribution)
        .where(MaintenanceProjectExpenseAttribution.project_id == project_id)
        .order_by(
            MaintenanceProjectExpenseAttribution.expense_date,
            MaintenanceProjectExpenseAttribution.expense_id,
        )
        .limit(_MAX_EXPENSE_ROWS_PER_PROJECT + 1)
    ).all()
    if len(rows) > _MAX_EXPENSE_ROWS_PER_PROJECT:
        raise MaintenanceMigrationSourceError("单项目报销明细超出迁移安全上限")
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
    project, workbook_state = lock_project_source_snapshot(
        db, project_id=clean_project_id
    )
    lock_optional_linkage_snapshot(db)
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
    source_blockers: list[dict[str, str]] = []
    if not warehouse_source_ready:
        source_blockers.append(
            {
                "code": "warehouse_source_not_ready",
                "detail": "权威仓库单据适配尚未接入，不能核定切换日后库存变动",
            }
        )

    required_expense_month = business_today().replace(day=1)
    if (
        workbook_state.expense_ready_through is None
        or workbook_state.expense_ready_through < required_expense_month
    ):
        source_blockers.append(
            {
                "code": "expense_readiness_missing",
                "detail": (
                    "项目费用完整水位未覆盖当前业务月份，"
                    "不能把后续空报销清单解释为零费用"
                ),
            }
        )
    elif workbook_state.expense_ready_through > required_expense_month:
        source_blockers.append(
            {
                "code": "expense_readiness_invalid",
                "detail": "项目费用完整水位处于未来月份，不能用于迁移审批",
            }
        )

    opening_candidates: list[tuple[Mapping[str, Any], int, str]] = []
    for row in opening_balances:
        key = _required_text(row.get("balance_key"), "库存期初稳定键", max_length=256)
        prefix, separator, raw_part_id = key.partition(":")
        try:
            part_id = int(raw_part_id)
        except (TypeError, ValueError) as exc:
            raise MaintenanceMigrationSourceError(
                "库存期初稳定键必须为 project_id:part_id"
            ) from exc
        expected_key = f"{clean_project_id}:{part_id}"
        if (
            separator != ":"
            or prefix != clean_project_id
            or raw_part_id != str(part_id)
            or part_id <= 0
            or key != expected_key
        ):
            raise MaintenanceMigrationSourceError(
                "库存期初稳定键必须为 project_id:part_id"
            )
        opening_candidates.append((row, part_id, expected_key))
    part_ids = {part_id for _row, part_id, _key in opening_candidates}
    active_parts = (
        {
            row.id: row
            for row in db.scalars(
                select(DimPart)
                .where(DimPart.id.in_(part_ids), DimPart.status == "active")
                .with_for_update(read=True)
            )
        }
        if part_ids
        else {}
    )
    if set(active_parts) != part_ids:
        raise MaintenanceMigrationSourceError("库存期初包含不存在或已停用的配件")
    normalized_opening: list[dict[str, Any]] = []
    for row, part_id, key in opening_candidates:
        part = active_parts[part_id]
        supplied_pn = str(row.get("pn") or "").strip() or None
        if supplied_pn is not None and supplied_pn != part.pn_std:
            raise MaintenanceMigrationSourceError(
                "库存期初 PN 与 active part_id 不一致"
            )
        normalized_opening.append(
            {
                "balance_key": key,
                "part_id": part_id,
                "pn": part.pn_std,
                "quantity": _canonical_decimal_text(
                    row.get("quantity"), "库存期初数量"
                ),
                "evidence_hash": str(row.get("evidence_hash") or "").strip().lower(),
                "approved": row.get("approved") is True,
            }
        )
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
            "workbook_revision": workbook_state.revision,
            "workbook_data_version": workbook_state.data_version,
            "expense_ready_through": (
                workbook_state.expense_ready_through.isoformat()
                if workbook_state.expense_ready_through
                else None
            ),
            "expense_required_through": required_expense_month.isoformat(),
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

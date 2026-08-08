"""Controlled operating facts for stable maintenance projects.

The module is deliberately independent from the legacy WBDD cost/read models.  It
is the only write path for project-contract relationships and the project-scoped
facts added by the stable-project workspace.
"""

from __future__ import annotations

from calendar import monthrange
from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
from uuid import uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models.maintenance_project import (
    MaintenanceProject,
    MaintenanceProjectAuditLog,
    MaintenanceProjectContract,
)
from app.models.dimensions import DimPart
from app.models.maintenance_project_operations import (
    MaintenanceCollectionSnapshot,
    MaintenanceProjectExpenseAttribution,
    MaintenanceProjectOperationAudit,
    MaintenanceSiteIssue,
    MaintenanceSiteIssueLine,
    MaintenanceProjectWorkbookState,
)
from app.business_time import business_today
from app.security import UserContext, is_field_hidden
from app.services import maintenance_project
from app.services import maintenance_consumption_cost


class MaintenanceOperationError(Exception):
    """Invalid stable-project operating-fact request."""


class MaintenanceOperationConflict(Exception):
    """Concurrent or duplicate operating-fact request."""


def _workbook_data_version(project_id: str, revision: int) -> str:
    return hashlib.sha256(f"{project_id}:{revision}".encode("utf-8")).hexdigest()


def get_or_create_workbook_state(
    db: Session,
    *,
    project_id: str,
    lock: bool = False,
) -> MaintenanceProjectWorkbookState:
    """Return the project concurrency row, creating revision zero idempotently."""

    db.execute(
        insert(MaintenanceProjectWorkbookState)
        .values(
            project_id=project_id,
            revision=0,
            data_version=_workbook_data_version(project_id, 0),
        )
        .on_conflict_do_nothing(index_elements=["project_id"])
    )
    statement = select(MaintenanceProjectWorkbookState).where(
        MaintenanceProjectWorkbookState.project_id == project_id
    )
    if lock:
        statement = statement.with_for_update()
    return db.execute(statement).scalar_one()


def bump_workbook_revision(
    db: Session,
    *,
    project_id: str,
) -> MaintenanceProjectWorkbookState:
    """Bump once inside the caller's transaction after an operating-fact write."""

    state = get_or_create_workbook_state(db, project_id=project_id, lock=True)
    return bump_locked_workbook_revision(db, state=state)


def bump_locked_workbook_revision(
    db: Session,
    *,
    state: MaintenanceProjectWorkbookState,
) -> MaintenanceProjectWorkbookState:
    """Bump a state row the caller already locked with ``FOR UPDATE``."""

    state.revision += 1
    state.data_version = _workbook_data_version(state.project_id, state.revision)
    db.flush()
    return state


def contract_dict(row: MaintenanceProjectContract) -> dict:
    return {
        "project_contract_id": row.project_contract_id,
        "project_id": row.project_id,
        "contract_id": row.contract_id,
        "contract_no": row.contract_no,
        "contract_amount": (
            format(row.contract_amount, "f") if row.contract_amount is not None else None
        ),
        "contract_status": row.contract_status,
        "status_mapping_state": row.status_mapping_state,
        "status_mapping_version": row.status_mapping_version,
        "included_in_total": row.included_in_total,
        "effective_from": row.effective_from.isoformat(),
        "effective_to": row.effective_to.isoformat() if row.effective_to else None,
        "source": row.source,
        "version": row.version,
    }


def _required(value: str | None, label: str, limit: int = 64) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise MaintenanceOperationError(f"{label}不能为空")
    if len(cleaned) > limit:
        raise MaintenanceOperationError(f"{label}过长")
    return cleaned


def _project(db: Session, project_id: str) -> MaintenanceProject | None:
    return db.scalar(
        select(MaintenanceProject)
        .where(MaintenanceProject.project_id == project_id)
        .with_for_update()
    )


def _lock_project_for_fact_write(
    db: Session, project_id: str
) -> MaintenanceProject | None:
    """Use one global lock order: workbook state, then the concrete fact row."""

    exists = db.scalar(
        select(MaintenanceProject.project_id).where(
            MaintenanceProject.project_id == project_id
        )
    )
    if exists is None:
        return None
    get_or_create_workbook_state(db, project_id=project_id, lock=True)
    return _project(db, project_id)


def _audit_contract(
    db: Session,
    row: MaintenanceProjectContract,
    *,
    action: str,
    before: dict | None,
    after: dict | None,
    reason: str,
    operated_by: str,
) -> None:
    db.add(
        MaintenanceProjectAuditLog(
            project_id=row.project_id,
            entity_type="project_contract",
            entity_id=row.project_contract_id,
            action=action,
            before_json=before,
            after_json=after,
            reason=_required(reason, "操作原因", 1000),
            operated_by=_required(operated_by, "操作人"),
        )
    )


def _fact_audit(
    db: Session,
    *,
    project_id: str,
    entity_type: str,
    entity_id: str,
    action: str,
    before: dict | None,
    after: dict | None,
    reason: str,
    operated_by: str,
) -> None:
    db.add(
        MaintenanceProjectOperationAudit(
            project_id=project_id,
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            before_json=before,
            after_json=after,
            reason=_required(reason, "操作原因", 1000),
            operated_by=_required(operated_by, "操作人"),
        )
    )


def _money(value: Decimal | None) -> str | None:
    return format(value, ".2f") if value is not None else None


def _qty(value: Decimal) -> str:
    return format(value, ".3f")


def collection_dict(row: MaintenanceCollectionSnapshot) -> dict:
    return {
        "collection_id": row.collection_id,
        "project_id": row.project_id,
        "project_contract_id": row.project_contract_id,
        "report_month": row.report_month.isoformat(),
        "cumulative_amount": _money(row.cumulative_amount),
        "status": row.status,
        "receipt_reference": row.receipt_reference,
        "remark": row.remark,
        "version": row.version,
    }


def site_issue_line_dict(row: MaintenanceSiteIssueLine) -> dict:
    return {
        "issue_line_id": row.issue_line_id,
        "line_no": row.line_no,
        "part_id": row.part_id,
        "pn": row.pn,
        "quantity": _qty(row.quantity),
        "linked_purchase_line_id": row.linked_purchase_line_id,
        "manual_unit_cost": _money(row.manual_unit_cost),
        "manual_evidence": row.manual_evidence,
        "unit_cost": _money(row.unit_cost),
        "cost_amount": _money(row.cost_amount),
        "cost_source": row.cost_source,
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


def site_issue_dict(
    row: MaintenanceSiteIssue,
    lines: list[MaintenanceSiteIssueLine],
) -> dict:
    return {
        "issue_id": row.issue_id,
        "project_id": row.project_id,
        "issue_no": row.issue_no,
        "issue_date": row.issue_date.isoformat(),
        "raw_status": row.raw_status,
        "status_mapping_state": row.status_mapping_state,
        "normalized_status": row.normalized_status,
        "status_mapping_version": row.status_mapping_version,
        "version": row.version,
        "lines": [site_issue_line_dict(line) for line in lines],
    }


def expense_dict(row: MaintenanceProjectExpenseAttribution) -> dict:
    return {
        "expense_id": row.expense_id,
        "project_id": row.project_id,
        "project_contract_id": row.project_contract_id,
        "expense_ref": row.expense_ref,
        "expense_date": row.expense_date.isoformat(),
        "applicant": row.applicant,
        "category": row.category,
        "expense_reason": row.expense_reason,
        "amount_ex_tax": _money(row.amount_ex_tax),
        "raw_status": row.raw_status,
        "status_mapping_state": row.status_mapping_state,
        "normalized_status": row.normalized_status,
        "status_mapping_version": row.status_mapping_version,
        "version": row.version,
    }


def create_expense(
    db: Session,
    *,
    project_id: str,
    expense_id: str,
    project_contract_id: str | None,
    expense_ref: str,
    expense_date: date,
    applicant: str | None,
    category: str | None,
    expense_reason: str | None,
    amount_ex_tax: Decimal,
    raw_status: str,
    status_mapping_state: str,
    normalized_status: str,
    status_mapping_version: str,
    reason: str,
    operated_by: str,
) -> dict | None:
    project = _lock_project_for_fact_write(db, project_id)
    if project is None:
        return None
    if not project.is_active:
        raise MaintenanceOperationError("项目主档已归档")
    if status_mapping_state not in {"mapped", "unmapped"}:
        raise MaintenanceOperationError("报销状态映射结果无效")
    if normalized_status not in {"approved", "rejected", "void", "unknown"}:
        raise MaintenanceOperationError("报销标准状态无效")
    if status_mapping_state != "mapped" and normalized_status != "unknown":
        raise MaintenanceOperationError("未映射报销必须使用 unknown 标准状态")
    if amount_ex_tax < 0 or amount_ex_tax >= Decimal("1000000000000"):
        raise MaintenanceOperationError("报销未税金额超出允许范围")
    if project_contract_id is not None:
        relation = db.scalar(
            select(MaintenanceProjectContract).where(
                MaintenanceProjectContract.project_contract_id == project_contract_id,
                MaintenanceProjectContract.project_id == project_id,
            )
        )
        if relation is None:
            raise MaintenanceOperationError("报销关联合同不存在或不属于当前项目")
    row = MaintenanceProjectExpenseAttribution(
        expense_id=_required(expense_id, "报销稳定编号"),
        project_id=project_id,
        project_contract_id=project_contract_id,
        expense_ref=_required(expense_ref, "报销单号", 128),
        expense_date=expense_date,
        applicant=applicant.strip() if applicant and applicant.strip() else None,
        category=category.strip() if category and category.strip() else None,
        expense_reason=(
            expense_reason.strip()
            if expense_reason and expense_reason.strip()
            else None
        ),
        amount_ex_tax=amount_ex_tax,
        raw_status=_required(raw_status, "报销原始状态"),
        status_mapping_state=status_mapping_state,
        normalized_status=normalized_status,
        status_mapping_version=_required(status_mapping_version, "状态映射版本"),
        version=1,
    )
    db.add(row)
    db.flush()
    payload = expense_dict(row)
    _fact_audit(
        db,
        project_id=project_id,
        entity_type="expense",
        entity_id=row.expense_id,
        action="create",
        before=None,
        after=payload,
        reason=reason,
        operated_by=operated_by,
    )
    bump_workbook_revision(db, project_id=project_id)
    db.flush()
    return payload


def mark_expense_readiness(
    db: Session,
    *,
    project_id: str,
    ready_through: date,
    reason: str,
    operated_by: str,
) -> dict | None:
    """Confirm that the approved-expense feed is complete through one month."""

    project = _lock_project_for_fact_write(db, project_id)
    if project is None:
        return None
    if not project.is_active:
        raise MaintenanceOperationError("项目主档已归档")
    if ready_through.day != 1:
        raise MaintenanceOperationError("费用数据水位必须使用当月第一天")
    state = get_or_create_workbook_state(db, project_id=project_id, lock=True)
    if state.expense_ready_through is not None and ready_through < state.expense_ready_through:
        raise MaintenanceOperationConflict("费用数据水位不能回退")
    before = {
        "expense_ready_through": (
            state.expense_ready_through.isoformat()
            if state.expense_ready_through
            else None
        )
    }
    if state.expense_ready_through == ready_through:
        return {
            "project_id": project_id,
            **before,
            "data_version": state.data_version,
        }
    state.expense_ready_through = ready_through
    state.revision += 1
    state.data_version = _workbook_data_version(project_id, state.revision)
    after = {"expense_ready_through": ready_through.isoformat()}
    _fact_audit(
        db,
        project_id=project_id,
        entity_type="expense_readiness",
        entity_id=project_id,
        action="mark_ready",
        before=before,
        after=after,
        reason=reason,
        operated_by=operated_by,
    )
    db.flush()
    return {
        "project_id": project_id,
        **after,
        "data_version": state.data_version,
    }


def create_site_issue(
    db: Session,
    *,
    project_id: str,
    issue_no: str,
    issue_date: date,
    raw_status: str,
    status_mapping_state: str,
    normalized_status: str,
    status_mapping_version: str,
    lines: list[dict],
    reason: str,
    operated_by: str,
) -> dict | None:
    project = _lock_project_for_fact_write(db, project_id)
    if project is None:
        return None
    if not project.is_active:
        raise MaintenanceOperationError("项目主档已归档")
    if status_mapping_state not in {"mapped", "unmapped"}:
        raise MaintenanceOperationError("现场领用状态映射结果无效")
    if normalized_status not in {"confirmed", "void", "unknown"}:
        raise MaintenanceOperationError("现场领用标准状态无效")
    if status_mapping_state != "mapped" and normalized_status != "unknown":
        raise MaintenanceOperationError("未映射现场领用必须使用 unknown 标准状态")
    if not lines:
        raise MaintenanceOperationError("现场领用至少需要一条明细")
    row = MaintenanceSiteIssue(
        issue_id=str(uuid4()),
        project_id=project_id,
        issue_no=_required(issue_no, "现场领用单号"),
        issue_date=issue_date,
        raw_status=_required(raw_status, "现场领用原始状态"),
        status_mapping_state=status_mapping_state,
        normalized_status=normalized_status,
        status_mapping_version=_required(status_mapping_version, "状态映射版本"),
        version=1,
    )
    db.add(row)
    db.flush()
    saved_lines: list[MaintenanceSiteIssueLine] = []
    seen_line_ids: set[str] = set()
    seen_line_numbers: set[int] = set()
    for raw_line in lines:
        line_id = _required(raw_line.get("issue_line_id"), "领用明细稳定编号")
        line_no = int(raw_line["line_no"])
        if line_id in seen_line_ids or line_no in seen_line_numbers:
            raise MaintenanceOperationError("现场领用明细编号或行号重复")
        seen_line_ids.add(line_id)
        seen_line_numbers.add(line_no)
        quantity = Decimal(raw_line["quantity"])
        if quantity <= 0 or quantity >= Decimal("1000000000000"):
            raise MaintenanceOperationError("现场领用数量超出允许范围")
        line = MaintenanceSiteIssueLine(
            issue_line_id=line_id,
            issue_id=row.issue_id,
            line_no=line_no,
            part_id=int(raw_line["part_id"]),
            pn=_required(raw_line.get("pn"), "料号", 128),
            quantity=quantity,
            linked_purchase_line_id=raw_line.get("linked_purchase_line_id"),
            manual_unit_cost=None,
            reference_sample_ids=[],
            reference_sample_count=0,
            reference_samples=[],
            algorithm_version=maintenance_consumption_cost.ALGORITHM_VERSION,
            version=1,
        )
        db.add(line)
        db.flush()
        if status_mapping_state == "mapped" and normalized_status == "confirmed":
            maintenance_consumption_cost.resolve_line(db, issue_date=issue_date, line=line)
        saved_lines.append(line)
    payload = site_issue_dict(row, saved_lines)
    _fact_audit(
        db,
        project_id=project_id,
        entity_type="site_issue",
        entity_id=row.issue_id,
        action="create",
        before=None,
        after=payload,
        reason=reason,
        operated_by=operated_by,
    )
    bump_workbook_revision(db, project_id=project_id)
    db.flush()
    return payload


_SITE_ISSUE_STATUS_TRANSITIONS = {
    "unknown": {"confirmed", "void"},
    "confirmed": {"void"},
    "void": set(),
}

_EXPENSE_STATUS_TRANSITIONS = {
    "unknown": {"approved", "rejected", "void"},
    "rejected": {"approved", "void"},
    "approved": {"void"},
    "void": set(),
}


def _status_transition_allowed(
    transitions: dict[str, set[str]],
    *,
    current: str,
    target: str,
) -> bool:
    return target == current or target in transitions.get(current, set())


def update_site_issue_status(
    db: Session,
    *,
    issue_id: str,
    version: int,
    raw_status: str,
    normalized_status: str,
    status_mapping_version: str,
    reason: str,
    operated_by: str,
) -> dict | None:
    """Advance one site issue without deleting or erasing its cost evidence."""

    project_id = db.scalar(
        select(MaintenanceSiteIssue.project_id).where(
            MaintenanceSiteIssue.issue_id == issue_id
        )
    )
    if project_id is None:
        return None
    get_or_create_workbook_state(db, project_id=project_id, lock=True)
    project = _project(db, project_id)
    if project is None:
        return None
    if not project.is_active:
        raise MaintenanceOperationError("项目主档已归档")
    row = db.scalar(
        select(MaintenanceSiteIssue)
        .where(MaintenanceSiteIssue.issue_id == issue_id)
        .with_for_update()
    )
    if row is None:
        return None
    lines = list(
        db.scalars(
            select(MaintenanceSiteIssueLine)
            .where(MaintenanceSiteIssueLine.issue_id == issue_id)
            .order_by(MaintenanceSiteIssueLine.line_no)
            .with_for_update()
        )
    )
    if row.version != version:
        raise MaintenanceOperationConflict(
            f"现场领用单已变化（当前版本 {row.version}），请刷新后重试"
        )
    if normalized_status not in {"confirmed", "void"}:
        raise MaintenanceOperationError("现场领用状态只能更新为已确认或已作废")
    if not _status_transition_allowed(
        _SITE_ISSUE_STATUS_TRANSITIONS,
        current=row.normalized_status,
        target=normalized_status,
    ):
        raise MaintenanceOperationError(
            f"现场领用状态不能从 {row.normalized_status} 变更为 {normalized_status}"
        )

    before = site_issue_dict(row, lines)
    previous_status = row.normalized_status
    row.raw_status = _required(raw_status, "现场领用原始状态")
    row.status_mapping_state = "mapped"
    row.normalized_status = normalized_status
    row.status_mapping_version = _required(
        status_mapping_version, "状态映射版本"
    )
    if previous_status != "confirmed" and normalized_status == "confirmed":
        for line in lines:
            line_before = site_issue_line_dict(line)
            maintenance_consumption_cost.resolve_line(
                db,
                issue_date=row.issue_date,
                line=line,
            )
            if site_issue_line_dict(line) != line_before:
                line.version += 1

    candidate = site_issue_dict(row, lines)
    if candidate == before:
        return before
    row.version += 1
    after = site_issue_dict(row, lines)
    _fact_audit(
        db,
        project_id=project_id,
        entity_type="site_issue",
        entity_id=row.issue_id,
        action="status_update",
        before=before,
        after=after,
        reason=reason,
        operated_by=operated_by,
    )
    bump_workbook_revision(db, project_id=project_id)
    db.flush()
    return after


def update_expense_status(
    db: Session,
    *,
    expense_id: str,
    version: int,
    raw_status: str,
    normalized_status: str,
    status_mapping_version: str,
    reason: str,
    operated_by: str,
) -> dict | None:
    """Advance one attributed expense while preserving the original fact row."""

    project_id = db.scalar(
        select(MaintenanceProjectExpenseAttribution.project_id).where(
            MaintenanceProjectExpenseAttribution.expense_id == expense_id
        )
    )
    if project_id is None:
        return None
    get_or_create_workbook_state(db, project_id=project_id, lock=True)
    project = _project(db, project_id)
    if project is None:
        return None
    if not project.is_active:
        raise MaintenanceOperationError("项目主档已归档")
    row = db.scalar(
        select(MaintenanceProjectExpenseAttribution)
        .where(MaintenanceProjectExpenseAttribution.expense_id == expense_id)
        .with_for_update()
    )
    if row is None:
        return None
    if row.version != version:
        raise MaintenanceOperationConflict(
            f"报销归集事实已变化（当前版本 {row.version}），请刷新后重试"
        )
    if normalized_status not in {"approved", "rejected", "void", "unknown"}:
        raise MaintenanceOperationError("报销标准状态无效")
    if not _status_transition_allowed(
        _EXPENSE_STATUS_TRANSITIONS,
        current=row.normalized_status,
        target=normalized_status,
    ):
        raise MaintenanceOperationError(
            f"报销状态不能从 {row.normalized_status} 变更为 {normalized_status}"
        )

    before = expense_dict(row)
    row.raw_status = _required(raw_status, "报销原始状态")
    row.status_mapping_state = (
        "unmapped" if normalized_status == "unknown" else "mapped"
    )
    row.normalized_status = normalized_status
    row.status_mapping_version = _required(
        status_mapping_version, "状态映射版本"
    )
    candidate = expense_dict(row)
    if candidate == before:
        return before
    row.version += 1
    after = expense_dict(row)
    _fact_audit(
        db,
        project_id=project_id,
        entity_type="expense",
        entity_id=row.expense_id,
        action="status_update",
        before=before,
        after=after,
        reason=reason,
        operated_by=operated_by,
    )
    bump_workbook_revision(db, project_id=project_id)
    db.flush()
    return after


def list_cost_gaps(
    db: Session,
    *,
    project_id: str,
    page: int = 1,
    page_size: int = 20,
) -> dict | None:
    project = db.get(MaintenanceProject, project_id)
    if project is None:
        return None
    rows: list[dict] = []
    gaps = db.execute(
            select(MaintenanceSiteIssue, MaintenanceSiteIssueLine, DimPart)
            .join(
                MaintenanceSiteIssueLine,
                MaintenanceSiteIssueLine.issue_id == MaintenanceSiteIssue.issue_id,
            )
            .join(DimPart, DimPart.id == MaintenanceSiteIssueLine.part_id)
            .where(
                MaintenanceSiteIssue.project_id == project_id,
                MaintenanceSiteIssue.status_mapping_state == "mapped",
                MaintenanceSiteIssue.normalized_status == "confirmed",
                MaintenanceSiteIssueLine.cost_amount.is_(None),
            )
            .order_by(
                MaintenanceSiteIssue.issue_date,
                MaintenanceSiteIssue.issue_no,
                MaintenanceSiteIssueLine.line_no,
            )
        ).all()
    for issue, line, part in gaps:
        contract_numbers = list(
            db.scalars(
                select(MaintenanceProjectContract.contract_no)
                .where(
                    MaintenanceProjectContract.project_id == project_id,
                    MaintenanceProjectContract.included_in_total.is_(True),
                    MaintenanceProjectContract.effective_from <= issue.issue_date,
                    (
                        MaintenanceProjectContract.effective_to.is_(None)
                        | (MaintenanceProjectContract.effective_to > issue.issue_date)
                    ),
                )
                .order_by(MaintenanceProjectContract.contract_no)
            )
        )
        rows.append(
            {
                "line_id": line.issue_line_id,
                "version": line.version,
                "project_id": project_id,
                "project_code": project.project_code,
                "order_no": issue.issue_no,
                "order_date": issue.issue_date.isoformat(),
                "contract_no": " / ".join(contract_numbers) or None,
                "pn": line.pn,
                "description": part.description,
                "quantity": format(line.quantity, "f"),
                "current_unit_cost": _money(line.unit_cost),
                "references": [
                    {
                        "source": line.cost_source,
                        "sample_id": sample["sample_id"],
                        "document_no": sample.get("document_no"),
                        "document_date": sample.get("document_date"),
                        "distance_days": sample.get("distance_days"),
                        "sample_quantity": sample["quantity"],
                        "weighted_unit_price": sample["unit_price_ex_tax"],
                        "tax_conversion": sample["tax_conversion"],
                    }
                    for sample in line.reference_samples
                ],
                "cost_source": line.cost_source,
                "algorithm_version": line.algorithm_version,
                "price_basis": line.price_basis,
            }
        )
    total = len(rows)
    offset = (page - 1) * page_size
    state = db.get(MaintenanceProjectWorkbookState, project_id)
    return {
        "rows": rows[offset : offset + page_size],
        "total": total,
        "page": page,
        "page_size": page_size,
        "data_version": (
            state.data_version
            if state is not None
            else _workbook_data_version(project_id, 0)
        ),
    }


_AUTOMATIC_COST_SOURCES = {"direct_purchase", "purchase_window", "sales_window"}
_COST_RESOLUTION_FIELDS = (
    "unit_cost",
    "cost_amount",
    "cost_source",
    "price_basis",
    "reference_side",
    "reference_sample_ids",
    "reference_sample_count",
    "reference_samples",
    "reference_window_from",
    "reference_window_to",
    "algorithm_version",
)


def recompute_cost_gaps(
    db: Session,
    *,
    project_id: str,
    reason: str,
    operated_by: str,
) -> dict | None:
    """Persist newly available deterministic evidence for unresolved issue lines.

    A single project revision represents the whole controlled run, while every
    resolved line retains its own before/after audit and optimistic-lock version.
    Repeating a run after all possible matches are resolved is a no-op.
    """

    project = _lock_project_for_fact_write(db, project_id)
    if project is None:
        return None
    if not project.is_active:
        raise MaintenanceOperationError("项目主档已归档")
    state = get_or_create_workbook_state(db, project_id=project_id)
    candidates = list(
        db.execute(
            select(MaintenanceSiteIssueLine, MaintenanceSiteIssue)
            .join(
                MaintenanceSiteIssue,
                MaintenanceSiteIssue.issue_id == MaintenanceSiteIssueLine.issue_id,
            )
            .where(
                MaintenanceSiteIssue.project_id == project_id,
                MaintenanceSiteIssue.status_mapping_state == "mapped",
                MaintenanceSiteIssue.normalized_status == "confirmed",
                MaintenanceSiteIssueLine.cost_amount.is_(None),
            )
            .order_by(
                MaintenanceSiteIssue.issue_date,
                MaintenanceSiteIssue.issue_no,
                MaintenanceSiteIssueLine.line_no,
            )
            .with_for_update(of=MaintenanceSiteIssueLine)
        )
    )
    resolved = 0
    for line, issue in candidates:
        before = site_issue_line_dict(line)
        prior_resolution = {
            field: getattr(line, field) for field in _COST_RESOLUTION_FIELDS
        }
        maintenance_consumption_cost.resolve_line(
            db,
            issue_date=issue.issue_date,
            line=line,
        )
        if line.cost_source not in _AUTOMATIC_COST_SOURCES:
            for field, value in prior_resolution.items():
                setattr(line, field, value)
            continue
        line.version += 1
        after = site_issue_line_dict(line)
        _fact_audit(
            db,
            project_id=project_id,
            entity_type="site_issue_cost",
            entity_id=line.issue_line_id,
            action="auto_recompute",
            before=before,
            after=after,
            reason=reason,
            operated_by=operated_by,
        )
        resolved += 1

    if resolved:
        bump_locked_workbook_revision(db, state=state)
    db.flush()
    return {
        "resolved": resolved,
        "remaining": len(candidates) - resolved,
        "data_version": state.data_version,
    }


def fill_manual_cost(
    db: Session,
    *,
    project_id: str,
    issue_line_id: str,
    version: int,
    manual_unit_cost: Decimal,
    evidence: str,
    reason: str,
    operated_by: str,
) -> dict | None:
    project = _lock_project_for_fact_write(db, project_id)
    if project is None:
        return None
    if not project.is_active:
        raise MaintenanceOperationError("项目主档已归档")
    state = get_or_create_workbook_state(db, project_id=project_id)
    result = db.execute(
        select(MaintenanceSiteIssueLine, MaintenanceSiteIssue)
        .join(MaintenanceSiteIssue, MaintenanceSiteIssue.issue_id == MaintenanceSiteIssueLine.issue_id)
        .where(
            MaintenanceSiteIssueLine.issue_line_id == issue_line_id,
            MaintenanceSiteIssue.project_id == project_id,
        )
        .with_for_update()
    ).one_or_none()
    if result is None:
        return None
    line, issue = result
    if line.version != version:
        raise MaintenanceOperationConflict(
            f"领用成本明细已变化（当前版本 {line.version}），请刷新后重试"
        )
    if issue.status_mapping_state != "mapped" or issue.normalized_status != "confirmed":
        raise MaintenanceOperationError("只有已确认且状态已映射的现场领用可以补价")
    if manual_unit_cost < 0 or manual_unit_cost >= Decimal("1000000000000"):
        raise MaintenanceOperationError("人工未税单价超出允许范围")
    before = site_issue_line_dict(line)
    if line.cost_source in _AUTOMATIC_COST_SOURCES and line.cost_amount is not None:
        raise MaintenanceOperationConflict("已有自动价格证据，人工补价不能覆盖")
    previous_manual = line.manual_unit_cost
    previous_evidence = line.manual_evidence
    line.manual_unit_cost = None
    line.manual_evidence = None
    maintenance_consumption_cost.resolve_line(db, issue_date=issue.issue_date, line=line)
    if line.cost_source in _AUTOMATIC_COST_SOURCES:
        line.manual_unit_cost = previous_manual
        line.manual_evidence = previous_evidence
        maintenance_consumption_cost.resolve_line(db, issue_date=issue.issue_date, line=line)
        line.version += 1
        after = site_issue_line_dict(line)
        _fact_audit(
            db,
            project_id=issue.project_id,
            entity_type="site_issue_cost",
            entity_id=line.issue_line_id,
            action="auto_recompute",
            before=before,
            after=after,
            reason=reason,
            operated_by=operated_by,
        )
        bump_locked_workbook_revision(db, state=state)
        db.flush()
        return {
            **after,
            "manual_applied": False,
            "resolution": "automatic_evidence",
        }
    line.manual_unit_cost = manual_unit_cost
    line.manual_evidence = _required(evidence, "人工补价证据", 1000)
    maintenance_consumption_cost.resolve_line(db, issue_date=issue.issue_date, line=line)
    line.version += 1
    after = site_issue_line_dict(line)
    _fact_audit(
        db,
        project_id=issue.project_id,
        entity_type="site_issue_cost",
        entity_id=line.issue_line_id,
        action="manual_fill",
        before=before,
        after=after,
        reason=reason,
        operated_by=operated_by,
    )
    bump_locked_workbook_revision(db, state=state)
    db.flush()
    return {**after, "manual_applied": True, "resolution": "manual"}


def create_contract(
    db: Session,
    *,
    project_id: str,
    contract_id: str,
    contract_no: str,
    contract_amount: Decimal | None,
    contract_status: str | None,
    status_mapping_state: str,
    status_mapping_version: str,
    included_in_total: bool,
    effective_from: date,
    effective_to: date | None,
    source: str,
    reason: str,
    operated_by: str,
) -> dict | None:
    project = _lock_project_for_fact_write(db, project_id)
    if project is None:
        return None
    if not project.is_active:
        raise MaintenanceOperationError("项目主档已归档")
    if status_mapping_state not in {"mapped", "unmapped"}:
        raise MaintenanceOperationError("合同状态映射结果无效")
    if status_mapping_state != "mapped" and included_in_total:
        raise MaintenanceOperationError("未映射合同不能计入合同总额")
    if effective_to is not None and effective_to <= effective_from:
        raise MaintenanceOperationError("合同关系结束日期必须晚于开始日期")
    if contract_amount is not None and (
        contract_amount < 0 or contract_amount >= Decimal("1000000000000")
    ):
        raise MaintenanceOperationError("合同金额超出允许范围")
    row = MaintenanceProjectContract(
        project_contract_id=str(uuid4()),
        project_id=project_id,
        contract_id=_required(contract_id, "合同稳定编号"),
        contract_no=_required(contract_no, "合同编号"),
        contract_amount=contract_amount,
        contract_status=(str(contract_status).strip() if contract_status else None),
        status_mapping_state=status_mapping_state,
        status_mapping_version=_required(status_mapping_version, "状态映射版本"),
        included_in_total=included_in_total,
        effective_from=effective_from,
        effective_to=effective_to,
        source=_required(source, "合同来源"),
        version=1,
    )
    db.add(row)
    db.flush()
    payload = contract_dict(row)
    _audit_contract(
        db,
        row,
        action="create",
        before=None,
        after=payload,
        reason=reason,
        operated_by=operated_by,
    )
    bump_workbook_revision(db, project_id=project_id)
    db.flush()
    return payload


def update_contract(
    db: Session,
    *,
    project_contract_id: str,
    version: int,
    updates: dict,
    reason: str,
    operated_by: str,
) -> dict | None:
    project_id = db.scalar(
        select(MaintenanceProjectContract.project_id).where(
            MaintenanceProjectContract.project_contract_id == project_contract_id
        )
    )
    if project_id is None:
        return None
    get_or_create_workbook_state(db, project_id=project_id, lock=True)
    project = _project(db, project_id)
    if project is None:
        return None
    if not project.is_active:
        raise MaintenanceOperationError("项目主档已归档")
    row = db.scalar(
        select(MaintenanceProjectContract)
        .where(MaintenanceProjectContract.project_contract_id == project_contract_id)
        .with_for_update()
    )
    if row is None:
        return None
    if row.version != version:
        raise MaintenanceOperationConflict(
            f"项目合同关系已变化（当前版本 {row.version}），请刷新后重试"
        )
    before = contract_dict(row)
    allowed = {
        "contract_no",
        "contract_amount",
        "contract_status",
        "status_mapping_state",
        "status_mapping_version",
        "included_in_total",
        "effective_from",
        "effective_to",
        "source",
    }
    values = {key: value for key, value in updates.items() if key in allowed}
    if not values:
        raise MaintenanceOperationError("没有可修改的合同关系字段")
    non_nullable = {
        "contract_no",
        "status_mapping_state",
        "status_mapping_version",
        "included_in_total",
        "effective_from",
        "source",
    }
    if any(key in values and values[key] is None for key in non_nullable):
        raise MaintenanceOperationError("合同关系必填字段不能清空")
    for key, value in values.items():
        if key in {"contract_no", "status_mapping_version", "source"}:
            value = _required(value, {
                "contract_no": "合同编号",
                "status_mapping_version": "状态映射版本",
                "source": "合同来源",
            }[key])
        elif key == "contract_status":
            value = str(value).strip() if value else None
        setattr(row, key, value)
    if row.status_mapping_state not in {"mapped", "unmapped"}:
        raise MaintenanceOperationError("合同状态映射结果无效")
    if row.status_mapping_state != "mapped" and row.included_in_total:
        raise MaintenanceOperationError("未映射合同不能计入合同总额")
    if row.effective_to is not None and row.effective_to <= row.effective_from:
        raise MaintenanceOperationError("合同关系结束日期必须晚于开始日期")
    if row.contract_amount is not None and (
        row.contract_amount < 0 or row.contract_amount >= Decimal("1000000000000")
    ):
        raise MaintenanceOperationError("合同金额超出允许范围")
    after = contract_dict(row)
    if after == before:
        return before
    row.version += 1
    after = contract_dict(row)
    _audit_contract(
        db,
        row,
        action="update",
        before=before,
        after=after,
        reason=reason,
        operated_by=operated_by,
    )
    bump_workbook_revision(db, project_id=row.project_id)
    db.flush()
    return after


def archive_contract(
    db: Session,
    *,
    project_contract_id: str,
    version: int,
    effective_to: date,
    reason: str,
    operated_by: str,
) -> dict | None:
    project_id = db.scalar(
        select(MaintenanceProjectContract.project_id).where(
            MaintenanceProjectContract.project_contract_id == project_contract_id
        )
    )
    if project_id is None:
        return None
    get_or_create_workbook_state(db, project_id=project_id, lock=True)
    project = _project(db, project_id)
    if project is None:
        return None
    if not project.is_active:
        raise MaintenanceOperationError("项目主档已归档")
    row = db.scalar(
        select(MaintenanceProjectContract)
        .where(MaintenanceProjectContract.project_contract_id == project_contract_id)
        .with_for_update()
    )
    if row is None:
        return None
    if row.version != version:
        raise MaintenanceOperationConflict(
            f"项目合同关系已变化（当前版本 {row.version}），请刷新后重试"
        )
    if effective_to <= row.effective_from:
        raise MaintenanceOperationError("合同关系结束日期必须晚于开始日期")
    before = contract_dict(row)
    row.effective_to = effective_to
    row.version += 1
    after = contract_dict(row)
    _audit_contract(
        db,
        row,
        action="archive",
        before=before,
        after=after,
        reason=reason,
        operated_by=operated_by,
    )
    bump_workbook_revision(db, project_id=row.project_id)
    db.flush()
    return after


def create_collection(
    db: Session,
    *,
    project_id: str,
    project_contract_id: str,
    report_month: date,
    cumulative_amount: Decimal,
    status: str,
    receipt_reference: str | None,
    remark: str | None,
    reason: str,
    operated_by: str,
    bump_revision: bool = True,
) -> dict | None:
    project = _lock_project_for_fact_write(db, project_id)
    if project is None:
        return None
    if not project.is_active:
        raise MaintenanceOperationError("项目主档已归档")
    relation = db.scalar(
        select(MaintenanceProjectContract).where(
            MaintenanceProjectContract.project_contract_id == project_contract_id,
            MaintenanceProjectContract.project_id == project_id,
        )
    )
    if relation is None:
        raise MaintenanceOperationError("项目合同关系不存在或不属于当前项目")
    if report_month.day != 1:
        raise MaintenanceOperationError("回款报告月份必须使用当月第一天")
    if status not in {"confirmed", "unconfirmed", "void"}:
        raise MaintenanceOperationError("回款确认状态无效")
    if cumulative_amount < 0 or cumulative_amount >= Decimal("1000000000000"):
        raise MaintenanceOperationError("累计回款超出允许范围")
    row = MaintenanceCollectionSnapshot(
        collection_id=str(uuid4()),
        project_id=project_id,
        project_contract_id=project_contract_id,
        report_month=report_month,
        cumulative_amount=cumulative_amount,
        status=status,
        receipt_reference=(receipt_reference.strip() if receipt_reference else None),
        remark=(remark.strip() if remark else None),
        version=1,
    )
    db.add(row)
    db.flush()
    payload = collection_dict(row)
    _fact_audit(
        db,
        project_id=project_id,
        entity_type="collection",
        entity_id=row.collection_id,
        action="create",
        before=None,
        after=payload,
        reason=reason,
        operated_by=operated_by,
    )
    if bump_revision:
        bump_workbook_revision(db, project_id=project_id)
    db.flush()
    return payload


def update_collection(
    db: Session,
    *,
    collection_id: str,
    version: int,
    updates: dict,
    reason: str,
    operated_by: str,
    bump_revision: bool = True,
) -> dict | None:
    project_id = db.scalar(
        select(MaintenanceCollectionSnapshot.project_id).where(
            MaintenanceCollectionSnapshot.collection_id == collection_id
        )
    )
    if project_id is None:
        return None
    get_or_create_workbook_state(db, project_id=project_id, lock=True)
    project = _project(db, project_id)
    if project is None:
        return None
    if not project.is_active:
        raise MaintenanceOperationError("项目主档已归档")
    row = db.scalar(
        select(MaintenanceCollectionSnapshot)
        .where(MaintenanceCollectionSnapshot.collection_id == collection_id)
        .with_for_update()
    )
    if row is None:
        return None
    if row.version != version:
        raise MaintenanceOperationConflict(
            f"回款快照已变化（当前版本 {row.version}），请刷新后重试"
        )
    before = collection_dict(row)
    if any(
        key in updates and updates[key] is None
        for key in {"cumulative_amount", "status"}
    ):
        raise MaintenanceOperationError("累计回款和确认状态不能清空")
    for key in {"cumulative_amount", "status", "receipt_reference", "remark"}:
        if key in updates:
            setattr(row, key, updates[key])
    if row.status not in {"confirmed", "unconfirmed", "void"}:
        raise MaintenanceOperationError("回款确认状态无效")
    if (
        row.cumulative_amount < 0
        or row.cumulative_amount >= Decimal("1000000000000")
    ):
        raise MaintenanceOperationError("累计回款超出允许范围")
    after = collection_dict(row)
    if after == before:
        return before
    row.version += 1
    after = collection_dict(row)
    _fact_audit(
        db,
        project_id=row.project_id,
        entity_type="collection",
        entity_id=row.collection_id,
        action="update",
        before=before,
        after=after,
        reason=reason,
        operated_by=operated_by,
    )
    if bump_revision:
        bump_workbook_revision(db, project_id=row.project_id)
    db.flush()
    return after


def _payload_token(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _task(
    *,
    project_id: str,
    rule_key: str,
    severity: str,
    title: str,
    detail: str,
    entity_id: str | None = None,
    task_type: str | None = None,
    due_date: date | None = None,
    task_status: str = "open",
    owner: str | None = None,
) -> dict:
    identity = f"{project_id}:{rule_key}:{entity_id or '-'}"
    return {
        "task_id": hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32],
        "project_id": project_id,
        "rule_key": rule_key,
        "severity": severity,
        "title": title,
        "detail": detail,
        "entity_id": entity_id,
        "task_type": task_type or rule_key.split(":", 1)[0],
        "due_date": due_date.isoformat() if due_date else None,
        "status": task_status,
        "owner": owner,
        "generated_by": "system",
    }


def _system_tasks(
    *,
    project_id: str,
    completeness: dict,
    has_confirmed_collection: bool,
    confirmed_collection: Decimal,
    total_contract_amount: Decimal | None,
    cost_gap_count: int,
    cost_status: str,
    as_of: date,
    project_manager_id: str | None,
    last_applied_at: datetime | None,
) -> list[dict]:
    tasks: list[dict] = []
    due_date = date(as_of.year, as_of.month, monthrange(as_of.year, as_of.month)[1])
    applied_date = business_today(last_applied_at) if last_applied_at else None
    monthly_completed = bool(
        applied_date
        and applied_date.year == as_of.year
        and applied_date.month == as_of.month
    )
    tasks.append(
        _task(
            project_id=project_id,
            rule_key=f"manager_update:{as_of:%Y-%m}",
            severity=("info" if monthly_completed or as_of < due_date else "warning"),
            title=(
                f"{as_of:%Y年%m月}项目工作簿已回填"
                if monthly_completed
                else f"请回填{as_of:%Y年%m月}项目工作簿"
            ),
            detail=(
                f"本月工作簿已于 {applied_date.isoformat()} 应用"
                if monthly_completed
                else "下载全量四表，在 01_总览回款表尾追加后上传并应用"
            ),
            task_type="项目经理月度更新",
            due_date=due_date,
            task_status="completed" if monthly_completed else "pending",
            owner=project_manager_id,
        )
    )
    for issue in completeness.get("issues", []):
        code = str(issue.get("code") or "incomplete")
        if code == "expense_data_not_ready":
            title = "确认本月审批报销数据已就绪"
            detail = (
                f"当前费用水位 {issue.get('ready_through') or '未确认'}；"
                f"需要覆盖 {issue.get('required_month') or '本月'}"
            )
        else:
            title = "补全项目经营事实"
            detail = code
        tasks.append(
            _task(
                project_id=project_id,
                rule_key=f"completeness:{code}",
                severity="warning",
                title=title,
                detail=detail,
                owner=project_manager_id,
            )
        )
    if not has_confirmed_collection:
        tasks.append(
            _task(
                project_id=project_id,
                rule_key="collection:missing_confirmed",
                severity="info",
                title="补充已确认累计回款",
                detail="当前没有已确认的累计回款快照",
                owner=project_manager_id,
            )
        )
    elif (
        total_contract_amount is not None
        and total_contract_amount > 0
        and confirmed_collection < total_contract_amount
    ):
        tasks.append(
            _task(
                project_id=project_id,
                rule_key="collection:incomplete",
                severity="info",
                title="项目回款尚未完成",
                detail="当前已确认累计回款低于全部合同额",
                owner=project_manager_id,
            )
        )
    if cost_gap_count:
        tasks.append(
            _task(
                project_id=project_id,
                rule_key="cost:missing_price",
                severity="warning",
                title="回填现场领用缺价",
                detail=f"仍有 {cost_gap_count} 条已确认领用缺少成本",
                owner=project_manager_id,
            )
        )
    if cost_status in {"yellow", "red"}:
        tasks.append(
            _task(
                project_id=project_id,
                rule_key=f"cost_ratio:{cost_status}",
                severity="critical" if cost_status == "red" else "warning",
                title="项目成本达到预警阈值",
                detail="成本已超过合同额" if cost_status == "red" else "成本已达到合同额 80%",
                owner=project_manager_id,
            )
        )
    return sorted(tasks, key=lambda row: (row["severity"], row["rule_key"], row["task_id"]))


def _visible_tasks(
    reminders: list[dict],
    completeness: dict,
    *,
    user_ctx: UserContext,
) -> tuple[list[dict], dict]:
    """Apply the same fail-closed task visibility to detail and directory reads."""

    cost_restricted = is_field_hidden(user_ctx, "unit_cost")
    profit_restricted = is_field_hidden(user_ctx, "contract_amount")
    expense_restricted = is_field_hidden(user_ctx, "expense_inc")
    hidden_issue_codes: set[str] = set()
    if cost_restricted:
        hidden_issue_codes.update(
            {"missing_consumption_cost", "unmapped_site_issue_status"}
        )
    if expense_restricted:
        hidden_issue_codes.update(
            {"unmapped_expense_status", "expense_data_not_ready"}
        )
    if completeness.get("status") != "restricted" and hidden_issue_codes:
        visible_issues = [
            issue
            for issue in completeness.get("issues", [])
            if issue.get("code") not in hidden_issue_codes
        ]
        completeness = {
            "status": "incomplete" if visible_issues else "complete",
            "issues": visible_issues,
        }
        reminders = [
            row
            for row in reminders
            if not any(row["rule_key"].endswith(code) for code in hidden_issue_codes)
        ]
    if cost_restricted:
        reminders = [
            row for row in reminders if not row["rule_key"].startswith("cost")
        ]
    if profit_restricted:
        reminders = [
            row
            for row in reminders
            if not row["rule_key"].startswith(("collection:", "cost_ratio:"))
        ]
    if expense_restricted:
        reminders = [
            row for row in reminders if not row["rule_key"].startswith("cost_ratio:")
        ]
    return reminders, completeness


def project_workspace(
    db: Session,
    *,
    project_id: str,
    as_of: date,
    user_ctx: UserContext,
) -> dict | None:
    base = maintenance_project.project_overview(
        db,
        project_id,
        as_of=as_of,
        user_ctx=user_ctx,
    )
    if base is None:
        return None
    effective_ids = {
        row["project_contract_id"]
        for row in base["contracts"]
        if row["is_effective"]
    }
    latest_confirmed: dict[str, Decimal] = {}
    if effective_ids:
        for relation_id, amount in db.execute(
            select(
                MaintenanceCollectionSnapshot.project_contract_id,
                MaintenanceCollectionSnapshot.cumulative_amount,
            )
            .where(
                MaintenanceCollectionSnapshot.project_contract_id.in_(effective_ids),
                MaintenanceCollectionSnapshot.status == "confirmed",
                MaintenanceCollectionSnapshot.report_month <= as_of,
            )
            .order_by(
                MaintenanceCollectionSnapshot.project_contract_id,
                MaintenanceCollectionSnapshot.report_month.desc(),
                MaintenanceCollectionSnapshot.collection_id.desc(),
            )
        ):
            latest_confirmed.setdefault(relation_id, amount)
    confirmed_collection = sum(latest_confirmed.values(), start=Decimal("0.00"))
    total = base["total_contract_amount"]
    collection_progress = (
        (confirmed_collection / Decimal(total) * Decimal("100")).quantize(Decimal("0.01"))
        if total is not None and Decimal(total) > 0
        else None
    )
    requisition_rows: list[dict] = []
    consumed_known = Decimal("0.00")
    cost_gap_count = 0
    issue_rows = db.execute(
        select(MaintenanceSiteIssue, MaintenanceSiteIssueLine)
        .join(
            MaintenanceSiteIssueLine,
            MaintenanceSiteIssueLine.issue_id == MaintenanceSiteIssue.issue_id,
        )
        .where(
            MaintenanceSiteIssue.project_id == project_id,
            MaintenanceSiteIssue.issue_date <= as_of,
        )
        .order_by(
            MaintenanceSiteIssue.issue_date,
            MaintenanceSiteIssue.issue_no,
            MaintenanceSiteIssueLine.line_no,
        )
    ).all()
    unmapped_issue_count = 0
    for issue, line in issue_rows:
        eligible = (
            issue.status_mapping_state == "mapped"
            and issue.normalized_status == "confirmed"
        )
        if issue.status_mapping_state != "mapped":
            unmapped_issue_count += 1
        if eligible and line.cost_amount is None:
            cost_gap_count += 1
        if eligible and line.cost_amount is not None:
            consumed_known += Decimal(line.cost_amount)
        requisition_rows.append(
            {
                "issue_id": issue.issue_id,
                "issue_no": issue.issue_no,
                "issue_date": issue.issue_date.isoformat(),
                "status_mapping_state": issue.status_mapping_state,
                "normalized_status": issue.normalized_status,
                **site_issue_line_dict(line),
                "counts_cost": eligible,
            }
        )

    expense_rows = list(
        db.scalars(
            select(MaintenanceProjectExpenseAttribution)
            .where(
                MaintenanceProjectExpenseAttribution.project_id == project_id,
                MaintenanceProjectExpenseAttribution.expense_date <= as_of,
            )
            .order_by(
                MaintenanceProjectExpenseAttribution.expense_date,
                MaintenanceProjectExpenseAttribution.expense_ref,
            )
        )
    )
    approved_expense_rows = [
        row
        for row in expense_rows
        if row.status_mapping_state == "mapped"
        and row.normalized_status == "approved"
    ]
    approved_expense = sum(
        (Decimal(row.amount_ex_tax) for row in approved_expense_rows),
        start=Decimal("0.00"),
    )
    unmapped_expense_count = sum(
        1 for row in expense_rows if row.status_mapping_state != "mapped"
    )
    state = db.get(MaintenanceProjectWorkbookState, project_id)
    expense_ready_through = state.expense_ready_through if state else None
    expense_data_ready = bool(
        expense_ready_through
        and expense_ready_through >= as_of.replace(day=1)
    )
    actual_cost_known = consumed_known + approved_expense
    cost_rate = (
        (actual_cost_known / Decimal(total) * Decimal("100")).quantize(Decimal("0.01"))
        if total is not None and Decimal(total) > 0
        else None
    )
    if cost_rate is None:
        cost_status = "unknown"
    elif cost_rate > Decimal("100"):
        cost_status = "red"
    elif cost_rate >= Decimal("80"):
        cost_status = "yellow"
    elif (
        cost_gap_count
        or unmapped_issue_count
        or unmapped_expense_count
        or not expense_data_ready
    ):
        cost_status = "unknown"
    else:
        cost_status = "normal"

    completeness_issues = list(base["completeness"].get("issues", []))
    if cost_gap_count:
        completeness_issues.append(
            {"code": "missing_consumption_cost", "line_count": cost_gap_count}
        )
    if unmapped_issue_count:
        completeness_issues.append(
            {"code": "unmapped_site_issue_status", "line_count": unmapped_issue_count}
        )
    if unmapped_expense_count:
        completeness_issues.append(
            {"code": "unmapped_expense_status", "row_count": unmapped_expense_count}
        )
    if not expense_data_ready:
        completeness_issues.append(
            {
                "code": "expense_data_not_ready",
                "ready_through": (
                    expense_ready_through.isoformat()
                    if expense_ready_through
                    else None
                ),
                "required_month": as_of.strftime("%Y-%m"),
            }
        )
    completeness = dict(base["completeness"])
    if completeness.get("status") != "restricted" and completeness_issues:
        completeness = {"status": "incomplete", "issues": completeness_issues}

    effective_contracts = [row for row in base["contracts"] if row["is_effective"]]
    known_contract_amount = sum(
        (
            Decimal(row["contract_amount"])
            for row in effective_contracts
            if row["contract_amount"] is not None
        ),
        start=Decimal("0.00"),
    )
    contract_numbers_by_id = {
        row["project_contract_id"]: row["contract_no"] for row in base["contracts"]
    }
    contract_rows = [
        {
            **row,
            "amount_status": (
                "restricted"
                if is_field_hidden(user_ctx, "contract_amount")
                else "missing" if row["contract_amount"] is None else "available"
            ),
            "received_amount": (
                None
                if is_field_hidden(user_ctx, "contract_amount")
                else _money(latest_confirmed.get(row["project_contract_id"]))
            ),
        }
        for row in base["contracts"]
    ]
    project_summary = {
        **base["project"],
        "contracts": contract_rows,
        "metrics": {
            "total_contract_amount": _money(total),
            "known_contract_amount": _money(known_contract_amount),
            "contract_amount_complete": base["completeness"]["status"] == "complete",
            "received_amount": _money(confirmed_collection),
            "collection_progress_pct": _money(collection_progress),
            "site_requisition_known_cost": _money(consumed_known),
            "approved_expense": _money(approved_expense),
            "actual_project_cost_known": _money(actual_cost_known),
            "cost_rate_lower_bound_pct": _money(cost_rate),
            "cost_status": cost_status,
            "cost_complete": (
                cost_gap_count == 0
                and unmapped_issue_count == 0
                and unmapped_expense_count == 0
                and expense_data_ready
            ),
            "missing_cost_lines": cost_gap_count,
            "expense_data_ready": expense_data_ready,
            "expense_ready_through": (
                expense_ready_through.isoformat()
                if expense_ready_through
                else None
            ),
        },
        "reminder_count": 0,
        "as_of": as_of.isoformat(),
    }
    reminders = _system_tasks(
        project_id=project_id,
        completeness=completeness,
        has_confirmed_collection=bool(latest_confirmed),
        confirmed_collection=confirmed_collection,
        total_contract_amount=(Decimal(total) if total is not None else None),
        cost_gap_count=cost_gap_count,
        cost_status=cost_status,
        as_of=as_of,
        project_manager_id=base["project"]["project_manager_id"],
        last_applied_at=state.last_applied_at if state else None,
    )
    cost_restricted = is_field_hidden(user_ctx, "unit_cost")
    profit_restricted = is_field_hidden(user_ctx, "contract_amount")
    expense_restricted = is_field_hidden(user_ctx, "expense_inc")
    reminders, completeness = _visible_tasks(
        reminders,
        completeness,
        user_ctx=user_ctx,
    )
    if cost_restricted:
        hidden_cost_keys = {
            "manual_unit_cost",
            "manual_evidence",
            "unit_cost",
            "cost_amount",
            "cost_source",
            "price_basis",
            "reference_side",
            "reference_sample_ids",
            "reference_sample_count",
            "reference_samples",
            "reference_window_from",
            "reference_window_to",
            "algorithm_version",
        }
        requisition_rows = [
            {
                key: ([] if key in {"reference_sample_ids", "reference_samples"} else None)
                if key in hidden_cost_keys
                else value
                for key, value in row.items()
            }
            for row in requisition_rows
        ]
    if expense_restricted:
        approved_expense_rows = []
    project_summary["metrics"].update(
        {
            "contract_amount_complete": (
                None
                if profit_restricted
                else project_summary["metrics"]["contract_amount_complete"]
            ),
            "known_contract_amount": (
                None
                if profit_restricted
                else project_summary["metrics"]["known_contract_amount"]
            ),
            "received_amount": (
                None if profit_restricted else project_summary["metrics"]["received_amount"]
            ),
            "collection_progress_pct": (
                None
                if profit_restricted
                else project_summary["metrics"]["collection_progress_pct"]
            ),
            "site_requisition_known_cost": (
                None
                if cost_restricted
                else project_summary["metrics"]["site_requisition_known_cost"]
            ),
            "approved_expense": (
                None
                if expense_restricted
                else project_summary["metrics"]["approved_expense"]
            ),
            "actual_project_cost_known": (
                None
                if cost_restricted or expense_restricted
                else project_summary["metrics"]["actual_project_cost_known"]
            ),
            "cost_rate_lower_bound_pct": (
                None
                if cost_restricted or expense_restricted or profit_restricted
                else project_summary["metrics"]["cost_rate_lower_bound_pct"]
            ),
            "cost_status": (
                None
                if cost_restricted or expense_restricted or profit_restricted
                else project_summary["metrics"]["cost_status"]
            ),
            "cost_complete": (
                None
                if cost_restricted or expense_restricted
                else project_summary["metrics"]["cost_complete"]
            ),
            "missing_cost_lines": (
                None
                if cost_restricted
                else project_summary["metrics"]["missing_cost_lines"]
            ),
        }
    )
    project_summary["reminder_count"] = sum(
        1 for row in reminders if row["status"] != "completed"
    )
    applicable_contracts_by_date: dict[date, str | None] = {}
    part_ids = {line.part_id for _issue, line in issue_rows}
    part_descriptions = {
        part_id: description
        for part_id, description in db.execute(
            select(DimPart.id, DimPart.description).where(DimPart.id.in_(part_ids))
        )
    } if part_ids else {}

    def contract_numbers_on(issue_date: date) -> str | None:
        if issue_date not in applicable_contracts_by_date:
            numbers = [
                row["contract_no"]
                for row in base["contracts"]
                if row["included_in_total"]
                and date.fromisoformat(row["effective_from"]) <= issue_date
                and (
                    row["effective_to"] is None
                    or issue_date < date.fromisoformat(row["effective_to"])
                )
            ]
            applicable_contracts_by_date[issue_date] = " / ".join(numbers) or None
        return applicable_contracts_by_date[issue_date]

    requisition_payload_rows = [
        {
            **row,
            "line_id": row["issue_line_id"],
            "order_no": row["issue_no"],
            "order_date": row["issue_date"],
            "contract_no": contract_numbers_on(date.fromisoformat(row["issue_date"])),
            "description": part_descriptions.get(row["part_id"]),
            "cost_status": (
                "restricted"
                if cost_restricted
                else "not_counted" if not row["counts_cost"]
                else "missing" if row["cost_amount"] is None
                else "available"
            ),
        }
        for row in requisition_rows
    ]
    expense_payload_rows = [
        {
            **expense_dict(row),
            "expense_no": row.expense_ref,
            "contract_no": contract_numbers_by_id.get(row.project_contract_id),
            "category": row.category,
            "reason": row.expense_reason,
            "amount": _money(row.amount_ex_tax),
            "approval_status": "approved",
            "counts_cost": True,
        }
        for row in approved_expense_rows
    ]
    reminder_rows = [
        {
            **row,
            "reminder_id": row["task_id"],
            "type": row["task_type"],
        }
        for row in reminders
    ]
    last_exported_at = (
        state.last_exported_at.isoformat() if state and state.last_exported_at else None
    )
    payload = {
        "project": project_summary,
        "requisitions": {
            "rows": requisition_payload_rows,
            "total": len(requisition_payload_rows),
        },
        "approved_expenses": {
            "rows": expense_payload_rows,
            "total": len(expense_payload_rows),
        },
        "reminders": reminder_rows,
        "workbook_preview": {
            "protocol_version": "2.0",
            "sheets": [
                {"code": "overview", "name": "01_总览", "row_count": 1, "ownership": "append_only"},
                {"code": "site_requisitions", "name": "02_备件消耗", "row_count": len(requisition_payload_rows), "ownership": "system"},
                {"code": "approved_expenses", "name": "03_报销单", "row_count": len(expense_payload_rows), "ownership": "system"},
                {"code": "manager_tracking", "name": "04_项目经理追踪与提醒", "row_count": len(reminder_rows), "ownership": "system"},
            ],
            "latest_tracking_month": as_of.strftime("%Y-%m"),
            "last_exported_at": last_exported_at,
            "data_version": (
                state.data_version
                if state is not None
                else _workbook_data_version(project_id, 0)
            ),
        },
        "as_of": as_of.isoformat(),
        "completeness": completeness,
    }
    payload["data_version"] = _payload_token(payload)
    return payload


def project_tasks(
    db: Session,
    *,
    project_id: str,
    as_of: date,
    user_ctx: UserContext,
) -> dict | None:
    workspace = project_workspace(db, project_id=project_id, as_of=as_of, user_ctx=user_ctx)
    if workspace is None:
        return None
    return {
        "project_id": project_id,
        "rows": workspace["reminders"],
        "total": len(workspace["reminders"]),
        "as_of": as_of.isoformat(),
        "data_version": workspace["data_version"],
    }


def project_workbook_workspace(
    db: Session,
    *,
    project_id: str,
    as_of: date,
    user_ctx: UserContext,
) -> dict | None:
    """Canonical, read-only input for the generated four-sheet v2 workbook."""

    workspace = project_workspace(db, project_id=project_id, as_of=as_of, user_ctx=user_ctx)
    if workspace is None:
        return None
    state = db.get(MaintenanceProjectWorkbookState, project_id)
    revision = state.revision if state is not None else 0
    collections = [
        collection_dict(row)
        for row in db.scalars(
            select(MaintenanceCollectionSnapshot)
            .where(
                MaintenanceCollectionSnapshot.project_id == project_id,
                MaintenanceCollectionSnapshot.report_month <= as_of,
            )
            .order_by(
                MaintenanceCollectionSnapshot.project_contract_id,
                MaintenanceCollectionSnapshot.report_month,
            )
        )
    ]
    if is_field_hidden(user_ctx, "contract_amount"):
        collections = [
            {
                **row,
                "cumulative_amount": None,
                "receipt_reference": None,
            }
            for row in collections
        ]
    expense_attributions = [
        {
            **expense_dict(row),
            "eligible": (
                row.status_mapping_state == "mapped"
                and row.normalized_status == "approved"
            ),
        }
        for row in db.scalars(
            select(MaintenanceProjectExpenseAttribution)
            .where(
                MaintenanceProjectExpenseAttribution.project_id == project_id,
                MaintenanceProjectExpenseAttribution.expense_date <= as_of,
            )
            .order_by(
                MaintenanceProjectExpenseAttribution.expense_date,
                MaintenanceProjectExpenseAttribution.expense_ref,
            )
        )
    ]
    if is_field_hidden(user_ctx, "expense_inc"):
        expense_attributions = []
    payload = {
        "project": {
            key: workspace["project"][key]
            for key in (
                "project_id",
                "project_code",
                "display_name",
                "project_manager_id",
                "lifecycle_status",
                "is_active",
                "version",
            )
        },
        "workbook_revision": revision,
        "as_of": as_of.isoformat(),
        "all_contracts": list(workspace["project"]["contracts"]),
        "effective_contracts": [
            row for row in workspace["project"]["contracts"] if row["is_effective"]
        ],
        "collection_snapshots": collections,
        "confirmed_site_consumptions": [
            row for row in workspace["requisitions"]["rows"] if row["counts_cost"]
        ],
        "approved_expenses": [
            row
            for row in workspace["approved_expenses"]["rows"]
            if row["counts_cost"]
        ],
        "expense_attributions": expense_attributions,
        "derived_tasks": workspace["reminders"],
    }
    payload["data_version"] = (
        state.data_version if state is not None else _workbook_data_version(project_id, 0)
    )
    return payload


def _directory_reminder_project_ids(
    db: Session,
    *,
    projects: list[MaintenanceProject],
    as_of: date,
    user_ctx: UserContext,
    reminder: str,
) -> list[str]:
    """Resolve reminder matches with bounded, summary-only database queries."""

    if not projects:
        return []
    project_ids = [project.project_id for project in projects]
    contracts = list(
        db.scalars(
            select(MaintenanceProjectContract)
            .where(MaintenanceProjectContract.project_id.in_(project_ids))
            .order_by(
                MaintenanceProjectContract.project_id,
                MaintenanceProjectContract.contract_no,
                MaintenanceProjectContract.effective_from,
                MaintenanceProjectContract.project_contract_id,
            )
        )
    )
    contracts_by_project: dict[str, list[MaintenanceProjectContract]] = defaultdict(list)
    effective_by_project: dict[str, list[MaintenanceProjectContract]] = defaultdict(list)
    for contract in contracts:
        contracts_by_project[contract.project_id].append(contract)
        if contract.included_in_total and contract.effective_from <= as_of and (
            contract.effective_to is None or as_of < contract.effective_to
        ):
            effective_by_project[contract.project_id].append(contract)

    effective_contract_ids = {
        contract.contract_id
        for rows in effective_by_project.values()
        for contract in rows
    }
    projects_by_contract: dict[str, set[str]] = defaultdict(set)
    if effective_contract_ids:
        for contract_id, related_project_id in db.execute(
            select(
                MaintenanceProjectContract.contract_id,
                MaintenanceProjectContract.project_id,
            ).where(
                MaintenanceProjectContract.contract_id.in_(effective_contract_ids),
                MaintenanceProjectContract.included_in_total.is_(True),
                MaintenanceProjectContract.effective_from <= as_of,
                or_(
                    MaintenanceProjectContract.effective_to.is_(None),
                    MaintenanceProjectContract.effective_to > as_of,
                ),
            )
        ):
            projects_by_contract[contract_id].add(related_project_id)
    cross_project_contract_ids = {
        contract_id
        for contract_id, related_project_ids in projects_by_contract.items()
        if len(related_project_ids) > 1
    }

    effective_relation_project = {
        contract.project_contract_id: project_id
        for project_id, rows in effective_by_project.items()
        for contract in rows
    }
    latest_confirmed_by_project: dict[str, dict[str, Decimal]] = defaultdict(dict)
    if effective_relation_project:
        for relation_id, amount in db.execute(
            select(
                MaintenanceCollectionSnapshot.project_contract_id,
                MaintenanceCollectionSnapshot.cumulative_amount,
            )
            .where(
                MaintenanceCollectionSnapshot.project_contract_id.in_(
                    effective_relation_project
                ),
                MaintenanceCollectionSnapshot.status == "confirmed",
                MaintenanceCollectionSnapshot.report_month <= as_of,
            )
            .order_by(
                MaintenanceCollectionSnapshot.project_contract_id,
                MaintenanceCollectionSnapshot.report_month.desc(),
                MaintenanceCollectionSnapshot.collection_id.desc(),
            )
        ):
            latest_confirmed_by_project[
                effective_relation_project[relation_id]
            ].setdefault(relation_id, amount)

    cost_facts: dict[str, dict[str, Decimal | int]] = defaultdict(
        lambda: {
            "consumed_known": Decimal("0.00"),
            "cost_gap_count": 0,
            "unmapped_issue_count": 0,
        }
    )
    for project_id, mapping_state, normalized_status, cost_amount in db.execute(
        select(
            MaintenanceSiteIssue.project_id,
            MaintenanceSiteIssue.status_mapping_state,
            MaintenanceSiteIssue.normalized_status,
            MaintenanceSiteIssueLine.cost_amount,
        )
        .join(
            MaintenanceSiteIssueLine,
            MaintenanceSiteIssueLine.issue_id == MaintenanceSiteIssue.issue_id,
        )
        .where(
            MaintenanceSiteIssue.project_id.in_(project_ids),
            MaintenanceSiteIssue.issue_date <= as_of,
        )
    ):
        facts = cost_facts[project_id]
        eligible = mapping_state == "mapped" and normalized_status == "confirmed"
        if mapping_state != "mapped":
            facts["unmapped_issue_count"] += 1
        if eligible and cost_amount is None:
            facts["cost_gap_count"] += 1
        elif eligible:
            facts["consumed_known"] += Decimal(cost_amount)

    expense_facts: dict[str, dict[str, Decimal | int]] = defaultdict(
        lambda: {
            "approved_expense": Decimal("0.00"),
            "unmapped_expense_count": 0,
        }
    )
    for project_id, mapping_state, normalized_status, amount in db.execute(
        select(
            MaintenanceProjectExpenseAttribution.project_id,
            MaintenanceProjectExpenseAttribution.status_mapping_state,
            MaintenanceProjectExpenseAttribution.normalized_status,
            MaintenanceProjectExpenseAttribution.amount_ex_tax,
        ).where(
            MaintenanceProjectExpenseAttribution.project_id.in_(project_ids),
            MaintenanceProjectExpenseAttribution.expense_date <= as_of,
        )
    ):
        facts = expense_facts[project_id]
        if mapping_state != "mapped":
            facts["unmapped_expense_count"] += 1
        if mapping_state == "mapped" and normalized_status == "approved":
            facts["approved_expense"] += Decimal(amount)

    state_by_project = {
        project_id: (last_applied_at, expense_ready_through)
        for project_id, last_applied_at, expense_ready_through in db.execute(
            select(
                MaintenanceProjectWorkbookState.project_id,
                MaintenanceProjectWorkbookState.last_applied_at,
                MaintenanceProjectWorkbookState.expense_ready_through,
            ).where(MaintenanceProjectWorkbookState.project_id.in_(project_ids))
        )
    }
    amount_restricted = is_field_hidden(user_ctx, "contract_amount")
    matching_project_ids: list[str] = []
    for project in projects:
        project_contracts = contracts_by_project[project.project_id]
        effective = effective_by_project[project.project_id]
        issues: list[dict] = []
        if not effective:
            issues.append({"code": "no_effective_contracts", "contract_ids": []})
        repeated = sorted(
            contract_id
            for contract_id, count in Counter(
                contract.contract_id for contract in effective
            ).items()
            if count > 1
        )
        if repeated:
            issues.append(
                {"code": "duplicate_effective_contract", "contract_ids": repeated}
            )
        unmapped = sorted(
            {
                contract.contract_id
                for contract in project_contracts
                if contract.effective_from <= as_of
                and (contract.effective_to is None or as_of < contract.effective_to)
                and contract.status_mapping_state != "mapped"
            }
        )
        if unmapped:
            issues.append({"code": "unmapped_contract_status", "contract_ids": unmapped})
        missing_amount = sorted(
            {
                contract.contract_id
                for contract in effective
                if contract.contract_amount is None
            }
        )
        if missing_amount:
            issues.append(
                {"code": "missing_contract_amount", "contract_ids": missing_amount}
            )
        cross_project_conflicts = sorted(
            {
                contract.contract_id
                for contract in effective
                if contract.contract_id in cross_project_contract_ids
            }
        )
        if cross_project_conflicts:
            issues.append(
                {
                    "code": "cross_project_contract_conflict",
                    "contract_ids": cross_project_conflicts,
                }
            )

        if amount_restricted:
            total_contract_amount: Decimal | None = None
            completeness = {"status": "restricted", "issues": []}
        elif issues:
            total_contract_amount = None
            completeness = {"status": "incomplete", "issues": issues}
        else:
            total_contract_amount = sum(
                (Decimal(contract.contract_amount) for contract in effective),
                start=Decimal("0.00"),
            )

            completeness = {"status": "complete", "issues": []}

        project_cost_facts = cost_facts[project.project_id]
        project_expense_facts = expense_facts[project.project_id]
        cost_gap_count = int(project_cost_facts["cost_gap_count"])
        unmapped_issue_count = int(project_cost_facts["unmapped_issue_count"])
        unmapped_expense_count = int(
            project_expense_facts["unmapped_expense_count"]
        )
        last_applied_at, expense_ready_through = state_by_project.get(
            project.project_id,
            (None, None),
        )
        expense_data_ready = bool(
            expense_ready_through
            and expense_ready_through >= as_of.replace(day=1)
        )
        completeness_issues = list(completeness.get("issues", []))
        if cost_gap_count:
            completeness_issues.append(
                {"code": "missing_consumption_cost", "line_count": cost_gap_count}
            )
        if unmapped_issue_count:
            completeness_issues.append(
                {
                    "code": "unmapped_site_issue_status",
                    "line_count": unmapped_issue_count,
                }
            )
        if unmapped_expense_count:
            completeness_issues.append(
                {
                    "code": "unmapped_expense_status",
                    "row_count": unmapped_expense_count,
                }
            )
        if not expense_data_ready:
            completeness_issues.append(
                {
                    "code": "expense_data_not_ready",
                    "ready_through": (
                        expense_ready_through.isoformat()
                        if expense_ready_through
                        else None
                    ),
                    "required_month": as_of.strftime("%Y-%m"),
                }
            )
        if completeness.get("status") != "restricted" and completeness_issues:
            completeness = {"status": "incomplete", "issues": completeness_issues}

        actual_cost_known = Decimal(project_cost_facts["consumed_known"]) + Decimal(
            project_expense_facts["approved_expense"]
        )
        cost_rate = (
            actual_cost_known / total_contract_amount * Decimal("100")
            if total_contract_amount is not None and total_contract_amount > 0
            else None
        )
        if cost_rate is None:
            cost_status = "unknown"
        elif cost_rate > Decimal("100"):
            cost_status = "red"
        elif cost_rate >= Decimal("80"):
            cost_status = "yellow"
        elif (
            cost_gap_count
            or unmapped_issue_count
            or unmapped_expense_count
            or not expense_data_ready
        ):
            cost_status = "unknown"
        else:
            cost_status = "normal"

        confirmed_collection = sum(
            latest_confirmed_by_project[project.project_id].values(),
            start=Decimal("0.00"),
        )
        tasks = _system_tasks(
            project_id=project.project_id,
            completeness=completeness,
            has_confirmed_collection=bool(
                latest_confirmed_by_project[project.project_id]
            ),
            confirmed_collection=confirmed_collection,
            total_contract_amount=total_contract_amount,
            cost_gap_count=cost_gap_count,
            cost_status=cost_status,
            as_of=as_of,
            project_manager_id=project.project_manager_id,
            last_applied_at=last_applied_at,
        )
        tasks, _visible_completeness = _visible_tasks(
            tasks,
            completeness,
            user_ctx=user_ctx,
        )
        open_tasks = [task for task in tasks if task["status"] != "completed"]
        if open_tasks and (
            reminder == "all"
            or any(
                task["task_type"] == reminder
                or task["rule_key"] == reminder
                or task["severity"] == reminder
                for task in open_tasks
            )
        ):
            matching_project_ids.append(project.project_id)
    return matching_project_ids


def project_operations(
    db: Session,
    *,
    as_of: date,
    user_ctx: UserContext,
    q_text: str | None = None,
    lifecycle: str = "all",
    reminder: str | None = None,
    include_inactive: bool = False,
    page: int = 1,
    page_size: int = 24,
) -> dict:
    rows: list[dict] = []
    filters = []
    if not include_inactive:
        filters.append(MaintenanceProject.is_active.is_(True))
    if lifecycle != "all":
        filters.append(MaintenanceProject.lifecycle_status == lifecycle)
    if q_text and (search := q_text.strip()):
        filters.append(
            or_(
                MaintenanceProject.project_code.icontains(search, autoescape=True),
                MaintenanceProject.display_name.icontains(search, autoescape=True),
                MaintenanceProject.project_id.in_(
                    select(MaintenanceProjectContract.project_id).where(
                        MaintenanceProjectContract.contract_no.icontains(
                            search,
                            autoescape=True,
                        )
                    )
                ),
            )
        )
    project_query = (
        select(MaintenanceProject)
        .where(*filters)
        .order_by(MaintenanceProject.project_code, MaintenanceProject.project_id)
    )
    offset = (page - 1) * page_size
    if reminder is None:
        total = int(
            db.scalar(
                select(func.count())
                .select_from(MaintenanceProject)
                .where(*filters)
            )
            or 0
        )
        project_ids = list(
            db.scalars(
                select(MaintenanceProject.project_id)
                .where(*filters)
                .order_by(MaintenanceProject.project_code, MaintenanceProject.project_id)
                .offset(offset)
                .limit(page_size)
            )
        )
    else:
        candidate_projects = list(db.scalars(project_query))
        matching_project_ids = _directory_reminder_project_ids(
            db,
            projects=candidate_projects,
            as_of=as_of,
            user_ctx=user_ctx,
            reminder=reminder,
        )
        total = len(matching_project_ids)
        project_ids = matching_project_ids[offset : offset + page_size]
    for project_id in project_ids:
        workspace = project_workspace(
            db,
            project_id=project_id,
            as_of=as_of,
            user_ctx=user_ctx,
        )
        if workspace is not None:
            rows.append(workspace["project"])
    payload = {
        "rows": rows,
        "total": total,
        "page": page,
        "page_size": page_size,
        "as_of": as_of.isoformat(),
    }
    payload["data_version"] = _payload_token(payload)
    return payload

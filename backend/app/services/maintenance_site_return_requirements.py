"""Return requirements keyed by site line, independent of received-part facts.

Workbook lines have no warehouse delivery identity. Their requirements are read
from current line facts and corrected with the existing versioned outbox/audit,
without manufacturing legacy delivery-based obligations.
"""

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.dimensions import DimPart
from app.models.maintenance_bad_return import MaintenanceReturnObligation
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_project_operations import (
    MaintenanceProjectOperationAudit,
    MaintenanceSiteIssue,
    MaintenanceSiteIssueLine,
    MaintenanceSiteIssueReturnEvent,
)
from app.models.master_data import ProductCategory

SCHEMA = "maintenance-site-line-return-requirement-v1"
REFERENCE = "maintenance-site-line-requirements:"


def line_details(
    db: Session,
    lines: Iterable[MaintenanceSiteIssueLine],
    *,
    project: MaintenanceProject,
) -> dict[str, dict[str, Any]]:
    """Batch current master data; never claim it is an issue-time snapshot."""
    from app.services.maintenance_bad_returns import classify_return_obligation

    rows = list(lines)
    obligations = (
        {
            obligation.issue_line_id: obligation
            for obligation in db.scalars(
                select(MaintenanceReturnObligation).where(
                    MaintenanceReturnObligation.issue_line_id.in_(
                        [line.issue_line_id for line in rows if line.delivery_line_id]
                    ),
                    MaintenanceReturnObligation.is_active.is_(True),
                )
            )
        }
        if rows
        else {}
    )
    parts = (
        {
            part.id: (part, category)
            for part, category in db.execute(
                select(DimPart, ProductCategory)
                .outerjoin(ProductCategory, ProductCategory.id == DimPart.category_id)
                .where(DimPart.id.in_({line.part_id for line in rows}))
            )
        }
        if rows
        else {}
    )
    result = {}
    for line in rows:
        part, category = parts.get(line.part_id, (None, None))
        basis = classify_return_obligation(
            category_id=category.id if category else None,
            category_major=category.category_major if category else None,
            category_minor=category.category_minor if category else None,
            no_return_line=line.no_return,
            project_no_return_default=project.no_return_default,
        )
        # Explicit 是/否 is itself sufficient evidence for the new line rule.
        if line.no_return is False:
            basis.update(
                classification="required", exemption_source="line_return_required"
            )
        obligation = obligations.get(line.issue_line_id)
        if obligation is not None:
            basis.update(
                classification=obligation.classification,
                category_id_snapshot=obligation.category_id_snapshot,
                category_major_snapshot=obligation.category_major_snapshot,
                category_minor_snapshot=obligation.category_minor_snapshot,
                exemption_source=obligation.exemption_source,
                rule_version=obligation.rule_version,
            )
        state = basis["classification"]
        quantity = (
            Decimal(
                obligation.source_quantity if obligation is not None else line.quantity
            )
            if line.is_active
            else Decimal(0)
        )
        required_quantity = (
            Decimal(obligation.required_quantity)
            if obligation is not None and line.is_active
            else quantity
            if state == "required"
            else Decimal(0)
        )
        result[line.issue_line_id] = {
            **{
                key: getattr(part, key, None)
                for key in (
                    "description",
                    "brand",
                    "category_major",
                    "category_minor",
                    "unit",
                )
            },
            "description_source": "current_master_data",
            "return_requirement": {
                "requirement_status": state,
                "required_quantity": format(required_quantity, ".3f"),
                "exempt_quantity": format(
                    quantity if state == "exempt" else Decimal(0), ".3f"
                ),
                "pending_quantity": format(
                    quantity if state == "pending_category" else Decimal(0), ".3f"
                ),
                "basis": basis,
            },
        }
    return result


def snapshot(line: MaintenanceSiteIssueLine) -> dict[str, Any]:
    return {
        "issue_line_id": line.issue_line_id,
        "no_return": line.no_return,
        "quantity": str(line.quantity),
        "part_id": line.part_id,
        "is_active": line.is_active,
        "version": line.version,
    }


def record_corrections(
    db: Session,
    *,
    project_id: str,
    before: Mapping[str, dict[str, Any]],
    operated_by: str,
    reason: str,
) -> None:
    """Called under the workbook's project/CAS locks, after final line writes."""
    grouped = {}
    for line_id, old in before.items():
        line = db.get(MaintenanceSiteIssueLine, line_id)
        if line is None:
            continue
        after = snapshot(line)
        if {k: v for k, v in old.items() if k != "version"} == {
            k: v for k, v in after.items() if k != "version"
        }:
            continue
        line.version += 1
        after = snapshot(line)
        grouped.setdefault(line.issue_id, []).append({"before": old, "after": after})
        db.add(
            MaintenanceProjectOperationAudit(
                project_id=project_id,
                entity_type="site_issue_line",
                entity_id=line_id,
                action="return_obligation_corrected",
                before_json=old,
                after_json=after,
                reason=reason,
                operated_by=operated_by,
            )
        )
    for issue_id, changes in grouped.items():
        issue = db.get(MaintenanceSiteIssue, issue_id)
        issue.version += 1
        legacy_event = db.scalar(
            select(MaintenanceSiteIssueReturnEvent.event_id)
            .where(
                MaintenanceSiteIssueReturnEvent.issue_id == issue_id,
                MaintenanceSiteIssueReturnEvent.payload["schema_version"].astext
                == "maintenance-return-obligation-interface-v1",
            )
            .limit(1)
        )
        event_type = (
            "return_obligation_voided"
            if issue.normalized_status == "void"
            else "return_obligation_corrected"
        )
        payload = {
            "schema_version": SCHEMA,
            "project_id": project_id,
            "issue_id": issue_id,
            "changes": changes,
            "operated_by": operated_by,
        }
        if legacy_event:
            from app.services.maintenance_project_operations import (
                _site_issue_return_payload,
            )

            lines = list(
                db.scalars(
                    select(MaintenanceSiteIssueLine).where(
                        MaintenanceSiteIssueLine.issue_id == issue_id,
                        MaintenanceSiteIssueLine.is_active.is_(True),
                    )
                )
            )
            payload = _site_issue_return_payload(issue, lines)
        event = MaintenanceSiteIssueReturnEvent(
            event_id=str(uuid4()),
            project_id=project_id,
            issue_id=issue_id,
            issue_version=issue.version,
            event_type=event_type,
            payload=payload,
        )
        db.add(event)
        if legacy_event:
            from app.services import maintenance_bad_returns
            from app.services.maintenance_expense_collection_workbook import (
                WorkbookError,
            )

            db.flush()
            try:
                maintenance_bad_returns.consume_return_event(db, event)
            except (
                maintenance_bad_returns.BadReturnConflict,
                maintenance_bad_returns.BadReturnError,
            ) as exc:
                raise WorkbookError("return_requirement_conflict", str(exc)) from exc
        else:
            acknowledge(event)
    db.flush()


def acknowledge(event: MaintenanceSiteIssueReturnEvent) -> None:
    """The authoritative line facts are already written in this transaction."""
    payload = event.payload
    if (
        payload.get("schema_version") != SCHEMA
        or payload.get("project_id") != event.project_id
        or payload.get("issue_id") != event.issue_id
    ):
        raise ValueError("领用行返还要求事件契约无效")
    reference = f"{REFERENCE}{event.issue_id}:v{event.issue_version}"
    if event.downstream_reference is not None:
        if event.downstream_reference != reference:
            raise ValueError("领用行返还要求事件已由其他下游消费")
        return
    event.downstream_reference = reference
    event.consumed_at = datetime.now(UTC)

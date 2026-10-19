"""Move live issue references while retaining immutable command/event history.

The workbook owns the transaction and the project/header/line lock prefix.
This adapter never commits, changes schema, or rewrites historical receipts.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
import json
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.maintenance_bad_return import MaintenanceReturnObligation
from app.models.maintenance_project_operations import (
    MaintenanceProjectOperationAudit,
    MaintenanceSiteIssue,
    MaintenanceSiteIssueLine,
    MaintenanceSiteIssueReturnEvent,
)
from app.models.maintenance_warehouse import (
    MaintenanceWarehouseAmbiguity,
    MaintenanceWarehouseAuditEvent,
    MaintenanceWarehouseDocumentLink,
)
from app.services.maintenance_expense_collection_workbook import WorkbookError

_LEGACY_SCHEMA = "maintenance-return-obligation-interface-v1"
_TARGET = "maintenance_site_issue"


def _json_value(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _row_snapshot(row) -> dict:
    return {column.key: _json_value(getattr(row, column.key))
            for column in row.__table__.columns}


def _conflict(message: str) -> WorkbookError:
    return WorkbookError("site_issue_merge_conflict", f"领用单合并失败：{message}")


def _is_candidate(candidate, issue_ids: set[str]) -> bool:
    return (isinstance(candidate, dict) and candidate.get("target_type") == _TARGET
            and str(candidate.get("target_id")) in issue_ids)


def _references(db: Session, issue_ids, *, lock: bool = False) -> dict[str, list]:
    ids = set(issue_ids)
    if not ids:
        return {"events": [], "obligations": [], "warehouse_ambiguities": [],
                "warehouse_links": []}

    def rows(statement):
        if lock:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return list(db.scalars(statement))

    # Projector order is event -> obligation; ambiguity resolution is ambiguity
    # -> link. Keep both orders even when callers also request a fresh snapshot.
    events = rows(select(MaintenanceSiteIssueReturnEvent).where(
        MaintenanceSiteIssueReturnEvent.issue_id.in_(ids),
    ).order_by(MaintenanceSiteIssueReturnEvent.issue_id,
               MaintenanceSiteIssueReturnEvent.issue_version,
               MaintenanceSiteIssueReturnEvent.event_id))
    obligations = rows(select(MaintenanceReturnObligation).where(
        MaintenanceReturnObligation.issue_id.in_(ids),
    ).order_by(MaintenanceReturnObligation.obligation_id))
    ambiguity_ids = [ambiguity_id for ambiguity_id, candidates in db.execute(
        select(MaintenanceWarehouseAmbiguity.ambiguity_id,
               MaintenanceWarehouseAmbiguity.candidates_json).where(
            MaintenanceWarehouseAmbiguity.status == "open",
        )
    ) if any(_is_candidate(candidate, ids) for candidate in candidates)]
    ambiguities = rows(select(MaintenanceWarehouseAmbiguity).where(
        MaintenanceWarehouseAmbiguity.ambiguity_id.in_(ambiguity_ids),
        MaintenanceWarehouseAmbiguity.status == "open",
    ).order_by(MaintenanceWarehouseAmbiguity.ambiguity_id))
    links = rows(select(MaintenanceWarehouseDocumentLink).where(
        MaintenanceWarehouseDocumentLink.target_type == _TARGET,
        MaintenanceWarehouseDocumentLink.target_id.in_(ids),
        MaintenanceWarehouseDocumentLink.status == "active",
    ).order_by(MaintenanceWarehouseDocumentLink.link_id))
    return {"events": events, "obligations": obligations,
            "warehouse_ambiguities": ambiguities, "warehouse_links": links}


def snapshot_references(db: Session, issue_ids, *, lock: bool = False) -> dict:
    """JSON-safe evidence for validate/apply comparison, including watermarks."""
    return {kind: [_row_snapshot(row) for row in rows]
            for kind, rows in _references(db, issue_ids, lock=lock).items()}


def validate_references(db: Session, issue_ids) -> None:
    ids = set(issue_ids)
    if not ids:
        return
    issues = list(db.scalars(select(MaintenanceSiteIssue).where(
        MaintenanceSiteIssue.issue_id.in_(ids),
    )))
    projects = {issue.issue_id: issue.project_id for issue in issues}
    if set(projects) != ids or len(set(projects.values())) != 1:
        raise _conflict("来源单不存在或不属于同一项目，请重新校验")
    project_id = next(iter(projects.values()))
    refs = _references(db, ids)
    projected_ids = {row.issue_id for row in refs["obligations"]} | {
        event.issue_id for event in refs["events"]
        if (event.payload or {}).get("schema_version") == _LEGACY_SCHEMA
    }
    if any(issue.issue_id in projected_ids and issue.normalized_status not in {"confirmed", "corrected"}
           for issue in issues):
        raise _conflict("未确认领用单已有关联返还义务，不能自动合并")
    seen_delivery: set[str] = set()
    for obligation in refs["obligations"]:
        if obligation.project_id != project_id:
            raise _conflict("返还义务归属与领用项目不一致")
        if obligation.delivery_line_id in seen_delivery:
            raise _conflict(f"多张单包含同一发货明细 {obligation.delivery_line_id} 的返还义务，不能自动合并")
        seen_delivery.add(obligation.delivery_line_id)
    for event in refs["events"]:
        if event.project_id != project_id or (event.payload or {}).get("project_id") != project_id:
            raise _conflict("返还事件的项目归属不一致")
        if (event.payload or {}).get("issue_id") != event.issue_id:
            raise _conflict("返还事件的领用身份不一致")
    for ambiguity in refs["warehouse_ambiguities"]:
        for candidate in ambiguity.candidates_json:
            if (_is_candidate(candidate, ids) and candidate.get("project_id")
                    and candidate["project_id"] != project_id):
                raise _conflict("仓库候选关联的项目归属不一致")


def prepare_references(db: Session, issue_ids) -> None:
    """Consume pending old identities before moving any facts or obligations."""
    from app.services import maintenance_bad_returns as returns

    ids = set(issue_ids)
    events = list(db.scalars(select(MaintenanceSiteIssueReturnEvent).where(
        MaintenanceSiteIssueReturnEvent.issue_id.in_(ids),
        MaintenanceSiteIssueReturnEvent.downstream_reference.is_(None),
    ).order_by(MaintenanceSiteIssueReturnEvent.issue_id,
               MaintenanceSiteIssueReturnEvent.issue_version,
               MaintenanceSiteIssueReturnEvent.created_at,
               MaintenanceSiteIssueReturnEvent.event_id).with_for_update()))
    try:
        for event in events:
            returns.consume_return_event(db, event)
    except (returns.BadReturnError, returns.BadReturnConflict) as exc:
        raise _conflict(f"原返还事件无法完成投影：{exc}") from exc
    # Pending events can materialize obligations absent from the preview read.
    # Their delivery identity must still be compatible before any reparenting.
    validate_references(db, ids)


def _audit(db, *, project_id, kind, entity_id, before, after, operated_by, reason):
    db.add(MaintenanceProjectOperationAudit(
        project_id=project_id, entity_type=kind, entity_id=entity_id,
        action="site_issue_merge", before_json=before, after_json=after,
        operated_by=operated_by, reason=reason,
    ))


def _move_return_references(db, *, project_id, survivor, absorbed, refs, operated_by, reason):
    from app.services import maintenance_bad_returns as returns
    from app.services.maintenance_project_operations import _site_issue_return_payload

    absorbed_ids = {issue.issue_id for issue in absorbed}
    all_ids = absorbed_ids | {survivor.issue_id}
    lines = list(db.scalars(select(MaintenanceSiteIssueLine).where(
        MaintenanceSiteIssueLine.issue_id == survivor.issue_id,
        MaintenanceSiteIssueLine.is_active.is_(True),
    ).order_by(MaintenanceSiteIssueLine.line_no, MaintenanceSiteIssueLine.issue_line_id)))
    projected = (bool(refs["obligations"])
                 or any((event.payload or {}).get("schema_version") == _LEGACY_SCHEMA
                        for event in refs["events"]))
    if survivor.normalized_status not in {"confirmed", "corrected"}:
        if projected:
            raise _conflict("未确认领用单已有关联返还义务，不能自动合并")
        return {"obligation_ids": [], "return_event_ids": []}
    has_legacy = projected or any(line.delivery_line_id for line in lines)
    if not has_legacy:
        return {"obligation_ids": [], "return_event_ids": []}
    watermarks = {issue_id: 0 for issue_id in all_ids}
    for event in refs["events"]:
        watermarks[event.issue_id] = max(watermarks[event.issue_id], event.issue_version)
    for obligation in refs["obligations"]:
        watermarks[obligation.issue_id] = max(watermarks[obligation.issue_id],
                                             obligation.source_issue_version)
    survivor.version = max(survivor.version, max(watermarks.values(), default=0) + 1)
    survivor_event = MaintenanceSiteIssueReturnEvent(
        event_id=str(uuid4()), project_id=project_id, issue_id=survivor.issue_id,
        event_type="return_obligation_corrected", issue_version=survivor.version,
        payload=_site_issue_return_payload(survivor, lines),
    )
    db.add(survivor_event)
    # There is no ORM relationship dependency between the event and the
    # obligation. Persist the FK target before updating inactive obligations.
    db.flush([survivor_event])
    moved = []
    before_rows = {}
    for obligation in refs["obligations"]:
        if obligation.issue_id not in absorbed_ids:
            continue
        before_rows[obligation.obligation_id] = _row_snapshot(obligation)
        obligation.issue_id = survivor.issue_id
        moved.append(obligation)
        # The projector increments active rows. Historical inactive obligations
        # are skipped there, so their identity/version move belongs here.
        if not obligation.is_active:
            obligation.version += 1
            obligation.source_issue_version = survivor.version
            obligation.last_source_event_id = survivor_event.event_id
    db.flush()
    events = [survivor_event]
    try:
        returns.consume_return_event(db, survivor_event)
        for issue in absorbed:
            issue.version = max(issue.version, watermarks[issue.issue_id] + 1)
            event = MaintenanceSiteIssueReturnEvent(
                event_id=str(uuid4()), project_id=project_id, issue_id=issue.issue_id,
                event_type="return_obligation_voided", issue_version=issue.version,
                payload=_site_issue_return_payload(issue, []),
            )
            db.add(event)
            db.flush()
            returns.consume_return_event(db, event)
            events.append(event)
    except (returns.BadReturnError, returns.BadReturnConflict) as exc:
        raise _conflict(f"合并后的返还义务无法保持一致：{exc}") from exc
    for obligation in moved:
        _audit(db, project_id=project_id, kind="return_obligation",
               entity_id=obligation.obligation_id,
               before=before_rows[obligation.obligation_id], after=_row_snapshot(obligation),
               operated_by=operated_by, reason=reason)
    return {"obligation_ids": [row.obligation_id for row in moved],
            "return_event_ids": [event.event_id for event in events]}


def _move_warehouse_references(db, *, project_id, survivor, absorbed, refs, operated_by, reason):
    from app.services.maintenance_warehouse import _hash, _link_id

    absorbed_ids = {issue.issue_id for issue in absorbed}
    new_link_ids = []
    for old in refs["warehouse_links"]:
        if old.target_id not in absorbed_ids:
            continue
        before = _row_snapshot(old)
        next_version = old.version + 1
        old.status = "superseded"
        old.version = next_version
        db.flush([old])  # Release the active (document, line, kind) slot.
        new = MaintenanceWarehouseDocumentLink(
            link_id=_link_id(old.document_id, old.line_id, old.link_kind,
                             _TARGET, survivor.issue_id, version=next_version),
            document_id=old.document_id, line_id=old.line_id, link_kind=old.link_kind,
            target_type=_TARGET, target_id=survivor.issue_id,
            stable_key_kind="manual_stable_id", stable_key_hash=_hash(survivor.issue_id),
            source="manual", status="active", supersedes_link_id=old.link_id,
            version=next_version, reason=reason, operated_by=operated_by,
        )
        db.add(new)
        db.flush([new])
        after = _row_snapshot(new)
        _audit(db, project_id=project_id, kind="warehouse_link", entity_id=new.link_id,
               before=before, after=after, operated_by=operated_by, reason=reason)
        db.add(MaintenanceWarehouseAuditEvent(
            event_id=str(uuid4()), action="integration_reconciled",
            before_json=before, after_json=after, reason=reason, operated_by=operated_by,
        ))
        new_link_ids.append(new.link_id)
    changed_ambiguities = []
    superseded_ambiguities = []
    for ambiguity in refs["warehouse_ambiguities"]:
        if not any(_is_candidate(candidate, absorbed_ids) for candidate in ambiguity.candidates_json):
            continue
        before = _row_snapshot(ambiguity)
        candidates = deepcopy(ambiguity.candidates_json)
        for candidate in candidates:
            if _is_candidate(candidate, absorbed_ids):
                candidate["target_id"] = survivor.issue_id
                if "label" in candidate:
                    candidate["label"] = survivor.display_no_effective
        # Preserve different delivery/SN evidence; only exact duplicates vanish.
        candidates = list({json.dumps(candidate, sort_keys=True,
            ensure_ascii=False): candidate for candidate in candidates}.values())
        # The database makes source candidates/evidence immutable. Retire the
        # old queue item through its permitted open->resolved transition, and
        # append a new open item rather than disabling that history guard.
        replacement = MaintenanceWarehouseAmbiguity(
            ambiguity_id=str(uuid4()), import_id=ambiguity.import_id,
            document_id=ambiguity.document_id, line_id=ambiguity.line_id,
            ambiguity_type=ambiguity.ambiguity_type, field_code=ambiguity.field_code,
            source_row=ambiguity.source_row, value_hash=ambiguity.value_hash,
            candidates_json=candidates,
            evidence_json={**(deepcopy(ambiguity.evidence_json) or {}),
                           "supersedes_ambiguity_id": ambiguity.ambiguity_id,
                           "merge_survivor_issue_id": survivor.issue_id},
            fingerprint=_hash({"schema": "site_issue_merge_ambiguity_v1",
                               "supersedes_ambiguity_id": ambiguity.ambiguity_id,
                               "candidates": candidates}),
            status="open", version=1,
        )
        ambiguity.status = "resolved"
        ambiguity.version += 1
        ambiguity.resolution_json = {
            "decision": "superseded_by_site_issue_merge",
            "survivor_issue_id": survivor.issue_id,
            "new_ambiguity_id": replacement.ambiguity_id,
        }
        ambiguity.resolution_reason = reason
        ambiguity.resolved_by = operated_by
        ambiguity.resolved_at = datetime.now(timezone.utc)
        db.add(replacement)
        db.flush([ambiguity, replacement])
        after = {"resolved": _row_snapshot(ambiguity),
                 "replacement": _row_snapshot(replacement)}
        _audit(db, project_id=project_id, kind="warehouse_ambiguity",
               entity_id=ambiguity.ambiguity_id, before=before, after=after,
               operated_by=operated_by, reason=reason)
        db.add(MaintenanceWarehouseAuditEvent(
            event_id=str(uuid4()), ambiguity_id=ambiguity.ambiguity_id,
            action="integration_reconciled", before_json=before, after_json=after,
            reason=reason, operated_by=operated_by,
        ))
        changed_ambiguities.append(replacement.ambiguity_id)
        superseded_ambiguities.append(ambiguity.ambiguity_id)
    return {"warehouse_link_ids": new_link_ids,
            "warehouse_ambiguity_ids": changed_ambiguities,
            "superseded_warehouse_ambiguity_ids": superseded_ambiguities}


def move_references(db: Session, *, project_id: str, survivor: MaintenanceSiteIssue,
                    absorbed: list[MaintenanceSiteIssue], operated_by: str, reason: str) -> dict:
    """Called after all lines move and final headers flush, before line edits/P3."""
    ids = {survivor.issue_id, *(issue.issue_id for issue in absorbed)}
    refs = _references(db, ids, lock=True)
    result = _move_return_references(
        db, project_id=project_id, survivor=survivor, absorbed=absorbed, refs=refs,
        operated_by=operated_by, reason=reason,
    )
    result.update(_move_warehouse_references(
        db, project_id=project_id, survivor=survivor, absorbed=absorbed, refs=refs,
        operated_by=operated_by, reason=reason,
    ))
    db.flush()
    return result

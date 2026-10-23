"""Explicit, atomic confirmation of D-27 WBDD missing-source candidates."""
from datetime import datetime, timezone

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.config import DATA_CHANGE_ADVISORY_LOCK_KEY
from app.models.maintenance import FMaintenanceLine as L, FMaintenanceOrder as O, MaintenanceDemandTombstone
from app.models.maintenance_project_operations import MaintenanceSiteIssue as I, MaintenanceSiteIssueLine as IL
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment as A
from app.models.system import SysAuditLog, SysImportBatch
from app.services.maintenance_project_audit import write_project_audit
from app.services import maintenance_demands as demands
from app.services import maintenance_project_operations as operations
from app.services import maintenance_site_return_requirements as requirements
from app.services import maintenance_wbdd_reconciliation as reconciliation


class ReconciliationConflict(Exception):
    """The displayed candidate set or a previously applied result has changed."""


def _state_digest(db, source_ids, line_ids, site_ids):
    """Fingerprint terminal state, including restoration and later new children."""
    orders = list(db.scalars(select(O).where(O.raw_order_id.in_(source_ids)).order_by(O.id)))
    lines = list(db.scalars(select(L).where(
        L.order_id.in_([order.id for order in orders]) | L.id.in_(line_ids)).order_by(L.id)))
    assignments = list(db.scalars(select(A).where(A.source_order_id.in_(source_ids)).order_by(A.assignment_id)))
    tombstones = list(db.scalars(select(MaintenanceDemandTombstone).where(
        MaintenanceDemandTombstone.source_order_id.in_(source_ids)).order_by(MaintenanceDemandTombstone.source_order_id)))
    sites = list(db.scalars(select(IL).where(IL.issue_line_id.in_(site_ids)).order_by(IL.issue_line_id)))
    issues = list(db.scalars(select(I).where(I.issue_id.in_({line.issue_id for line in sites})).order_by(I.issue_id)))
    return reconciliation._digest({name: [reconciliation._entity(row) for row in rows]
        for name, rows in (("orders", orders), ("lines", lines), ("assignments", assignments),
                           ("tombstones", tombstones), ("site_lines", sites), ("issues", issues))})


def _audit(db, *, project_id, entity_type, entity_id, before, after, reason, operated_by, action="VOID"):
    if project_id:
        write_project_audit(db, project_id=project_id, entity_type=entity_type,
                     entity_id=entity_id, action=action, before=before, after=after,
                     reason=reason, operated_by=operated_by)
    else:
        db.add(SysAuditLog(entity_type=entity_type, entity_id=int(entity_id), action=action,
                           before_json=before, after_json=after, reason=reason,
                           operated_by=operated_by))


def apply_reconciliation(db: Session, *, batch_id, expected_digest, source_order_ids,
                         line_ids, reason, operated_by, allowed_project_ids=None,
                         idempotency_key):
    """Apply one displayed selection; caller commits or rolls back the transaction.

    Global lock precedes every project, order and line lock. Exact candidate
    membership and digest are checked again while all affected rows are locked.
    """
    reason = str(reason or "").strip()
    operated_by = str(operated_by or "").strip()
    if not reason or len(reason) > demands.MAX_REASON_LENGTH or not operated_by:
        raise ValueError("必须提供实名操作人和有效作废原因")
    if not isinstance(idempotency_key, str) or not 8 <= len(idempotency_key) <= 128:
        raise ValueError("必须提供8–128字符的幂等键")
    if not isinstance(expected_digest, str) or len(expected_digest) != 64:
        raise ValueError("缺少有效差异摘要，请重新核对")
    try:
        source_ids = sorted({str(value) for value in source_order_ids})
        selected_line_ids = sorted({int(value) for value in line_ids})
    except (TypeError, ValueError) as exc:
        raise ValueError("作废选择格式不正确") from exc
    if not source_ids and not selected_line_ids:
        raise ValueError("请选择要同步作废的单据或明细")
    if len(source_ids) + len(selected_line_ids) > reconciliation.MAX_CANDIDATES:
        raise ValueError("一次最多确认1000项差异")
    if any(not value or len(value) > 64 for value in source_ids) or any(value <= 0 for value in selected_line_ids):
        raise ValueError("作废选择包含无效身份")
    request = {"batch_id": batch_id, "digest": expected_digest, "source_order_ids": source_ids,
               "line_ids": selected_line_ids, "reason": reason, "operated_by": operated_by,
               "scope": sorted(allowed_project_ids) if allowed_project_ids is not None else None}
    request_digest = reconciliation._digest(request)
    replay_key = reconciliation._digest([operated_by, idempotency_key])
    db.execute(select(func.pg_advisory_xact_lock(DATA_CHANGE_ADVISORY_LOCK_KEY)))
    # The API's preceding reads must not leave stale ORM instances in the guard.
    db.expire_all()
    batch = db.scalar(select(SysImportBatch).where(SysImportBatch.id == batch_id)
                      .with_for_update().execution_options(populate_existing=True))
    if batch is None or batch.file_type != "maintenance" or batch.status not in {"success", "superseded"}:
        raise ReconciliationConflict("原件上传批次不存在或未成功，请重新上传")
    saved = dict((batch.report_json or {}).get("_wbdd_sync_results") or {})
    prior = saved.get(replay_key)
    if prior is not None:
        if prior["request_digest"] != request_digest:
            raise ReconciliationConflict("同一幂等键已用于其他确认请求")
        if _state_digest(db, prior["source_ids"], prior["line_ids"], prior["site_ids"]) != prior["terminal_digest"]:
            raise ReconciliationConflict("此前作废的数据已被恢复或修改，请重新核对差异")
        return {**prior["result"], "replayed": True}
    latest = db.scalar(select(SysImportBatch.id).where(
        SysImportBatch.file_type == "maintenance", SysImportBatch.status == "success",
        SysImportBatch.report_json.op("?")(reconciliation.SNAPSHOT_KEY))
        .order_by(SysImportBatch.id.desc()).limit(1))
    if latest != batch.id:
        raise ReconciliationConflict("已有更新的原件上传，请重新核对最新差异")
    current = reconciliation.build_candidates(db, batch, allowed_project_ids=allowed_project_ids)
    if current["readiness"] != "ready" or current["candidate_digest"] != expected_digest:
        raise ReconciliationConflict("差异或相关数据已变化，请重新核对后确认")
    available_orders = {row["source_order_id"]: row for row in current["missing_orders"]}
    available_lines = {row["line_id"]: row for row in current["missing_lines"]}
    if not set(source_ids) <= available_orders.keys() or not set(selected_line_ids) <= available_lines.keys():
        raise ReconciliationConflict("所选条目不在当前有权处理的差异清单中")
    parents = set(source_ids) | {available_lines[line_id]["source_order_id"] for line_id in selected_line_ids}
    project_ids = {row["assigned_project_id"] for row in
        [*(available_orders[key] for key in source_ids), *(available_lines[key] for key in selected_line_ids)]
        if row["assigned_project_id"]}
    states = operations.lock_workbook_states(db, project_ids=project_ids)
    orders = list(db.scalars(select(O).where(O.raw_order_id.in_(parents)).order_by(O.id)
                            .with_for_update().execution_options(populate_existing=True)))
    assignments = list(db.scalars(select(A).where(A.source_order_id.in_(parents), A.is_active.is_(True))
                                  .order_by(A.assignment_id).with_for_update().execution_options(populate_existing=True)))
    assignment_by_source = {item.source_order_id: item for item in assignments}
    if any(item.project_id not in states for item in assignments):
        raise ReconciliationConflict("项目归属发生变化，请重新核对")
    if allowed_project_ids is not None and any(
            source not in assignment_by_source or assignment_by_source[source].project_id not in allowed_project_ids
            for source in parents):
        raise PermissionError("只能处理本人有权项目下的差异")
    order_by_id = {item.id: item for item in orders}
    whole_order_ids = {item.id for item in orders if item.raw_order_id in source_ids}
    locked_lines = list(db.scalars(select(L).where(L.order_id.in_(order_by_id)).order_by(L.id)
                                  .with_for_update().execution_options(populate_existing=True)))
    to_void = [line for line in locked_lines if line.is_active
               and (line.order_id in whole_order_ids or line.id in selected_line_ids)]
    if len(to_void) > demands.MAX_DELETE_LINES:
        raise ValueError(f"一次最多涉及{demands.MAX_DELETE_LINES}行备件")
    context = {order.id: (order, assignment_by_source.get(order.raw_order_id), None) for order in orders}
    site_rows = reconciliation.related_site_lines(db, orders=context, lines=to_void)
    guarded_site_rows = reconciliation.related_site_lines(
        db, orders=context, lines=to_void, include_order_references=True)
    issue_ids = {issue.issue_id for _, issue in guarded_site_rows}
    site_ids = {line.issue_line_id for line, _ in guarded_site_rows}
    list(db.scalars(select(I).where(I.issue_id.in_(issue_ids)).order_by(I.issue_id)
                    .with_for_update().execution_options(populate_existing=True)))
    list(db.scalars(select(IL).where(IL.issue_line_id.in_(site_ids)).order_by(IL.issue_line_id)
                    .with_for_update().execution_options(populate_existing=True)))
    checked = reconciliation.build_candidates(db, batch, allowed_project_ids=allowed_project_ids)
    if checked["candidate_digest"] != expected_digest:
        raise ReconciliationConflict("差异或关联领用已变化，请重新核对后确认")
    now = datetime.now(timezone.utc)
    for line in to_void:
        before = reconciliation._entity(line)
        line.is_active, line.voided_at = False, now
        line.voided_by, line.void_reason = operated_by, reason
        owner = assignment_by_source.get(order_by_id[line.order_id].raw_order_id)
        _audit(db, project_id=owner.project_id if owner else None, entity_type="maintenance_line",
               entity_id=line.id, before=before, after=reconciliation._entity(line),
               reason=reason, operated_by=operated_by)
    requirement_before = {}
    changed_issues = {}
    header_before = {}
    site_void_count = 0
    for line, issue in site_rows:
        if not line.is_active or issue.normalized_status == "void":
            continue
        requirement_before.setdefault(issue.project_id, {})[line.issue_line_id] = requirements.snapshot(line)
        header_before.setdefault(issue.issue_id, reconciliation._entity(issue))
        before = reconciliation._entity(line)
        line.is_active = False
        changed_issues[issue.issue_id] = issue
        site_void_count += 1
        _audit(db, project_id=issue.project_id, entity_type="site_issue_line", entity_id=line.issue_line_id,
               before=before, after=reconciliation._entity(line), reason=reason, operated_by=operated_by)
    db.flush()
    voided_issues = 0
    for issue in changed_issues.values():
        remaining = db.scalar(select(func.count()).select_from(IL).where(
            IL.issue_id == issue.issue_id, IL.is_active.is_(True)))
        if not remaining:
            issue.normalized_status, issue.voided_at = "void", now
            issue.raw_status = "void"
            issue.status_mapping_state = "mapped"
            issue.status_mapping_version = "site-issue-v2-workflow-v1"
            voided_issues += 1
    for project_id, before in requirement_before.items():
        requirements.record_corrections(db, project_id=project_id, before=before,
                                       operated_by=operated_by, reason=reason)
    # record_corrections increments each changed issue and line exactly once,
    # including a partial void whose remaining siblings keep the header active.
    for issue in changed_issues.values():
        _audit(db, project_id=issue.project_id, entity_type="site_issue", entity_id=issue.issue_id,
               before=header_before[issue.issue_id], after=reconciliation._entity(issue),
               reason=reason, operated_by=operated_by,
               action="VOID" if issue.normalized_status == "void" else "UPDATE")
    # States are already locked for every parent before any line lock; the
    # existing whole-order writer only reacquires this same lock set.
    db.flush()
    empty_sources = [order.raw_order_id for order in orders if not db.scalar(
        select(func.count()).select_from(L).where(L.order_id == order.id, L.is_active.is_(True)))]
    tombstone_result = {"voided": 0}
    if empty_sources:
        tombstone_result = demands.void_fast(db, source_order_ids=empty_sources, reason=reason,
            operated_by=operated_by, allowed_project_ids=allowed_project_ids,
            idempotency_key=f"wbdd-sync:{replay_key}")
    for project_id, state in states.items():
        operations.bump_locked_workbook_revision(db, state=state)
    db.flush()
    result = {"batch_id": batch_id, "voided_orders": tombstone_result["voided"],
              "voided_lines": len(to_void), "voided_site_lines": site_void_count,
              "voided_site_issues": voided_issues, "replayed": False}
    saved[replay_key] = {"request_digest": request_digest, "source_ids": sorted(parents),
        "line_ids": [line.id for line in to_void], "site_ids": sorted(site_ids), "result": result,
        "terminal_digest": _state_digest(db, sorted(parents), [line.id for line in to_void], sorted(site_ids))}
    batch.report_json = {**(batch.report_json or {}), "_wbdd_sync_results": saved}
    db.flush()
    return result

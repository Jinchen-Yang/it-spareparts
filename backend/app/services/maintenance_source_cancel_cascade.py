"""D-34 source cancellation impacts; reads never change operating facts.

A candidate is one source header in its latest successfully applied upsert.
Archived files and the D-27 missing-member algorithm are not execution evidence.
"""
from datetime import UTC

from sqlalchemy import and_, func, literal_column, or_, select
from sqlalchemy.orm import Session

from app.models.maintenance import (
    FMaintenanceLine as L,
)
from app.models.maintenance import (
    FMaintenanceOrder as O,
)
from app.models.maintenance import (
    MaintenanceDemandTombstone as T,
)
from app.models.maintenance import (
    MaintenanceManualCostOverride,
)
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_project_operations import MaintenanceSiteIssue as I
from app.models.maintenance_project_operations import MaintenanceSiteIssueLine as IL
from app.models.maintenance_source_assignment import (
    MaintenanceSourceOrderAssignment as A,
)
from app.models.system import SysImportBatch
from app.services.maintenance_field_protection import (
    is_manual_created,
    protected_fields,
)
from app.services.maintenance_wbdd_reconciliation import _digest, _entity, _json

SNAPSHOT_KEY = "_wbdd_source_status_snapshot"
RESULTS_KEY = "_source_cancel_confirmations"
KNOWN_STATES = {"已生效", "草稿", "进行中", ""}


class CancellationConflict(Exception):
    """The reviewed evidence or impact has changed; refresh before confirming."""


def _evidence(batch, order):
    snapshot = (batch.report_json or {}).get(SNAPSHOT_KEY) or {}
    entries = [row for row in snapshot.get("orders", [])
               if row.get("raw_order_id") == order.raw_order_id]
    evidence = entries[0] if len(entries) == 1 else {}
    valid = (
        batch.status in {"success", "superseded"} and batch.file_type == "maintenance"
        and snapshot.get("version") == 1 and snapshot.get("mode") == "upsert"
        and snapshot.get("batch_id") == batch.id and snapshot.get("file_hash") == batch.file_hash
        and snapshot.get("captured_at") and order.import_batch_id == batch.id
        and evidence.get("applied") is True and not evidence.get("blocked_reasons")
        and evidence.get("data_status") == order.data_status
    )
    return snapshot, bool(valid)


def _summary(batch, order):
    _, valid = _evidence(batch, order)
    warnings = []
    if order.data_status not in KNOWN_STATES | {None, "已取消"}:
        warnings.append("未知非生效状态，需人工")
    if not valid:
        warnings.append("缺少当前单头成功 upsert 的明确状态证据，请使用更新模式重新上传并核对。")
    consumed = any(row.get("result", {}).get("source_order_id") == order.raw_order_id
                   for row in ((batch.report_json or {}).get(RESULTS_KEY) or {}).values())
    if consumed:
        warnings.append("此上传候选已经人工确认；再次处理须重新上传并核对。")
    return {"batch_id": batch.id, "source_order_id": order.raw_order_id,
            "order_no": order.order_no, "data_status": order.data_status,
            "executable": valid and order.data_status == "已取消" and not consumed, "warnings": warnings}


def list_candidates(db: Session, *, project_id, page=1, page_size=20):
    """Paginate current project headers, including non-executable warnings."""
    query = select(O, SysImportBatch).join(A, and_(
        A.source_order_id == O.raw_order_id, A.is_active.is_(True))).join(
        SysImportBatch, SysImportBatch.id == O.import_batch_id).where(
        A.project_id == project_id, O.data_status.not_in(KNOWN_STATES),
        ~select(T.source_order_id).where(T.source_order_id == O.raw_order_id,
                                        T.restored_at.is_(None)).exists())
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.execute(query.order_by(O.order_no, O.id).offset((page - 1) * page_size).limit(page_size))
    return {"items": [_summary(batch, order) for order, batch in rows],
            "total": total, "page": page, "page_size": page_size}


def _impact(db, *, project_id, batch_id, source_order_id):
    batch = db.get(SysImportBatch, batch_id)
    order = db.scalar(select(O).where(O.raw_order_id == source_order_id))
    if batch is None or order is None:
        raise CancellationConflict("来源批次或单头不存在，请刷新影响清单")
    assignments = list(db.scalars(select(A).where(A.source_order_id == source_order_id)
                                 .order_by(A.assignment_id)))
    active = [row for row in assignments if row.is_active]
    if len(active) != 1 or active[0].project_id != project_id:
        raise CancellationConflict("项目归属已变化，请刷新影响清单")
    project = db.get(MaintenanceProject, project_id)
    lines = list(db.scalars(select(L).where(L.order_id == order.id).order_by(L.id)))
    raw_ids = {line.raw_line_id for line in lines}
    # Exact order-number references only discover warnings, never execution.
    sites = list(db.execute(select(IL, I).join(I, I.issue_id == IL.issue_id).where(
        I.project_id == project_id, or_(IL.source_line_id.in_(raw_ids),
            IL.source_order_id == source_order_id, IL.demand_order_no == order.order_no))
        .order_by(IL.issue_line_id)))
    overrides = list(db.scalars(select(MaintenanceManualCostOverride).where(
        MaintenanceManualCostOverride.line_id.in_([line.id for line in lines]))
        .order_by(MaintenanceManualCostOverride.id)))
    siblings = list(db.scalars(select(IL).where(IL.issue_id.in_({issue.issue_id for _, issue in sites}))
                              .order_by(IL.issue_line_id)))
    tombstone = db.get(T, source_order_id)
    exact, unlinked = [], []
    for line, issue in sites:
        if not line.is_active or issue.normalized_status == "void":
            continue
        # Delivery identities belong to the warehouse namespace. Conflicting
        # header references are not reliable WBDD lineage either.
        reliable = (line.source_line_id in raw_ids and line.delivery_line_id is None
                    and issue.source != "site_issue_v2"
                    and line.source_order_id in (None, source_order_id)
                    and line.demand_order_no in (None, "", order.order_no))
        (exact if reliable else unlinked).append((line, issue))
    result = _summary(batch, order)
    if not project.is_active:
        result["executable"] = False
        result["warnings"].append("项目已停用，不能确认连带作废。")
    if tombstone is not None and tombstone.restored_at is None:
        result["executable"] = False
        result["warnings"].append("该单已系统作废；来源重传不会复活，恢复须走实名更正。")
    if unlinked:
        result["warnings"].append(f"无法精确关联的 {len(unlinked)} 条领用将保留，请另行核对。")
    snapshot, _ = _evidence(batch, order)
    result["expected_digest"] = _digest({
        "snapshot": snapshot, "batch": {"id": batch.id, "file_hash": batch.file_hash,
            "status": batch.status, "file_type": batch.file_type},
        "project": _entity(project), "order": _entity(order),
        "assignments": [_entity(row) for row in assignments],
        "lines": [_entity(row) for row in lines], "overrides": [_entity(row) for row in overrides],
        "sites": [{"line": _entity(line), "issue": _entity(issue)} for line, issue in sites],
        "site_siblings": [_entity(line) for line in siblings],
        "tombstone": _entity(tombstone) if tombstone else None,
    })
    # These labels are derived from fields already covered by the entity digest.
    # Keep display metadata out of the hash to preserve existing confirmations.
    manual_labels = {
        line.raw_line_id: {
            "manual_created": is_manual_created(line.raw_line_id),
            "protected_fields": sorted(protected_fields(
                raw_line_id=line.raw_line_id, edited_source=line.edited_source,
                manual_override=line.manual_override,
            )),
        } for line in lines
    }
    source_lines = {line.raw_line_id: line for line in lines}
    result["demands"] = [{"line_id": line.id, "raw_line_id": line.raw_line_id,
        "pn": line.pn_std or line.pn_raw, "qty": _json(line.qty), "is_active": line.is_active,
        **manual_labels[line.raw_line_id]} for line in lines]

    def site_dto(pair):
        line, issue = pair
        reason = "原始需求明细 ID"
        if pair in unlinked:
            reason = ("仓库来源身份" if line.delivery_line_id or issue.source == "site_issue_v2"
                      else "来源身份冲突" if line.source_line_id in raw_ids
                      else "仅需求单头或单号关联")
        # Never infer manual provenance for an ambiguous warehouse/header match.
        source = source_lines.get(line.source_line_id) if pair in exact else None
        labels = manual_labels[source.raw_line_id] if source else {
            "manual_created": False, "protected_fields": [],
        }
        return {"issue_line_id": line.issue_line_id, "issue_id": issue.issue_id,
                "issue_no": issue.display_no_effective, "issue_date": _json(issue.issue_date),
                "link_reason": reason, "pn": line.pn, "quantity": _json(line.quantity),
                "is_active": line.is_active,
                "source_demand_line_id": source.id if source else None, **labels}

    result["exact_site_lines"] = [site_dto(pair) for pair in exact]
    result["unlinked_site_lines"] = [site_dto(pair) for pair in unlinked]
    return result, batch, order, lines, sites, exact


def candidate_detail(db: Session, *, project_id, batch_id, source_order_id):
    return _impact(db, project_id=project_id, batch_id=batch_id, source_order_id=source_order_id)[0]


def receipt_batch_query(replay_key):
    # Keep the expression identical to the GIN index (including prepared plans).
    # A batch/time filter would miss historical cross-batch reuse of a key.
    return select(SysImportBatch).where(
        SysImportBatch.report_json.op("->")(
            literal_column("'_source_cancel_confirmations'")
        ).op("?")(replay_key)
    )


def confirmation_status(db: Session, *, project_id, batch_id, source_order_id,
                        operated_by, idempotency_key):
    """Read a historical receipt, including after its candidate disappears.

    A missing receipt is only an observation, never proof that an in-flight
    confirmation failed. Callers must retain the same request for any retry.
    """
    batch = db.get(SysImportBatch, batch_id)
    replay_key = _digest([operated_by, idempotency_key])
    prior = ((batch.report_json or {}).get(RESULTS_KEY) or {}).get(replay_key) if batch else None
    if prior is None:
        return {"result": None}
    result = prior["result"]
    if (prior["project_id"] != project_id or result["batch_id"] != batch_id
            or result["source_order_id"] != source_order_id):
        raise CancellationConflict("同一幂等键已用于其他确认请求，请刷新核对")
    return {"result": {**result, "replayed": True}}


def confirm_cancellation(db: Session, *, project_id, batch_id, source_order_id,
                         expected_digest, reason, operated_by, idempotency_key, reauthorize):
    """Confirm one reviewed candidate atomically; the API owns commit/rollback.

    Global -> workbook state -> project -> source header -> assignment -> demand
    lines -> issue headers -> all their lines. Never take a new upper-level lock
    after a lower one. Replay is a historical receipt, not a current-state claim.
    """
    from datetime import datetime

    from app.config import DATA_CHANGE_ADVISORY_LOCK_KEY
    from app.services import maintenance_demands as demands
    from app.services import maintenance_project_operations as operations
    from app.services import maintenance_site_return_requirements as requirements
    from app.services.maintenance_wbdd_sync import _audit

    reason = str(reason or "").strip()
    if not reason or len(reason) > demands.MAX_REASON_LENGTH or not operated_by:
        raise ValueError("必须提供实名操作人和有效作废原因（最多1000字）")
    request_digest = _digest({"project_id": project_id, "batch_id": batch_id,
        "source_order_id": source_order_id, "expected_digest": expected_digest,
        "reason": reason, "operated_by": operated_by})
    replay_key = _digest([operated_by, idempotency_key])
    db.execute(select(func.pg_advisory_xact_lock(DATA_CHANGE_ADVISORY_LOCK_KEY)))
    db.expire_all()
    reauthorize()
    prior_batch = db.scalar(receipt_batch_query(replay_key))
    if prior_batch is not None:
        prior = prior_batch.report_json[RESULTS_KEY][replay_key]
        if prior["request_digest"] != request_digest:
            raise CancellationConflict("同一幂等键已用于其他确认请求，请刷新核对")
        return {**prior["result"], "replayed": True}

    before, batch, order, lines, sites, exact = _impact(
        db, project_id=project_id, batch_id=batch_id, source_order_id=source_order_id)
    if not before["executable"] or before["expected_digest"] != expected_digest:
        raise CancellationConflict("候选或相关数据已变化，本次未执行，请刷新影响清单后重新核对")
    if len(lines) > demands.MAX_DELETE_LINES:
        raise ValueError(f"一次最多涉及{demands.MAX_DELETE_LINES}行备件")
    states = operations.lock_workbook_states(db, project_ids=[project_id])
    db.scalar(select(MaintenanceProject).where(MaintenanceProject.project_id == project_id)
              .with_for_update().execution_options(populate_existing=True))
    db.scalar(select(O).where(O.id == order.id).with_for_update().execution_options(populate_existing=True))
    list(db.scalars(select(A).where(A.source_order_id == source_order_id).order_by(A.assignment_id)
                    .with_for_update().execution_options(populate_existing=True)))
    list(db.scalars(select(L).where(L.order_id == order.id).order_by(L.id)
                    .with_for_update().execution_options(populate_existing=True)))
    issue_ids = {issue.issue_id for _, issue in sites}
    list(db.scalars(select(I).where(I.issue_id.in_(issue_ids)).order_by(I.issue_id)
                    .with_for_update().execution_options(populate_existing=True)))
    list(db.scalars(select(IL).where(IL.issue_id.in_(issue_ids)).order_by(IL.issue_line_id)
                    .with_for_update().execution_options(populate_existing=True)))
    # Global lock serializes imports; the batch row lock also protects append-only
    # command receipts. Reload everything after waiting on lower-level writers.
    db.scalar(select(SysImportBatch).where(SysImportBatch.id == batch_id)
              .with_for_update().execution_options(populate_existing=True))
    db.expire_all()
    reauthorize()
    checked, batch, order, lines, sites, exact = _impact(
        db, project_id=project_id, batch_id=batch_id, source_order_id=source_order_id)
    if not checked["executable"] or checked["expected_digest"] != expected_digest:
        raise CancellationConflict("候选或关联领用已变化，本次未执行，请刷新影响清单后重新核对")

    now = datetime.now(UTC)
    active_lines = [line for line in lines if line.is_active]
    for line in active_lines:
        old = _entity(line)
        line.is_active, line.voided_at = False, now
        line.voided_by, line.void_reason = operated_by, reason
        _audit(db, project_id=project_id, entity_type="maintenance_line", entity_id=line.id,
               before=old, after=_entity(line), reason=reason, operated_by=operated_by)
    requirement_before = {line.issue_line_id: requirements.snapshot(line) for line, _ in exact}
    line_before = {line.issue_line_id: _entity(line) for line, _ in exact}
    changed_issues = {issue.issue_id: issue for _, issue in exact}
    header_before = {key: _entity(issue) for key, issue in changed_issues.items()}
    for line, _ in exact:
        line.is_active = False
    db.flush()
    voided_issues = 0
    for issue in changed_issues.values():
        if not db.scalar(select(func.count()).select_from(IL).where(
                IL.issue_id == issue.issue_id, IL.is_active.is_(True))):
            issue.normalized_status, issue.raw_status = "void", "void"
            issue.status_mapping_state = "mapped"
            issue.status_mapping_version = "site-issue-v2-workflow-v1"
            issue.voided_at = now
            voided_issues += 1
    requirements.record_corrections(db, project_id=project_id, before=requirement_before,
                                   operated_by=operated_by, reason=reason)
    db.flush()
    for line, _ in exact:
        _audit(db, project_id=project_id, entity_type="site_issue_line", entity_id=line.issue_line_id,
               before=line_before[line.issue_line_id], after=_entity(line), reason=reason, operated_by=operated_by)
    for issue in changed_issues.values():
        _audit(db, project_id=project_id, entity_type="site_issue", entity_id=issue.issue_id,
               before=header_before[issue.issue_id], after=_entity(issue), reason=reason, operated_by=operated_by,
               action="VOID" if issue.normalized_status == "void" else "UPDATE")
    # Do not pass an inner idempotency key: void_fast's key->global prefix would
    # invert this command's global-first protocol. Our receipt is the sole replay.
    tombstone = demands.void_fast(db, source_order_ids=[source_order_id], reason=reason,
                                 operated_by=operated_by, allowed_project_ids={project_id})
    operations.bump_locked_workbook_revision(db, state=states[project_id])
    result = {"batch_id": batch_id, "source_order_id": source_order_id,
              "voided_orders": tombstone["voided"], "voided_lines": len(active_lines),
              "voided_site_lines": len(exact), "voided_site_issues": voided_issues,
              "replayed": False, "confirmed_at": now.isoformat(), "warnings": checked["warnings"]}
    saved = dict((batch.report_json or {}).get(RESULTS_KEY) or {})
    saved[replay_key] = {"request_digest": request_digest, "result": result,
                         "operated_by": operated_by, "reason": reason, "project_id": project_id}
    batch.report_json = {**(batch.report_json or {}), RESULTS_KEY: saved}
    db.flush()
    return result

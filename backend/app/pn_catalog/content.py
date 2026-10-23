"""Condition content and proposed changes share one transaction and actor boundary."""
import base64
import hashlib
import json
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from . import api
from .content_dto import (AdoptCandidate, CreateContentCandidate, CreateProfileCandidate, Grade,
                          LegacyEffectiveContent, PatchContent, RejectCandidate)
from .content_models import ConditionContent, ContentCandidate
from .dto import Authorize, CATEGORY_FIELDS, CommandContext, PatchProfile, Provenance
from .errors import CatalogError
from .persistence import lock_part, read_versions


def _grade(grade):
    if grade not in {"good", "bad"}:
        raise CatalogError("request.validation_failed", "好坏件类型无效", status_code=422)


def _read_action(resource):
    return "read_content" if resource == "content" else "read"


def _path(resource, grade, key):
    return f"content.{grade}.{key}" if resource == "content" else key


def _read_allowed(decision, path):
    return api._allowed(decision.readable_fields, path)


def _require_fields(write, read, paths):
    api._check_fields(write, paths)
    if any(not _read_allowed(read, path) for path in paths):
        raise CatalogError("auth.forbidden", "需要读取所选字段的权限", status_code=403)


def _human(context):
    if context.actor.actor_kind != "human":
        raise CatalogError("catalog.human_confirmation_required", "需要实际操作人确认", status_code=403)


def _identity(db, part, expected=None):
    revision = read_versions(db, part.id)["identity_revision"]
    if expected is not None and (revision != expected or part.status != "active"):
        raise CatalogError("catalog.identity_changed", "型号身份已变化，请重新确认目标")
    return revision


def _row(db, part_id, grade, *, write=False):
    if write:
        # Parent SHARE locks preserve PN identity, while only this grade is serialized.
        db.execute(insert(ConditionContent).values(part_id=part_id, grade=grade)
                   .on_conflict_do_nothing(index_elements=["part_id", "grade"]))
    statement = select(ConditionContent).where(ConditionContent.part_id == part_id,
                                              ConditionContent.grade == grade)
    if write:
        statement = statement.with_for_update()
    return db.scalar(statement.execution_options(populate_existing=True))


def _content_projection(row, part_id, grade, identity_revision, decision):
    result = {"part_id": part_id, "grade": grade, "identity_revision": identity_revision,
              "revision": row.revision if row else 0,
              "status": row.status if row else "pending"}
    allowed = {key for key in ("title", "description")
               if _read_allowed(decision, f"content.{grade}.{key}")}
    for key in allowed:
        result[key] = getattr(row, key) if row else None
    result["field_versions"] = {key: (row.field_versions or {}).get(key, 0) if row else 0 for key in allowed}
    result["protected_fields"] = [key for key in (row.protected_fields if row else []) if key in allowed]
    result["field_provenance"] = {key: value for key, value in (row.field_provenance if row else {}).items()
                                  if key in allowed}
    # Status is specifically description confirmation; hiding description hides its status too.
    if "description" not in allowed:
        result.pop("status")
    return result


def get_content(db: Session, part_id: int, grade: Grade, *, context: CommandContext, authorize: Authorize):
    _grade(grade)
    decision = api._authorize(db, context, authorize, "read_content", part_id)
    part = lock_part(db, part_id, shared=True)
    identity = _identity(db, part)
    row = _row(db, part_id, grade)
    data = _content_projection(row, part_id, grade, identity, decision)
    return {"data": data, "meta": {"request_id": str(context.request_id), "resource_revision": data["revision"],
        "data_revision": data["revision"], "identity_revision": identity, "replayed": False}}


def _content_baseline(row, context, expected):
    if context.if_match_revision is not None and context.if_match_revision != row.revision:
        raise CatalogError("request.precondition_failed", "商品描述版本已变化", status_code=412)
    if row.revision != expected:
        raise CatalogError("catalog.field_conflict", "商品描述已变化，请核对草稿",
                           details={"resolution": "reload_and_confirm"})


def _provenance(context):
    return {**context.actor.provenance.model_dump(mode="json", exclude_none=True),
            "principal_id": context.actor.principal_id, "request_id": str(context.request_id),
            "confirmed_by": context.confirmed_by,
            "adopted_from_candidate_id": context.adopted_from_candidate_id}


def _change_content(db, row, changes, *, context, decision, digest, action, identity,
                    status=None, provenance=None, protected_fields=None):
    old_revision, old_status = row.revision, row.status
    versions, origins = dict(row.field_versions), dict(row.field_provenance)
    audit_changes = {}
    for key, value in changes.items():
        audit_changes[f"content.{row.grade}.{key}"] = {"before": getattr(row, key), "after": value}
        setattr(row, key, value)
        versions[key] = versions.get(key, 0) + 1
        origins[key] = provenance if provenance is not None else _provenance(context)
    row.field_versions, row.field_provenance = versions, origins
    row.protected_fields = (sorted(set(row.protected_fields) | set(changes))
                            if protected_fields is None else sorted(set(protected_fields)))
    if status is not None:
        row.status = status
    elif "description" in changes:
        row.status = "confirmed"
    if old_status != row.status:
        audit_changes[f"content.{row.grade}.status"] = {"before": old_status, "after": row.status}
    row.revision += 1
    event = api.record_audit(db, row.part_id, context=context, action=action, changes=audit_changes,
        before_revision=old_revision, after_revision=row.revision, authorization=decision.metadata,
        payload_hash=digest, revisions={"axis": f"content.{row.grade}",
            "before": {"identity": identity, f"content.{row.grade}": old_revision},
            "after": {"identity": identity, f"content.{row.grade}": row.revision}})
    return event


def patch_content(db: Session, part_id: int, grade: Grade, command: PatchContent, *,
                  context: CommandContext, authorize: Authorize):
    _grade(grade)
    _human(context)
    if context.actor.provenance.kind != "manual":
        raise CatalogError("catalog.candidate_required", "来源内容必须经候选采用流程生效", status_code=422)
    handle = api.begin_command(db, context=context, authorize=authorize, operation="update_content",
        part_id=part_id, resource_type="content", resource_id=f"{part_id}:{grade}", payload=command)
    read = api._authorize(db, context, authorize, "read_content", part_id)
    _require_fields(handle.decision, read, [f"content.{grade}.{key}" for key in command.changes])
    if handle.receipt:
        return render_receipt(db, handle.receipt, context=context, authorize=authorize, replayed=True)
    part = lock_part(db, part_id, shared=True)
    identity = _identity(db, part, command.expected_identity_revision)
    row = _row(db, part_id, grade, write=True)
    _content_baseline(row, context, command.expected_revision)
    confirmed = context.model_copy(update={"confirmed_by": context.actor.principal_id})
    event = _change_content(db, row, command.changes, context=confirmed, decision=handle.decision,
                           digest=handle.payload_hash, action="update_content", identity=identity)
    receipt = api.finish_command(db, handle, part_id=part_id, applied_revision=row.revision,
        audit_id=event.id, result={"resource": "content", "part_id": part_id, "grade": grade,
                                  "identity_revision": identity})
    return render_receipt(db, receipt, context=context, authorize=authorize, replayed=False)


def _candidate(db, candidate_id, *, write=False):
    statement = select(ContentCandidate).where(ContentCandidate.id == candidate_id)
    if write:
        statement = statement.with_for_update()
    candidate = db.scalar(statement.execution_options(populate_existing=True))
    if candidate is None:
        raise CatalogError("catalog.candidate_not_found", "候选不存在", status_code=404)
    return candidate


def _proposal_fields(candidate):
    return set(candidate.proposal.get("changes", {})) | {f"specs.{key}" for key in candidate.proposal.get("spec_updates", {})}


def _candidate_projection(candidate, decision):
    def allowed(key):
        return _read_allowed(decision, _path(candidate.resource, candidate.grade, key))
    proposal = {"changes": {key: value for key, value in candidate.proposal.get("changes", {}).items() if allowed(key)},
                "spec_updates": {key: value for key, value in candidate.proposal.get("spec_updates", {}).items()
                                 if allowed(f"specs.{key}")}}
    return {"id": str(candidate.id), "part_id": candidate.part_id, "resource": candidate.resource,
            "grade": candidate.grade, "identity_revision": candidate.identity_revision,
            "base_revision": candidate.base_revision, "revision": candidate.revision, "status": candidate.status,
            "proposal": proposal, "provenance": candidate.provenance, "created_by": candidate.created_by,
            "adopted_fields": [key for key in candidate.adopted_fields if allowed(key)],
            "decisions": [{**item, "fields": [key for key in item.get("fields", []) if allowed(key)]}
                          for item in candidate.decisions], "created_at": candidate.created_at.isoformat()}


def get_candidate(db: Session, candidate_id: UUID, *, context: CommandContext, authorize: Authorize):
    candidate = _candidate(db, candidate_id)
    api._authorize(db, context, authorize, "read_candidates", candidate.part_id)
    read = api._authorize(db, context, authorize, _read_action(candidate.resource), candidate.part_id)
    return {"data": _candidate_projection(candidate, read), "meta": {
        "request_id": str(context.request_id), "resource_revision": candidate.revision,
        "data_revision": candidate.revision, "identity_revision": candidate.identity_revision, "replayed": False}}


def _candidate_audit(db, candidate, *, context, handle, before, action, fields):
    paths = {_path(candidate.resource, candidate.grade, key): {"candidate_id": str(candidate.id),
              "decision": action, "before": before, "after": candidate.revision} for key in fields}
    return api.record_audit(db, candidate.part_id, context=context, action=action, changes=paths,
        before_revision=before, after_revision=candidate.revision, authorization=handle.decision.metadata,
        payload_hash=handle.payload_hash, revisions={"axis": "candidate", "candidate_id": str(candidate.id),
            "before": {"candidate": before}, "after": {"candidate": candidate.revision}})


def _finish_candidate(db, handle, candidate, audit_id):
    return api.finish_command(db, handle, part_id=candidate.part_id, applied_revision=candidate.revision,
        audit_id=audit_id, result={"resource": "candidate", "candidate_id": str(candidate.id),
                                  "identity_revision": candidate.identity_revision})


def create_candidate(db: Session, command: CreateProfileCandidate | CreateContentCandidate, *,
                     context: CommandContext, authorize: Authorize):
    handle = api.begin_command(db, context=context, authorize=authorize, operation="create_candidate",
        part_id=command.part_id, resource_type="candidate", resource_id=None, payload=command)
    read = api._authorize(db, context, authorize, _read_action(command.resource), command.part_id)
    api._authorize(db, context, authorize, "read_candidates", command.part_id)
    fields = set(command.changes)
    if command.resource == "profile":
        fields |= {f"specs.{key}" for key in command.spec_updates}
    # Candidate permission may propose without permission to replace effective values.
    if any(not _read_allowed(read, _path(command.resource, getattr(command, "grade", None), key)) for key in fields):
        raise CatalogError("auth.forbidden", "需要读取候选所涉及字段的权限", status_code=403)
    if handle.receipt:
        return render_receipt(db, handle.receipt, context=context, authorize=authorize, replayed=True)
    part = lock_part(db, command.part_id, shared=True)
    identity = _identity(db, part, command.expected_identity_revision)
    row = _row(db, part.id, command.grade) if command.resource == "content" else None
    revision = (row.revision if row else 0) if command.resource == "content" else read_versions(db, part.id)["profile_revision"]
    if revision != command.base_revision:
        raise CatalogError("catalog.field_conflict", "候选生成时的资料版本已变化")
    proposal = {"changes": command.changes}
    if command.resource == "profile":
        api._manual_specs(command)
        proposal["spec_updates"] = {key: value.model_dump(mode="json") if value else None
                                     for key, value in command.spec_updates.items()}
    candidate = ContentCandidate(id=uuid4(), part_id=part.id, resource=command.resource,
        grade=getattr(command, "grade", None), identity_revision=identity, base_revision=revision,
        revision=1, status="pending", proposal=proposal, provenance=command.provenance.model_dump(mode="json", exclude_none=True),
        created_by=context.actor.principal_id, adopted_fields=[], decisions=[])
    db.add(candidate)
    db.flush()
    event = _candidate_audit(db, candidate, context=context, handle=handle, before=0,
                             action="create_candidate", fields=fields)
    receipt = _finish_candidate(db, handle, candidate, event.id)
    return render_receipt(db, receipt, context=context, authorize=authorize, replayed=False)


def adopt_candidate(db: Session, candidate_id: UUID, command: AdoptCandidate, *,
                    context: CommandContext, authorize: Authorize):
    _human(context)
    header = _candidate(db, candidate_id)
    handle = api.begin_command(db, context=context, authorize=authorize, operation="adopt_candidate",
        part_id=header.part_id, resource_type="candidate", resource_id=candidate_id, payload=command)
    read = api._authorize(db, context, authorize, _read_action(header.resource), header.part_id)
    api._authorize(db, context, authorize, "read_candidates", header.part_id)
    update = api._authorize(db, context, authorize, "update_content" if header.resource == "content" else "update_profile", header.part_id)
    fields = set(command.fields)
    paths = {_path(header.resource, header.grade, key) for key in fields}
    if header.resource == "profile" and fields & CATEGORY_FIELDS:
        paths.update(CATEGORY_FIELDS)
    _require_fields(update, read, paths)
    if handle.receipt:
        return render_receipt(db, handle.receipt, context=context, authorize=authorize, replayed=True)
    # Always parent before candidate; profile mutation never upgrades an already held SHARE lock.
    part = lock_part(db, header.part_id, shared=header.resource == "content")
    identity = _identity(db, part, command.expected_identity_revision)
    candidate = _candidate(db, candidate_id, write=True)
    if candidate.identity_revision != identity:
        raise CatalogError("catalog.identity_changed", "候选对应的型号身份已变化")
    if context.if_match_revision is not None and context.if_match_revision != candidate.revision:
        raise CatalogError("request.precondition_failed", "候选版本已变化", status_code=412)
    if candidate.revision != command.expected_candidate_revision:
        raise CatalogError("catalog.candidate_conflict", "候选已被处理，请刷新")
    if candidate.status not in {"pending", "partially_adopted"}:
        raise CatalogError("catalog.candidate_closed", "候选已完成处理")
    if not fields <= _proposal_fields(candidate) or fields & set(candidate.adopted_fields):
        raise CatalogError("catalog.candidate_fields_invalid", "所选字段不存在或已经采用", status_code=422)
    confirmed = context.model_copy(update={
        "actor": context.actor.model_copy(update={"provenance": Provenance.model_validate(candidate.provenance)}),
        "confirmed_by": context.actor.principal_id, "adopted_from_candidate_id": str(candidate.id),
        "reason": command.reason or context.reason, "if_match_revision": None})
    changes = {key: value for key, value in candidate.proposal["changes"].items() if key in fields}
    if candidate.resource == "content":
        row = _row(db, part.id, candidate.grade, write=True)
        _content_baseline(row, confirmed, command.expected_target_revision)
        if command.field_versions is not None:
            for key in fields:
                if key not in command.field_versions:
                    raise CatalogError("request.precondition_required", "缺少所选字段版本", status_code=428)
                if command.field_versions[key] != row.field_versions.get(key, 0):
                    raise CatalogError("catalog.field_conflict", "所选字段已变化")
        effective = _change_content(db, row, changes, context=confirmed, decision=update, digest=handle.payload_hash,
                                    action="adopt_content_candidate", identity=identity)
        target_revision = row.revision
    else:
        specs = {key: value for key, value in candidate.proposal.get("spec_updates", {}).items() if f"specs.{key}" in fields}
        patch = PatchProfile.model_validate({"expected_identity_revision": identity,
            "expected_profile_revision": command.expected_target_revision, "field_versions": command.field_versions,
            "changes": changes, "spec_updates": specs})
        applied = api._mutate_profile(db, part.id, patch, context=confirmed, decision=update, read_decision=read,
                                      digest=handle.payload_hash, action="adopt_profile_candidate")
        effective = None
        target_revision = applied["snapshot"]["profile_revision"]
    before = candidate.revision
    candidate.revision += 1
    candidate.adopted_fields = sorted(set(candidate.adopted_fields) | fields)
    candidate.status = "adopted" if set(candidate.adopted_fields) == _proposal_fields(candidate) else "partially_adopted"
    candidate.decisions = [*candidate.decisions, {"action": "adopt", "fields": sorted(fields),
        "principal_id": context.actor.principal_id, "request_id": str(context.request_id),
        "target_revision": target_revision, "effective_audit_id": effective.id if effective else applied["audit_id"],
        "reason": command.reason}]
    event = _candidate_audit(db, candidate, context=confirmed, handle=handle, before=before,
                             action="adopt_candidate", fields=fields)
    receipt = _finish_candidate(db, handle, candidate, event.id)
    return render_receipt(db, receipt, context=context, authorize=authorize, replayed=False)


def reject_candidate(db: Session, candidate_id: UUID, command: RejectCandidate, *,
                     context: CommandContext, authorize: Authorize):
    _human(context)
    header = _candidate(db, candidate_id)
    handle = api.begin_command(db, context=context, authorize=authorize, operation="reject_candidate",
        part_id=header.part_id, resource_type="candidate", resource_id=candidate_id, payload=command)
    api._authorize(db, context, authorize, "read_candidates", header.part_id)
    read = api._authorize(db, context, authorize, _read_action(header.resource), header.part_id)
    remaining = _proposal_fields(header) - set(header.adopted_fields)
    if any(not _read_allowed(read, _path(header.resource, header.grade, key)) for key in remaining):
        raise CatalogError("auth.forbidden", "需要读取待拒绝字段的权限", status_code=403)
    if handle.receipt:
        return render_receipt(db, handle.receipt, context=context, authorize=authorize, replayed=True)
    lock_part(db, header.part_id, shared=True)
    candidate = _candidate(db, candidate_id, write=True)
    if context.if_match_revision is not None and context.if_match_revision != candidate.revision:
        raise CatalogError("request.precondition_failed", "候选版本已变化", status_code=412)
    if candidate.revision != command.expected_candidate_revision:
        raise CatalogError("catalog.candidate_conflict", "候选已被处理，请刷新")
    if candidate.status not in {"pending", "partially_adopted"}:
        raise CatalogError("catalog.candidate_closed", "候选已完成处理")
    remaining = _proposal_fields(candidate) - set(candidate.adopted_fields)
    before = candidate.revision
    candidate.revision += 1
    candidate.status = "rejected"
    candidate.decisions = [*candidate.decisions, {"action": "reject", "fields": sorted(remaining),
        "principal_id": context.actor.principal_id, "request_id": str(context.request_id), "reason": command.reason}]
    event = _candidate_audit(db, candidate, context=context, handle=handle, before=before,
                             action="reject_candidate", fields=remaining)
    receipt = _finish_candidate(db, handle, candidate, event.id)
    return render_receipt(db, receipt, context=context, authorize=authorize, replayed=False)


def list_candidates(db: Session, part_id: int, *, context: CommandContext, authorize: Authorize,
                    status: str | None = None, cursor: str | None = None, limit: int = 50):
    api._authorize(db, context, authorize, "read_candidates", part_id)
    if not 1 <= limit <= 200 or status not in {None, "pending", "partially_adopted", "adopted", "rejected"}:
        raise CatalogError("request.validation_failed", "候选查询参数无效", status_code=422)
    decisions = {resource: api._authorize(db, context, authorize, _read_action(resource), part_id)
                 for resource in ("profile", "content")}
    fingerprint = api._digest({"part_id": part_id, "status": status,
                              "actor": context.actor.principal_id, "client": context.actor.client_id})
    after = None
    if cursor:
        try:
            if len(cursor) > 512:
                raise ValueError()
            value = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
            if value["q"] != fingerprint:
                raise ValueError()
            after = UUID(value["id"])
        except (ValueError, KeyError, TypeError):
            raise CatalogError("request.validation_failed", "分页游标无效", status_code=422) from None
    query = select(ContentCandidate).where(ContentCandidate.part_id == part_id)
    if status:
        query = query.where(ContentCandidate.status == status)
    if after:
        query = query.where(ContentCandidate.id > after)
    rows = db.scalars(query.order_by(ContentCandidate.id).limit(limit + 1)).all()
    items, total = [], 0
    for candidate in rows[:limit]:
        item = _candidate_projection(candidate, decisions[candidate.resource])
        size = len(api._json(item).encode())
        if items and total + size > 512 * 1024:
            break
        items.append(item)
        total += size
    next_cursor = None
    if len(rows) > len(items):
        next_cursor = base64.urlsafe_b64encode(api._json({"id": items[-1]["id"], "q": fingerprint}).encode()).decode().rstrip("=")
    return {"data": items, "meta": {"request_id": str(context.request_id), "replayed": False, "next_cursor": next_cursor}}


def migrate_effective_content(db: Session, part_id: int, command: LegacyEffectiveContent, *,
                              context: CommandContext, authorize: Authorize):
    handle = api.begin_command(db, context=context, authorize=authorize, operation="migrate_content",
        part_id=part_id, resource_type="content", resource_id=f"{part_id}:{command.grade}", payload=command)
    read = api._authorize(db, context, authorize, "read_content", part_id)
    _require_fields(handle.decision, read, [f"content.{command.grade}.{key}" for key in ("title", "description")])
    if handle.receipt:
        return render_receipt(db, handle.receipt, context=context, authorize=authorize, replayed=True)
    # Source identity is global across target PNs and request keys.
    key = int.from_bytes(hashlib.sha256(api._json(["content-migration", command.source_namespace,
                         command.source_record_id]).encode()).digest()[:8], "big", signed=True)
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
    existing = db.scalar(select(ConditionContent).where(
        ConditionContent.legacy_source_namespace == command.source_namespace,
        ConditionContent.legacy_source_record_id == command.source_record_id))
    if existing is not None:
        raise CatalogError("migration.source_already_imported", "来源记录已迁移，请使用原请求回执")
    part = lock_part(db, part_id, shared=True)
    identity = _identity(db, part, command.expected_identity_revision)
    row = _row(db, part_id, command.grade, write=True)
    _content_baseline(row, context, 0)
    row.legacy_source_namespace, row.legacy_source_record_id = command.source_namespace, command.source_record_id
    row.legacy_source_revision = command.source_revision
    historical = {**command.provenance.model_dump(mode="json", exclude_none=True),
        "principal_id": command.historical_actor_id, "display_name": command.historical_actor_display,
        "confirmed_by": command.historical_actor_id if command.status == "confirmed" else None,
        "source_namespace": command.source_namespace, "source_record_id": command.source_record_id,
        "source_revision": command.source_revision, "migration_request_id": str(context.request_id)}
    event = _change_content(db, row, {"title": command.title, "description": command.description}, context=context,
        decision=handle.decision, digest=handle.payload_hash, action="migrate_content", identity=identity,
        status=command.status, provenance=historical, protected_fields=command.protected_fields)
    receipt = api.finish_command(db, handle, part_id=part_id, applied_revision=row.revision, audit_id=event.id,
        result={"resource": "content", "part_id": part_id, "grade": command.grade, "identity_revision": identity})
    return render_receipt(db, receipt, context=context, authorize=authorize, replayed=False)


def render_receipt(db, receipt, *, context, authorize, replayed):
    api._authorize(db, context, authorize, receipt.operation, receipt.part_id)
    if receipt.result["resource"] == "content":
        current = get_content(db, receipt.part_id, receipt.result["grade"], context=context, authorize=authorize)
    else:
        current = get_candidate(db, UUID(receipt.result["candidate_id"]), context=context, authorize=authorize)
    current["meta"] = api.receipt_meta(receipt, data_revision=current["data"]["revision"],
        identity_revision=current["data"]["identity_revision"], replayed=replayed)
    return current

"""Small transaction primitives shared by gallery, uploads and inbox commands."""

from datetime import datetime, timezone
import hashlib
from uuid import UUID

from sqlalchemy import select, text

from . import api
from .dto import AuthorizationDecision
from .errors import CatalogError
from .models import PartState
from .persistence import acquire_identity_lock, lock_part, state_for_write


def now():
    return datetime.now(timezone.utc)


def key_lock(db, *parts):
    value = int.from_bytes(
        hashlib.sha256(repr(parts).encode()).digest()[:8], "big", signed=True
    )
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": value})


def access(db, context, authorize, action, part_id, *, field="gallery", write=False):
    decision = authorize(db, context, action, part_id)
    if not isinstance(decision, AuthorizationDecision):
        raise CatalogError(
            "auth.unavailable", "权限服务不可用", status_code=503, retryable=True
        )
    grant(decision, field, write=write)
    return decision


def grant(decision, field, *, write=True):
    fields = decision.writable_fields if write else decision.readable_fields
    if "*" not in fields and field not in fields:
        raise CatalogError("auth.forbidden", "没有该资源的操作权限", status_code=403)


def begin(
    db,
    *,
    context,
    authorize,
    operation,
    part_id,
    resource,
    identifier,
    command,
    field="gallery",
):
    handle = api.begin_command(
        db,
        context=context,
        authorize=authorize,
        operation=operation,
        part_id=part_id,
        resource_type=resource,
        resource_id=identifier,
        payload=command,
    )
    grant(handle.decision, field)
    grant(handle.decision, field, write=False)
    return handle


def load(db, model, identifier, *, lock=False, shared=False, code="request.not_found"):
    query = select(model).where(list(model.__table__.primary_key)[0] == identifier)
    if lock:
        query = query.with_for_update(read=shared)
    value = db.scalar(query.execution_options(populate_existing=True))
    if value is None:
        raise CatalogError(code, "资源不存在", status_code=404)
    return value


def identity(db, part_ids, *, expected=None):
    """Global order: identity topology -> sorted PN -> media resource -> job."""
    acquire_identity_lock(db, shared=True)
    states = {}
    for pid in sorted(set(p for p in part_ids if p is not None)):
        part = lock_part(db, pid)
        state = state_for_write(db, pid)
        if expected is not None and pid in expected:
            if (
                state.identity_revision != expected[pid]
                or part.merged_into_id is not None
            ):
                raise CatalogError(
                    "catalog.identity_changed", "型号身份已变化，请重新确认目标"
                )
            if state.availability != "active":
                raise CatalogError("catalog.inactive", "型号已停用")
        states[pid] = state
    return states


def read_identity(db, part_ids):
    """Hold the read snapshot without manufacturing version rows or taking writer locks."""
    acquire_identity_lock(db, shared=True)
    states = {}
    for pid in sorted(set(p for p in part_ids if p is not None)):
        lock_part(db, pid, shared=True)
        states[pid] = db.get(PartState, pid, populate_existing=True)
    return states


def revision(context, actual, expected, *, code="gallery.version_conflict"):
    if context.if_match_revision is not None and context.if_match_revision != actual:
        raise CatalogError(
            "request.precondition_failed", "资源版本已变化", status_code=412
        )
    if actual != expected:
        raise CatalogError(code, "资源已被修改，请刷新后重试")


def owner(db, row, context, authorize, *, admin_action, field="uploads"):
    if row.owner_principal_id != context.actor.principal_id:
        access(db, context, authorize, admin_action, None, field=field, write=True)


def finish(
    db,
    handle,
    *,
    part_id,
    identifier,
    before,
    after,
    changes,
    identity_revision=None,
    result_fields=None,
):
    event = api.record_audit(
        db,
        part_id,
        context=handle.context,
        action=handle.operation,
        changes=changes,
        before_revision=before,
        after_revision=after,
        authorization=handle.decision.metadata,
        payload_hash=handle.payload_hash,
        revisions={
            "axis": handle.resource_type,
            "before": {"resource": before},
            "after": {"resource": after, "identity": identity_revision},
        },
    )
    return api.finish_command(
        db,
        handle,
        part_id=part_id,
        applied_revision=after,
        audit_id=event.id,
        result={
            "resource": handle.resource_type,
            "id": str(identifier),
            "identity_revision": identity_revision,
            **(result_fields or {}),
        },
    )


def response(db, receipt, data, *, replayed):
    state = (
        db.get(PartState, receipt.part_id, populate_existing=True)
        if receipt.part_id
        else None
    )
    return {
        "data": data,
        "meta": api.receipt_meta(
            receipt,
            data_revision=data.get("revision", 0),
            identity_revision=state.identity_revision if state else None,
            replayed=replayed,
        ),
    }


def read_response(context, data):
    return {
        "data": data,
        "meta": {
            "request_id": str(context.request_id),
            "replayed": False,
            "data_revision": data.get("revision", 0),
        },
    }


def uuid(value):
    return value if isinstance(value, UUID) else UUID(value)

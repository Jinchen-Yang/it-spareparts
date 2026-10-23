"""Bounded JSON attributes share PN identity and transaction/audit boundaries."""
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from . import api
from .attribute_dto import PatchAttributes, validate_attributes
from .attribute_models import AttributeState, AttributeValue
from .errors import CatalogError
from .persistence import lock_part, read_versions, state_for_write
from .profile_status import advance_fill_status


def _read(db, part_id, context, authorize):
    decision = api._authorize(db, context, authorize, "read_attributes", part_id)
    if not api._allowed(decision.readable_fields, "attributes"):
        raise CatalogError("auth.forbidden", "没有读取自定义参数的权限", status_code=403)
    return decision


def _rows(db, part_id):
    return {row.key: row for row in db.scalars(select(AttributeValue).where(
        AttributeValue.part_id == part_id).order_by(AttributeValue.key)
        .execution_options(populate_existing=True))}


def _item(row):
    return {"value": row.value, "deleted": row.deleted, "version": row.version,
            "protected": row.protected, "provenance": row.provenance}


def get_attributes(db, part_id, *, context, authorize):
    _read(db, part_id, context, authorize)
    from app.models.dimensions import DimPart
    from .models import PartState
    # One statement supplies identity, aggregate revision and every item from the
    # same READ COMMITTED snapshot, including in a READ ONLY transaction.
    rows = db.execute(select(DimPart.id, PartState.identity_revision, AttributeState.revision, AttributeValue)
        .outerjoin(PartState, PartState.part_id == DimPart.id)
        .outerjoin(AttributeState, AttributeState.part_id == DimPart.id)
        .outerjoin(AttributeValue, AttributeValue.part_id == DimPart.id)
        .where(DimPart.id == part_id).order_by(AttributeValue.key)
        .execution_options(populate_existing=True)).all()
    if not rows:
        raise CatalogError("catalog.part_not_found", "型号不存在", status_code=404)
    identity, revision = rows[0][1] or 0, rows[0][2] or 0
    return {"data": {"part_id": part_id, "identity_revision": identity, "revision": revision,
                     "items": {row.key: _item(row) for _, _, _, row in rows if row is not None}},
            "meta": {"request_id": str(context.request_id), "resource_revision": revision,
                     "data_revision": revision, "identity_revision": identity, "replayed": False}}



def patch_attributes(db, part_id, command: PatchAttributes, *, context, authorize):
    if context.actor.actor_kind != "human" or context.actor.provenance.kind != "manual":
        raise CatalogError("catalog.human_confirmation_required", "自定义参数需要实际操作人确认", status_code=403)
    handle = api.begin_command(db, context=context, authorize=authorize, operation="update_attributes",
        part_id=part_id, resource_type="attributes", resource_id=part_id, payload=command)
    _read(db, part_id, context, authorize)
    api._check_fields(handle.decision, {"attributes"})
    if handle.receipt:
        return render_receipt(db, handle.receipt, context=context, authorize=authorize, replayed=True)
    part = lock_part(db, part_id)
    identity = read_versions(db, part_id)["identity_revision"]
    if part.status != "active" or identity != command.expected_identity_revision:
        raise CatalogError("catalog.identity_changed", "型号身份已变化，请重新确认目标")
    rows = _rows(db, part_id)
    conflicts = [key for key in command.changes
                 if (rows[key].version if key in rows else 0) != command.field_versions[key]]
    if conflicts:
        raise CatalogError("catalog.field_conflict", "自定义参数已变化，请核对草稿",
            details={"fields": conflicts, "resolution": "reload_and_confirm"})
    values = {key: row.value if not row.deleted else None for key, row in rows.items()}
    for key, change in command.changes.items():
        values[key] = change.value if change.operation == "set" else None
    try:
        validate_attributes(values)
    except (ValueError, UnicodeError):
        raise CatalogError("catalog.attribute_limit", "自定义参数超过键数、深度或容量限制", status_code=422) from None
    db.execute(insert(AttributeState).values(part_id=part_id).on_conflict_do_nothing())
    state = db.get(AttributeState, part_id, populate_existing=True)
    before_revision = state.revision
    provenance = {**context.actor.provenance.model_dump(mode="json", exclude_none=True),
                  "principal_id": context.actor.principal_id, "request_id": str(context.request_id),
                  "confirmed_by": context.actor.principal_id}
    changes = {}
    for key, change in command.changes.items():
        row = rows.get(key)
        before = _item(row) if row else None
        if row is None:
            row = AttributeValue(part_id=part_id, key=key, version=0)
            db.add(row)
        row.version += 1
        row.value = change.value if change.operation == "set" else None
        row.deleted = change.operation == "delete"
        row.protected, row.provenance = True, dict(provenance)
        changes[key] = {"before": before, "after": _item(row)}
    state.revision += 1
    profile_state = state_for_write(db, part_id)
    previous_fill_revision = profile_state.fill_status_revision
    status_change = advance_fill_status(profile_state, "confirmed", context=context)
    audit_changes = {"attributes": changes}
    if status_change:
        audit_changes["fill_status"] = status_change
    event = api.record_audit(db, part_id, context=context, action="update_attributes",
        changes=audit_changes, before_revision=before_revision, after_revision=state.revision,
        authorization=handle.decision.metadata, payload_hash=handle.payload_hash,
        revisions={"axis": "attributes", "before": {"attributes": before_revision, "identity": identity, "fill_status": previous_fill_revision},
                   "after": {"attributes": state.revision, "identity": identity, "fill_status": profile_state.fill_status_revision}})
    receipt = api.finish_command(db, handle, part_id=part_id, applied_revision=state.revision,
        audit_id=event.id, result={"resource": "attributes", "part_id": part_id, "identity_revision": identity})
    return render_receipt(db, receipt, context=context, authorize=authorize, replayed=False)


def render_receipt(db, receipt, *, context, authorize, replayed):
    api._authorize(db, context, authorize, receipt.operation, receipt.part_id)
    current = get_attributes(db, receipt.part_id, context=context, authorize=authorize)
    current["meta"] = api.receipt_meta(receipt, data_revision=current["data"]["revision"],
        identity_revision=current["data"]["identity_revision"], replayed=replayed)
    return current

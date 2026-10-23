"""Transaction protocol shared by public commands and transitional in-process writers."""
import hashlib
import re
from collections.abc import Iterable
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.dimensions import DimPart, PartAlias
from app.models.master_data import ProductSpec
from .dto import ActorContext, CommandContext, PROFILE_FIELDS, Provenance
from .errors import CatalogError
from .models import AuditEvent, PartState


def lock_part(db: Session, part_id: int, *, shared: bool = False) -> DimPart:
    with db.no_autoflush:
        part = db.scalar(select(DimPart).where(DimPart.id == part_id)
                         .with_for_update(read=shared).execution_options(populate_existing=True))
    if part is None:
        raise CatalogError("catalog.part_not_found", "型号不存在", status_code=404)
    return part


def state_for_write(db: Session, part_id: int) -> PartState:
    db.execute(pg_insert(PartState).values(part_id=part_id).on_conflict_do_nothing())
    return db.scalar(select(PartState).where(PartState.part_id == part_id)
                     .execution_options(populate_existing=True))


def _spec_snapshot(rows) -> dict:
    return {s.spec_key: {"value": s.spec_value, "unit": s.spec_unit,
            "numeric_value": format(s.numeric_value.normalize(), "f") if s.numeric_value is not None else None,
            "source": s.source} for s in rows}


_UNLOADED = object()


def acquire_identity_lock(db: Session, *, shared: bool = False) -> None:
    """Stabilize alias/merge topology before locking sorted PN parents."""
    function = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
    db.execute(text(f"SELECT {function}(742031, 2)"))


def acquire_creation_locks(db: Session, pns: Iterable[str], *, ingest: bool = False) -> None:
    """Coordinate manual duplicate checks against concurrent source imports.

    Call after the topology lock and before any parent lock. Imports preserve their
    source-authoritative semantics and share the gate; manual creation checks need
    the exclusive gate because fuzzy candidates can have different compact keys.
    These transaction locks never serialize ordinary profile edits.
    """
    function = "pg_advisory_xact_lock_shared" if ingest else "pg_advisory_xact_lock"
    db.execute(text(f"SELECT {function}(742031, 3)"))
    compact_keys = {re.sub(r"[^A-Za-z0-9]", "", pn).upper() or pn.strip().upper() for pn in pns}
    for compact in sorted(compact_keys):
        key = int.from_bytes(hashlib.sha256(f"pn-catalog-create:{compact}".encode()).digest()[:8], "big", signed=True)
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def _alias_snapshot(row: PartAlias) -> dict:
    return {k: getattr(row, k) for k in ("id", "pn_raw", "pn_std", "status", "needs_review", "source", "part_id")}


def snapshot(db: Session, part: DimPart, state=_UNLOADED,
             specs: dict | None = None, aliases: list | None = None) -> dict:
    state = db.get(PartState, part.id, populate_existing=True) if state is _UNLOADED else state
    if specs is None:
        specs = _spec_snapshot(db.scalars(select(ProductSpec).where(ProductSpec.part_id == part.id).execution_options(populate_existing=True)).all())
    if aliases is None:
        aliases = [_alias_snapshot(row) for row in db.scalars(select(PartAlias).where(
            PartAlias.part_id == part.id).order_by(PartAlias.id).execution_options(populate_existing=True)).all()]
    versions = dict(state.field_versions) if state else {}
    for name in PROFILE_FIELDS:
        versions.setdefault(name, 0)
    deleted = list(state.deleted_specs) if state else []
    for key in specs.keys() | set(deleted):
        versions.setdefault(f"specs.{key}", 0)
    return {
        "part_id": part.id, "pn_std": part.pn_std,
        "identity": {"pn_std": part.pn_std, "status": part.status, "merged_into_id": part.merged_into_id},
        "aliases": aliases,
        "identity_revision": state.identity_revision if state else 0,
        "profile_revision": state.profile_revision if state else 0,
        "availability": state.availability if state else "active",
        "availability_revision": state.availability_revision if state else 0,
        "profile": {k: (state.manufacturer if state else None) if k == "manufacturer" else getattr(part, k)
                    for k in sorted(PROFILE_FIELDS)},
        "field_versions": versions, "specs": specs, "deleted_specs": deleted,
        "field_provenance": dict(state.field_provenance) if state else {},
        "locked_fields": list(part.locked_fields or []),
    }


def capture_states(db: Session, part_ids: Iterable[int]) -> dict[int, dict]:
    """Capture once, with sorted parent locks; never call after mutating ORM objects."""
    ids = sorted(set(part_ids))
    if not ids:
        return {}
    with db.no_autoflush:
        parts = db.scalars(select(DimPart).where(DimPart.id.in_(ids)).order_by(DimPart.id)
                           .with_for_update().execution_options(populate_existing=True)).all()
    return snapshots_for_parts(db, parts)


def read_states(db: Session, part_ids: Iterable[int]) -> dict[int, dict]:
    """Bounded consistent batch read; retain ordered SHARE parent locks until commit."""
    ids = sorted(set(part_ids))
    if not ids:
        return {}
    with db.no_autoflush:
        parts = db.scalars(select(DimPart).where(DimPart.id.in_(ids)).order_by(DimPart.id)
                           .with_for_update(read=True).execution_options(populate_existing=True)).all()
    return snapshots_for_parts(db, parts)


def snapshots_for_parts(db: Session, parts: list[DimPart]) -> dict[int, dict]:
    ids = [part.id for part in parts]
    if not ids:
        return {}
    states = {s.part_id: s for s in db.scalars(select(PartState).where(PartState.part_id.in_(ids))
                                             .execution_options(populate_existing=True)).all()}
    specs: dict[int, dict] = {}
    for s in db.scalars(select(ProductSpec).where(ProductSpec.part_id.in_(ids)).execution_options(populate_existing=True)).all():
        specs.setdefault(s.part_id, {}).update(_spec_snapshot([s]))
    aliases: dict[int, list] = {}
    for row in db.scalars(select(PartAlias).where(PartAlias.part_id.in_(ids)).order_by(PartAlias.id).execution_options(populate_existing=True)).all():
        aliases.setdefault(row.part_id, []).append(_alias_snapshot(row))
    return {p.id: snapshot(db, p, states.get(p.id), specs.get(p.id, {}), aliases.get(p.id, [])) for p in parts}


def capture_state(db: Session, part_id: int) -> dict | None:
    return capture_states(db, [part_id]).get(part_id)


def blocked_spec_keys(db: Session, part_ids: Iterable[int]) -> dict[int, set[str]]:
    ids = list(set(part_ids))
    if not ids:
        return {}
    return {pid: set(keys) for pid, keys in db.execute(select(PartState.part_id, PartState.deleted_specs)
                                                      .where(PartState.part_id.in_(ids))).all()}


def legacy_context(*, operated_by: str | None = None, principal_id: str | None = None,
                   source: str = "manual", entrypoint: str = "itsp_ui",
                   evidence_id: str | None = None) -> CommandContext:
    """Unknown historical identity is explicitly unknown, never forged from display name."""
    return CommandContext(request_id=uuid4(), actor=ActorContext(
        principal_id=principal_id or "legacy:unknown",
        actor_kind="human" if principal_id else "unknown", client_id="itsp",
        executor_id="itsp-legacy", entrypoint=entrypoint, display_name=operated_by,
        issuer=None if principal_id else "legacy-itsp", subject=None,
        provenance=Provenance(kind=source, evidence_id=evidence_id)))


def append_audit(db: Session, part_id: int | None, *, context: CommandContext, action: str,
                 changes: dict, before_revision: int, after_revision: int,
                 authorization: dict | None = None, payload_hash: str | None = None,
                 revisions: dict | None = None) -> AuditEvent:
    event = AuditEvent(part_id=part_id, action=action, principal_id=context.actor.principal_id,
        client_id=context.actor.client_id, request_id=context.request_id,
        actor=context.actor.model_dump(mode="json"),
        context=context.model_dump(mode="json", exclude={"actor", "request_id"}),
        authorization=authorization or {}, payload_hash=payload_hash,
        before_revision=before_revision, after_revision=after_revision, changes=changes,
        revisions=revisions or {"axis": "profile", "before": {"profile": before_revision}, "after": {"profile": after_revision}})
    db.add(event)
    db.flush()
    return event


def _change_state(state: PartState, before: dict | None, after: dict, *,
                  context: CommandContext, deleted_spec_keys=(), restored_spec_keys=(), force_fields=(), force_identity=False) -> tuple[dict, int]:
    old_deleted = set(before["deleted_specs"]) if before else set()
    deleted = (set(state.deleted_specs) | set(deleted_spec_keys)) - set(restored_spec_keys)
    state.deleted_specs = sorted(deleted)
    changes = {}
    changed = set(force_fields)
    for key in PROFILE_FIELDS:
        old = before["profile"].get(key) if before else None
        if old != after["profile"].get(key):
            changed.add(key)
    old_locked = set(before["locked_fields"]) if before else set()
    changed.update((set(after["locked_fields"]) ^ old_locked) & PROFILE_FIELDS)
    old_specs = before["specs"] if before else {}
    for key in old_specs.keys() | after["specs"].keys() | old_deleted | deleted:
        if old_specs.get(key) != after["specs"].get(key) or (key in old_deleted) != (key in deleted):
            changed.add(f"specs.{key}")
    if {"category_major", "category_minor"} & changed:
        changed.update({"category_major", "category_minor"})
    old_revision = state.profile_revision
    versions = dict(state.field_versions)
    provenance = dict(state.field_provenance)
    for path in sorted(changed):
        versions[path] = versions.get(path, 0) + 1
        if path.startswith("specs."):
            key = path.removeprefix("specs.")
            old_value, new_value = old_specs.get(key), after["specs"].get(key)
        else:
            old_value = before["profile"].get(path) if before else None
            new_value = after["profile"].get(path)
        changes[path] = {"before": old_value, "after": new_value,
                         "deleted": path.startswith("specs.") and path[6:] in deleted}
        provenance[path] = {**context.actor.provenance.model_dump(mode="json", exclude_none=True),
                            "request_id": str(context.request_id),
                            "principal_id": context.actor.principal_id,
                            "confirmed_by": context.confirmed_by,
                            "adopted_from_candidate_id": context.adopted_from_candidate_id}
    if changed:
        state.profile_revision += 1
        state.field_versions = versions
        state.field_provenance = provenance
    if before is None or before["identity"] != after["identity"]:
        changes["identity"] = {"before": before["identity"] if before else None, "after": after["identity"]}
    if (before["aliases"] if before else []) != after["aliases"]:
        changes["aliases"] = {"before": before["aliases"] if before else [], "after": after["aliases"]}
    if force_identity:
        changes["identity_group"] = {"before": state.identity_revision, "after": state.identity_revision + 1}
    if "identity" in changes or "aliases" in changes or force_identity:
        state.identity_revision += 1
    return changes, old_revision


def _state_snapshot(after: dict, state: PartState) -> dict:
    return {**after, "profile_revision": state.profile_revision,
            "identity_revision": state.identity_revision,
            "field_versions": {**after["field_versions"], **state.field_versions},
            "field_provenance": dict(state.field_provenance), "deleted_specs": list(state.deleted_specs)}


def revision_vector(before: dict | None, state: PartState, *, axis: str | None = None) -> dict:
    previous = {key: before[f"{key}_revision"] if before else 0 for key in ("identity", "profile", "availability")}
    current = {key: getattr(state, f"{key}_revision") for key in previous}
    if axis is None:
        axis = "profile" if current["profile"] != previous["profile"] else "identity"
    return {"axis": axis, "before": previous, "after": current}


def record_legacy_change(db: Session, part_id: int, before: dict | None, *,
                         context: CommandContext, action: str,
                         deleted_spec_keys: Iterable[str] = (),
                         restored_spec_keys: Iterable[str] = (),
                         force_fields: Iterable[str] = (),
                         force_identity: bool = False,
                         authorization: dict | None = None,
                         payload_hash: str | None = None) -> dict:
    """Record a host-authorized mutation in its caller transaction, without committing.

    Caller captured/locked before mutation (or owns a newly created parent). This
    adapter is not an externally accessible alternative authorization path.
    """
    db.flush()
    part = db.get(DimPart, part_id)
    if part is None:
        raise CatalogError("catalog.part_not_found", "型号不存在", status_code=404)
    state = state_for_write(db, part_id)
    after = snapshot(db, part, state)
    changes, _ = _change_state(state, before, after, context=context,
        deleted_spec_keys=deleted_spec_keys, restored_spec_keys=restored_spec_keys, force_fields=force_fields, force_identity=force_identity)
    if not changes:
        return {"snapshot": after, "audit_id": None}
    revisions = revision_vector(before, state)
    event = append_audit(db, part_id, context=context, action=action, changes=changes,
                         before_revision=revisions["before"][revisions["axis"]],
                         after_revision=revisions["after"][revisions["axis"]],
                         authorization=authorization, payload_hash=payload_hash, revisions=revisions)
    return {"snapshot": _state_snapshot(after, state), "audit_id": event.id}


def record_legacy_changes(db: Session, before_by_id: dict[int, dict], *,
                          context: CommandContext, action: str,
                          part_ids: Iterable[int] | None = None,
                          force_identity_ids: Iterable[int] = (),
                          authorization: dict | None = None) -> dict[int, dict]:
    """Batch snapshots and persistence; no per-PN SELECT/flush loop for full refresh."""
    ids = sorted(set(part_ids if part_ids is not None else before_by_id))
    db.flush()
    after_by_id = capture_states(db, ids)
    keys = ("profile", "identity", "aliases", "specs", "locked_fields", "deleted_specs")
    force_ids = set(force_identity_ids)
    changed_ids = [pid for pid, after in after_by_id.items() if pid in force_ids or pid not in before_by_id or
                   any(before_by_id[pid][key] != after[key] for key in keys)]
    if not changed_ids:
        return {}
    for offset in range(0, len(changed_ids), 1000):
        db.execute(pg_insert(PartState).values([{"part_id": pid} for pid in changed_ids[offset:offset + 1000]])
                   .on_conflict_do_nothing())
    states = {row.part_id: row for row in db.scalars(select(PartState).where(
        PartState.part_id.in_(changed_ids)).execution_options(populate_existing=True)).all()}
    events = {}
    for pid in changed_ids:
        changes, _ = _change_state(states[pid], before_by_id.get(pid), after_by_id[pid], context=context, force_identity=pid in force_ids)
        if not changes:
            continue
        revisions = revision_vector(before_by_id.get(pid), states[pid])
        event = AuditEvent(part_id=pid, action=action, principal_id=context.actor.principal_id,
            client_id=context.actor.client_id, request_id=context.request_id,
            actor=context.actor.model_dump(mode="json"),
            context=context.model_dump(mode="json", exclude={"actor", "request_id"}),
            authorization=authorization or {}, payload_hash=None, before_revision=revisions["before"][revisions["axis"]],
            after_revision=revisions["after"][revisions["axis"]], changes=changes, revisions=revisions)
        db.add(event)
        events[pid] = event
    db.flush()
    return {pid: {"snapshot": _state_snapshot(after_by_id[pid], states[pid]), "audit_id": event.id}
            for pid, event in events.items()}


def read_versions(db: Session, part_id: int) -> dict:
    """SELECT-only compatibility baseline, also valid in a READ ONLY transaction."""
    state = db.get(PartState, part_id, populate_existing=True)
    versions = {key: 0 for key in PROFILE_FIELDS}
    if state:
        versions.update(state.field_versions)
    return {"identity_revision": state.identity_revision if state else 0,
            "profile_revision": state.profile_revision if state else 0,
            "field_versions": versions,
            "deleted_specs": list(state.deleted_specs) if state else []}

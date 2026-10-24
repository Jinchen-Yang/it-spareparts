"""Bounded state conversion after complete frozen-source conservation checks."""

from types import SimpleNamespace
from uuid import UUID

from sqlalchemy import select

from . import api
from .errors import CatalogError
from .migration_dto import fingerprint
from .migration_materialize import _arrange_imported, _content, _image
from .migration_models import LegacySourceRecord, MigrationBatch
from .media_models import GalleryEntry
from .migration_profile import materialize_profile
from .migration_history import HISTORY_KINDS, materialize_history
from .migration_media import (
    materialize_upload,
    materialize_multipart,
    materialize_upload_intent,
    materialize_inbox,
)
from .migration_state import _access, _namespace_lock, snapshot_report
from .migration_state_dto import ApplySourceRecords
from .migration_state_models import MigrationRecordApplication, MigrationSnapshotState
from .models import PartState
from .persistence import acquire_identity_lock, lock_part
from .references import (
    bind_migration_reference,
    effective_reference,
    lock_reference_batch,
)


def _write_application(
    db, row, snapshot_id, state, reason, target, *, context, authorization
):
    application = db.get(MigrationRecordApplication, row.id)
    previous = application.revision if application else 0
    event = api.record_audit(
        db,
        row.target_part_id,
        context=context,
        action="migration.apply_record",
        changes={
            "legacy_evidence": {
                "source_record_id": row.id,
                "source_sha256": row.payload_sha256,
                "before": application.state if application else None,
                "after": state,
                "reason_code": reason,
                "target_ref": target,
            }
        },
        before_revision=previous,
        after_revision=previous + 1,
        authorization=authorization,
        payload_hash=row.payload_sha256,
        revisions={
            "axis": "migration_application",
            "before": {"application": previous},
            "after": {"application": previous + 1},
        },
    )
    if application is None:
        application = MigrationRecordApplication(
            source_record_id=row.id,
            namespace=row.namespace,
            snapshot_id=snapshot_id,
            source_sha256=row.payload_sha256,
            revision=0,
        )
        db.add(application)
    application.state, application.reason_code, application.target_ref = (
        state,
        reason,
        target,
    )
    application.revision, application.audit_id = previous + 1, event.id
    return application


def _resolved_rows(db, rows, *, context, authorize):
    refs = {
        (row.namespace, row.source_pn_id)
        for row in rows
        if row.source_pn_id is not None
    }
    lock_reference_batch(db, refs)
    acquire_identity_lock(db, shared=True)
    resolved, blocked = {}, {}
    for row in rows:
        if row.source_pn_id is None:
            continue
        reference = effective_reference(db, row.namespace, row.source_pn_id)
        if reference:
            resolved[row.id] = (reference.part_id, reference.identity_revision)
        elif row.target_part_id is not None:
            resolved[row.id] = (
                row.target_part_id,
                row.mapping["expected_identity_revision"],
            )
        else:
            blocked[row.id] = "migration.reference_required"
    parents = {}
    for pid in sorted({target[0] for target in resolved.values()}):
        part = lock_part(db, pid)
        state = db.get(PartState, pid, populate_existing=True)
        parents[pid] = (part.status, state.identity_revision if state else 0)
    for row in rows:
        if row.id not in resolved:
            continue
        pid, baseline = resolved[row.id]
        if parents[pid] != ("active", baseline):
            blocked[row.id] = "catalog.identity_changed"
            continue
        if effective_reference(db, row.namespace, row.source_pn_id) is None:
            bind_migration_reference(
                db,
                namespace=row.namespace,
                source_id=row.source_pn_id,
                part_id=pid,
                expected_identity_revision=baseline,
                decision_ref=row.mapping["decision_ref"],
                context=context,
                authorize=authorize,
            )
    return resolved, blocked


def apply_source_records(
    db,
    namespace: str,
    snapshot_id: UUID,
    command: ApplySourceRecords,
    *,
    context,
    authorize,
):
    decision = _access(db, context, authorize, write=True)
    handle = api.begin_command(
        db,
        context=context,
        authorize=authorize,
        operation="materialize_migration",
        part_id=None,
        resource_type="migration_snapshot",
        resource_id=f"{namespace}:{snapshot_id}",
        payload=command,
    )
    _namespace_lock(db, namespace)
    registered = db.get(
        MigrationSnapshotState, (namespace, snapshot_id), populate_existing=True
    )
    if registered is None:
        raise CatalogError(
            "migration.snapshot_not_found", "快照尚未完成全量守恒核对", status_code=404
        )
    if handle.receipt:
        return snapshot_report(
            db,
            namespace,
            snapshot_id,
            context=context,
            authorize=authorize,
            replayed=True,
        )
    if registered.revision != command.expected_snapshot_revision:
        raise CatalogError("migration.snapshot_conflict", "冻结快照版本不同")
    rows = list(
        db.scalars(
            select(LegacySourceRecord)
            .join(MigrationBatch, MigrationBatch.id == LegacySourceRecord.batch_id)
            .where(
                LegacySourceRecord.namespace == namespace,
                MigrationBatch.snapshot_id == snapshot_id,
                LegacySourceRecord.id.in_(command.record_ids),
            )
            .order_by(LegacySourceRecord.id)
        )
    )
    if {row.id for row in rows} != set(command.record_ids):
        raise CatalogError(
            "migration.record_not_found", "所选记录不属于当前冻结快照", status_code=404
        )
    resolved, blocked = _resolved_rows(db, rows, context=context, authorize=authorize)
    last_audit = registered.audit_id
    for row in rows:
        if fingerprint(row.payload) != row.payload_sha256:
            raise CatalogError("migration.source_changed", "原始证据与冻结指纹不一致")
        prior = db.get(MigrationRecordApplication, row.id, populate_existing=True)
        if prior and prior.state in {
            "applied",
            "historical_terminal",
            "recovery_required",
        }:
            last_audit = prior.audit_id
            continue
        target, state, reason = None, "applied", None
        try:
            with db.begin_nested():
                if row.id in blocked:
                    raise CatalogError(blocked[row.id], "缺少有效的来源映射或身份基线")
                pid, identity = resolved.get(row.id, (None, None))
                if row.kind == "pn":
                    if pid is None:
                        raise CatalogError(
                            "migration.reference_required", "来源 PN 尚未绑定"
                        )
                    target = materialize_profile(
                        db, row, pid, context=context, authorization=decision.metadata
                    )
                elif row.kind == "upload":
                    state, reason, target = materialize_upload(db, row, pid, identity)
                elif row.kind == "multipart":
                    state, reason, target = materialize_multipart(db, row)
                elif row.kind == "upload_intent":
                    state, reason, target = materialize_upload_intent(db, row)
                elif row.kind == "inbox":
                    state, reason, target = materialize_inbox(db, row, pid, identity)
                elif row.kind in HISTORY_KINDS:
                    state, reason, target = materialize_history(db, row, pid)
                elif row.kind in {"image", "content"}:
                    # The immutable archive's initial mapping never changes. This
                    # temporary projection carries the separately approved reference.
                    view = SimpleNamespace(
                        **{
                            name: getattr(row, name)
                            for name in (
                                "id",
                                "namespace",
                                "source_id",
                                "source_pn_id",
                                "source_revision",
                                "payload",
                                "payload_sha256",
                                "batch_id",
                                "state",
                                "materialized_ref",
                            )
                        },
                        target_part_id=pid,
                        mapping={"expected_identity_revision": identity},
                    )
                    if row.materialized_ref:
                        target = row.materialized_ref
                        if row.kind == "image":
                            if pid is None:
                                raise CatalogError(
                                    "migration.reference_required", "图片 PN 尚未绑定"
                                )
                            entry = db.get(GalleryEntry, UUID(target["entry_id"]))
                            if (
                                entry is None
                                or entry.origin_namespace != row.namespace
                                or entry.source.get("legacy_record_id") != row.id
                            ):
                                raise CatalogError(
                                    "migration.record_conflict", "原图片来源关系不一致"
                                )
                            if entry.origin_part_id is None and entry.revision == 1:
                                entry.origin_part_id = pid
                                db.flush()
                                target = {
                                    **target,
                                    "gallery_revision": _arrange_imported(db, pid),
                                }
                            elif entry.origin_part_id != pid:
                                raise CatalogError(
                                    "migration.record_conflict",
                                    "图片已经归属于另一目标",
                                )
                    elif pid is None:
                        raise CatalogError(
                            "migration.reference_required", "来源 PN 尚未绑定"
                        )
                    elif row.kind == "content":
                        _content(db, view, context=context, authorize=authorize)
                        target = view.materialized_ref
                    else:
                        _image(db, view)
                        db.flush()
                        revision = _arrange_imported(db, pid)
                        target = {**view.materialized_ref, "gallery_revision": revision}
                    row.materialized_ref = target
                    row.state = "materialized"
                else:
                    raise CatalogError(
                        "migration.converter_pending", "该来源状态的生效转换尚未实现"
                    )
                db.flush()
        except CatalogError as exc:
            if exc.status_code in {401, 403, 503}:
                raise
            reason = exc.code
            state = (
                "dependency_blocked"
                if reason
                in {
                    "migration.reference_required",
                    "migration.converter_pending",
                    "migration.owner_binding_required",
                    "migration.dependency_missing",
                }
                else "conflict"
            )
        application = _write_application(
            db,
            row,
            snapshot_id,
            state,
            reason,
            target,
            context=context,
            authorization=decision.metadata,
        )
        db.flush()
        last_audit = application.audit_id
    api.finish_command(
        db,
        handle,
        part_id=None,
        applied_revision=registered.revision,
        audit_id=last_audit,
        result={
            "resource": "migration_snapshot",
            "namespace": namespace,
            "snapshot_id": str(snapshot_id),
        },
    )
    return snapshot_report(
        db, namespace, snapshot_id, context=context, authorize=authorize
    )

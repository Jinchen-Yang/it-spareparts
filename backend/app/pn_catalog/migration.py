"""Offline migration staging. Callers own transactions; this module never contacts a source or OSS."""
import hashlib
import hmac

from sqlalchemy import select, text, tuple_
from sqlalchemy.orm import Session

from .api import begin_command, finish_command, record_audit
from .dto import AuthorizationDecision, Authorize, CommandContext
from .errors import CatalogError
from .migration_dto import (
    MigrationManifest,
    VerifiedSourceSnapshot,
    canonical_bytes,
    source_key,
)
from .migration_models import LegacySourceRecord, MigrationBatch
from .models import PartState
from .persistence import acquire_identity_lock, lock_part


def _full_archive_access(db, context, authorize, action, *, writing=False):
    decision = authorize(db, context, action, None)
    if not isinstance(decision, AuthorizationDecision):
        raise CatalogError("auth.unavailable", "迁移权限暂不可用", status_code=503, retryable=True)
    # Raw historical payloads may contain every profile field. No partial grant can expose them.
    if not {"*", "legacy_evidence"} & decision.readable_fields or (
        writing and not {"*", "legacy_evidence"} & decision.writable_fields
    ):
        raise CatalogError("auth.forbidden", "历史迁移证据需要完整资料权限", status_code=403)
    if decision.metadata.get("scope", decision.metadata.get("scope_kind")) != "all":
        raise CatalogError("auth.forbidden", "历史迁移证据需要全范围权限", status_code=403)
    return decision


def _verify_source(manifest: MigrationManifest, source: VerifiedSourceSnapshot) -> None:
    expected = {source_key(row): row.payload_sha256 for row in manifest.records}
    if (source.namespace != manifest.namespace or source.snapshot_id != manifest.snapshot_id
            or not hmac.compare_digest(source.manifest_sha256, manifest.sha256)
            or source.record_hashes != expected):
        raise CatalogError("migration.source_changed", "源快照与冻结清单不一致，请重新核对")


def stage_manifest(db: Session, manifest: MigrationManifest, *, verified_source: VerifiedSourceSnapshot,
                   context: CommandContext, authorize: Authorize) -> dict:
    """Archive all states atomically; archive success is explicitly not materialization success.

    Source verification must run against an isolated frozen copy before this call.
    A live cross-database transaction cannot be implied by these local locks.
    """
    _full_archive_access(db, context, authorize, "stage_migration", writing=True)
    if context.actor.entrypoint != "migration" or context.actor.provenance.kind != "migration":
        raise CatalogError("auth.forbidden", "迁移只能由已授权的迁移入口执行", status_code=403)
    _verify_source(manifest, verified_source)
    handle = begin_command(db, context=context, authorize=authorize, operation="stage_migration",
        part_id=None, resource_type="migration", resource_id=manifest.batch_id,
        payload={"manifest_sha256": manifest.sha256, "writer_fence_ref": verified_source.writer_fence_ref})
    if handle.receipt is not None:
        return migration_receipt(db, handle.receipt, context=context, authorize=authorize)
    acquire_identity_lock(db, shared=True)
    by_source = {item.source_pn_id: item for item in manifest.mappings}
    baselines: dict[int, int] = {}
    for item in manifest.mappings:
        if item.target_part_id is not None:
            if item.target_part_id in baselines and baselines[item.target_part_id] != item.expected_identity_revision:
                raise CatalogError("migration.mapping_conflict", "同一目标的身份基线不一致")
            baselines[item.target_part_id] = item.expected_identity_revision
    for part_id, expected in sorted(baselines.items()):
        part = lock_part(db, part_id, shared=True)
        state = db.get(PartState, part_id, populate_existing=True)
        revision = state.identity_revision if state else 0
        if part.status != "active" or revision != expected:
            raise CatalogError("catalog.identity_changed", "迁移目标身份已变化，请重新核对映射")
    # Migration-only serialization; ordinary PN commands never take this lock.
    key = int.from_bytes(hashlib.sha256(f"pn-migration:{manifest.namespace}".encode()).digest()[:8], "big", signed=True)
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
    batch = db.get(MigrationBatch, manifest.batch_id, populate_existing=True)
    if batch is not None:
        if batch.namespace != manifest.namespace or batch.manifest_sha256 != manifest.sha256:
            raise CatalogError("migration.batch_conflict", "批次编号对应另一份迁移清单")
        finish_command(db, handle, part_id=None, applied_revision=1, audit_id=batch.audit_id,
                       result={"resource": "migration", "batch_id": str(batch.id)})
        return _batch_result(batch, replayed=True)
    keys = [(row.kind, row.source_id) for row in manifest.records]
    existing = {(row.kind, row.source_id): row for row in db.scalars(select(LegacySourceRecord).where(
        LegacySourceRecord.namespace == manifest.namespace,
        tuple_(LegacySourceRecord.kind, LegacySourceRecord.source_id).in_(keys))).all()}
    # Validate everything before adding anything: no partial batch on a later conflict.
    for item in manifest.records:
        prior = existing.get((item.kind, item.source_id))
        mapping = by_source[item.source_pn_id].model_dump(mode="json") if item.source_pn_id else {}
        if prior is not None and (prior.payload_sha256 != item.payload_sha256
                or prior.source_revision != item.source_revision or prior.source_pn_id != item.source_pn_id
                or prior.mapping != mapping):
            raise CatalogError("migration.record_conflict", "来源记录或映射已变化，需显式增量裁决",
                details={"kind": item.kind, "source_id": item.source_id})
        if prior is not None:
            raise CatalogError("migration.record_already_staged", "该来源记录已属于另一冻结批次，请重放原批次",
                details={"kind": item.kind, "source_id": item.source_id, "batch_id": str(prior.batch_id)})
    batch = MigrationBatch(id=manifest.batch_id, namespace=manifest.namespace, snapshot_id=manifest.snapshot_id,
        manifest_sha256=manifest.sha256, source_schema=manifest.source_schema,
        writer_fence_ref=verified_source.writer_fence_ref, record_count=len(manifest.records),
        principal_id=context.actor.principal_id, status="archived")
    db.add(batch)
    db.flush()
    for item in manifest.records:
        if (item.kind, item.source_id) in existing:
            continue
        mapping = by_source.get(item.source_pn_id)
        state = mapping.disposition if mapping and mapping.disposition != "mapped" else "pending"
        db.add(LegacySourceRecord(namespace=manifest.namespace, kind=item.kind, source_id=item.source_id,
            source_pn_id=item.source_pn_id, source_revision=item.source_revision,
            payload_sha256=item.payload_sha256, payload=item.payload, batch_id=batch.id,
            target_part_id=mapping.target_part_id if mapping else None,
            mapping=mapping.model_dump(mode="json") if mapping else {}, state=state))
    event = record_audit(db, None, context=context, action="migration.archive",
        changes={"batch": {"id": str(batch.id), "manifest_sha256": manifest.sha256,
                           "record_count": len(manifest.records), "status": "archived"}},
        before_revision=0, after_revision=1, authorization=handle.decision.metadata,
        payload_hash=handle.payload_hash, revisions={"axis": "migration", "before": {"migration": 0}, "after": {"migration": 1}})
    batch.audit_id = event.id
    finish_command(db, handle, part_id=None, applied_revision=1, audit_id=event.id,
                   result={"resource": "migration", "batch_id": str(batch.id)})
    return _batch_result(batch, replayed=False)


def _batch_result(batch: MigrationBatch, *, replayed: bool) -> dict:
    return {"batch_id": str(batch.id), "namespace": batch.namespace, "status": batch.status,
            "manifest_sha256": batch.manifest_sha256, "record_count": batch.record_count,
            "audit_id": batch.audit_id, "replayed": replayed}


def migration_receipt(db, receipt, *, context, authorize):
    from uuid import UUID
    _full_archive_access(db, context, authorize, "read_legacy")
    batch = db.get(MigrationBatch, UUID(receipt.result["batch_id"]))
    if batch is None:
        raise CatalogError("migration.batch_not_found", "迁移批次不存在", status_code=404)
    return _batch_result(batch, replayed=True)


def list_legacy_records(db: Session, *, namespace: str, context: CommandContext, authorize: Authorize,
                        kind: str | None = None, after_id: int = 0, limit: int = 50) -> dict:
    _full_archive_access(db, context, authorize, "read_legacy")
    if not namespace or len(namespace) > 64 or not 1 <= limit <= 100 or after_id < 0:
        raise CatalogError("request.validation_failed", "历史记录分页参数无效", status_code=422)
    query = select(LegacySourceRecord).where(LegacySourceRecord.namespace == namespace,
                                            LegacySourceRecord.id > after_id)
    if kind is not None:
        query = query.where(LegacySourceRecord.kind == kind)
    rows = db.scalars(query.order_by(LegacySourceRecord.id).limit(limit + 1)).all()
    items, total_bytes = [], 0
    for row in rows[:limit]:
        value = {"id": row.id, "namespace": row.namespace, "kind": row.kind,
            "source_id": row.source_id, "source_pn_id": str(row.source_pn_id) if row.source_pn_id else None,
            "target_part_id": row.target_part_id, "state": row.state, "mapping": row.mapping,
            "payload": row.payload, "payload_sha256": row.payload_sha256, "read_only": True}
        size = len(canonical_bytes(value))
        if total_bytes + size > 768 * 1024:
            break
        items.append(value)
        total_bytes += size
    return {"items": items, "next_after_id": items[-1]["id"] if items and len(rows) > len(items) else None}

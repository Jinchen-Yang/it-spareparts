"""Offline snapshot conservation and explicit state conversion progress."""

import hashlib
from typing import get_args
from uuid import UUID

from sqlalchemy import func, select, text

from . import api
from .errors import CatalogError
from .migration import _full_archive_access
from .migration_dto import RecordKind, canonical_bytes, fingerprint
from .migration_models import LegacySourceRecord, MigrationBatch
from .migration_state_dto import SnapshotRegistration
from .migration_state_models import MigrationRecordApplication, MigrationSnapshotState


def _access(db, context, authorize, *, write=False):
    decision = _full_archive_access(
        db,
        context,
        authorize,
        "materialize_migration" if write else "read_legacy",
        writing=write,
    )
    if (
        context.actor.client_id != "itsp"
        or context.actor.entrypoint != "migration"
        or context.actor.provenance.kind != "migration"
    ):
        raise CatalogError(
            "auth.forbidden", "迁移状态仅限本机可信迁移入口", status_code=403
        )
    return decision


def _namespace_lock(db, namespace):
    # Same migration-only serialization as stage_manifest; no normal write uses it.
    value = int.from_bytes(
        hashlib.sha256(f"pn-migration:{namespace}".encode()).digest()[:8],
        "big",
        signed=True,
    )
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": value})


def archive_inventory(db, namespace: str, snapshot_id: UUID) -> dict:
    """Stream hash metadata only, never load all raw payloads into memory."""
    result = {}
    for kind in get_args(RecordKind):
        digest, count = hashlib.sha256(), 0
        digest.update(b"[")
        query = (
            select(LegacySourceRecord.source_id, LegacySourceRecord.payload_sha256)
            .join(MigrationBatch, MigrationBatch.id == LegacySourceRecord.batch_id)
            .where(
                LegacySourceRecord.namespace == namespace,
                MigrationBatch.snapshot_id == snapshot_id,
                LegacySourceRecord.kind == kind,
            )
            .order_by(LegacySourceRecord.source_id.collate("C"))
        )
        for source_id, source_hash in db.execute(
            query.execution_options(yield_per=256)
        ):
            if count:
                digest.update(b",")
            digest.update(canonical_bytes([source_id, source_hash]))
            count += 1
        digest.update(b"]")
        result[kind] = {"count": count, "records_sha256": digest.hexdigest()}
    return result


def register_snapshot(db, command: SnapshotRegistration, *, context, authorize):
    _access(db, context, authorize, write=True)
    handle = api.begin_command(
        db,
        context=context,
        authorize=authorize,
        operation="materialize_migration",
        part_id=None,
        resource_type="migration_snapshot",
        resource_id=f"{command.namespace}:{command.snapshot_id}",
        payload=command,
    )
    _namespace_lock(db, command.namespace)
    row = db.get(
        MigrationSnapshotState,
        (command.namespace, command.snapshot_id),
        populate_existing=True,
    )
    digest = fingerprint(command.model_dump(mode="json"))
    if row is not None:
        if row.registration_sha256 != digest:
            raise CatalogError(
                "migration.snapshot_conflict", "冻结快照登记与已有证据不同"
            )
        if handle.receipt is None:
            api.finish_command(
                db,
                handle,
                part_id=None,
                applied_revision=row.revision,
                audit_id=row.audit_id,
                result={
                    "resource": "migration_snapshot",
                    "namespace": row.namespace,
                    "snapshot_id": str(row.snapshot_id),
                },
            )
        return snapshot_report(
            db,
            row.namespace,
            row.snapshot_id,
            context=context,
            authorize=authorize,
            replayed=True,
        )
    expected = {
        item.kind: item.model_dump(exclude={"kind"}) for item in command.inventory
    }
    actual = archive_inventory(db, command.namespace, command.snapshot_id)
    mismatched = sorted(kind for kind in expected if expected[kind] != actual[kind])
    headers = list(
        db.execute(
            select(MigrationBatch.source_schema, MigrationBatch.writer_fence_ref).where(
                MigrationBatch.namespace == command.namespace,
                MigrationBatch.snapshot_id == command.snapshot_id,
            )
        ).all()
    )
    if (
        mismatched
        or not headers
        or any(
            schema != command.source_schema or fence != command.writer_fence_ref
            for schema, fence in headers
        )
    ):
        raise CatalogError(
            "migration.inventory_mismatch",
            "冻结来源数量、指纹或批次证据不一致",
            details={"kinds": mismatched},
        )
    event = api.record_audit(
        db,
        None,
        context=context,
        action="migration.snapshot_registered",
        changes={
            "namespace": command.namespace,
            "snapshot_id": str(command.snapshot_id),
            "inventory": expected,
            "verification_scope": command.verification_scope,
            "writer_fence_ref": command.writer_fence_ref,
        },
        before_revision=0,
        after_revision=1,
        authorization=handle.decision.metadata,
        payload_hash=handle.payload_hash,
        revisions={
            "axis": "migration_snapshot",
            "before": {"snapshot": 0},
            "after": {"snapshot": 1},
        },
    )
    row = MigrationSnapshotState(
        namespace=command.namespace,
        snapshot_id=command.snapshot_id,
        source_schema=command.source_schema,
        registration_sha256=digest,
        inventory=expected,
        fence_evidence=command.model_dump(
            mode="json",
            exclude={"namespace", "snapshot_id", "source_schema", "inventory"},
        ),
        revision=1,
        audit_id=event.id,
    )
    db.add(row)
    db.flush()
    api.finish_command(
        db,
        handle,
        part_id=None,
        applied_revision=1,
        audit_id=event.id,
        result={
            "resource": "migration_snapshot",
            "namespace": row.namespace,
            "snapshot_id": str(row.snapshot_id),
        },
    )
    return snapshot_report(
        db, row.namespace, row.snapshot_id, context=context, authorize=authorize
    )


def snapshot_report(
    db, namespace: str, snapshot_id: UUID, *, context, authorize, replayed=False
):
    _access(db, context, authorize)
    row = db.get(
        MigrationSnapshotState, (namespace, snapshot_id), populate_existing=True
    )
    if row is None:
        raise CatalogError(
            "migration.snapshot_not_found", "快照尚未完成核对登记", status_code=404
        )
    counts = dict(
        db.execute(
            select(MigrationRecordApplication.state, func.count())
            .where(
                MigrationRecordApplication.namespace == namespace,
                MigrationRecordApplication.snapshot_id == snapshot_id,
            )
            .group_by(MigrationRecordApplication.state)
        ).all()
    )
    total = sum(item["count"] for item in row.inventory.values())
    pending = total - sum(counts.values())
    blocked = pending + sum(
        counts.get(state, 0)
        for state in ("recovery_required", "dependency_blocked", "conflict")
    )
    return {
        "namespace": namespace,
        "snapshot_id": str(snapshot_id),
        "revision": row.revision,
        "verification_scope": row.fence_evidence["verification_scope"],
        "inventory": row.inventory,
        "states": counts,
        "pending": pending,
        "record_count": total,
        "materialization_complete": blocked == 0,
        "live_complete": blocked == 0,
        "cutover_ready": False,
        "replayed": replayed,
    }

"""Migration management is native ITSP only; source verification remains an offline tool."""

from typing import Annotated
from uuid import UUID
from fastapi import Depends, Path, Query, Request
from sqlalchemy import select
from app.authz.principal import current_verified_identity
from app.services.pn_catalog_access import resolve_migration_access, resolve_job_access
from . import api
from .authz import CatalogAccess
from .http import AccessDep, DbDep, _WRITE_HEADERS, _context, _commit, router
from .migration_history import list_history
from .migration_apply import apply_source_records
from .migration_models import LegacySourceRecord, MigrationBatch
from .migration_recovery import recover_record, render_receipt
from .migration_recovery_dto import RecoveryCommand
from .migration_state import _access, snapshot_report
from .migration_state_dto import ApplySourceRecords
from .migration_state_models import MigrationRecordApplication, MigrationSnapshotState
from .wire_migration import (
    LegacyHistoryResponse,
    MigrationRecordResponse,
    MigrationRecordsResponse,
    MigrationSnapshotsResponse,
    MigrationProgressResponse,
    MigrationEvidenceResponse,
    LegacyBundleDownloadResponse,
)


def native_migration(db: DbDep, identity=Depends(current_verified_identity)):
    return resolve_migration_access(db, identity)


MigrationAccess = Annotated[CatalogAccess, Depends(native_migration)]
Namespace = Annotated[
    str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
]
PRIVATE = {"x-itsp-only": True}
PRIVATE_WRITE = {**_WRITE_HEADERS, **PRIVATE}


@router.get(
    "/legacy-history/{record_id}/bundle-downloads",
    response_model=LegacyBundleDownloadResponse,
)
def historical_downloads(
    record_id: Annotated[int, Path(gt=0)],
    request: Request,
    db: DbDep,
    access: AccessDep,
):
    from .migration_artifacts import bundle_downloads
    from .errors import CatalogError

    storage = getattr(request.app.state, "pn_media_storage", None)
    if storage is None:
        raise CatalogError("media.not_configured", "图片存储尚未配置", status_code=503)
    return bundle_downloads(
        db,
        record_id,
        context=_context(request, access),
        authorize=access.authorize,
        storage=storage,
    )


@router.get(
    "/migration/records/{record_id}/evidence",
    response_model=MigrationEvidenceResponse,
    openapi_extra=PRIVATE,
)
def evidence(
    record_id: Annotated[int, Path(gt=0)],
    request: Request,
    db: DbDep,
    access: MigrationAccess,
):
    context = _context(request, access)
    _access(db, context, access.authorize)
    row = db.get(LegacySourceRecord, record_id)
    from .errors import CatalogError

    if row is None:
        raise CatalogError(
            "migration.record_not_found", "来源记录不存在", status_code=404
        )
    batch = db.get(MigrationBatch, row.batch_id)
    snapshot = db.get(MigrationSnapshotState, (row.namespace, batch.snapshot_id))
    return {
        "data": {
            "source_record_id": row.id,
            "source_sha256": row.payload_sha256,
            "payload": row.payload,
            "frozen_mapping": row.mapping,
            "fence_evidence": snapshot.fence_evidence if snapshot else {},
        },
        "meta": {"request_id": str(context.request_id), "replayed": False},
    }


@router.get("/legacy-history", response_model=LegacyHistoryResponse)
def history(
    request: Request,
    db: DbDep,
    access: AccessDep,
    part_id: Annotated[int | None, Query(gt=0)] = None,
    inbox_id: UUID | None = None,
    cursor: Annotated[str | None, Query(max_length=4096)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
):
    return list_history(
        db,
        context=_context(request, access),
        authorize=access.authorize,
        part_id=part_id,
        inbox_id=inbox_id,
        cursor=cursor,
        limit=limit,
    )


@router.get(
    "/migration/snapshots",
    response_model=MigrationSnapshotsResponse,
    openapi_extra=PRIVATE,
)
def snapshots(
    request: Request,
    db: DbDep,
    access: MigrationAccess,
    before: Annotated[str | None, Query(max_length=256)] = None,
):
    context = _context(request, access)
    _access(db, context, access.authorize)
    stmt = (
        select(MigrationSnapshotState)
        .order_by(
            MigrationSnapshotState.created_at.desc(),
            MigrationSnapshotState.snapshot_id.desc(),
            MigrationSnapshotState.namespace.desc(),
        )
        .limit(51)
    )
    if before:
        # Fixed keyset cursor contains both tie-break components, never a source payload.
        import base64
        import json
        from datetime import datetime
        from sqlalchemy import tuple_
        from .errors import CatalogError

        try:
            stamp, identifier, source_namespace = json.loads(
                base64.urlsafe_b64decode(before + "=" * (-len(before) % 4))
            )
            stmt = stmt.where(
                tuple_(
                    MigrationSnapshotState.created_at,
                    MigrationSnapshotState.snapshot_id,
                    MigrationSnapshotState.namespace,
                )
                < (datetime.fromisoformat(stamp), UUID(identifier), source_namespace)
            )
        except (ValueError, TypeError) as exc:
            raise CatalogError(
                "request.invalid_cursor", "迁移分页游标无效", status_code=422
            ) from exc
    rows = list(db.scalars(stmt))
    cursor = None
    if len(rows) > 50:
        import base64
        import json

        last = rows[49]
        cursor = (
            base64.urlsafe_b64encode(
                json.dumps(
                    [last.created_at.isoformat(), str(last.snapshot_id), last.namespace]
                ).encode()
            )
            .decode()
            .rstrip("=")
        )
    return {
        "data": [
            {
                "namespace": r.namespace,
                "snapshot_id": str(r.snapshot_id),
                "source_schema": r.source_schema,
                "revision": r.revision,
                "verification_scope": r.fence_evidence["verification_scope"],
                "created_at": r.created_at.isoformat(),
            }
            for r in rows[:50]
        ],
        "meta": {
            "request_id": str(context.request_id),
            "replayed": False,
            "next_cursor": cursor,
        },
    }


@router.get(
    "/migration/snapshots/{namespace}/{snapshot_id}",
    response_model=MigrationProgressResponse,
    openapi_extra=PRIVATE,
)
def progress(
    namespace: Namespace,
    snapshot_id: UUID,
    request: Request,
    db: DbDep,
    access: MigrationAccess,
):
    context = _context(request, access)
    return {
        "data": snapshot_report(
            db, namespace, snapshot_id, context=context, authorize=access.authorize
        ),
        "meta": {"request_id": str(context.request_id), "replayed": False},
    }


@router.get(
    "/migration/snapshots/{namespace}/{snapshot_id}/records",
    response_model=MigrationRecordsResponse,
    openapi_extra=PRIVATE,
)
def records(
    namespace: Namespace,
    snapshot_id: UUID,
    request: Request,
    db: DbDep,
    access: MigrationAccess,
    after: Annotated[int, Query(ge=0)] = 0,
    state: Annotated[str | None, Query(max_length=32)] = None,
):
    context = _context(request, access)
    _access(db, context, access.authorize)
    stmt = (
        select(
            LegacySourceRecord.id,
            LegacySourceRecord.kind,
            LegacySourceRecord.source_id,
            LegacySourceRecord.payload_sha256,
            MigrationRecordApplication.state,
            MigrationRecordApplication.revision,
            MigrationRecordApplication.reason_code,
            MigrationRecordApplication.target_ref,
        )
        .join(MigrationBatch, MigrationBatch.id == LegacySourceRecord.batch_id)
        .outerjoin(
            MigrationRecordApplication,
            MigrationRecordApplication.source_record_id == LegacySourceRecord.id,
        )
        .where(
            LegacySourceRecord.namespace == namespace,
            MigrationBatch.snapshot_id == snapshot_id,
            LegacySourceRecord.id > after,
        )
    )
    if state == "pending":
        stmt = stmt.where(MigrationRecordApplication.source_record_id.is_(None))
    elif state:
        stmt = stmt.where(MigrationRecordApplication.state == state)
    rows = db.execute(stmt.order_by(LegacySourceRecord.id).limit(51)).all()
    return {
        "data": [
            {
                "source_record_id": r.id,
                "namespace": namespace,
                "kind": r.kind,
                "source_id": r.source_id,
                "source_sha256": r.payload_sha256,
                "state": r.state or "pending",
                "revision": r.revision or 0,
                "reason_code": r.reason_code,
                "target_ref": r.target_ref,
            }
            for r in rows[:50]
        ],
        "meta": {
            "request_id": str(context.request_id),
            "replayed": False,
            "next_cursor": str(rows[49].id) if len(rows) > 50 else None,
        },
    }


@router.post(
    "/migration/snapshots/{namespace}/{snapshot_id}/apply",
    response_model=MigrationProgressResponse,
    openapi_extra=PRIVATE_WRITE,
)
def apply(
    namespace: Namespace,
    snapshot_id: UUID,
    command: ApplySourceRecords,
    request: Request,
    db: DbDep,
    access: MigrationAccess,
):
    context = _context(request, access, write=True)
    result = _commit(
        db,
        lambda: apply_source_records(
            db,
            namespace,
            snapshot_id,
            command,
            context=context,
            authorize=access.authorize,
        ),
    )
    return {
        "data": result,
        "meta": {"request_id": str(context.request_id), "replayed": result["replayed"]},
    }


@router.post(
    "/migration/records/{record_id}/recovery",
    response_model=MigrationRecordResponse,
    openapi_extra=PRIVATE_WRITE,
)
def recover(
    record_id: Annotated[int, Path(gt=0)],
    command: RecoveryCommand,
    request: Request,
    db: DbDep,
    access: MigrationAccess,
):
    context = _context(request, access, write=True)

    def restart(session, replacement, *, part_id, inbox_id, context):
        from .http_enrichment import _enabled
        from app.services import pn_catalog_enrichment as commands

        _enabled(
            request,
            {"recognize": "recognition", "research": "research", "bundle": "bundle"}[
                replacement.kind
            ],
        )
        current = resolve_job_access(session, context.actor)
        result = getattr(commands, replacement.kind)(
            session,
            inbox_id if replacement.kind == "recognize" else part_id,
            replacement.command,
            context=context,
            authorize=current.authorize,
        )
        return result["data"]["job_id"]

    return _commit(
        db,
        lambda: recover_record(
            db,
            record_id,
            command,
            context=context,
            authorize=access.authorize,
            restart_job=restart,
        ),
    )


@router.get(
    "/migration/requests/{request_id}",
    response_model=MigrationRecordResponse,
    openapi_extra=PRIVATE,
)
def receipt(request_id: UUID, request: Request, db: DbDep, access: MigrationAccess):
    context = _context(request, access)
    return api.get_receipt(
        db,
        request_id,
        context=context,
        authorize=access.authorize,
        projector=render_receipt,
    )

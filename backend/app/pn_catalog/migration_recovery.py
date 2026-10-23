"""Audited continuation after a frozen source: no implicit cloud or worker replay."""

from uuid import UUID, uuid5

from sqlalchemy import select

from . import api
from .dto import CommandContext, Provenance
from .errors import CatalogError
from .job_models import CatalogJob
from .media_models import GalleryEntry, InboxItem, MultipartUpload, UploadTicket
from .migration_apply import _resolved_rows, _write_application
from .migration_dto import fingerprint
from .migration_models import LegacySourceRecord
from .migration_state import _access, _namespace_lock
from .migration_state_models import (
    MigrationRecordApplication,
    MigrationRecoveryDecision,
    MigrationSnapshotState,
)


def record_response(db, record_id, *, context, authorize, replayed=False):
    _access(db, context, authorize)
    row = db.get(LegacySourceRecord, record_id)
    application = db.get(MigrationRecordApplication, record_id)
    if row is None or application is None:
        raise CatalogError(
            "migration.record_not_found", "转换记录不存在", status_code=404
        )
    return {
        "data": {
            "source_record_id": row.id,
            "namespace": row.namespace,
            "kind": row.kind,
            "source_id": row.source_id,
            "source_sha256": row.payload_sha256,
            "state": application.state,
            "revision": application.revision,
            "reason_code": application.reason_code,
            "target_ref": application.target_ref,
        },
        "meta": {"request_id": str(context.request_id), "replayed": replayed},
    }


def render_receipt(db, receipt, *, context, authorize, replayed=True):
    if receipt.result.get("resource") != "migration_record":
        raise CatalogError("request.not_found", "恢复请求不存在", status_code=404)
    return record_response(
        db,
        receipt.result["source_record_id"],
        context=context,
        authorize=authorize,
        replayed=replayed,
    )


def _retire_media(db, row, target):
    upload_id = target.get("upload_id")
    if row.kind == "inbox":
        item_id = target.get("inbox_id")
        initial = db.get(InboxItem, UUID(item_id)) if item_id else None
        if initial is None:
            raise CatalogError("migration.dependency_missing", "旧收件箱尚未转入")
        upload_id = str(initial.upload_id)
    ticket = (
        db.scalar(
            select(UploadTicket)
            .where(UploadTicket.id == UUID(upload_id))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if upload_id
        else None
    )
    if ticket and (ticket.revision != 1 or ticket.job_id):
        raise CatalogError(
            "migration.target_changed", "当前上传已被操作，不能按旧证据结束"
        )
    if row.kind == "inbox":
        item = db.scalar(
            select(InboxItem)
            .where(InboxItem.id == initial.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if item.status == "assigned":
            raise CatalogError(
                "migration.assignment_confirmation_required",
                "已归档图片需要确认归属，不能退回或作废",
            )
        if item.revision != max(1, row.payload.get("revision", 0)):
            raise CatalogError("migration.target_changed", "收件箱已有新操作")
        item.status, item.revision = "failed", item.revision + 1
        item.claimed_by, item.claim_expires_at = None, None
        item.claim_revision += 1
    if ticket:
        if ticket.status == "committed":
            raise CatalogError("migration.target_changed", "已归档上传不能作废")
        ticket.status, ticket.error_code = "cancelled", "migration.retired_after_review"
        ticket.revision += 1
        multipart = db.get(MultipartUpload, ticket.id)
        if multipart:
            multipart.state, multipart.revision = "cancelled", multipart.revision + 1
    return {
        **target,
        "reviewed_terminal": "cancelled",
        "remote_side_effect": "none_issued",
    }


def recover_record(db, record_id, command, *, context, authorize, restart_job=None):
    decision = _access(db, context, authorize, write=True)
    handle = api.begin_command(
        db,
        context=context,
        authorize=authorize,
        operation="materialize_migration",
        part_id=None,
        resource_type="migration_record",
        resource_id=record_id,
        payload=command,
    )
    row = db.get(LegacySourceRecord, record_id)
    if row is None:
        raise CatalogError(
            "migration.record_not_found", "来源记录不存在", status_code=404
        )
    _namespace_lock(db, row.namespace)
    if handle.receipt:
        return render_receipt(db, handle.receipt, context=context, authorize=authorize)
    application = db.get(MigrationRecordApplication, record_id, populate_existing=True)
    if application is None:
        raise CatalogError(
            "migration.record_not_found", "来源尚未转换", status_code=404
        )
    if application.revision != command.expected_revision:
        raise CatalogError(
            "migration.recovery_conflict", "恢复记录已有新决定，请重新读取"
        )
    if (
        command.source_sha256 != row.payload_sha256
        or fingerprint(row.payload) != row.payload_sha256
    ):
        raise CatalogError("migration.source_changed", "恢复证据与原始来源不一致")
    if application.state != "recovery_required":
        raise CatalogError("migration.recovery_conflict", "该记录当前不需要恢复")
    snapshot = db.get(MigrationSnapshotState, (row.namespace, application.snapshot_id))
    if snapshot is None or not snapshot.fence_evidence.get("lease_evidence_ref"):
        raise CatalogError("migration.fence_required", "缺少独立旧租约隔离证据")
    resolved, blocked = _resolved_rows(db, [row], context=context, authorize=authorize)
    if row.id in blocked:
        raise CatalogError(blocked[row.id], "当前来源归属尚未核对")
    part_id, _ = resolved.get(row.id, (None, None))
    target = dict(application.target_ref or {})
    confirmed = None
    if command.decision == "confirm_assignment":
        if row.kind != "inbox" or row.payload.get("status") != "assigned":
            raise CatalogError(
                "migration.recovery_kind_mismatch", "此决定仅用于已归档旧收件箱"
            )
        initial = db.get(InboxItem, UUID(target["inbox_id"]))
        if initial is None or part_id is None:
            raise CatalogError("migration.dependency_missing", "归档对象尚未迁入")
        from .media_common import identity, key_lock

        identity(db, [part_id], expected={part_id: command.expected_identity_revision})
        db.scalar(
            select(UploadTicket)
            .where(UploadTicket.id == initial.upload_id)
            .with_for_update()
        )
        item = db.scalar(
            select(InboxItem)
            .where(InboxItem.id == initial.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        key_lock(db, "inbox-assignment", str(command.assignment_key))
        entry = db.get(GalleryEntry, item.entry_id)
        if (
            item.status != "assigned"
            or item.revision != command.expected_inbox_revision
            or item.assignment_intent is not None
            or entry is None
            or entry.revision != command.expected_entry_revision
            or entry.origin_part_id != part_id
            or entry.origin_namespace != row.namespace
        ):
            raise CatalogError(
                "migration.target_changed", "已归档图片或认领状态发生变化"
            )
        if command.assignment_key == item.assignment_key or db.scalar(
            select(InboxItem.id).where(
                InboxItem.assignment_key == command.assignment_key
            )
        ):
            raise CatalogError(
                "request.idempotency_mismatch", "恢复需要独立的新归档确认号"
            )
        item.assignment_key = command.assignment_key
        # A new human confirmation fences the old (possibly zero) claim baseline.
        item.claim_revision += 1
        item.revision += 1
        confirmed = (item, entry)
        state = "applied"
        target = {
            **target,
            "recovery_assignment_key": str(command.assignment_key),
            "inbox_revision": item.revision,
        }
    elif command.decision == "restart_current":
        if restart_job is None:
            raise CatalogError(
                "migration.restart_unavailable",
                "当前任务启动端口未配置",
                status_code=503,
            )
        kind = command.replacement.kind
        history = target.get("history", {})
        inbox_id = history.get("inbox_id")
        if (
            (kind == "recognize" and (row.kind != "inbox_job" or not inbox_id))
            or (
                kind == "bundle"
                and (row.kind not in {"bundle", "agent_job"} or part_id is None)
            )
            or (
                kind == "research"
                and (
                    row.kind
                    not in {
                        "agent_job",
                        "agent_run",
                        "manual_research",
                        "image_recovery",
                    }
                    or part_id is None
                )
            )
        ):
            raise CatalogError(
                "migration.recovery_kind_mismatch", "重新发起的任务与原来源不匹配"
            )
        actor = context.actor.model_copy(
            update={
                "entrypoint": "itsp_ui",
                "provenance": Provenance(
                    kind="manual", evidence_id=f"legacy-record:{record_id}"
                ),
            }
        )
        fresh = CommandContext(
            actor=actor,
            request_id=uuid5(context.request_id, "replacement-job"),
            causation_id=str(context.request_id),
            reason=command.reason,
        )
        job_id = restart_job(
            db,
            command.replacement,
            part_id=part_id,
            inbox_id=UUID(inbox_id) if inbox_id else None,
            context=fresh,
        )
        job = db.get(CatalogJob, UUID(str(job_id)))
        expected_kind = {
            "recognize": "enrichment.recognize",
            "research": "enrichment.research",
            "bundle": "enrichment.bundle",
        }[kind]
        if (
            job is None
            or job.kind != expected_kind
            or job.context != fresh.model_dump(mode="json")
            or job.part_id != part_id
            or job.status != "pending"
        ):
            raise CatalogError(
                "migration.restart_not_created", "新当前任务未实际建立，恢复未完成"
            )
        state = "historical_terminal"
        target = {
            **target,
            "replacement_job_id": str(job.id),
            "reviewed_terminal": "superseded",
        }
    else:
        if row.kind in {"inbox", "multipart", "upload_intent", "upload"}:
            target = _retire_media(db, row, target)
        elif row.kind not in {
            "inbox_job",
            "agent_job",
            "agent_run",
            "identity_event",
            "manual_research",
            "image_recovery",
            "bundle",
            "catalog_conflict",
        }:
            raise CatalogError(
                "migration.recovery_kind_mismatch", "此类型不能结束为历史记录"
            )
        state = "historical_terminal"
        target = {
            **target,
            "reviewed_terminal": "cancelled",
            "no_legacy_lease_transferred": True,
        }
    target["recovery"] = {
        "decision": command.decision,
        "evidence_ref": command.evidence_ref,
        "external_outcome": command.external_outcome,
        "reviewer": context.actor.principal_id,
    }
    updated = _write_application(
        db,
        row,
        application.snapshot_id,
        state,
        None,
        target,
        context=context,
        authorization=decision.metadata,
    )
    db.flush()
    if confirmed:
        item, entry = confirmed
        item.assignment_intent = {
            "part_id": part_id,
            "purpose": entry.purpose,
            "expected_identity_revision": command.expected_identity_revision,
            "claim_revision": item.claim_revision,
            "audit_id": updated.audit_id,
            "applied_revision": item.revision,
            "recovery_source_record_id": record_id,
        }
    db.add(
        MigrationRecoveryDecision(
            source_record_id=record_id,
            expected_revision=command.expected_revision,
            decision_kind=command.decision,
            evidence={
                **command.model_dump(mode="json"),
                "writer_fence_ref": snapshot.fence_evidence["writer_fence_ref"],
                "lease_evidence_ref": snapshot.fence_evidence["lease_evidence_ref"],
                "target_ref": target,
            },
            audit_id=updated.audit_id,
        )
    )
    api.finish_command(
        db,
        handle,
        part_id=part_id,
        applied_revision=updated.revision,
        audit_id=updated.audit_id,
        result={"resource": "migration_record", "source_record_id": record_id},
    )
    db.flush()
    return record_response(db, record_id, context=context, authorize=authorize)

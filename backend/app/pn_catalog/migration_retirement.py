"""Retire related legacy media only from untouched or explicitly audited state."""

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import select

from .errors import CatalogError
from .media_models import InboxItem, MultipartUpload, UploadTicket
from .migration_dto import fingerprint
from .migration_media import _source, owner_binding
from .migration_models import LegacySourceRecord
from .migration_state_models import (
    MigrationRecordApplication,
    MigrationRecoveryDecision,
)


def _changed(message="当前上传已被操作，不能按旧证据结束"):
    raise CatalogError("migration.target_changed", message)


def _digest(ticket, multipart):
    # Hash all persisted fields, including job/token/parts and revisions. No object
    # location or operation token is exposed by the receipt's compatibility proof.
    def values(row):
        if row is None:
            return None
        result = {}
        for column in row.__table__.columns:
            value = getattr(row, column.name)
            result[column.name] = (
                value.isoformat()
                if isinstance(value, (datetime, date))
                else str(value)
                if isinstance(value, UUID)
                else value
            )
        return result

    return fingerprint({"upload": values(ticket), "multipart": values(multipart)})


def _prior_retirement(db, row, snapshot, proof):
    """The immutable decision, current ledger and frozen source must agree."""
    saved = MigrationRecoveryDecision.evidence["target_ref"]["retired_upload"]
    candidates = db.execute(
        select(MigrationRecoveryDecision, LegacySourceRecord)
        .join(
            MigrationRecordApplication,
            MigrationRecordApplication.source_record_id
            == MigrationRecoveryDecision.source_record_id,
        )
        .join(
            LegacySourceRecord,
            LegacySourceRecord.id == MigrationRecordApplication.source_record_id,
        )
        .where(
            MigrationRecoveryDecision.decision_kind == "retire_fenced",
            MigrationRecordApplication.state == "historical_terminal",
            MigrationRecordApplication.audit_id == MigrationRecoveryDecision.audit_id,
            MigrationRecordApplication.namespace == row.namespace,
            MigrationRecordApplication.snapshot_id == snapshot.snapshot_id,
            LegacySourceRecord.namespace == row.namespace,
            LegacySourceRecord.kind.in_(
                ["upload", "multipart", "upload_intent", "inbox"]
            ),
            saved == proof,
        )
        .order_by(
            MigrationRecoveryDecision.created_at.desc(),
            MigrationRecoveryDecision.id.desc(),
        )
        .limit(1)
    ).first()
    if candidates is None:
        return None
    decision, origin = candidates
    if (
        fingerprint(origin.payload) != origin.payload_sha256
        or decision.evidence.get("source_sha256") != origin.payload_sha256
        or decision.evidence.get("writer_fence_ref")
        != snapshot.fence_evidence["writer_fence_ref"]
        or decision.evidence.get("lease_evidence_ref")
        != snapshot.fence_evidence["lease_evidence_ref"]
    ):
        return None
    return {
        "source_record_id": origin.id,
        "recovery_id": str(decision.id),
        "audit_id": decision.audit_id,
    }


def retire_media(db, row, target, *, snapshot, command):
    initial = None
    if row.kind == "inbox":
        if target.get("inbox_id") != row.source_id:
            _changed("收件箱与来源证据不一致")
        initial = db.get(InboxItem, UUID(row.source_id))
        if initial is None:
            raise CatalogError("migration.dependency_missing", "旧收件箱尚未转入")
        upload_id = initial.upload_id
        if str(upload_id) != row.payload.get("upload_id"):
            _changed("收件箱上传归属与来源证据不一致")
    else:
        upload_id = UUID(target["upload_id"])
        if str(upload_id) != row.source_id:
            _changed("上传与来源证据不一致")
    source = _source(db, row, "upload", upload_id)
    application = db.get(MigrationRecordApplication, source.id) if source else None
    if (
        source is None
        or application is None
        or application.snapshot_id != snapshot.snapshot_id
        or application.namespace != row.namespace
        or application.state not in {"applied", "historical_terminal"}
        or application.source_sha256 != source.payload_sha256
        or fingerprint(source.payload) != source.payload_sha256
        or (application.target_ref or {}).get("upload_id") != str(upload_id)
    ):
        raise CatalogError(
            "migration.dependency_missing", "缺少同冻结快照内的上传转换证据"
        )
    canonical, binding = owner_binding(db, row.namespace, source.payload.get("user_id"))
    ticket = db.scalar(
        select(UploadTicket)
        .where(UploadTicket.id == upload_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if ticket is None:
        raise CatalogError("migration.dependency_missing", "旧上传尚未转入")
    original_binding = (application.target_ref or {}).get("owner_binding", {})
    if (
        ticket.owner_principal_id != canonical
        or ticket.client_id != "shop"
        or original_binding.get("canonical_user_id") != binding["canonical_user_id"]
        or original_binding.get("subject") != binding["subject"]
        or original_binding.get("issuer") != binding["issuer"]
    ):
        _changed("上传账号与原迁移绑定不一致")
    multipart = db.scalar(
        select(MultipartUpload)
        .where(MultipartUpload.upload_id == upload_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    proof = {
        "schema_version": 1,
        "namespace": row.namespace,
        "snapshot_id": str(snapshot.snapshot_id),
        "upload_id": str(upload_id),
        "upload_source_record_id": source.id,
        "upload_source_sha256": source.payload_sha256,
        "owner_binding": binding,
        "writer_fence_ref": snapshot.fence_evidence["writer_fence_ref"],
        "lease_evidence_ref": snapshot.fence_evidence["lease_evidence_ref"],
        "state_sha256": _digest(ticket, multipart),
    }
    untouched = (
        ticket.revision == 1
        and ticket.job_id is None
        and (multipart is None or multipart.revision == 1)
    )
    prior = None if untouched else _prior_retirement(db, row, snapshot, proof)
    if not untouched and prior is None:
        _changed()
    if prior and (
        ticket.job_id is not None or ticket.status not in {"cancelled", "committed"}
    ):
        _changed()
    item = None
    if initial:
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
            _changed("收件箱已有新操作")
    committed = ticket.status == "committed"
    if committed:
        # An expired legacy multipart cleanup may be uncertain even though its
        # original gallery publication is known. Retire cleanup, retain publication.
        if (
            row.kind != "multipart"
            or row.payload.get("status") != "completed"
            or command.external_outcome != "retained_effect_reviewed"
            or ticket.entry_id is None
            or source.payload.get("consumed_image_id") != str(ticket.entry_id)
        ):
            _changed("已归档上传只能核对保留原图片并结束旧分片清理")
    if item:
        item.status, item.revision = "failed", item.revision + 1
        item.claimed_by, item.claim_expires_at = None, None
        item.claim_revision += 1
    if prior is None:
        if not committed:
            ticket.status, ticket.error_code = (
                "cancelled",
                "migration.retired_after_review",
            )
            ticket.revision += 1
        if multipart:
            multipart.state, multipart.revision = "cancelled", multipart.revision + 1
    db.flush()
    return {
        **target,
        "reviewed_terminal": "cancelled",
        "remote_side_effect": "none_issued",
        "retained_entry_id": str(ticket.entry_id) if committed else None,
        "compatible_retirement": prior,
        "retired_upload": {**proof, "state_sha256": _digest(ticket, multipart)},
    }

"""Upload intent and multipart state. Cloud operations run through host ports outside transactions."""

import re
import json
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import select

from . import jobs
from .dto import CommandContext
from .errors import CatalogError
from .job_dto import JobSpec
from .media_common import (
    access,
    begin,
    finish,
    identity,
    key_lock,
    load,
    now,
    owner,
    read_response,
    response,
    revision,
)
from .media_models import Asset, InboxItem, MultipartUpload, UploadTicket


def upload_data(ticket):
    return {
        "upload_id": str(ticket.id),
        "part_id": ticket.part_id,
        "identity_revision": ticket.identity_revision,
        "purpose": ticket.purpose,
        "filename": ticket.filename,
        "size_bytes": ticket.declared_bytes,
        "content_type": ticket.content_type,
        "status": ticket.status,
        "revision": ticket.revision,
        "generation": ticket.generation,
        "asset_id": str(ticket.asset_id) if ticket.asset_id else None,
        "entry_id": str(ticket.entry_id) if ticket.entry_id else None,
        "job_id": str(ticket.job_id) if ticket.job_id else None,
        "error_code": ticket.error_code,
        "expires_at": ticket.expires_at.isoformat(),
    }


def owned_upload(db, identifier, context, authorize, *, lock=False):
    ticket = load(db, UploadTicket, identifier, lock=lock)
    owner(db, ticket, context, authorize, admin_action="uploads.manage")
    return ticket


def get_upload(db, upload_id, *, context, authorize):
    access(db, context, authorize, "uploads.read", None, field="uploads")
    return read_response(
        context, upload_data(owned_upload(db, upload_id, context, authorize))
    )


def create_upload(db, command, *, context, authorize):
    handle = begin(
        db,
        context=context,
        authorize=authorize,
        operation="uploads.create",
        part_id=command.part_id,
        resource="media.upload",
        identifier=command.client_request_id,
        command=command,
        field="uploads",
    )
    from .media import render_receipt

    if handle.receipt:
        return render_receipt(
            db, handle.receipt, context=context, authorize=authorize, replayed=True
        )
    if command.part_id:
        access(db, context, authorize, "gallery.archive", command.part_id, write=True)
    identity(
        db,
        [command.part_id],
        expected={command.part_id: command.expected_identity_revision}
        if command.part_id
        else {},
    )
    key_lock(
        db,
        "upload-client-request",
        context.actor.principal_id,
        context.actor.client_id,
        str(command.client_request_id),
    )
    ticket = db.scalar(
        select(UploadTicket).where(
            UploadTicket.owner_principal_id == context.actor.principal_id,
            UploadTicket.client_id == context.actor.client_id,
            UploadTicket.client_request_id == command.client_request_id,
        )
    )
    if ticket is not None:
        same = (
            ticket.part_id == command.part_id
            and ticket.identity_revision == (command.expected_identity_revision or 0)
            and ticket.declared_bytes == command.size_bytes
            and ticket.content_type == command.content_type
            and ticket.purpose == command.purpose
            and ticket.filename == command.filename
        )
        if not same:
            raise CatalogError(
                "request.idempotency_mismatch", "上传客户端编号已用于另一文件或目标"
            )
    else:
        identifier = uuid4()
        ticket = UploadTicket(
            id=identifier,
            owner_principal_id=context.actor.principal_id,
            client_id=context.actor.client_id,
            client_request_id=command.client_request_id,
            part_id=command.part_id,
            identity_revision=command.expected_identity_revision or 0,
            purpose=command.purpose,
            filename=command.filename,
            declared_bytes=command.size_bytes,
            content_type=command.content_type,
            staging_key=f"uploads/pn-catalog/{identifier}",
            expires_at=now() + timedelta(hours=24),
            status="issued",
            revision=1,
            generation=1,
        )
        db.add(ticket)
        db.flush()
    receipt = finish(
        db,
        handle,
        part_id=ticket.part_id,
        identifier=ticket.id,
        before=0,
        after=ticket.revision,
        changes={"upload_id": str(ticket.id), "part_id": ticket.part_id},
        identity_revision=ticket.identity_revision,
    )
    return response(db, receipt, upload_data(ticket), replayed=False)


def check_live(ticket):
    if ticket.status == "cancelled":
        raise CatalogError("media.deleted", "上传已取消")
    if ticket.expires_at <= now() and ticket.status == "issued":
        raise CatalogError("media.upload_expired", "上传票据已过期", status_code=410)


def _verify_job(db, ticket, context):
    job = jobs.enqueue(
        db,
        JobSpec(
            kind="media.verify_upload",
            queue="media",
            resource_type="media.upload",
            resource_id=str(ticket.id),
            part_id=ticket.part_id,
            resource_generation=ticket.generation,
            input_revision=ticket.identity_revision,
            payload={
                "upload_id": str(ticket.id),
                "staging_key": ticket.staging_key,
                "target_key": f"img/pn-catalog/assets/{ticket.id}/original",
                "content_type": ticket.content_type,
                "declared_bytes": ticket.declared_bytes,
            },
            deduplication_key=f"verify-upload:{ticket.id}:{ticket.generation}",
        ),
        context=context,
    )
    ticket.job_id, ticket.status = job.job_id, "verifying"


def complete_upload(db, upload_id, command, *, context, authorize):
    initial = owned_upload(db, upload_id, context, authorize)
    handle = begin(
        db,
        context=context,
        authorize=authorize,
        operation="uploads.complete",
        part_id=initial.part_id,
        resource="media.upload",
        identifier=upload_id,
        command=command,
        field="uploads",
    )
    from .media import render_receipt

    if handle.receipt:
        return render_receipt(
            db, handle.receipt, context=context, authorize=authorize, replayed=True
        )
    identity(
        db,
        [initial.part_id],
        expected={initial.part_id: initial.identity_revision}
        if initial.part_id
        else {},
    )
    ticket = owned_upload(db, upload_id, context, authorize, lock=True)
    check_live(ticket)
    revision(
        context,
        ticket.revision,
        command.expected_revision,
        code="media.upload_conflict",
    )
    before = ticket.revision
    multipart = db.get(MultipartUpload, ticket.id, populate_existing=True)
    if multipart and multipart.state != "completed":
        raise CatalogError("request.in_progress", "分片尚未完成合并")
    if ticket.status in ("issued", "failed"):
        _verify_job(db, ticket, context)
        ticket.revision += 1
    receipt = finish(
        db,
        handle,
        part_id=ticket.part_id,
        identifier=ticket.id,
        before=before,
        after=ticket.revision,
        changes={"status": ticket.status, "job_id": str(ticket.job_id)},
    )
    return response(db, receipt, upload_data(ticket), replayed=False)


def cancel_upload(db, upload_id, command, *, context, authorize):
    initial = owned_upload(db, upload_id, context, authorize)
    handle = begin(
        db,
        context=context,
        authorize=authorize,
        operation="uploads.cancel",
        part_id=initial.part_id,
        resource="media.upload",
        identifier=upload_id,
        command=command,
        field="uploads",
    )
    from .media import render_receipt

    if handle.receipt:
        return render_receipt(
            db, handle.receipt, context=context, authorize=authorize, replayed=True
        )
    identity(db, [initial.part_id])
    ticket = owned_upload(db, upload_id, context, authorize, lock=True)
    revision(
        context,
        ticket.revision,
        command.expected_revision,
        code="media.upload_conflict",
    )
    if ticket.status == "committed":
        raise CatalogError(
            "media.upload_already_committed", "图片已归档，请删除图库条目"
        )
    before = ticket.revision
    ticket.status = "cancelled"
    ticket.generation += 1
    ticket.revision += 1
    multipart = db.get(MultipartUpload, ticket.id)
    if multipart:
        multipart.state, multipart.operation_token = "cancelled", uuid4()
        multipart.revision += 1
    if ticket.job_id and jobs.get_job(db, ticket.job_id).status not in {
        "succeeded",
        "stale",
    }:
        jobs.cancel(db, ticket.job_id, context=context)
    if multipart:
        abort_job = jobs.enqueue(
            db,
            JobSpec(
                kind="media.abort_multipart",
                queue="media",
                resource_type="media.multipart",
                resource_id=str(ticket.id),
                part_id=ticket.part_id,
                resource_generation=multipart.revision,
                input_revision=0,
                payload={
                    "upload_id": str(ticket.id),
                    "key": ticket.staging_key,
                    "storage_upload_id": multipart.storage_upload_id,
                    "operation_token": str(multipart.operation_token),
                },
                deduplication_key=f"abort-multipart:{ticket.id}:{ticket.generation}",
            ),
            context=context,
        )
        ticket.job_id = abort_job.job_id
    inbox = db.scalar(
        select(InboxItem).where(InboxItem.upload_id == ticket.id).with_for_update()
    )
    if inbox and inbox.status != "assigned":
        inbox.status, inbox.deleted_at = "deleted", now()
        inbox.generation += 1
        inbox.revision += 1
    receipt = finish(
        db,
        handle,
        part_id=ticket.part_id,
        identifier=ticket.id,
        before=before,
        after=ticket.revision,
        changes={"status": "cancelled", "generation": ticket.generation},
    )
    return response(db, receipt, upload_data(ticket), replayed=False)


def verify_upload_resource(db, lease, *, authorize):
    ticket = load(db, UploadTicket, UUID(lease.resource_id))
    states = identity(db, [ticket.part_id])
    ticket = load(db, UploadTicket, ticket.id, lock=True)
    context = CommandContext.model_validate_json(json.dumps(lease.context))
    access(
        db,
        context,
        authorize,
        "uploads.complete",
        ticket.part_id,
        field="uploads",
        write=True,
    )
    owner(db, ticket, context, authorize, admin_action="uploads.manage")
    if ticket.status == "cancelled":
        return None
    current_identity = states[ticket.part_id].identity_revision if ticket.part_id else 0
    return ticket.generation, current_identity


def publish_verified_upload(db, lease, verified):
    """DB-only publisher called by queue.complete after its authoritative fences."""
    ticket = load(db, UploadTicket, UUID(lease.resource_id), lock=True)
    if ticket.status != "verifying":
        raise CatalogError(
            "request.precondition_failed", "上传不处于验图状态", status_code=412
        )
    valid = (
        re.fullmatch(r"[a-f0-9]{64}", verified.sha256) is not None
        and verified.size_bytes == ticket.declared_bytes
        and verified.content_type == ticket.content_type
        and 0 < verified.width * verified.height <= 40_000_000
        and verified.width > 0
        and verified.height > 0
        and bool(verified.source_etag)
        and verified.object_key == f"img/pn-catalog/assets/{ticket.id}/original"
    )
    if not valid:
        raise CatalogError("media.invalid_image", "云端校验结果无效", status_code=422)
    asset = Asset(
        id=uuid4(),
        object_key=verified.object_key,
        sha256=verified.sha256,
        etag=verified.source_etag,
        size_bytes=verified.size_bytes,
        content_type=verified.content_type,
        width=verified.width,
        height=verified.height,
        owner_principal_id=ticket.owner_principal_id,
        source_namespace="pn_upload",
        source_id=str(ticket.id),
    )
    db.add(asset)
    db.flush()
    ticket.asset_id, ticket.status = asset.id, "ready"
    ticket.revision += 1
    if ticket.part_id is None:
        inbox = db.scalar(
            select(InboxItem).where(InboxItem.upload_id == ticket.id).with_for_update()
        )
        if inbox is None:
            inbox = InboxItem(
                id=uuid4(),
                upload_id=ticket.id,
                owner_principal_id=ticket.owner_principal_id,
                status="ready",
                asset_id=asset.id,
                revision=1,
                generation=1,
                claim_revision=0,
            )
            db.add(inbox)
        elif inbox.status != "deleted":
            inbox.status, inbox.asset_id = "ready", asset.id
            inbox.revision += 1
    db.flush()
    return {"upload_id": str(ticket.id), "asset_id": str(asset.id)}


def multipart_data(ticket, multipart):
    return {
        "upload_id": str(ticket.id),
        "revision": ticket.revision,
        "state": multipart.state,
        "part_size": multipart.part_size,
        "parts": list(multipart.parts),
        "generation": ticket.generation,
        "aborted_at": multipart.aborted_at.isoformat()
        if multipart.aborted_at
        else None,
    }


def start_multipart(db, upload_id, command, *, context, authorize):
    initial = owned_upload(db, upload_id, context, authorize)
    handle = begin(
        db,
        context=context,
        authorize=authorize,
        operation="uploads.multipart.start",
        part_id=initial.part_id,
        resource="media.upload",
        identifier=upload_id,
        command=command,
        field="uploads",
    )
    from .media import render_receipt

    if handle.receipt:
        return render_receipt(
            db, handle.receipt, context=context, authorize=authorize, replayed=True
        )
    identity(db, [initial.part_id])
    ticket = owned_upload(db, upload_id, context, authorize, lock=True)
    check_live(ticket)
    revision(
        context,
        ticket.revision,
        command.expected_revision,
        code="media.upload_conflict",
    )
    if ticket.status != "issued":
        raise CatalogError(
            "media.upload_already_committed", "上传已提交，不能新建分片会话"
        )
    multipart = db.get(MultipartUpload, ticket.id)
    before = ticket.revision
    if multipart is None:
        multipart = MultipartUpload(
            upload_id=ticket.id,
            state="initializing",
            part_size=1048576,
            parts=[],
            operation_token=uuid4(),
            revision=1,
        )
        db.add(multipart)
        ticket.revision += 1
    receipt = finish(
        db,
        handle,
        part_id=ticket.part_id,
        identifier=ticket.id,
        before=before,
        after=ticket.revision,
        changes={"multipart_state": multipart.state},
    )
    return response(db, receipt, upload_data(ticket), replayed=False)


def multipart_start_plan(db, upload_id, *, context, authorize):
    """Host commits reservation before executing this immutable provider request."""
    access(
        db,
        context,
        authorize,
        "uploads.multipart.start",
        None,
        field="uploads",
        write=True,
    )
    ticket = owned_upload(db, upload_id, context, authorize)
    check_live(ticket)
    multipart = load(db, MultipartUpload, upload_id)
    return {
        "upload_id": ticket.id,
        "operation_token": multipart.operation_token,
        "storage_upload_id": multipart.storage_upload_id,
        "state": multipart.state,
        "key": ticket.staging_key,
        "content_type": ticket.content_type,
        "fingerprint": f"pn-upload:{ticket.id}:{ticket.generation}",
    }


def publish_multipart_started(db, upload_id, *, operation_token, storage_upload_id):
    ticket = load(db, UploadTicket, upload_id, lock=True)
    multipart = load(db, MultipartUpload, upload_id, lock=True)
    if (
        ticket.status != "issued"
        or ticket.expires_at <= now()
        or multipart.operation_token != operation_token
        or multipart.state != "initializing"
    ):
        return False
    if not storage_upload_id or len(storage_upload_id) > 1024:
        raise CatalogError(
            "media.provider_unavailable", "存储端未返回有效分片编号", status_code=503
        )
    multipart.storage_upload_id, multipart.state = storage_upload_id, "uploading"
    multipart.revision += 1
    ticket.revision += 1
    return True


def multipart_command_replay(
    db, upload_id, command, *, context, authorize, completing=False
):
    """Check accepted replay before contacting OSS, which may have consumed multipart state."""
    ticket = owned_upload(db, upload_id, context, authorize)
    handle = begin(
        db,
        context=context,
        authorize=authorize,
        operation="uploads.multipart.complete"
        if completing
        else "uploads.multipart.part",
        part_id=ticket.part_id,
        resource="media.upload",
        identifier=upload_id,
        command=command,
        field="uploads",
    )
    if handle.receipt:
        from .media import render_receipt

        return render_receipt(
            db, handle.receipt, context=context, authorize=authorize, replayed=True
        )
    return None


def get_multipart(db, upload_id, *, context, authorize):
    access(db, context, authorize, "uploads.read", None, field="uploads")
    ticket = owned_upload(db, upload_id, context, authorize)
    return read_response(
        context, multipart_data(ticket, load(db, MultipartUpload, upload_id))
    )


def upload_transport_plan(db, upload_id, *, context, authorize):
    """Authorized immutable input for the host's local sign_upload implementation."""
    access(db, context, authorize, "uploads.create", None, field="uploads", write=True)
    ticket = owned_upload(db, upload_id, context, authorize)
    check_live(ticket)
    if ticket.status != "issued" or db.get(MultipartUpload, ticket.id) is not None:
        raise CatalogError(
            "request.precondition_failed", "票据不允许普通上传", status_code=412
        )
    return {
        "key": ticket.staging_key,
        "expected_size": ticket.declared_bytes,
        "content_type": ticket.content_type,
        "expires_at": min(ticket.expires_at, now() + timedelta(minutes=5)),
    }


def multipart_part_plan(db, upload_id, command, *, context, authorize):
    """Every signed part belongs to the fixed ticket and bounded part-number whitelist."""
    access(
        db,
        context,
        authorize,
        "uploads.multipart.part",
        None,
        field="uploads",
        write=True,
    )
    ticket = owned_upload(db, upload_id, context, authorize)
    check_live(ticket)
    revision(
        context,
        ticket.revision,
        command.expected_revision,
        code="media.upload_conflict",
    )
    multipart = load(db, MultipartUpload, upload_id)
    count = (ticket.declared_bytes + multipart.part_size - 1) // multipart.part_size
    numbers = command.part_numbers
    if multipart.state != "uploading" or not multipart.storage_upload_id:
        raise CatalogError(
            "request.precondition_failed", "分片会话尚不可写", status_code=412
        )
    if len(numbers) != len(set(numbers)) or any(
        type(n) is not int or n < 1 or n > count for n in numbers
    ):
        raise CatalogError("media.invalid_part", "分片编号无效", status_code=422)
    return [
        {
            "key": ticket.staging_key,
            "upload_id": multipart.storage_upload_id,
            "number": number,
            "size": min(
                multipart.part_size,
                ticket.declared_bytes - (number - 1) * multipart.part_size,
            ),
            "expires_at": min(ticket.expires_at, now() + timedelta(minutes=5)),
        }
        for number in numbers
    ]


def multipart_storage_plan(db, upload_id, *, context, authorize):
    """Owner-checked list_parts/head input; original OSS upload ID is never regenerated."""
    access(db, context, authorize, "uploads.read", None, field="uploads")
    ticket = owned_upload(db, upload_id, context, authorize)
    multipart = load(db, MultipartUpload, upload_id)
    return {
        "key": ticket.staging_key,
        "upload_id": multipart.storage_upload_id,
        "state": multipart.state,
        "operation_token": multipart.operation_token,
    }


def validate_parts(ticket, multipart, parts, *, complete=False):
    count = (ticket.declared_bytes + multipart.part_size - 1) // multipart.part_size
    seen = set()
    for part in parts:
        number = part.part_number
        expected = min(
            multipart.part_size,
            ticket.declared_bytes - (number - 1) * multipart.part_size,
        )
        if (
            number in seen
            or number < 1
            or number > count
            or part.size_bytes != expected
        ):
            raise CatalogError(
                "media.invalid_part", "分片编号或大小无效", status_code=422
            )
        seen.add(number)
    if complete and seen != set(range(1, count + 1)):
        raise CatalogError("media.invalid_part", "分片未完整上传", status_code=422)


def record_multipart_part(db, upload_id, command, *, server_parts, context, authorize):
    """server_parts must come from Storage.list_parts outside this transaction."""
    initial = owned_upload(db, upload_id, context, authorize)
    handle = begin(
        db,
        context=context,
        authorize=authorize,
        operation="uploads.multipart.part",
        part_id=initial.part_id,
        resource="media.upload",
        identifier=upload_id,
        command=command,
        field="uploads",
    )
    from .media import render_receipt

    if handle.receipt:
        return render_receipt(
            db, handle.receipt, context=context, authorize=authorize, replayed=True
        )
    identity(db, [initial.part_id])
    ticket = owned_upload(db, upload_id, context, authorize, lock=True)
    multipart = load(db, MultipartUpload, upload_id, lock=True)
    check_live(ticket)
    revision(
        context,
        ticket.revision,
        command.expected_revision,
        code="media.upload_conflict",
    )
    if multipart.state != "uploading":
        raise CatalogError("request.in_progress", "分片会话不允许修改")
    validate_parts(ticket, multipart, server_parts)
    actual = next(
        (part for part in server_parts if part.part_number == command.part.part_number),
        None,
    )
    if actual is None or (actual.etag, actual.size_bytes) != (
        command.part.etag,
        command.part.size_bytes,
    ):
        raise CatalogError(
            "media.invalid_part", "存储端分片与客户端回执不符", status_code=422
        )
    before = ticket.revision
    multipart.parts = [
        {"part_number": p.part_number, "etag": p.etag, "size_bytes": p.size_bytes}
        for p in sorted(server_parts, key=lambda part: part.part_number)
    ]
    multipart.revision += 1
    ticket.revision += 1
    receipt = finish(
        db,
        handle,
        part_id=ticket.part_id,
        identifier=ticket.id,
        before=before,
        after=ticket.revision,
        changes={"part_number": actual.part_number, "size_bytes": actual.size_bytes},
    )
    return response(db, receipt, upload_data(ticket), replayed=False)


def complete_multipart(db, upload_id, command, *, server_parts, context, authorize):
    initial = owned_upload(db, upload_id, context, authorize)
    handle = begin(
        db,
        context=context,
        authorize=authorize,
        operation="uploads.multipart.complete",
        part_id=initial.part_id,
        resource="media.upload",
        identifier=upload_id,
        command=command,
        field="uploads",
    )
    from .media import render_receipt

    if handle.receipt:
        return render_receipt(
            db, handle.receipt, context=context, authorize=authorize, replayed=True
        )
    identity(db, [initial.part_id])
    ticket = owned_upload(db, upload_id, context, authorize, lock=True)
    multipart = load(db, MultipartUpload, upload_id, lock=True)
    check_live(ticket)
    revision(
        context,
        ticket.revision,
        command.expected_revision,
        code="media.upload_conflict",
    )
    if multipart.state not in ("uploading", "completing"):
        raise CatalogError(
            "request.precondition_failed", "分片状态不允许合并", status_code=412
        )
    validate_parts(ticket, multipart, server_parts, complete=True)

    def canonical(parts):
        return sorted((p.part_number, p.etag, p.size_bytes) for p in parts)

    if canonical(command.parts) != canonical(server_parts):
        raise CatalogError(
            "media.invalid_part", "存储端分片与合并清单不符", status_code=422
        )
    before = ticket.revision
    multipart.parts = [
        {"part_number": p.part_number, "etag": p.etag, "size_bytes": p.size_bytes}
        for p in sorted(server_parts, key=lambda p: p.part_number)
    ]
    multipart.state = "completing"
    multipart.revision += 1
    ticket.revision += 1
    job = jobs.enqueue(
        db,
        JobSpec(
            kind="media.complete_multipart",
            queue="media",
            resource_type="media.upload",
            resource_id=str(ticket.id),
            part_id=ticket.part_id,
            resource_generation=ticket.generation,
            input_revision=ticket.identity_revision,
            payload={
                "upload_id": str(ticket.id),
                "key": ticket.staging_key,
                "storage_upload_id": multipart.storage_upload_id,
                "parts": multipart.parts,
                "operation_token": str(multipart.operation_token),
            },
            deduplication_key=f"multipart:{ticket.id}:{ticket.generation}",
        ),
        context=context,
    )
    ticket.job_id = job.job_id
    receipt = finish(
        db,
        handle,
        part_id=ticket.part_id,
        identifier=ticket.id,
        before=before,
        after=ticket.revision,
        changes={"multipart_state": multipart.state, "job_id": str(job.job_id)},
    )
    return response(db, receipt, upload_data(ticket), replayed=False)


def publish_multipart_completed(db, lease):
    ticket = load(db, UploadTicket, UUID(lease.resource_id), lock=True)
    multipart = load(db, MultipartUpload, ticket.id, lock=True)
    if (
        multipart.state != "completing"
        or str(multipart.operation_token) != lease.payload["operation_token"]
    ):
        raise CatalogError(
            "request.precondition_failed", "分片合并已失效", status_code=412
        )
    multipart.state = "completed"
    multipart.revision += 1
    _verify_job(
        db, ticket, CommandContext.model_validate_json(json.dumps(lease.context))
    )
    ticket.revision += 1
    return {"upload_id": str(ticket.id), "verify_job_id": str(ticket.job_id)}


def verify_multipart_abort_resource(db, lease, *, authorize):
    ticket = load(db, UploadTicket, UUID(lease.resource_id))
    identity(db, [ticket.part_id])
    ticket = load(db, UploadTicket, ticket.id, lock=True)
    context = CommandContext.model_validate_json(json.dumps(lease.context))
    access(
        db,
        context,
        authorize,
        "uploads.cancel",
        ticket.part_id,
        field="uploads",
        write=True,
    )
    multipart = load(db, MultipartUpload, ticket.id, lock=True)
    if (
        multipart.state != "cancelled"
        or str(multipart.operation_token) != lease.payload["operation_token"]
    ):
        return None
    # Abort concerns the fixed staging upload, independent of later PN rename.
    return multipart.revision, 0


def publish_multipart_aborted(db, lease, _prepared=None):
    multipart = load(db, MultipartUpload, UUID(lease.resource_id), lock=True)
    multipart.aborted_at = now()
    multipart.revision += 1
    return {"upload_id": lease.resource_id, "multipart_aborted": True}

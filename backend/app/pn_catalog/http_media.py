"""Media HTTP adapter. Provider I/O uses short, separate host transactions."""

from dataclasses import asdict
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Path, Query, Request
from pydantic import Field

from app.db import SessionLocal
from app.services.pn_media_execution import MediaTransport
from . import api, media
from .errors import CatalogError
from .http import AccessDep, DbDep, PartId, _WRITE_HEADERS, _commit, _context, router
from .media_dto import (
    AddEntry,
    AddInboxGroupItem,
    ArrangeGallery,
    ArrangeInboxGroup,
    AssignInbox,
    ClaimInbox,
    CompleteMultipart,
    CreateDerivative,
    CreateInboxGroup,
    CreateUpload,
    ExpectedRevision,
    MultipartPart,
    MultipartPartURLs,
    PatchEntry,
    RecordMultipartPart,
    ReleaseInbox,
    RestoreEntry,
    SuppressHash,
)
from .wire_media import (
    DerivativeResponse,
    EntryLinksResponse,
    EntryResponse,
    GalleryResponse,
    InboxGroupResponse,
    InboxListResponse,
    InboxLinksResponse,
    InboxResponse,
    MultipartResponse,
    MultipartPartsResponse,
    PartSignaturesResponse,
    SuppressionResponse,
    SuppressionListResponse,
    UploadFormResponse,
    UploadResponse,
)


def _storage(request):
    storage = getattr(request.app.state, "pn_media_storage", None)
    if storage is None:
        raise CatalogError(
            "media.provider_unavailable",
            "图片存储尚未接入",
            status_code=503,
            retryable=True,
        )
    return storage


def _transport(request, access):
    return MediaTransport(
        SessionLocal, storage=_storage(request), authorize=access.authorize
    )


def _read_meta(context):
    return {
        "request_id": str(context.request_id),
        "replayed": False,
        "next_cursor": None,
    }


@router.get(
    "/parts/{part_id}/gallery",
    response_model=GalleryResponse,
    response_model_exclude_unset=True,
)
def gallery(
    part_id: PartId,
    request: Request,
    db: DbDep,
    access: AccessDep,
    status: Literal["gallery", "candidate", "processing", "deleted"] = "gallery",
    purpose: Literal["common", "defect"] | None = None,
    cursor: str | None = Query(default=None, max_length=512),
    limit: int = Query(default=50, ge=1, le=200),
):
    return media.get_gallery(
        db,
        part_id,
        context=_context(request, access),
        authorize=access.authorize,
        status=status,
        purpose=purpose,
        cursor=cursor,
        limit=limit,
    )


@router.post(
    "/parts/{part_id}/gallery/entries",
    response_model=EntryResponse,
    status_code=201,
    openapi_extra=_WRITE_HEADERS,
)
def add_entry(
    part_id: PartId, command: AddEntry, request: Request, db: DbDep, access: AccessDep
):
    context = _context(request, access, write=True)
    return _commit(
        db,
        lambda: media.add_entry(
            db, part_id, command, context=context, authorize=access.authorize
        ),
    )


@router.patch(
    "/parts/{part_id}/gallery",
    response_model=GalleryResponse,
    openapi_extra=_WRITE_HEADERS,
)
def arrange_gallery(
    part_id: PartId,
    command: ArrangeGallery,
    request: Request,
    db: DbDep,
    access: AccessDep,
):
    context = _context(request, access, write=True)
    return _commit(
        db,
        lambda: media.arrange_gallery(
            db, part_id, command, context=context, authorize=access.authorize
        ),
    )


@router.patch(
    "/gallery/entries/{entry_id}",
    response_model=EntryResponse,
    openapi_extra=_WRITE_HEADERS,
)
def patch_entry(
    entry_id: UUID, command: PatchEntry, request: Request, db: DbDep, access: AccessDep
):
    context = _context(request, access, write=True)
    return _commit(
        db,
        lambda: media.patch_entry(
            db, entry_id, command, context=context, authorize=access.authorize
        ),
    )


@router.delete(
    "/gallery/entries/{entry_id}",
    response_model=EntryResponse,
    openapi_extra=_WRITE_HEADERS,
)
def delete_entry(
    entry_id: UUID,
    request: Request,
    db: DbDep,
    access: AccessDep,
    expected_revision: int = Query(ge=1),
):
    context = _context(request, access, write=True)
    return _commit(
        db,
        lambda: media.delete_entry(
            db,
            entry_id,
            ExpectedRevision(expected_revision=expected_revision),
            context=context,
            authorize=access.authorize,
        ),
    )


@router.post(
    "/gallery/entries/{entry_id}/restoration",
    response_model=EntryResponse,
    openapi_extra=_WRITE_HEADERS,
)
def restore_entry(
    entry_id: UUID,
    command: RestoreEntry,
    request: Request,
    db: DbDep,
    access: AccessDep,
):
    context = _context(request, access, write=True)
    return _commit(
        db,
        lambda: media.restore_entry(
            db, entry_id, command, context=context, authorize=access.authorize
        ),
    )


@router.get("/gallery/entries/{entry_id}/links", response_model=EntryLinksResponse)
def entry_links(entry_id: UUID, request: Request, db: DbDep, access: AccessDep):
    context = _context(request, access)
    return {
        "data": media.entry_links(
            db,
            entry_id,
            context=context,
            authorize=access.authorize,
            signer=_storage(request),
        ),
        "meta": _read_meta(context),
    }


@router.get("/gallery-groups/{group_id}/suppressions", response_model=SuppressionListResponse)
def suppressions(group_id: UUID, request: Request, db: DbDep, access: AccessDep,
                 cursor: str | None = Query(default=None, max_length=512),
                 limit: int = Query(default=20, ge=1, le=50)):
    return media.list_suppressions(db, group_id, context=_context(request, access),
                                  authorize=access.authorize, cursor=cursor, limit=limit)


@router.post(
    "/gallery-groups/{group_id}/suppressions",
    response_model=SuppressionResponse,
    openapi_extra=_WRITE_HEADERS,
)
def suppress_hash(
    group_id: UUID,
    command: SuppressHash,
    request: Request,
    db: DbDep,
    access: AccessDep,
):
    context = _context(request, access, write=True)
    return _commit(
        db,
        lambda: media.suppress_hash(
            db, group_id, command, context=context, authorize=access.authorize
        ),
    )


@router.delete(
    "/gallery-groups/{group_id}/suppressions/{suppression_id}",
    response_model=SuppressionResponse,
    openapi_extra=_WRITE_HEADERS,
)
def unsuppress_hash(
    group_id: UUID,
    suppression_id: UUID,
    request: Request,
    db: DbDep,
    access: AccessDep,
    expected_revision: int = Query(ge=1),
):
    context = _context(request, access, write=True)
    return _commit(
        db,
        lambda: media.unsuppress_hash(
            db,
            group_id,
            suppression_id,
            ExpectedRevision(expected_revision=expected_revision),
            context=context,
            authorize=access.authorize,
        ),
    )


@router.post(
    "/assets/{asset_id}/derivatives",
    response_model=DerivativeResponse,
    status_code=202,
    openapi_extra=_WRITE_HEADERS,
)
def create_derivative(
    asset_id: UUID,
    command: CreateDerivative,
    request: Request,
    db: DbDep,
    access: AccessDep,
):
    _storage(request)
    context = _context(request, access, write=True)
    return _commit(
        db,
        lambda: media.create_derivative(
            db, asset_id, command, context=context, authorize=access.authorize
        ),
    )


@router.post(
    "/uploads",
    response_model=UploadResponse,
    response_model_exclude_unset=True,
    status_code=201,
    openapi_extra=_WRITE_HEADERS,
)
def create_upload(
    command: CreateUpload, request: Request, db: DbDep, access: AccessDep
):
    storage = _storage(request)
    context = _context(request, access, write=True)
    result = _commit(
        db,
        lambda: media.create_upload(
            db, command, context=context, authorize=access.authorize
        ),
    )
    if result["data"]["status"] == "issued":
        identifier = UUID(result["data"]["upload_id"])
        # Local signing only; cloud SDK calls are not permitted in this port.
        try:
            plan = media.upload_transport_plan(
                db, identifier, context=context, authorize=access.authorize
            )
        except CatalogError as error:
            if error.code != "request.precondition_failed":
                raise
        else:
            result["data"]["upload_form"] = asdict(storage.sign_upload(**plan))
    return result


@router.get(
    "/uploads/{upload_id}",
    response_model=UploadResponse,
    response_model_exclude_unset=True,
)
def get_upload(upload_id: UUID, request: Request, db: DbDep, access: AccessDep):
    return media.get_upload(
        db, upload_id, context=_context(request, access), authorize=access.authorize
    )


@router.get("/uploads/{upload_id}/transport", response_model=UploadFormResponse)
def upload_transport(upload_id: UUID, request: Request, db: DbDep, access: AccessDep):
    context = _context(request, access)
    plan = media.upload_transport_plan(
        db, upload_id, context=context, authorize=access.authorize
    )
    return {
        "data": asdict(_storage(request).sign_upload(**plan)),
        "meta": _read_meta(context),
    }


@router.post(
    "/uploads/{upload_id}/completion",
    response_model=UploadResponse,
    status_code=202,
    openapi_extra=_WRITE_HEADERS,
)
def complete_upload(
    upload_id: UUID,
    command: ExpectedRevision,
    request: Request,
    db: DbDep,
    access: AccessDep,
):
    context = _context(request, access, write=True)
    return _commit(
        db,
        lambda: media.complete_upload(
            db, upload_id, command, context=context, authorize=access.authorize
        ),
    )


@router.post(
    "/uploads/{upload_id}/cancellation",
    response_model=UploadResponse,
    openapi_extra=_WRITE_HEADERS,
)
def cancel_upload(
    upload_id: UUID,
    command: ExpectedRevision,
    request: Request,
    db: DbDep,
    access: AccessDep,
):
    context = _context(request, access, write=True)
    return _commit(
        db,
        lambda: media.cancel_upload(
            db, upload_id, command, context=context, authorize=access.authorize
        ),
    )


@router.post(
    "/uploads/{upload_id}/multipart",
    response_model=UploadResponse,
    status_code=201,
    openapi_extra=_WRITE_HEADERS,
)
def start_multipart(
    upload_id: UUID,
    command: ExpectedRevision,
    request: Request,
    db: DbDep,
    access: AccessDep,
):
    transport = _transport(request, access)
    context = _context(request, access, write=True)
    _commit(
        db,
        lambda: media.start_multipart(
            db, upload_id, command, context=context, authorize=access.authorize
        ),
    )
    state = media.get_multipart(
        db, upload_id, context=context, authorize=access.authorize
    )["data"]["state"]
    db.commit()
    if state == "initializing":
        transport.initialize_multipart(upload_id, context=context)
    result = api.get_receipt(
        db, context.request_id, context=context, authorize=access.authorize
    )
    result["data"]["multipart"] = media.get_multipart(
        db, upload_id, context=context, authorize=access.authorize
    )["data"]
    return result


@router.get("/uploads/{upload_id}/multipart", response_model=MultipartResponse)
def get_multipart(upload_id: UUID, request: Request, db: DbDep, access: AccessDep):
    return media.get_multipart(
        db, upload_id, context=_context(request, access), authorize=access.authorize
    )


@router.post(
    "/uploads/{upload_id}/multipart/part-urls",
    response_model=PartSignaturesResponse,
    openapi_extra=_WRITE_HEADERS,
)
def multipart_part_urls(
    upload_id: UUID,
    command: MultipartPartURLs,
    request: Request,
    db: DbDep,
    access: AccessDep,
):
    context = _context(request, access, write=True)
    plans = media.multipart_part_plan(
        db, upload_id, command, context=context, authorize=access.authorize
    )
    storage = _storage(request)
    return {
        "data": [
            {"part_number": plan["number"], **asdict(storage.sign_part(**plan))}
            for plan in plans
        ],
        "meta": _read_meta(context),
    }


class MultipartPartReceipt(ExpectedRevision):
    etag: str = Field(min_length=1, max_length=256)
    size_bytes: int = Field(gt=0, le=20 * 1024 * 1024)


@router.put(
    "/uploads/{upload_id}/multipart/parts/{part_number}",
    response_model=UploadResponse,
    openapi_extra=_WRITE_HEADERS,
)
def record_part(
    upload_id: UUID,
    part_number: Annotated[int, Path(ge=1, le=10000)],
    command: MultipartPartReceipt,
    request: Request,
    db: DbDep,
    access: AccessDep,
):
    context = _context(request, access, write=True)
    bounded = RecordMultipartPart(
        expected_revision=command.expected_revision,
        part=MultipartPart(
            part_number=part_number, etag=command.etag, size_bytes=command.size_bytes
        ),
    )
    replay = _commit(
        db,
        lambda: media.multipart_command_replay(
            db, upload_id, bounded, context=context, authorize=access.authorize
        ),
    )
    if replay is not None:
        return replay
    actual = _transport(request, access).list_parts(upload_id, context=context)
    return _commit(
        db,
        lambda: media.record_multipart_part(
            db,
            upload_id,
            bounded,
            server_parts=actual,
            context=context,
            authorize=access.authorize,
        ),
    )


@router.post(
    "/uploads/{upload_id}/multipart/completion",
    response_model=UploadResponse,
    status_code=202,
    openapi_extra=_WRITE_HEADERS,
)
def complete_multipart(
    upload_id: UUID,
    command: CompleteMultipart,
    request: Request,
    db: DbDep,
    access: AccessDep,
):
    context = _context(request, access, write=True)
    replay = _commit(
        db,
        lambda: media.multipart_command_replay(
            db,
            upload_id,
            command,
            context=context,
            authorize=access.authorize,
            completing=True,
        ),
    )
    if replay is not None:
        return replay
    actual = _transport(request, access).list_parts(upload_id, context=context)
    return _commit(
        db,
        lambda: media.complete_multipart(
            db,
            upload_id,
            command,
            server_parts=actual,
            context=context,
            authorize=access.authorize,
        ),
    )


@router.get("/inbox-items", response_model=InboxListResponse)
def list_inbox(
    request: Request,
    db: DbDep,
    access: AccessDep,
    scope: Literal["mine", "shared"] = "mine",
    group_id: UUID | None = None,
    status: Literal["all", "ready", "assigned", "deleted", "receiving", "failed"] = "ready",
    cursor: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
):
    return media.list_inbox(
        db,
        context=_context(request, access),
        authorize=access.authorize,
        scope=scope,
        group_id=group_id,
        status=status,
        after_id=cursor,
        limit=limit,
    )


@router.get("/inbox-items/{inbox_id}", response_model=InboxResponse)
def get_inbox(inbox_id: UUID, request: Request, db: DbDep, access: AccessDep):
    return media.get_inbox(
        db, inbox_id, context=_context(request, access), authorize=access.authorize
    )


@router.get("/inbox-items/{inbox_id}/links", response_model=InboxLinksResponse)
def inbox_links(inbox_id: UUID, request: Request, db: DbDep, access: AccessDep):
    context = _context(request, access)
    return {"data": media.inbox_links(db, inbox_id, context=context,
        authorize=access.authorize, signer=_storage(request)), "meta": _read_meta(context)}


@router.post(
    "/inbox-items/{inbox_id}/claim",
    response_model=InboxResponse,
    openapi_extra=_WRITE_HEADERS,
)
def claim_inbox(
    inbox_id: UUID, command: ClaimInbox, request: Request, db: DbDep, access: AccessDep
):
    context = _context(request, access, write=True)
    return _commit(
        db,
        lambda: media.claim_inbox(
            db, inbox_id, command, context=context, authorize=access.authorize
        ),
    )


@router.post(
    "/inbox-items/{inbox_id}/release",
    response_model=InboxResponse,
    openapi_extra=_WRITE_HEADERS,
)
def release_inbox(
    inbox_id: UUID,
    command: ReleaseInbox,
    request: Request,
    db: DbDep,
    access: AccessDep,
):
    context = _context(request, access, write=True)
    return _commit(
        db,
        lambda: media.release_inbox(
            db, inbox_id, command, context=context, authorize=access.authorize
        ),
    )


@router.post(
    "/inbox-items/{inbox_id}/assignment",
    response_model=InboxResponse,
    openapi_extra=_WRITE_HEADERS,
)
def assign_inbox(
    inbox_id: UUID, command: AssignInbox, request: Request, db: DbDep, access: AccessDep
):
    context = _context(request, access, write=True)
    return _commit(
        db,
        lambda: media.assign_inbox(
            db, inbox_id, command, context=context, authorize=access.authorize
        ),
    )


@router.delete(
    "/inbox-items/{inbox_id}",
    response_model=InboxResponse,
    openapi_extra=_WRITE_HEADERS,
)
def delete_inbox(
    inbox_id: UUID,
    request: Request,
    db: DbDep,
    access: AccessDep,
    expected_revision: int = Query(ge=1),
):
    context = _context(request, access, write=True)
    return _commit(
        db,
        lambda: media.delete_inbox(
            db,
            inbox_id,
            ExpectedRevision(expected_revision=expected_revision),
            context=context,
            authorize=access.authorize,
        ),
    )


@router.post(
    "/inbox-items/{inbox_id}/restoration",
    response_model=InboxResponse,
    openapi_extra=_WRITE_HEADERS,
)
def restore_inbox(
    inbox_id: UUID,
    command: RestoreEntry,
    request: Request,
    db: DbDep,
    access: AccessDep,
):
    context = _context(request, access, write=True)
    return _commit(
        db,
        lambda: media.restore_inbox(
            db, inbox_id, command, context=context, authorize=access.authorize
        ),
    )


@router.post(
    "/inbox-groups",
    response_model=InboxGroupResponse,
    status_code=201,
    openapi_extra=_WRITE_HEADERS,
)
def create_group(
    command: CreateInboxGroup, request: Request, db: DbDep, access: AccessDep
):
    context = _context(request, access, write=True)
    return _commit(
        db,
        lambda: media.create_inbox_group(
            db, command, context=context, authorize=access.authorize
        ),
    )


@router.post(
    "/inbox-groups/{group_id}/items",
    response_model=InboxGroupResponse,
    status_code=201,
    openapi_extra=_WRITE_HEADERS,
)
def add_group_item(
    group_id: UUID,
    command: AddInboxGroupItem,
    request: Request,
    db: DbDep,
    access: AccessDep,
):
    context = _context(request, access, write=True)
    return _commit(
        db,
        lambda: media.add_group_item(
            db, group_id, command, context=context, authorize=access.authorize
        ),
    )


@router.patch(
    "/inbox-groups/{group_id}",
    response_model=InboxGroupResponse,
    openapi_extra=_WRITE_HEADERS,
)
def arrange_group(
    group_id: UUID,
    command: ArrangeInboxGroup,
    request: Request,
    db: DbDep,
    access: AccessDep,
):
    context = _context(request, access, write=True)
    return _commit(
        db,
        lambda: media.arrange_inbox_group(
            db, group_id, command, context=context, authorize=access.authorize
        ),
    )


@router.get("/uploads/{upload_id}/multipart/parts", response_model=MultipartPartsResponse)
def list_storage_parts(upload_id: UUID, request: Request, db: DbDep, access: AccessDep):
    context = _context(request, access)
    # The host opens and closes its own short read transactions around provider I/O.
    db.commit()
    parts = _transport(request, access).list_parts(upload_id, context=context)
    return {"data": [asdict(part) for part in parts], "meta": _read_meta(context)}


@router.get("/uploads/{upload_id}/inbox", response_model=InboxResponse)
def upload_inbox(upload_id: UUID, request: Request, db: DbDep, access: AccessDep):
    from .media_uploads import get_upload_inbox
    return get_upload_inbox(db, upload_id, context=_context(request, access), authorize=access.authorize)


@router.get("/gallery/entries/{entry_id}", response_model=EntryResponse)
def get_entry(entry_id: UUID, request: Request, db: DbDep, access: AccessDep):
    from .media_derivatives import get_entry as read_entry
    return read_entry(db, entry_id, context=_context(request, access), authorize=access.authorize)


@router.get("/inbox-groups/{group_id}", response_model=InboxGroupResponse)
def get_inbox_group(group_id: UUID, request: Request, db: DbDep, access: AccessDep):
    from .media_inbox import get_inbox_group as read_group
    return read_group(db, group_id, context=_context(request, access), authorize=access.authorize)

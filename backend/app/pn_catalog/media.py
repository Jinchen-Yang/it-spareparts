"""Authoritative media facade. Commands never commit and never perform cloud I/O."""

from .media_derivatives import (
    create_derivative,
    entry_links,
    publish_derivative,
    verify_derivative_resource,
)
from .media_gallery import (
    add_entry,
    arrange_gallery,
    change_entry,
    entry_data,
    get_gallery,
    suppress_hash,
    suppression_data,
    unsuppress_hash,
)
from .media_inbox import (
    add_group_item,
    arrange_inbox_group,
    assign_inbox,
    can_read,
    change_inbox,
    claim_inbox,
    create_inbox_group,
    get_inbox,
    group_data,
    inbox_data,
    inbox_links,
    list_inbox,
    owned_group,
    release_inbox,
)
from .media_uploads import (
    cancel_upload,
    complete_multipart,
    complete_upload,
    create_upload,
    get_multipart,
    get_upload,
    multipart_part_plan,
    multipart_start_plan,
    multipart_storage_plan,
    multipart_command_replay,
    owned_upload,
    publish_multipart_completed,
    publish_multipart_started,
    publish_verified_upload,
    record_multipart_part,
    start_multipart,
    upload_data,
    upload_transport_plan,
    verify_upload_resource,
    verify_multipart_abort_resource,
    publish_multipart_aborted,
)
from .media_common import access, load, response, uuid
from .media_models import (
    Derivative,
    GalleryEntry,
    GalleryGroup,
    GallerySuppression,
    InboxItem,
)
from .errors import CatalogError
from .media_identity import (
    ensure_identity_gallery_members,
    merge_gallery_groups,
    unmerge_gallery_groups,
)
from .media_research import (
    bundle_snapshot,
    publish_research_candidate,
    verify_bundle_snapshot,
)
from .media_job_access import authorize_job_resource


def delete_entry(db, entry_id, command, *, context, authorize):
    return change_entry(
        db,
        entry_id,
        command,
        context=context,
        authorize=authorize,
        operation="gallery.delete",
    )


def restore_entry(db, entry_id, command, *, context, authorize):
    return change_entry(
        db,
        entry_id,
        command,
        context=context,
        authorize=authorize,
        operation="gallery.restore",
    )


def patch_entry(db, entry_id, command, *, context, authorize):
    return change_entry(
        db,
        entry_id,
        command,
        context=context,
        authorize=authorize,
        operation="gallery.edit",
    )


def delete_inbox(db, inbox_id, command, *, context, authorize):
    return change_inbox(db, inbox_id, command, context=context, authorize=authorize)


def restore_inbox(db, inbox_id, command, *, context, authorize):
    return change_inbox(
        db, inbox_id, command, context=context, authorize=authorize, restore=True
    )


def render_receipt(db, receipt, *, context, authorize, replayed):
    """Project current authorized state, retaining the original applied revision."""
    resource, identifier = receipt.result["resource"], uuid(receipt.result["id"])
    field = (
        "uploads"
        if resource == "media.upload"
        else "inbox"
        if resource.startswith("media.inbox")
        else "gallery"
    )
    access(
        db,
        context,
        authorize,
        receipt.operation,
        receipt.part_id,
        field=field,
        write=True,
    )
    access(
        db,
        context,
        authorize,
        {"uploads": "uploads.read", "inbox": "inbox.read", "gallery": "gallery.read"}[
            field
        ],
        receipt.part_id,
        field=field,
    )
    if resource == "media.entry":
        if receipt.result.get("source_entry_id"):
            from .media_gallery import check_reuse_source

            source = load(db, GalleryEntry, uuid(receipt.result["source_entry_id"]))
            check_reuse_source(
                db,
                source,
                uuid(receipt.result["source_asset_id"]),
                receipt.result["purpose"],
                context=context,
                authorize=authorize,
            )
        entry = load(db, GalleryEntry, identifier)
        access(db, context, authorize, "gallery.read", entry.origin_part_id)
        data = entry_data(db, entry)
    elif resource == "media.gallery":
        group = load(db, GalleryGroup, identifier)
        page = get_gallery(
            db, group.canonical_part_id, context=context, authorize=authorize
        )
        result = response(db, receipt, page["data"], replayed=replayed)
        result["meta"]["next_cursor"] = page["meta"]["next_cursor"]
        return result
    elif resource == "media.upload":
        data = upload_data(owned_upload(db, identifier, context, authorize))
    elif resource == "media.inbox":
        item = load(db, InboxItem, identifier)
        can_read(db, item, context, authorize)
        data = inbox_data(db, item, context=context)
    elif resource == "media.inbox_group":
        data = group_data(db, owned_group(db, identifier, context, authorize))
    elif resource == "media.suppression":
        item = load(db, GallerySuppression, identifier)
        group = load(db, GalleryGroup, item.group_id)
        access(db, context, authorize, "gallery.read", group.canonical_part_id)
        data = suppression_data(item)
    elif resource == "media.derivative":
        from .media_derivatives import derivative_data

        item = load(db, Derivative, identifier)
        entry = load(db, GalleryEntry, uuid(receipt.result["entry_id"]))
        access(db, context, authorize, "gallery.read", entry.origin_part_id)
        data = derivative_data(item)
    else:
        raise CatalogError("request.not_found", "媒体回执类型不存在", status_code=404)
    return response(db, receipt, data, replayed=replayed)


__all__ = [
    "add_entry",
    "arrange_gallery",
    "get_gallery",
    "delete_entry",
    "restore_entry",
    "patch_entry",
    "suppress_hash",
    "unsuppress_hash",
    "create_derivative",
    "entry_links",
    "publish_derivative",
    "verify_derivative_resource",
    "create_upload",
    "get_upload",
    "complete_upload",
    "cancel_upload",
    "start_multipart",
    "get_multipart",
    "record_multipart_part",
    "complete_multipart",
    "multipart_start_plan",
    "multipart_part_plan",
    "multipart_storage_plan",
    "multipart_command_replay",
    "upload_transport_plan",
    "publish_multipart_started",
    "publish_multipart_completed",
    "publish_verified_upload",
    "verify_upload_resource",
    "verify_multipart_abort_resource",
    "publish_multipart_aborted",
    "create_inbox_group",
    "add_group_item",
    "arrange_inbox_group",
    "list_inbox",
    "get_inbox",
    "inbox_links",
    "claim_inbox",
    "release_inbox",
    "assign_inbox",
    "delete_inbox",
    "restore_inbox",
    "render_receipt",
    "merge_gallery_groups",
    "ensure_identity_gallery_members",
    "authorize_job_resource",
    "unmerge_gallery_groups",
    "bundle_snapshot",
    "publish_research_candidate",
    "verify_bundle_snapshot",
]

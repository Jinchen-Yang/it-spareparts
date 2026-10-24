"""Versioned derivative recipes and permission-checked ephemeral links."""

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
    read_identity,
    response,
    revision,
)
from .media_models import (
    Asset,
    Derivative,
    GalleryEntry,
    GalleryGroup,
    GalleryMember,
    GallerySuppression,
)


def derivative_data(item):
    return {
        "derivative_id": str(item.id),
        "asset_id": str(item.asset_id),
        "recipe_id": item.recipe_id,
        "status": item.status,
        "revision": 1,
    }


def assert_visible(db, entry):
    if entry.status != "gallery":
        raise CatalogError("media.deleted", "图片当前不可访问")
    _assert_not_suppressed(db, entry)


def _assert_not_suppressed(db, entry):
    if entry.origin_part_id:
        member = db.get(GalleryMember, entry.origin_part_id)
        if member is None:
            return
        group = load(db, GalleryGroup, member.group_id, lock=True, shared=True)
        hidden = db.scalar(
            select(GallerySuppression.id).where(
                GallerySuppression.group_id == group.id,
                GallerySuppression.content_hash == entry.content_hash,
                GallerySuppression.active.is_(True),
            )
        )
        if hidden:
            raise CatalogError("media.deleted", "图片当前已隐藏")


def assert_previewable(db, entry, *, context, authorize):
    """Only the two read ports may preview explicitly authorized candidates.

    The caller holds the identity and entry read locks. Derivation, job access
    and worker publication keep the stricter gallery-only assert_visible check.
    """
    access(db, context, authorize, "gallery.read", entry.origin_part_id)
    if entry.status == "candidate":
        access(db, context, authorize, "gallery.candidate.read", entry.origin_part_id)
        _assert_not_suppressed(db, entry)
    else:
        assert_visible(db, entry)


def create_derivative(db, asset_id, command, *, context, authorize):
    entry = load(db, GalleryEntry, command.entry_id)
    handle = begin(
        db,
        context=context,
        authorize=authorize,
        operation="gallery.derive",
        part_id=entry.origin_part_id,
        resource="media.derivative",
        identifier=asset_id,
        command=command,
    )
    access(db, context, authorize, "gallery.read", entry.origin_part_id)
    from .media import render_receipt

    if handle.receipt:
        return render_receipt(
            db, handle.receipt, context=context, authorize=authorize, replayed=True
        )
    states = identity(db, [entry.origin_part_id])
    entry = load(db, GalleryEntry, command.entry_id, lock=True)
    if entry.asset_id != asset_id:
        raise CatalogError("auth.forbidden", "图片来源不匹配", status_code=403)
    assert_visible(db, entry)
    revision(context, entry.revision, command.expected_revision)
    asset = load(db, Asset, asset_id)
    key_lock(db, "derivative", str(asset_id), command.recipe_id)
    item = db.scalar(
        select(Derivative).where(
            Derivative.asset_id == asset_id, Derivative.recipe_id == command.recipe_id
        )
    )
    if item is None:
        item = Derivative(
            id=uuid4(),
            asset_id=asset_id,
            recipe_id=command.recipe_id,
            object_key=f"thumb/pn/{asset.id}/{asset.sha256}/{command.recipe_id}/thumbnail.webp",
            status="pending",
        )
        db.add(item)
        db.flush()
    input_revision = (
        states[entry.origin_part_id].identity_revision if entry.origin_part_id else 0
    )
    if item.status != "ready":
        jobs.enqueue(
            db,
            JobSpec(
                kind="media.thumbnail",
                queue="media",
                resource_type="media.entry",
                resource_id=str(entry.id),
                part_id=entry.origin_part_id,
                resource_generation=entry.generation,
                input_revision=input_revision,
                payload={
                    "derivative_id": str(item.id),
                    "asset_id": str(asset.id),
                    "recipe_id": item.recipe_id,
                    "source_key": asset.object_key,
                    "target_key": item.object_key,
                    "expected_sha256": asset.sha256,
                    "expected_bytes": asset.size_bytes,
                },
                deduplication_key=f"thumbnail:{entry.id}:{entry.generation}:{input_revision}:{command.recipe_id}",
            ),
            context=context,
        )
    receipt = finish(
        db,
        handle,
        part_id=entry.origin_part_id,
        identifier=item.id,
        before=0,
        after=1,
        changes={"recipe_id": item.recipe_id, "entry_id": str(entry.id)},
        identity_revision=input_revision,
        result_fields={"entry_id": str(entry.id)},
    )
    return response(db, receipt, derivative_data(item), replayed=False)


def verify_derivative_resource(db, lease, *, authorize):
    entry = load(db, GalleryEntry, UUID(lease.resource_id))
    states = identity(db, [entry.origin_part_id])
    entry = load(db, GalleryEntry, entry.id, lock=True)
    context = CommandContext.model_validate_json(json.dumps(lease.context))
    access(db, context, authorize, "gallery.derive", entry.origin_part_id, write=True)
    access(db, context, authorize, "gallery.read", entry.origin_part_id)
    try:
        assert_visible(db, entry)
    except CatalogError as error:
        if error.code == "media.deleted":
            return None
        raise
    return entry.generation, states[
        entry.origin_part_id
    ].identity_revision if entry.origin_part_id else 0


def publish_derivative(db, lease, _prepared=None):
    item = load(db, Derivative, UUID(lease.payload["derivative_id"]), lock=True)
    entry = load(db, GalleryEntry, UUID(lease.resource_id))
    if (
        item.asset_id != entry.asset_id
        or item.object_key != lease.payload["target_key"]
    ):
        raise CatalogError(
            "request.precondition_failed", "派生图输入已变化", status_code=412
        )
    item.status = "ready"
    return {"derivative_id": str(item.id)}


def entry_links(db, entry_id, *, context, authorize, signer):
    """Signer is a local URL signer, never a network download or durable receipt cache."""
    entry = load(db, GalleryEntry, entry_id, code="media.entry_not_found")
    access(db, context, authorize, "gallery.read", entry.origin_part_id)
    read_identity(db, [entry.origin_part_id])
    entry = load(db, GalleryEntry, entry_id, lock=True, shared=True)
    assert_previewable(db, entry, context=context, authorize=authorize)
    asset = load(db, Asset, entry.asset_id)
    derivatives = list(
        db.scalars(
            select(Derivative).where(
                Derivative.asset_id == asset.id, Derivative.status == "ready"
            )
        )
    )
    return {
        "entry_id": str(entry.id),
        "asset_id": str(asset.id),
        "url": signer.signed_get(asset.object_key, expires_in=300),
        "derivatives": [
            {
                "recipe_id": d.recipe_id,
                "url": signer.signed_get(d.object_key, expires_in=300),
            }
            for d in derivatives
        ],
        "expires_at": (now() + timedelta(seconds=300)).isoformat(),
    }


def get_entry(db, entry_id, *, context, authorize):
    """Read one visible immutable-origin entry, including its current independent revision."""
    from .media_common import read_response
    from .media_gallery import entry_data
    entry = load(db, GalleryEntry, entry_id, code="media.entry_not_found")
    access(db, context, authorize, "gallery.read", entry.origin_part_id)
    read_identity(db, [entry.origin_part_id])
    entry = load(db, GalleryEntry, entry_id, lock=True, shared=True)
    assert_previewable(db, entry, context=context, authorize=authorize)
    return read_response(context, entry_data(db, entry))

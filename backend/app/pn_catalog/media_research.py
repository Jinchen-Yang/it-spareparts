"""Trusted research publication and bundle read ports; no provider/network implementation."""

import json
import re
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select

from . import api
from .errors import CatalogError
from .media_common import access, identity, load, read_identity
from .media_gallery import group_for, reconcile, visible_entries
from .media_models import Asset, GalleryEntry, GalleryGroup, GalleryMember


def publish_research_candidate(
    db,
    part_id,
    verified,
    *,
    source_job_id,
    image_index,
    expected_identity_revision,
    expected_gallery_revision,
    provenance,
    context,
    authorize,
):
    decision = access(db, context, authorize, "gallery.research", part_id, write=True)
    access(db, context, authorize, "gallery.read", part_id)
    states = identity(db, [part_id], expected={part_id: expected_identity_revision})
    group = group_for(db, part_id)
    if group.revision != expected_gallery_revision:
        raise CatalogError("gallery.version_conflict", "研究期间图库已变化")
    allowed = {"source_url", "provider", "model", "run_id", "template_version"}
    if (
        set(provenance) - allowed
        or len(json.dumps(provenance).encode()) > 4096
        or any(
            not isinstance(value, str) or len(value) > 2048
            for value in provenance.values()
        )
    ):
        raise CatalogError(
            "request.validation_failed", "研究来源证据无效", status_code=422
        )
    source_url = urlsplit(provenance.get("source_url", ""))
    if (
        source_url.scheme != "https"
        or not source_url.hostname
        or source_url.username
        or source_url.password
    ):
        raise CatalogError(
            "request.validation_failed", "研究来源 URL 无效", status_code=422
        )
    if (
        type(image_index) is not int
        or not 0 <= image_index < 200
        or (
            not re.fullmatch(r"[a-f0-9]{64}", verified.sha256)
            or verified.object_key
            != f"img/pn-research/{source_job_id}/{image_index}/{verified.sha256}/original"
            or not 0 < verified.size_bytes <= 20 * 1024 * 1024
            or not 0 < verified.width * verified.height <= 40_000_000
            or verified.width <= 0
            or verified.height <= 0
            or not verified.source_etag
            or verified.content_type not in {"image/jpeg", "image/png", "image/webp"}
        )
    ):
        raise CatalogError(
            "media.invalid_image", "研究图片验图结果无效", status_code=422
        )
    matches = list(
        db.scalars(
            select(GalleryEntry)
            .where(
                GalleryEntry.origin_part_id == part_id,
                GalleryEntry.content_hash == verified.sha256,
            )
            .with_for_update()
        )
    )
    if any(entry.status == "deleted" for entry in matches):
        raise CatalogError("media.deleted", "该来源图片已被删除，研究不能恢复它")
    existing = next((entry for entry in matches if entry.purpose == "common"), None)
    if existing:
        return {
            "entry_id": str(existing.id),
            "asset_id": str(existing.asset_id),
            "status": existing.status,
            "gallery_revision": group.revision,
            "created": False,
        }
    stable = f"pn-research/{source_job_id}/{image_index}"
    asset = Asset(
        id=uuid5(NAMESPACE_URL, stable + "/asset"),
        object_key=verified.object_key,
        sha256=verified.sha256,
        etag=verified.source_etag,
        size_bytes=verified.size_bytes,
        content_type=verified.content_type,
        width=verified.width,
        height=verified.height,
        owner_principal_id=context.actor.principal_id,
        source_namespace="research_job",
        source_id=f"{source_job_id}:{image_index}",
    )
    db.add(asset)
    db.flush()
    entry = GalleryEntry(
        id=uuid5(NAMESPACE_URL, stable + "/entry"),
        asset_id=asset.id,
        origin_part_id=part_id,
        origin_namespace="pn_catalog",
        origin_id=str(part_id),
        content_hash=asset.sha256,
        purpose="common",
        status="candidate",
        revision=1,
        generation=1,
        source={
            "principal_id": context.actor.principal_id,
            "source_job_id": str(source_job_id),
            "image_index": image_index,
            "provenance": dict(provenance),
        },
    )
    db.add(entry)
    db.flush()
    before = group.revision
    reconcile(db, group)
    api.record_audit(
        db,
        part_id,
        context=context,
        action="gallery.research_candidate",
        changes={
            "entry_id": str(entry.id),
            "asset_id": str(asset.id),
            "source_job_id": str(source_job_id),
            "status": "candidate",
            "provenance": dict(provenance),
        },
        before_revision=before,
        after_revision=group.revision,
        authorization=decision.metadata,
        revisions={
            "axis": "gallery",
            "before": {"gallery": before},
            "after": {
                "gallery": group.revision,
                "identity": states[part_id].identity_revision,
            },
        },
    )
    return {
        "entry_id": str(entry.id),
        "asset_id": str(asset.id),
        "status": "candidate",
        "gallery_revision": group.revision,
        "created": True,
    }


def bundle_snapshot(db, part_id, *, grade, entry_ids=None, context, authorize):
    if grade not in {"good", "bad"} or (
        entry_ids is not None
        and (
            not entry_ids
            or len(entry_ids) > 1000
            or len(entry_ids) != len(set(entry_ids))
        )
    ):
        raise CatalogError("request.validation_failed", "打包范围无效", status_code=422)
    access(db, context, authorize, "gallery.bundle", part_id)
    states = read_identity(db, [part_id])
    member = db.get(GalleryMember, part_id)
    if member is None:
        raise CatalogError("media.entry_not_found", "没有可打包图片", status_code=404)
    group = load(db, GalleryGroup, member.group_id, lock=True, shared=True)
    for origin_part_id in db.scalars(
        select(GalleryMember.part_id).where(GalleryMember.group_id == group.id)
    ):
        access(db, context, authorize, "gallery.read", origin_part_id)
    entries = visible_entries(
        db, group, purpose="common" if grade == "good" else None, entry_ids=entry_ids
    )
    if entry_ids is not None:
        by_id = {entry.id: entry for entry in entries}
        if set(entry_ids) - by_id.keys():
            raise CatalogError(
                "media.entry_not_found",
                "打包图片已删除、隐藏或不符合用途",
                status_code=404,
            )
        entries = [by_id[identifier] for identifier in entry_ids]
    if not entries or len(entries) > 1000:
        raise CatalogError(
            "request.validation_failed",
            "每次打包应选择 1 至 1000 张图片",
            status_code=422,
        )
    assets = {
        asset.id: asset
        for asset in db.scalars(
            select(Asset).where(Asset.id.in_({entry.asset_id for entry in entries}))
        )
    }
    if sum(assets[entry.asset_id].size_bytes for entry in entries) > 1024 * 1024 * 1024:
        raise CatalogError(
            "media.file_too_large", "打包文件总量超过限制", status_code=413
        )
    values = []
    for entry in entries:
        asset = assets[entry.asset_id]
        filename = (
            str(entry.filename or f"{entry.id}.jpg").replace("\\", "/").split("/")[-1]
        )
        if (
            not filename
            or filename in {".", ".."}
            or len(filename) > 255
            or any(ord(char) < 32 for char in filename)
        ):
            filename = f"{entry.id}.jpg"
        values.append(
            {
                "entry_id": str(entry.id),
                "asset_id": str(asset.id),
                "generation": entry.generation,
                "revision": entry.revision,
                "object_key": asset.object_key,
                "sha256": asset.sha256,
                "size_bytes": asset.size_bytes,
                "content_type": asset.content_type,
                "filename": filename,
                "purpose": entry.purpose,
            }
        )
    return {
        "part_id": part_id,
        "grade": grade,
        "identity_revision": states[part_id].identity_revision
        if states[part_id]
        else 0,
        "gallery_revision": group.revision,
        "group_id": str(group.id),
        "entries": values,
    }


def verify_bundle_snapshot(db, snapshot, *, context, authorize):
    from uuid import UUID

    current = bundle_snapshot(
        db,
        snapshot["part_id"],
        grade=snapshot["grade"],
        entry_ids=[UUID(entry["entry_id"]) for entry in snapshot["entries"]],
        context=context,
        authorize=authorize,
    )
    if current != snapshot:
        raise CatalogError("gallery.version_conflict", "打包期间图库或图片身份已变化")
    return current

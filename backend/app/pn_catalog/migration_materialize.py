"""Materialize frozen legacy images/content; original objects and inventory remain untouched."""
from datetime import datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Integer, func, select, text

from . import api
from .content_dto import ContentStatus, Grade, LegacyEffectiveContent
from .dto import Provenance
from .errors import CatalogError
from .media_models import Asset, Derivative, GalleryEntry, GalleryGroup, GalleryMember
from .migration import _full_archive_access
from .migration_dto import fingerprint
from .migration_models import LegacySourceRecord, MigrationBatch
from .models import PartState
from .persistence import acquire_identity_lock, lock_part


class LegacyImage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: UUID
    pn_id: UUID
    oss_key: str = Field(min_length=1, max_length=1024)
    thumb_key: str | None = Field(default=None, max_length=1024)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    bytes: int = Field(gt=0)
    content_type: str = Field(min_length=1, max_length=128)
    status: str = Field(pattern=r"^(gallery|candidate|processing|deleted)$")
    sort_order: int = Field(ge=0)
    is_defect: bool
    thumbnail_status: str = Field(pattern=r"^(pending|ready|failed)$")
    uploaded_by: UUID | None = None
    deleted_at: datetime | None = None
    created_at: datetime | None = None


class LegacyContent(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: UUID
    pn_id: UUID
    grade: Grade
    title: str | None = Field(max_length=200)
    description: str | None = Field(max_length=20000)
    desc_status: ContentStatus


def _image(db, row):
    from pydantic import ValidationError
    try:
        source = LegacyImage.model_validate(row.payload)
    except ValidationError as exc:
        raise CatalogError("migration.invalid_record", "历史图片字段不完整，需核对原始记录", status_code=422) from exc
    if str(source.id) != row.source_id or source.pn_id != row.source_pn_id:
        raise CatalogError("migration.record_conflict", "图片来源编号与清单不一致")
    # Stable one-to-one asset mapping; physical same-hash deduplication is deliberately deferred.
    asset_id = uuid5(NAMESPACE_URL, f"pn-catalog/legacy/{row.namespace}/image/{source.id}")
    existing = db.get(GalleryEntry, source.id)
    if existing is not None:
        raise CatalogError("migration.record_conflict", "图片编号已被另一条目占用")
    asset = Asset(id=asset_id, object_key=source.oss_key, sha256=source.sha256, etag=None,
        verification_status="legacy_metadata", size_bytes=source.bytes, content_type=source.content_type,
        width=source.width, height=source.height,
        owner_principal_id=f"legacy:{row.namespace}:{source.uploaded_by}" if source.uploaded_by else "legacy:unknown",
        source_namespace=row.namespace, source_id=str(source.id))
    db.add(asset)
    db.flush()
    entry = GalleryEntry(id=source.id, asset_id=asset.id, origin_part_id=row.target_part_id,
        origin_namespace=row.namespace, origin_id=str(source.pn_id), content_hash=source.sha256,
        purpose="defect" if source.is_defect else "common", status=source.status,
        revision=1, generation=1, deleted_at=source.deleted_at,
        source={"legacy_record_id": row.id, "source_image_id": row.source_id,
                "source_pn_id": str(source.pn_id), "historical_actor_id": str(source.uploaded_by) if source.uploaded_by else None,
                "sort_order": source.sort_order, "filename": row.payload.get("filename"),
                "file_no": row.payload.get("file_no"), "original_filename": row.payload.get("original_filename"),
                "kind": row.payload.get("source"), "source_url": row.payload.get("source_url"),
                "has_foreign_watermark": row.payload.get("has_foreign_watermark"),
                "verification": "legacy_metadata"})
    if source.created_at is not None:
        if source.created_at.tzinfo is None:
            raise CatalogError("migration.invalid_record", "历史图片时间缺少时区", status_code=422)
        entry.created_at = source.created_at
    db.add(entry)
    if source.thumb_key:
        db.add(Derivative(id=uuid5(asset.id, "legacy-thumbnail-v1"), asset_id=asset.id,
            recipe_id="legacy-thumbnail-v1", object_key=source.thumb_key, status=source.thumbnail_status))
    row.materialized_ref = {"entry_id": str(entry.id), "asset_id": str(asset.id), "verified_bytes": False}
    if row.target_part_id is not None:
        row.state = "materialized"


def _content(db, row, *, context, authorize):
    from pydantic import ValidationError
    try:
        source = LegacyContent.model_validate(row.payload)
    except ValidationError as exc:
        raise CatalogError("migration.invalid_record", "历史商品描述字段不完整", status_code=422) from exc
    if str(source.id) != row.source_id or source.pn_id != row.source_pn_id:
        raise CatalogError("migration.record_conflict", "商品描述来源编号与清单不一致")
    migrated = api.migrate_effective_content(db, row.target_part_id, LegacyEffectiveContent(
        grade=source.grade, expected_identity_revision=row.mapping["expected_identity_revision"],
        source_namespace=row.namespace, source_record_id=row.source_id,
        source_revision=row.source_revision or f"sha256:{row.payload_sha256}", title=source.title,
        description=source.description, status=source.desc_status,
        provenance=Provenance(kind="ai" if source.desc_status == "ai_filled" else "unknown"),
        protected_fields=["description"] if source.desc_status == "confirmed" else []),
        context=context.model_copy(update={"request_id": uuid5(row.batch_id, f"content:{row.source_id}")}),
        authorize=authorize)
    row.state = "materialized"
    row.materialized_ref = {"part_id": row.target_part_id, "grade": source.grade,
                            "revision": migrated["data"]["revision"]}


def _arrange_imported(db, part_id):
    member = db.get(GalleryMember, part_id)
    if member is None:
        group = GalleryGroup(id=uuid5(NAMESPACE_URL, f"pn-catalog/gallery/{part_id}"),
                             canonical_part_id=part_id, revision=0, ordered_entry_ids=[])
        db.add(group)
        db.flush()
        db.add(GalleryMember(part_id=part_id, group_id=group.id))
    else:
        group = db.get(GalleryGroup, member.group_id)
        members = set(db.scalars(select(GalleryMember.part_id).where(GalleryMember.group_id == group.id)))
        if group.canonical_part_id != part_id or members != {part_id}:
            raise CatalogError("migration.gallery_conflict", "目标图库已发生合并，需重新核对")
        imported_revision = db.scalar(select(func.max(
            LegacySourceRecord.materialized_ref["gallery_revision"].as_string().cast(Integer))).where(
            LegacySourceRecord.kind == "image", LegacySourceRecord.id.in_(
                select(GalleryEntry.source["legacy_record_id"].as_string().cast(Integer)).where(
                    GalleryEntry.origin_part_id == part_id)))) or 0
        if group.revision != imported_revision:
            raise CatalogError("migration.gallery_conflict", "目标图库排序已变化，不能覆盖")
    all_entries = list(db.scalars(select(GalleryEntry).where(GalleryEntry.origin_part_id == part_id)))
    if any(not entry.source.get("legacy_record_id") or entry.revision != 1 for entry in all_entries):
        raise CatalogError("migration.gallery_conflict", "目标图库已被编辑，不能重新排列迁移数据")
    entries = [entry for entry in all_entries if entry.status == "gallery"]
    entries.sort(key=lambda entry: (entry.origin_namespace, entry.origin_id,
                                    entry.source.get("sort_order", 0), entry.created_at, str(entry.id)))
    group.ordered_entry_ids = [str(entry.id) for entry in entries]
    group.primary_entry_id = entries[0].id if entries else None
    group.revision += 1
    return group.revision


def materialize_batch(db, batch_id: UUID, *, context, authorize):
    _full_archive_access(db, context, authorize, "materialize_migration", writing=True)
    if context.actor.entrypoint != "migration" or context.actor.provenance.kind != "migration":
        raise CatalogError("auth.forbidden", "迁移只能由已授权的迁移入口执行", status_code=403)
    handle = api.begin_command(db, context=context, authorize=authorize, operation="materialize_migration",
        part_id=None, resource_type="migration", resource_id=batch_id, payload={"batch_id": str(batch_id)})
    batch = db.get(MigrationBatch, batch_id)
    if batch is None:
        raise CatalogError("migration.batch_not_found", "迁移批次不存在", status_code=404)
    if handle.receipt:
        return _result(db, batch, replayed=True)
    from .migration_state import _namespace_lock
    _namespace_lock(db, batch.namespace)
    acquire_identity_lock(db, shared=True)
    rows = list(db.scalars(select(LegacySourceRecord).where(LegacySourceRecord.batch_id == batch_id)
                           .order_by(LegacySourceRecord.id)))
    targets = {row.target_part_id for row in rows if row.target_part_id is not None}
    for part_id in sorted(targets):
        part = lock_part(db, part_id)
        state = db.get(PartState, part_id, populate_existing=True)
        current = state.identity_revision if state else 0
        if part.status != "active" or any(row.mapping["expected_identity_revision"] != current
                                         for row in rows if row.target_part_id == part_id):
            raise CatalogError("catalog.identity_changed", "迁移目标身份已变化")
    # Lock batch after sorted parents. Metadata insertion is already serialized by source.
    batch = db.scalar(select(MigrationBatch).where(MigrationBatch.id == batch_id).with_for_update()
                       .execution_options(populate_existing=True))
    db.expire_all()
    changed_parts = set()
    for row in rows:
        if fingerprint(row.payload) != row.payload_sha256:
            raise CatalogError("migration.source_changed", "已留档来源与冻结指纹不一致")
        if row.materialized_ref is not None:
            continue
        if row.kind == "image":
            _image(db, row)
            if row.target_part_id is not None:
                changed_parts.add(row.target_part_id)
        elif row.kind == "content" and row.target_part_id is not None:
            _content(db, row, context=context, authorize=authorize)
    db.flush()
    for part_id in sorted(changed_parts):
        gallery_revision = _arrange_imported(db, part_id)
        for row in rows:
            if row.kind == "image" and row.target_part_id == part_id and row.materialized_ref:
                row.materialized_ref = {**row.materialized_ref, "gallery_revision": gallery_revision}
    if all(row.state in {"materialized", "historical"} for row in rows):
        batch.status = "materialized"
    event = api.record_audit(db, None, context=context, action="migration.materialize",
        changes={"batch_id": str(batch.id), "status": batch.status, "record_count": len(rows)},
        before_revision=0, after_revision=1, authorization=handle.decision.metadata,
        payload_hash=handle.payload_hash, revisions={"axis": "migration", "before": {"migration": 0}, "after": {"migration": 1}})
    api.finish_command(db, handle, part_id=None, applied_revision=1, audit_id=event.id,
                       result={"resource": "migration", "batch_id": str(batch.id)})
    return _result(db, batch, replayed=False)


def _result(db, batch, *, replayed):
    states = db.execute(text("SELECT state, count(*) FROM pn_catalog_legacy_record WHERE batch_id=:id GROUP BY state"),
                        {"id": batch.id}).all()
    counts = dict(states)
    return {"batch_id": str(batch.id), "status": batch.status, "states": counts,
            "batch_complete": not any(counts.get(state, 0) for state in ("pending", "unresolved", "conflict")),
            "cutover_ready": False,  # Global conservation, ownership and worker fences are separate gates.
            "replayed": replayed}

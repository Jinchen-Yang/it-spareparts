"""Usable historical results, with current visibility and no revived worker authority."""

from datetime import timedelta
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import func, select

from . import api
from .enrichment_dto import RecognitionResult
from .errors import CatalogError
from .media_models import GalleryEntry
from .migration_media import _date
from .migration_models import LegacySourceRecord
from .migration_state_models import MigrationRecordApplication


def recognition_projection(source):
    """Keep hints, never old matched-PN payloads or provider costs; matching is current."""
    if source.get("recognition_status") != "done":
        return None
    raw = source.get("candidates", [])
    category, confidence = None, None
    if isinstance(raw, dict):
        category, confidence = raw.get("item_category"), raw.get("category_confidence")
        raw = raw.get("items", [])
    elif isinstance(raw, list) and raw and isinstance(raw[0], dict):
        category, confidence = (
            raw[0].get("item_category"),
            raw[0].get("category_confidence"),
        )
    if not isinstance(raw, list) or any(not isinstance(value, dict) for value in raw):
        raise CatalogError(
            "migration.invalid_recognition", "历史识别候选格式无效", status_code=422
        )
    try:
        result = RecognitionResult(
            candidates=[
                {
                    "pn": item.get("raw_pn") or item.get("pn_norm"),
                    "confidence": item.get("confidence"),
                }
                for item in raw
            ],
            item_category=category,
            category_confidence=confidence,
            # Explicit absence of historic provider metadata, never a guessed provider.
            provider="legacy",
            model="unrecorded",
            tokens=None,
        )
    except ValidationError as exc:
        raise CatalogError(
            "migration.invalid_recognition",
            "历史识别候选超出安全投影范围",
            status_code=422,
        ) from exc
    stamp = _date(source.get("updated_at"))
    return {
        "status": "succeeded",
        "result": result.model_dump(mode="json"),
        "completed_at": stamp.isoformat() if stamp else None,
    }


def inbox_recognitions(db, items):
    """One bounded join; caller has already applied current inbox read/owner checks."""
    ids = {
        str(item.id): item.id
        for item in items
        if item.status in {"ready", "assigned"} and item.generation == 1
    }
    if not ids:
        return {}
    rows = db.execute(
        select(
            LegacySourceRecord.source_id,
            MigrationRecordApplication.target_ref["recognition"],
        )
        .join(
            MigrationRecordApplication,
            MigrationRecordApplication.source_record_id == LegacySourceRecord.id,
        )
        .where(
            LegacySourceRecord.kind == "inbox",
            LegacySourceRecord.source_id.in_(ids),
            MigrationRecordApplication.state.in_(["applied", "recovery_required"]),
        )
    ).all()
    results = {}
    for source_id, recognition in rows:
        if not recognition:
            continue
        try:
            value = RecognitionResult.model_validate(recognition["result"]).model_dump(
                mode="json"
            )
            results[ids[source_id]] = {
                "status": "succeeded",
                "result": value,
                "completed_at": recognition.get("completed_at"),
            }
        except (ValidationError, KeyError, TypeError):
            continue
    return results


def bundle_downloads(db, record_id, *, context, authorize, storage):
    from .migration_history import HistoryProjection, _authorize
    from .media import bundle_snapshot

    application = db.get(MigrationRecordApplication, record_id)
    if (
        application is None
        or not application.target_ref
        or "history" not in application.target_ref
    ):
        raise CatalogError(
            "migration.record_not_found", "历史压缩包不存在", status_code=404
        )
    history = HistoryProjection.model_validate(application.target_ref["history"])
    _authorize(db, history, context, authorize)
    if (
        history.kind != "bundle"
        or history.legacy_status != "done"
        or history.part_id is None
    ):
        raise CatalogError("enrichment.bundle_unavailable", "历史压缩包尚不可用")
    grant = api._authorize(
        db, context, authorize, "enrichment.bundle.read", history.part_id
    )
    if not api._allowed(grant.readable_fields, "gallery"):
        raise CatalogError("auth.forbidden", "没有图片读取权限", status_code=403)
    source = db.get(LegacySourceRecord, record_id)
    value = source.payload
    instant = db.scalar(select(func.clock_timestamp()))
    expiry, created = _date(value.get("expires_at")), _date(value.get("created_at"))
    if expiry is None or created is None:
        raise CatalogError("enrichment.bundle_unavailable", "历史压缩包缺少有效期证据")
    ttl = min(
        900, int((min(expiry, created + timedelta(days=1)) - instant).total_seconds())
    )
    if ttl < 1:
        raise CatalogError(
            "enrichment.bundle_expired",
            "历史压缩包已过期，请按当前图库重新打包",
            status_code=410,
        )
    images, archives = value.get("images"), value.get("archives")
    try:
        if not isinstance(images, list) or not 1 <= len(images) <= 1000:
            raise ValueError()
        entry_ids = [UUID(image["image_id"]) for image in images]
        if not isinstance(archives, list) or not 1 <= len(archives) <= 100:
            raise ValueError()
        for number, archive in enumerate(archives, start=1):
            if (
                archive["part"] != number
                or type(archive["bytes"]) is not int
                or archive["bytes"] <= 0
                or archive["oss_key"]
                != f"bundle/{UUID(source.source_id)}/part-{number}.zip"
                or type(archive["image_count"]) is not int
                or archive["image_count"] <= 0
                or not isinstance(archive["sha256"], str)
                or len(archive["sha256"]) != 64
                or any(c not in "0123456789abcdef" for c in archive["sha256"])
                or not isinstance(archive["filename"], str)
                or not 1 <= len(archive["filename"]) <= 255
                or any(ord(c) < 32 or c in "/\\" for c in archive["filename"])
            ):
                raise ValueError()
        if sum(a["image_count"] for a in archives) != len(images):
            raise ValueError()
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise CatalogError(
            "migration.invalid_bundle", "历史压缩包证据不完整", status_code=422
        ) from exc
    current = bundle_snapshot(
        db,
        history.part_id,
        grade=value.get("grade"),
        entry_ids=entry_ids,
        context=context,
        authorize=authorize,
    )
    origins = dict(
        db.execute(
            select(GalleryEntry.id, GalleryEntry.origin_namespace).where(
                GalleryEntry.id.in_(entry_ids)
            )
        ).all()
    )
    for original, entry in zip(images, current["entries"], strict=True):
        if (
            origins[UUID(entry["entry_id"])] != source.namespace
            or original.get("oss_key") != entry["object_key"]
            or original.get("sha256") != entry["sha256"]
            or original.get("bytes") != entry["size_bytes"]
            or original.get("content_type") != entry["content_type"]
            or bool(original.get("is_defect")) != (entry["purpose"] == "defect")
        ):
            raise CatalogError(
                "gallery.version_conflict", "历史打包图片与当前图库不一致"
            )
    # Signing is local cryptography. No provider call and no stored URL or public object key.
    return {
        "data": {
            "source_record_id": record_id,
            "expires_in": ttl,
            "archives": [
                {
                    "number": a["part"],
                    "filename": a["filename"],
                    "size_bytes": a["bytes"],
                    "image_count": a["image_count"],
                    "sha256": a["sha256"],
                    "url": storage.signed_get(
                        a["oss_key"], expires_in=ttl, download_filename=a["filename"]
                    ),
                }
                for a in archives
            ],
        },
        "meta": {"request_id": str(context.request_id), "replayed": False},
    }

"""SQL paging and byte budgets apply equally to reads and current write receipts."""

import json
from uuid import uuid4

from sqlalchemy import event

from app.pn_catalog import api, media
from app.pn_catalog.errors import CatalogError
from app.pn_catalog.media_dto import ArrangeGallery
from app.pn_catalog.media_models import Asset, GalleryEntry, GalleryGroup, GalleryMember
from tests.test_pn_catalog_media import allow, context, seed


def large_gallery(db, pid, count=230):
    ids = []
    for index in range(count):
        asset_id, entry_id = uuid4(), uuid4()
        sha = f"{index:064x}"
        db.add(
            Asset(
                id=asset_id,
                object_key=f"img/{asset_id}",
                sha256=sha,
                etag="etag",
                size_bytes=10,
                content_type="image/jpeg",
                width=1,
                height=1,
                owner_principal_id="account:42",
                source_namespace="test",
                source_id=str(asset_id),
            )
        )
        db.flush()
        db.add(
            GalleryEntry(
                id=entry_id,
                asset_id=asset_id,
                origin_part_id=pid,
                origin_namespace="shop",
                origin_id=str(pid),
                content_hash=sha,
                purpose="common",
                status="gallery",
                revision=1,
                generation=1,
                source={"legacy_evidence": "a" * 16000},
            )
        )
        ids.append(entry_id)
    group = GalleryGroup(
        id=uuid4(),
        canonical_part_id=pid,
        revision=1,
        ordered_entry_ids=[str(item) for item in ids],
        primary_entry_id=ids[0],
    )
    db.add(group)
    db.flush()
    db.add(GalleryMember(part_id=pid, group_id=group.id))
    db.commit()
    db.expunge_all()
    return ids


def test_large_gallery_only_loads_page_payload_and_receipts_keep_next_cursor(db):
    pid = seed(db)
    ids = large_gallery(db, pid)
    loaded = []

    def capture(session, item):
        if isinstance(item, GalleryEntry):
            loaded.append(item.id)

    event.listen(db, "loaded_as_persistent", capture)
    try:
        first = media.get_gallery(
            db, pid, context=context(), authorize=allow, limit=200
        )
    finally:
        event.remove(db, "loaded_as_persistent", capture)
    page = first["data"]["entries"]
    assert 0 < len(page) < 50
    assert len(loaded) == len(page)  # no full-group ORM payload hydration
    assert len(json.dumps(first).encode()) < 800 * 1024
    assert first["meta"]["next_cursor"]
    collected, cursor = [row["entry_id"] for row in page], first["meta"]["next_cursor"]
    while cursor:
        result = media.get_gallery(
            db, pid, context=context(), authorize=allow, limit=200, cursor=cursor
        )
        collected += [row["entry_id"] for row in result["data"]["entries"]]
        cursor = result["meta"]["next_cursor"]
    assert collected == [str(item) for item in ids]
    ctx = context()
    result = media.arrange_gallery(
        db,
        pid,
        ArrangeGallery(
            expected_gallery_revision=1,
            ordered_entry_ids=ids[::-1],
            primary_entry_id=ids[-1],
        ),
        context=ctx,
        authorize=allow,
    )
    db.commit()
    assert result["meta"]["next_cursor"]
    assert result["data"]["entries"][0]["entry_id"] == str(ids[-1])
    assert len(json.dumps(result).encode()) < 800 * 1024
    receipt = api.get_receipt(db, ctx.request_id, context=context(), authorize=allow)
    assert receipt["meta"]["next_cursor"] == result["meta"]["next_cursor"]
    assert len(json.dumps(receipt).encode()) < 800 * 1024


def test_duplicate_selection_filters_current_origin_grants_before_deduplication(db):
    target, source = seed(db, "TARGET"), seed(db, "SOURCE")
    target_ids, source_ids = (
        large_gallery(db, target, count=1),
        large_gallery(db, source, count=1),
    )
    media.merge_gallery_groups(db, source, target)
    db.commit()

    def source_only(session, ctx, action, part_id):
        if part_id == target:
            raise CatalogError(
                "auth.forbidden", "target origin hidden", status_code=403
            )
        return allow()

    page = media.get_gallery(db, source, context=context(), authorize=source_only)
    assert [row["entry_id"] for row in page["data"]["entries"]] == [str(source_ids[0])]
    assert str(target_ids[0]) not in json.dumps(page)

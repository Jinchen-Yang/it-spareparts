"""Group-wide suppression has bounded discovery and cannot bypass origin grants."""
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.pn_catalog import media
from app.pn_catalog.authz import CatalogAccess
from app.pn_catalog.dto import AuthorizationDecision
from app.pn_catalog.errors import CatalogError
from app.pn_catalog.http import install_http
from app.pn_catalog.media_dto import ExpectedRevision, SuppressHash
from app.pn_catalog.media_models import GalleryGroup, GalleryMember, GallerySuppression
from tests.test_pn_catalog_media import allow, context, seed


def group(db, *parts):
    row = GalleryGroup(id=uuid4(), canonical_part_id=parts[0], revision=1, ordered_entry_ids=[])
    db.add(row)
    db.flush()
    for pid in parts:
        db.add(GalleryMember(part_id=pid, group_id=row.id))
    db.commit()
    return row.id


def hide(db, gid, number, *, ctx=None):
    current = db.get(GalleryGroup, gid)
    return media.suppress_hash(db, gid, SuppressHash(
        expected_gallery_revision=current.revision, content_hash=f"{number:064x}", reason=f"隐藏 {number}"),
        context=ctx or context(), authorize=allow)


def test_bounded_http_list_contains_active_metadata_only(db):
    gid = group(db, seed(db))
    for i in range(3):
        hide(db, gid, i + 1)
    db.commit()
    app = FastAPI()
    install_http(app)
    app.state.pn_catalog_access_resolver = lambda *_: CatalogAccess(context().actor, allow, b"cursor-test-key")
    client = TestClient(app)
    url = f"/api/pn-catalog/v1/gallery-groups/{gid}/suppressions"
    first = client.get(url, params={"limit": 2})
    assert first.status_code == 200, first.text
    page = first.json()
    assert len(page["data"]) == 2
    assert set(page["data"][0]) == {"suppression_id", "group_id", "content_hash", "active", "revision", "reason"}
    second = client.get(url, params={"limit": 2, "cursor": page["meta"]["next_cursor"]}).json()
    assert len(second["data"]) == 1 and second["meta"]["next_cursor"] is None
    assert {r["suppression_id"] for r in page["data"]}.isdisjoint({r["suppression_id"] for r in second["data"]})
    assert client.get(url, params={"limit": 51}).status_code == 422
    assert client.get(url, params={"cursor": "invalid"}).status_code == 422


def test_group_all_origins_read_write_and_receipt_recheck_current_grants(db):
    first, second = seed(db, "SUP-A"), seed(db, "SUP-B")
    gid = group(db, first, second)
    ctx = context()
    command = SuppressHash(expected_gallery_revision=1, content_hash="a" * 64, reason="重复来源")
    result = media.suppress_hash(db, gid, command, context=ctx, authorize=allow)
    db.commit()
    sid = UUID(result["data"]["suppression_id"])

    def no_origin_read(_db, _ctx, action, pid):
        if pid == second and action == "gallery.read":
            return AuthorizationDecision(frozenset(), frozenset(), {"scope": "all"})
        return allow()

    for read in [
        lambda: media.list_suppressions(db, gid, context=context(), authorize=no_origin_read),
        lambda: media.suppress_hash(db, gid, command, context=ctx, authorize=no_origin_read),
    ]:
        with pytest.raises(CatalogError) as failure:
            read()
        assert failure.value.code == "auth.forbidden"
        db.rollback()

    def no_origin_write(_db, _ctx, action, pid):
        if pid == second and action in {"gallery.suppress", "gallery.unsuppress"}:
            return AuthorizationDecision(frozenset({"gallery"}), frozenset(), {"scope": "all"})
        return allow()

    for write in [
        lambda: media.unsuppress_hash(db, gid, sid, ExpectedRevision(expected_revision=1), context=context(), authorize=no_origin_write),
        lambda: media.suppress_hash(db, gid, command, context=ctx, authorize=no_origin_write),
        lambda: media.suppress_hash(db, gid, SuppressHash(expected_gallery_revision=2, content_hash="b" * 64, reason="new"), context=context(), authorize=no_origin_write),
    ]:
        with pytest.raises(CatalogError) as failure:
            write()
        assert failure.value.code == "auth.forbidden"
        db.rollback()
    assert db.get(GallerySuppression, sid).active


def test_cursor_is_bound_to_actor_group_version_and_current_grants(db):
    gid = group(db, seed(db))
    for i in range(2):
        hide(db, gid, i + 1)
    db.commit()
    page = media.list_suppressions(db, gid, context=context(), authorize=allow, limit=1)
    cursor = page["meta"]["next_cursor"]
    db.commit()

    def changed_grant(*_):
        return AuthorizationDecision(frozenset({"gallery"}), frozenset(), {"scope": "all", "version": 2})

    for ctx, auth in [(context("account:other"), allow), (context(), changed_grant)]:
        with pytest.raises(CatalogError):
            media.list_suppressions(db, gid, context=ctx, authorize=auth, cursor=cursor)
        db.rollback()
    sid = UUID(page["data"][0]["suppression_id"])
    media.unsuppress_hash(db, gid, sid, ExpectedRevision(expected_revision=1), context=context(), authorize=allow)
    db.commit()
    with pytest.raises(CatalogError):
        media.list_suppressions(db, gid, context=context(), authorize=allow, cursor=cursor)
    db.rollback()
    fresh = media.list_suppressions(db, gid, context=context(), authorize=allow)
    assert len(fresh["data"]) == 1
    with pytest.raises(CatalogError) as stale:
        media.unsuppress_hash(db, gid, sid, ExpectedRevision(expected_revision=1), context=context(), authorize=allow)
    assert stale.value.code == "gallery.version_conflict"


def test_retired_group_cannot_publish_suppression(db):
    first, second = seed(db, "OLD-G"), seed(db, "NEW-G")
    old, current = group(db, first), group(db, second)
    member = db.scalar(select(GalleryMember).where(GalleryMember.part_id == first))
    member.group_id = current
    db.commit()
    for operation in [
        lambda: media.list_suppressions(db, old, context=context(), authorize=allow),
        lambda: hide(db, old, 1),
    ]:
        with pytest.raises(CatalogError) as failure:
            operation()
        assert failure.value.code == "catalog.identity_changed"
        db.rollback()

"""Shared directory uses current grants and exact IDs, including gallery predicates."""

import pytest
from sqlalchemy import event

from app.models.dimensions import DimPart
from app.pn_catalog import api
from app.pn_catalog.dto import AuthorizationDecision
from app.pn_catalog.errors import CatalogError
from app.pn_catalog.media_models import GalleryEntry, GalleryGroup, GallerySuppression
from app.pn_catalog.models import PartState
from tests.test_pn_catalog_media import context
from tests.test_pn_catalog_media_pagination import large_gallery


def allow(*_args):
    return AuthorizationDecision(
        readable_fields=frozenset({"*"}), metadata={"scope": "all"}
    )


def test_profile_confirmation_status_is_independent_and_read_granted(db):
    part = DimPart(pn_std="PROFILE-STATUS")
    db.add(part)
    db.flush()
    db.add(PartState(part_id=part.id, fill_status="ai_filled", fill_status_revision=3))
    db.commit()
    result = api.list_parts(db, context=context(), authorize=allow)
    assert result["data"][0]["fill_status"] == "ai_filled"
    assert result["data"][0]["fill_status_revision"] == 3
    def restricted(*_args):
        return AuthorizationDecision(readable_fields=frozenset({"description"}), metadata={"scope": "all"})
    hidden = api.list_parts(db, context=context(), authorize=restricted)
    assert "fill_status" not in hidden["data"][0]
    assert "fill_status_revision" not in hidden["data"][0]


def test_directory_exact_ids_and_gallery_use_one_query_with_purpose_and_suppression(db):
    parts = [
        DimPart(pn_std=f"DIRECTORY-{i}", category_major="disk", description="hidden")
        for i in range(3)
    ]
    db.add_all(parts)
    db.commit()
    pids = [part.id for part in parts]
    first = large_gallery(db, pids[0], count=3)
    second = large_gallery(db, pids[1], count=1)
    db.get(GalleryEntry, first[0]).purpose = "defect"
    db.get(GalleryEntry, second[0]).purpose = "defect"
    group = db.query(GalleryGroup).filter_by(canonical_part_id=pids[0]).one()
    group.ordered_entry_ids = [str(first[0]), str(first[2]), str(first[1])]
    db.commit()
    queries = []

    def count(_conn, _cursor, statement, *_rest):
        queries.append(statement)

    event.listen(db.get_bind(), "before_cursor_execute", count)
    try:
        result = api.list_parts(
            db,
            context=context(),
            authorize=allow,
            part_ids=pids[:2],
            category_major="disk",
            include_gallery=True,
        )
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", count)
    assert len(queries) == 1
    assert [row["part_id"] for row in result["data"]] == pids[:2]
    assert result["data"][0]["has_image"] is True
    assert result["data"][0]["main_entry_id"] == first[2]
    assert result["data"][1]["has_image"] is True
    assert result["data"][1]["main_entry_id"] is None
    db.add(
        GallerySuppression(
            group_id=group.id,
            content_hash=db.get(GalleryEntry, first[2]).content_hash,
            active=True,
            reason="test",
        )
    )
    db.flush()
    assert (
        api.list_parts(
            db,
            context=context(),
            authorize=allow,
            part_ids=[pids[0]],
            include_gallery=True,
        )["data"][0]["main_entry_id"]
        == first[1]
    )
    assert [
        r["part_id"]
        for r in api.list_parts(
            db, context=context(), authorize=allow, missing_image=True
        )["data"]
    ] == [pids[2]]
    assert (
        api.list_parts(db, context=context(), authorize=allow, part_ids=[])["data"]
        == []
    )


def test_fill_status_filter_keeps_unrecorded_distinct_and_checks_field_permission(db):
    ids = []
    for index, status in enumerate((None, "pending", "ai_filled", "confirmed", "needs_review")):
        part = DimPart(pn_std=f"STATUS-{index}")
        db.add(part)
        db.flush()
        ids.append(part.id)
        if status:
            db.add(PartState(part_id=part.id, fill_status=status))
    db.commit()
    for index, status in enumerate(("unrecorded", "pending", "ai_filled", "confirmed", "needs_review")):
        result = api.list_parts(db, context=context(), authorize=allow, fill_status=status)
        assert [row["part_id"] for row in result["data"]] == [ids[index]]
    def hidden(*_args):
        return AuthorizationDecision(readable_fields=frozenset({"description"}), metadata={"scope": "all"})
    with pytest.raises(CatalogError) as error:
        api.list_parts(db, context=context(), authorize=hidden, fill_status="pending")
    assert error.value.status_code == 403
    first = api.list_parts(db, context=context(), authorize=allow, limit=1)
    with pytest.raises(CatalogError):
        api.list_parts(db, context=context(), authorize=allow, fill_status="pending", cursor=first["meta"]["next_cursor"])


def test_directory_filters_cannot_probe_hidden_fields_and_cursor_is_filter_bound(db):
    def limited(_db, _ctx, _action, _pid):
        return AuthorizationDecision(
            readable_fields=frozenset({"brand"}), metadata={"scope": "all"}
        )

    for filters in (
        {"category_major": "secret"},
        {"missing_image": True},
        {"include_gallery": True},
    ):
        with pytest.raises(CatalogError) as exc:
            api.list_parts(db, context=context(), authorize=limited, **filters)
        assert exc.value.code == "auth.forbidden"
    db.add_all([DimPart(pn_std=f"CURSOR-{i}") for i in range(3)])
    db.commit()
    first = api.list_parts(db, context=context(), authorize=allow, limit=1)
    with pytest.raises(CatalogError):
        api.list_parts(
            db,
            context=context(),
            authorize=allow,
            limit=1,
            cursor=first["meta"]["next_cursor"],
            include_gallery=True,
        )


def test_old_metadata_token_cannot_bypass_shared_personal_grants(db, monkeypatch):
    import hashlib
    from fastapi import HTTPException
    from pydantic import SecretStr
    from starlette.requests import Request
    from app.authz import runtime
    from app.api.resale_catalog_export import _catalog_key
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "authorization_mode", "unified_modules")
    monkeypatch.setattr(settings, "authz_enabled_resources", ["part_master"])
    token = "source-reader-token" * 3
    monkeypatch.setattr(
        settings,
        "resale_catalog_token_sha256",
        SecretStr(hashlib.sha256(token.encode()).hexdigest()),
    )
    runtime.activate_configured_resources(db.get_bind())
    for suffix in ("list", "resolve", "snapshots", "search"):
        request = Request(
            {
                "type": "http",
                "path": f"/api/integrations/resale/parts/{suffix}",
                "headers": [],
            }
        )
        with pytest.raises(HTTPException) as exc:
            _catalog_key(request, db, f"Bearer {token}")
        assert exc.value.status_code == 410
    # Price evidence is a separate scoped business integration, unchanged.
    request = Request(
        {
            "type": "http",
            "path": "/api/integrations/resale/parts/price-evidence",
            "headers": [],
        }
    )
    _catalog_key(request, db, f"Bearer {token}")
    monkeypatch.setattr(settings, "authz_enabled_resources", [])
    request = Request(
        {"type": "http", "path": "/api/integrations/resale/parts/list", "headers": []}
    )
    with pytest.raises(HTTPException) as exc:
        _catalog_key(request, db, f"Bearer {token}")
    assert exc.value.status_code == 410

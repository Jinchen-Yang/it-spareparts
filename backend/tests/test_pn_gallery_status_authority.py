"""Nonpublic gallery reads use actual PostgreSQL, Cerbos and delegated authority."""
# ruff: noqa: F811 -- imported fixtures are resolved by pytest argument name.

from uuid import uuid4

import pytest
from app.config import get_settings
from app.models.authorization import SysAuthorizationGrant
from app.pn_catalog.media_models import Asset, GalleryEntry, GalleryGroup, GalleryMember
from app.services import pn_catalog_access as access
from sqlalchemy import select

from tests.authz_pdp_helpers import cerbos_url  # noqa: F401
from tests.test_pn_catalog_access import authority, call, identity  # noqa: F401
from tests.test_pn_catalog_authority_http import (  # noqa: F401
    exchange,
    http,
    native,
    seed,
)

STATES = ("candidate", "processing", "deleted")


def populate_gallery(db, pid):
    entries = {}
    for index, state in enumerate(("gallery", *STATES), 1):
        asset_id, entry_id = uuid4(), uuid4()
        digest = f"{index:064x}"
        db.add(
            Asset(
                id=asset_id,
                object_key=f"img/test/{asset_id}",
                sha256=digest,
                etag=digest,
                size_bytes=100,
                content_type="image/png",
                width=10,
                height=10,
                owner_principal_id="synthetic",
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
                origin_namespace="test",
                origin_id=str(entry_id),
                content_hash=digest,
                purpose="common",
                status=state,
                revision=1,
                generation=1,
                source={},
            )
        )
        entries[state] = str(entry_id)
    group = GalleryGroup(
        id=uuid4(),
        canonical_part_id=pid,
        revision=1,
        ordered_entry_ids=[entries["gallery"]],
        primary_entry_id=entries["gallery"],
    )
    db.add(group)
    db.flush()
    db.add(GalleryMember(part_id=pid, group_id=group.id))
    db.commit()
    return entries


def request_gallery(http, authority, pid, status, *, delegated):
    path = f"/api/pn-catalog/v1/parts/{pid}/gallery?status={status}&limit=50"
    headers = (
        exchange(http, authority[2], "GET", path)[0]
        if delegated
        else native(authority[0])
    )
    return http.get(path, headers=headers)


@pytest.mark.parametrize("status", STATES)
@pytest.mark.parametrize("delegated", [False, True])
def test_gallery_state_read_requires_explicit_current_grant_before_and_after_first_image(
    db, authority, http, status, delegated
):
    pid = seed(db)
    grant = db.scalar(
        select(SysAuthorizationGrant).where(
            SysAuthorizationGrant.user_id == authority[0].id,
            SysAuthorizationGrant.resource == "part_gallery",
        )
    )
    action = f"gallery.{status}.read"
    original = list(grant.actions)
    grant.actions = [a for a in original if a != action]
    grant.version += 1
    db.commit()
    # No first-image shortcut may bypass the state-specific permission.
    denied_empty = request_gallery(http, authority, pid, status, delegated=delegated)
    assert denied_empty.status_code == 403, denied_empty.text
    entries = populate_gallery(db, pid)
    grant.actions = original
    grant.version += 1
    db.commit()
    allowed = request_gallery(http, authority, pid, status, delegated=delegated)
    assert allowed.status_code == 200, allowed.text
    assert [entry["entry_id"] for entry in allowed.json()["data"]["entries"]] == [
        entries[status]
    ]
    resolved = access.resolve_native_access(db, identity(authority[0]))
    assert call(db, resolved, action).writable_fields == frozenset()
    db.rollback()
    grant.actions = [a for a in original if a != action]
    grant.version += 1
    db.commit()
    denied = request_gallery(http, authority, pid, status, delegated=delegated)
    assert (
        denied.status_code == 403 and denied.json()["error"]["code"] == "auth.forbidden"
    )
    visible = request_gallery(http, authority, pid, "gallery", delegated=delegated)
    assert visible.status_code == 200
    assert [entry["entry_id"] for entry in visible.json()["data"]["entries"]] == [
        entries["gallery"]
    ]


@pytest.mark.parametrize("status", STATES)
def test_gallery_state_read_keeps_client_capability_and_field_intersection(
    db, authority, http, status, monkeypatch
):
    pid = seed(db)
    populate_gallery(db, pid)
    settings = get_settings()
    original = settings.pn_shop_allowed_actions
    monkeypatch.setattr(
        settings,
        "pn_shop_allowed_actions",
        [a for a in original if a != f"gallery.{status}.read"],
    )
    assert (
        request_gallery(http, authority, pid, status, delegated=True).status_code == 403
    )
    assert (
        request_gallery(http, authority, pid, status, delegated=False).status_code
        == 200
    )
    monkeypatch.setattr(settings, "pn_shop_allowed_actions", original)
    grant = db.scalar(
        select(SysAuthorizationGrant).where(
            SysAuthorizationGrant.user_id == authority[0].id,
            SysAuthorizationGrant.resource == "part_gallery",
        )
    )
    grant.field_permissions = [
        field for field in grant.field_permissions if field != "gallery"
    ]
    grant.version += 1
    db.commit()
    assert (
        request_gallery(http, authority, pid, status, delegated=False).status_code
        == 403
    )
    assert (
        request_gallery(http, authority, pid, status, delegated=True).status_code == 403
    )

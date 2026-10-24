"""Candidate preview is a read-only grant, never a derivative/publication bypass."""
# ruff: noqa: F811 -- imported fixtures are resolved by pytest argument name.

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from uuid import UUID, uuid4

import pytest
from app.config import get_settings
from app.db import engine
from app.models.authorization import SysAuthorizationGrant
from app.models.dimensions import DimPart
from app.pn_catalog import jobs
from app.pn_catalog.job_models import CatalogJob
from app.pn_catalog.media_models import (
    Derivative,
    GalleryEntry,
    GalleryMember,
    GallerySuppression,
)
from app.services.pn_catalog_runtime import original_actor_authorize
from app.services.pn_media_execution import media_handlers
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from tests.authz_pdp_helpers import cerbos_url  # noqa: F401
from tests.test_pn_catalog_access import authority  # noqa: F401
from tests.test_pn_catalog_authority_http import http, seed  # noqa: F401
from tests.test_pn_catalog_authority_jobs import request
from tests.test_pn_gallery_status_authority import populate_gallery

PREFIX = "/api/pn-catalog/v1"


class Signer:
    def __init__(self):
        self.calls = []

    def signed_get(self, key, *, expires_in):
        self.calls.append((key, expires_in))
        return "https://media.example.test/signed/" + str(len(self.calls))


def candidate(db, pid):
    entries = populate_gallery(db, pid)
    entry = db.get(GalleryEntry, UUID(entries["candidate"]))
    db.add(
        Derivative(
            id=uuid4(),
            asset_id=entry.asset_id,
            recipe_id="thumbnail-600-webp85-v1",
            object_key=f"thumb/test/{entry.asset_id}.webp",
            status="ready",
        )
    )
    db.commit()
    return entry


def gallery_grant(db, authority):
    return db.scalar(
        select(SysAuthorizationGrant).where(
            SysAuthorizationGrant.user_id == authority[0].id,
            SysAuthorizationGrant.resource == "part_gallery",
        )
    )


def preview(http, authority, client, entry_id, expected):
    for suffix in ("", "/links"):
        response = request(
            http,
            authority,
            client,
            "GET",
            f"{PREFIX}/gallery/entries/{entry_id}{suffix}",
        )
        assert response.status_code == expected, response.text
        if expected == 200:
            assert response.json()["data"]["entry_id"] == str(entry_id)


@pytest.mark.parametrize("client", ["itsp", "shop"])
@pytest.mark.parametrize("deny", ["candidate_grant", "gallery_field"])
def test_explicit_candidate_preview_and_current_revoke_never_signs_when_denied(
    db, authority, http, client, deny
):
    pid = seed(db)
    item = candidate(db, pid)
    signer = http.app.state.pn_media_storage = Signer()
    preview(http, authority, client, item.id, 200)
    assert len(signer.calls) == 2  # Original plus the already existing derivative.
    assert {expiry for _, expiry in signer.calls} == {300}
    signer.calls.clear()
    grant = gallery_grant(db, authority)
    if deny == "candidate_grant":
        grant.actions = [
            action for action in grant.actions if action != "gallery.candidate.read"
        ]
    else:
        grant.field_permissions = [
            field for field in grant.field_permissions if field != "gallery"
        ]
    grant.version += 1
    db.commit()
    preview(http, authority, client, item.id, 403)
    assert signer.calls == []


def test_shop_candidate_preview_needs_client_capability_even_with_native_grant(
    db, authority, http, monkeypatch
):
    pid = seed(db)
    item = candidate(db, pid)
    signer = http.app.state.pn_media_storage = Signer()
    settings = get_settings()
    monkeypatch.setattr(
        settings,
        "pn_shop_allowed_actions",
        [
            action
            for action in settings.pn_shop_allowed_actions
            if action != "gallery.candidate.read"
        ],
    )
    preview(http, authority, "shop", item.id, 403)
    assert signer.calls == []
    preview(http, authority, "itsp", item.id, 200)
    assert len(signer.calls) == 2


@pytest.mark.parametrize("client", ["itsp", "shop"])
def test_candidate_preview_uses_immutable_origin_and_its_suppression(
    db, authority, http, client
):
    origin = seed(db)
    item = candidate(db, origin)
    other = DimPart(pn_std="OTHER-PREVIEW-ORIGIN")
    db.add(other)
    db.commit()
    populate_gallery(db, other.id)
    other_member = db.get(GalleryMember, other.id)
    db.add(
        GallerySuppression(
            group_id=other_member.group_id,
            content_hash=item.content_hash,
            reason="other origin",
        )
    )
    db.commit()
    observed = []
    original_resolver = http.app.state.pn_catalog_access_resolver

    def record_authority(request, session):
        resolved = original_resolver(request, session)

        def authorize(db, context, action, part_id):
            observed.append((action, part_id))
            return resolved.authorize(db, context, action, part_id)

        return replace(resolved, authorize=authorize)

    http.app.state.pn_catalog_access_resolver = record_authority
    signer = http.app.state.pn_media_storage = Signer()
    preview(http, authority, client, item.id, 200)
    assert observed and {pid for _, pid in observed} == {origin}
    assert ("gallery.candidate.read", origin) in observed
    assert len(signer.calls) == 2
    path = f"{PREFIX}/parts/{origin}/gallery?status=candidate&limit=1"
    visible_page = request(http, authority, client, "GET", path)
    assert visible_page.status_code == 200, visible_page.text
    assert [entry["entry_id"] for entry in visible_page.json()["data"]["entries"]] == [
        str(item.id)
    ]
    signer.calls.clear()
    origin_member = db.get(GalleryMember, origin)
    db.add(
        GallerySuppression(
            group_id=origin_member.group_id,
            content_hash=item.content_hash,
            reason="hide actual origin",
        )
    )
    db.commit()
    preview(http, authority, client, item.id, 409)
    assert signer.calls == []
    hidden_page = request(http, authority, client, "GET", path)
    assert hidden_page.status_code == 200, hidden_page.text
    assert hidden_page.json()["data"]["entries"] == []
    assert hidden_page.json()["meta"]["next_cursor"] is None


@pytest.mark.parametrize("client", ["itsp", "shop"])
@pytest.mark.parametrize("status", ["processing", "deleted"])
def test_other_nonpublic_states_remain_unsignable_even_with_all_grants(
    db, authority, http, client, status
):
    pid = seed(db)
    entries = populate_gallery(db, pid)
    signer = http.app.state.pn_media_storage = Signer()
    preview(http, authority, client, entries[status], 409)
    assert signer.calls == []


@pytest.mark.parametrize("client", ["itsp", "shop"])
def test_candidate_preview_does_not_authorize_new_derivation(
    db, authority, http, client
):
    pid = seed(db)
    item = candidate(db, pid)
    http.app.state.pn_media_storage = Signer()
    response = request(
        http,
        authority,
        client,
        "POST",
        f"{PREFIX}/assets/{item.asset_id}/derivatives",
        {"entry_id": str(item.id), "expected_revision": item.revision},
    )
    assert response.status_code == 409, response.text
    assert db.scalar(select(func.count()).select_from(CatalogJob)) == 0


@pytest.mark.parametrize("client", ["itsp", "shop"])
def test_hidden_candidates_are_filtered_before_pagination_without_hiding_history(
    db, authority, http, client
):
    pid = seed(db)
    entries = populate_gallery(db, pid)
    source = db.get(GalleryEntry, UUID(entries["candidate"]))
    start = datetime(2026, 1, 1, tzinfo=UTC)
    source.created_at = start
    group_id = db.get(GalleryMember, pid).group_id
    db.add(
        GallerySuppression(
            group_id=group_id,
            content_hash=source.content_hash,
            reason="hidden candidate",
        )
    )

    def add(template, position, status="candidate"):
        entry = GalleryEntry(
            id=uuid4(),
            asset_id=template.asset_id,
            origin_part_id=pid,
            origin_namespace="test",
            origin_id=str(uuid4()),
            content_hash=template.content_hash,
            purpose="common",
            status=status,
            revision=1,
            generation=1,
            source={},
            created_at=start + timedelta(seconds=position),
        )
        db.add(entry)
        return str(entry.id)

    first = add(db.get(GalleryEntry, UUID(entries["processing"])), 1)
    add(source, 2)
    second = add(db.get(GalleryEntry, UUID(entries["deleted"])), 3)
    add(source, 4)
    hidden_history = {
        state: add(source, 5, state) for state in ("processing", "deleted")
    }
    db.commit()
    signer = http.app.state.pn_media_storage = Signer()

    def page(status="candidate", cursor=None):
        params = {"status": status, "limit": 1 if status == "candidate" else 50}
        if cursor:
            params["cursor"] = cursor
        result = request(
            http,
            authority,
            client,
            "GET",
            f"{PREFIX}/parts/{pid}/gallery?{urlencode(params)}",
        )
        assert result.status_code == 200, result.text
        return result.json()

    first_page = page()
    assert [entry["entry_id"] for entry in first_page["data"]["entries"]] == [first]
    assert first_page["meta"]["next_cursor"]
    second_page = page(cursor=first_page["meta"]["next_cursor"])
    assert [entry["entry_id"] for entry in second_page["data"]["entries"]] == [second]
    assert second_page["meta"]["next_cursor"] is None
    for visible in (first, second):
        preview(http, authority, client, visible, 200)
    assert len(signer.calls) == 2
    for state, hidden_id in hidden_history.items():
        assert {entry["entry_id"] for entry in page(state)["data"]["entries"]} == {
            entries[state],
            hidden_id,
        }


@pytest.mark.parametrize("change_stage", ["before_prepare", "before_publish"])
def test_worker_never_derives_or_publishes_candidate_after_formal_job_is_queued(
    db, authority, http, change_stage
):
    pid = seed(db)
    entries = populate_gallery(db, pid)
    entry_id = UUID(entries["gallery"])
    item = db.get(GalleryEntry, entry_id)
    http.app.state.pn_media_storage = Signer()
    result = request(
        http,
        authority,
        "itsp",
        "POST",
        f"{PREFIX}/assets/{item.asset_id}/derivatives",
        {"entry_id": str(item.id), "expected_revision": item.revision},
    )
    assert result.status_code == 202, result.text
    derivative_id = UUID(result.json()["data"]["derivative_id"])
    db.rollback()

    def demote():
        with Session(engine) as concurrent:
            entry = concurrent.get(GalleryEntry, entry_id)
            entry.status = "candidate"
            entry.revision += 1
            # The real gallery -> candidate command keeps generation unchanged.
            # Staleness must come from gallery-only visibility, not a version bump.
            concurrent.commit()

    class Cloud:
        calls = 0

        def thumbnail(self, **_kwargs):
            self.calls += 1
            demote()

    cloud = Cloud()
    if change_stage == "before_prepare":
        demote()
    sessions = sessionmaker(engine)
    handlers = media_handlers(
        storage=object(),
        cloud=cloud,
        authorize=original_actor_authorize,
        session_factory=sessions,
    )
    job = jobs.run_once(
        sessions,
        worker_id="candidate-preview-regression",
        queues=["media"],
        handlers=handlers,
    )
    assert job.status == "stale", job
    assert cloud.calls == (0 if change_stage == "before_prepare" else 1)
    assert db.get(Derivative, derivative_id, populate_existing=True).status == "pending"

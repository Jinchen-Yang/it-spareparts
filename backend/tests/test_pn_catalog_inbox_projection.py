"""Inbox discovery exposes safe file/hint projections, never another actor's task controls."""
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, select

from app.pn_catalog import jobs, media
from app.pn_catalog.dto import AuthorizationDecision
from app.pn_catalog.errors import CatalogError
from app.pn_catalog.job_dto import JobSpec
from app.pn_catalog.job_models import CatalogJob
from app.pn_catalog.media_models import InboxItem
from app.pn_catalog.wire_media import InboxSnapshot
from app.services import pn_catalog_enrichment
from tests.test_pn_catalog_media import allow, context, create_ticket
from tests.test_pn_catalog_media_http import media_client  # noqa: F401


def inbox(db, ready=True):
    ticket = create_ticket(db, ready=ready)
    item = db.scalar(select(InboxItem).where(InboxItem.upload_id == ticket))
    if item is None and not ready:
        item = InboxItem(upload_id=ticket, owner_principal_id="account:42", status="receiving")
        db.add(item)
        db.commit()
    return item


def recognition(db, item, *, generation=None, status="succeeded", result=None):
    value = jobs.enqueue(db, JobSpec(kind="enrichment.recognize", queue="enrichment",
        resource_type="enrichment.recognize", resource_id=str(item.id),
        resource_generation=item.generation if generation is None else generation,
        input_revision=item.revision, deduplication_key=str(uuid4()), payload={"object_key": "private/provider/key"}), context=context())
    row = db.get(CatalogJob, value.job_id)
    row.status = status
    row.result = result or {"candidates": [{"pn": "EXAMPLE-PN", "confidence": 0.9}], "item_category": "硬盘", "category_confidence": 0.8,
        "provider": "local-test", "model": "test-model", "tokens": 10}
    db.commit()
    return row


def test_shared_inbox_shows_valid_hints_without_exposing_other_owners_job(db):
    item = inbox(db)
    row = recognition(db, item)
    viewer = context("account:99")
    value = media.get_inbox(db, item.id, context=viewer, authorize=allow)["data"]
    InboxSnapshot.model_validate(value)
    assert value["filename"] == "image.jpg"
    assert value["size_bytes"] == 100
    assert value["uploaded_by"] == {"principal_id": "account:42", "client_id": "shop"}
    assert value["uploaded_by_me"] is False
    assert value["claimed_by_me"] is False
    assert value["recognition"]["result"]["candidates"][0]["pn"] == "EXAMPLE-PN"
    assert "private/provider/key" not in str(value)
    assert "job_id" not in str(value)
    with pytest.raises(CatalogError):
        pn_catalog_enrichment.authorize_job(db, viewer, "job.read", jobs.get_job(db, row.id), authorize=allow)


def test_inbox_current_actor_flags_use_canonical_identity(db):
    item = inbox(db)
    item.claimed_by = "account:99"
    db.commit()
    uploader = media.get_inbox(db, item.id, context=context(), authorize=allow)["data"]
    claimant = media.get_inbox(db, item.id, context=context("account:99"), authorize=allow)["data"]
    assert (uploader["uploaded_by_me"], uploader["claimed_by_me"]) == (True, False)
    assert (claimant["uploaded_by_me"], claimant["claimed_by_me"]) == (False, True)


def test_inbox_recognition_hides_old_generation_and_malformed_results(db):
    item = inbox(db)
    recognition(db, item, generation=0)
    assert media.get_inbox(db, item.id, context=context(), authorize=allow)["data"]["recognition"] is None
    recognition(db, item, result={"provider_secret": "must not leak"})
    assert media.get_inbox(db, item.id, context=context(), authorize=allow)["data"]["recognition"]["result"] is None


@pytest.mark.parametrize("row_count", [1, 3, 20])
def test_inbox_page_queries_are_bounded_independently_of_row_count(db, row_count):
    items = [inbox(db) for _ in range(row_count)]
    recognition(db, items[0])
    statements = []
    def count(_conn, _cursor, statement, *_):
        statements.append(statement)
    event.listen(db.get_bind(), "before_cursor_execute", count)
    try:
        page = media.list_inbox(db, context=context(), authorize=allow, limit=50)
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", count)
    assert len(page["data"]) == row_count
    # One bounded history join preserves migrated recognition results; its cost
    # stays constant as the page grows, like current recognition/upload joins.
    assert len(statements) <= 5


def test_fresh_inbox_links_check_owner_scope_ready_state_and_delete_before_signing(db):
    class Signer:
        def __init__(self):
            self.keys = []
        def signed_get(self, key, expires_in):
            self.keys.append((key, expires_in))
            return "https://storage.invalid/fresh"
    signer = Signer()
    receiving = inbox(db, ready=False)
    with pytest.raises(CatalogError) as error:
        media.inbox_links(db, receiving.id, context=context(), authorize=allow, signer=signer)
    assert error.value.code == "media.inbox_not_ready"
    assert signer.keys == []
    item = inbox(db)
    def own_only(*_):
        return AuthorizationDecision(frozenset({"inbox"}), frozenset(), {"owner_principal_ids": []})
    with pytest.raises(CatalogError):
        media.inbox_links(db, item.id, context=context("account:99"), authorize=own_only, signer=signer)
    assert signer.keys == []
    links = media.inbox_links(db, item.id, context=context(), authorize=allow, signer=signer)
    assert links["inbox_id"] == str(item.id)
    assert signer.keys[0][1] == 300
    item.status = "deleted"
    db.commit()
    with pytest.raises(CatalogError) as error:
        media.inbox_links(db, item.id, context=context(), authorize=allow, signer=signer)
    assert error.value.status_code == 404
    assert len(signer.keys) == 1


def test_inbox_http_metadata_and_links_share_current_visibility(db, media_client):  # noqa: F811
    item = inbox(db)
    response = media_client.get(f"/api/pn-catalog/v1/inbox-items/{item.id}")
    assert response.status_code == 200, response.text
    assert response.json()["data"]["filename"] == "image.jpg"
    response = media_client.get(f"/api/pn-catalog/v1/inbox-items/{item.id}/links")
    assert response.status_code == 200, response.text
    assert UUID(response.json()["data"]["asset_id"]) == item.asset_id
    assert response.headers["Cache-Control"] == "no-store"


def test_all_status_keeps_shared_receiving_private_and_mine_complete(db):
    waiting = inbox(db, ready=False)
    ready = inbox(db)
    own = media.list_inbox(db, context=context(), authorize=allow, status="all")["data"]
    shared = media.list_inbox(db, context=context("account:99"), authorize=allow, status="all", scope="shared")["data"]
    assert {row["inbox_id"] for row in own} == {str(waiting.id), str(ready.id)}
    assert [row["inbox_id"] for row in shared] == [str(ready.id)]

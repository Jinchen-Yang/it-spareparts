"""Audit retains actor evidence while current permissions govern change bodies."""
import pytest

from app.pn_catalog import api
from app.pn_catalog.dto import AuthorizationDecision
from app.pn_catalog.errors import CatalogError
from tests.test_pn_catalog_core import context, seed


def add(db, pid, axis, changes, ctx=None):
    event = api.record_audit(db, pid, context=ctx or context(), action=axis + ".test",
        changes=changes, before_revision=0, after_revision=1, revisions={"axis": axis})
    db.commit()
    return event.id


def policy(grants):
    def authorize(_db, _ctx, action, _pid):
        if action not in grants:
            raise CatalogError("auth.forbidden", "not granted", status_code=403)
        return AuthorizationDecision(readable_fields=frozenset(grants[action]))
    return authorize


def test_content_and_attributes_audit_use_their_own_current_field_grants(db):
    pid = seed(db)
    cid = add(db, pid, "content.good", {"content.good.title": {"after": "public title"},
        "content.good.description": {"after": "private"}, "content.good.status": {"after": "confirmed"}})
    aid = add(db, pid, "attributes", {"attributes": {"x": {"after": 12}}, "fill_status": {"after": "confirmed"}})
    grants = {"read_audit": [], "read": ["description"], "read_content": ["content.good.title"], "read_attributes": ["attributes"]}
    result = api.list_audit_events(db, pid, context=context(), authorize=policy(grants))
    by_id = {r["audit_id"]: r for r in result["data"]}
    assert set(by_id[cid]["changes"]) == {"content.good.title"}
    assert set(by_id[aid]["changes"]) == {"attributes"}
    assert by_id[cid]["actor"]["principal_id"] == "account:42"
    grants.pop("read_content")
    assert next(r for r in api.list_audit_events(db, pid, context=context(), authorize=policy(grants))["data"] if r["audit_id"] == cid)["changes"] == {}


def test_media_audit_is_currently_granted_and_never_exposes_private_storage_payloads(db):
    pid = seed(db)
    gallery = add(db, pid, "media.entry", {"purpose": "defect", "storage_key": "private-key", "signed_url": "private-url"})
    other = context().model_copy(update={"actor": context().actor.model_copy(update={"principal_id": "account:99"})})
    upload = add(db, pid, "media.upload", {"filename": "private.jpg", "storage_key": "private-key"}, other)
    identity = add(db, pid, "identity", {"part_ids": [pid], "outcome": {"business_amount": 123}})
    grants = {"read_audit": [], "read": [], "gallery.read": ["gallery"], "uploads.read": ["uploads"], "identity.read": ["identity"]}
    result = {r["audit_id"]: r["changes"] for r in api.list_audit_events(db, pid, context=context(), authorize=policy(grants))["data"]}
    assert result[gallery] == {"purpose": "defect"}
    assert result[upload] == {}
    assert result[identity] == {"part_ids": [pid]}
    grants["uploads.manage"] = ["uploads"]
    managed = {r["audit_id"]: r["changes"] for r in api.list_audit_events(db, pid, context=context(), authorize=policy(grants))["data"]}
    assert managed[upload] == {"filename": "private.jpg"}


def test_audit_does_not_mask_permission_service_failure_as_an_empty_grant(db):
    pid = seed(db)
    add(db, pid, "media.entry", {"purpose": "common"})
    def unavailable(_db, _ctx, action, _pid):
        if action == "gallery.read":
            raise CatalogError("auth.unavailable", "PDP down", status_code=503)
        return AuthorizationDecision()
    with pytest.raises(CatalogError) as exc:
        api.list_audit_events(db, pid, context=context(), authorize=unavailable)
    assert exc.value.code == "auth.unavailable"

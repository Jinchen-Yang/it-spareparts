"""Cross-request archive retries cannot silently change their original intent."""
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from app.pn_catalog import media
from app.pn_catalog.errors import CatalogError
from app.pn_catalog.media_dto import AssignInbox, ClaimInbox
from app.pn_catalog.media_models import GalleryEntry, InboxItem
from app.pn_catalog.models import AuditEvent
from tests.test_pn_catalog_media import allow, context, create_ticket, seed


def prepared(db):
    pid = seed(db)
    ticket = create_ticket(db)
    item = db.scalar(select(InboxItem).where(InboxItem.upload_id == ticket))
    claim = media.claim_inbox(db, item.id, ClaimInbox(expected_revision=item.revision), context=context(), authorize=allow)
    db.commit()
    command = AssignInbox(claim_revision=claim["data"]["claim_revision"], part_id=pid,
        expected_identity_revision=0, assignment_key=uuid4(), purpose="common")
    return item.id, command


def test_assignment_retries_preserve_intent_even_after_gallery_purpose_changes(db):
    iid, command = prepared(db)
    original = context()
    result = media.assign_inbox(db, iid, command, context=original, authorize=allow)
    db.commit()
    count = db.scalar(select(func.count()).select_from(AuditEvent))
    eid = UUID(result["data"]["entry_id"])
    db.get(GalleryEntry, eid).purpose = "defect"
    db.commit()
    retry = media.assign_inbox(db, iid, command, context=context(), authorize=allow)
    db.commit()
    assert retry["data"]["entry_id"] == str(eid)
    assert retry["meta"]["replayed"]
    assert retry["meta"]["audit_id"] == result["meta"]["audit_id"]
    assert retry["meta"]["applied_revision"] == result["meta"]["applied_revision"]
    assert db.scalar(select(func.count()).select_from(AuditEvent)) == count
    assert db.get(GalleryEntry, eid).purpose == "defect"
    with pytest.raises(CatalogError) as exc:
        media.assign_inbox(db, iid, command.model_copy(update={"purpose": "defect"}), context=context(), authorize=allow)
    assert exc.value.code == "request.idempotency_mismatch"
    db.rollback()
    assert db.get(InboxItem, iid).assignment_intent["purpose"] == "common"


def test_legacy_unknown_intent_uses_original_receipt_and_never_guesses_current_purpose(db):
    iid, command = prepared(db)
    ctx = context()
    media.assign_inbox(db, iid, command, context=ctx, authorize=allow)
    db.commit()
    db.get(InboxItem, iid).assignment_intent = None
    db.commit()
    assert media.assign_inbox(db, iid, command, context=ctx, authorize=allow)["meta"]["replayed"]
    with pytest.raises(CatalogError) as exc:
        media.assign_inbox(db, iid, command, context=context(), authorize=allow)
    assert exc.value.code == "inbox.assignment_recovery_required"

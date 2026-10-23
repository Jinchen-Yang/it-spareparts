"""Independent review reproductions; expected invariants become regression tests after fixes."""
from uuid import UUID

import pytest
from sqlalchemy import select

from app.pn_catalog import api, media
from app.pn_catalog.errors import CatalogError
from app.pn_catalog.media_dto import AddEntry, ExpectedRevision
from app.pn_catalog.media_models import GalleryEntry, InboxItem
from tests.test_pn_catalog_media import allow, context, create_ticket, entry, seed


def test_deleted_inbox_cannot_be_archived_through_upload_endpoint(db):
    pid = seed(db)
    ticket = create_ticket(db)
    item = db.scalar(select(InboxItem).where(InboxItem.upload_id == ticket))
    media.delete_inbox(db, item.id, ExpectedRevision(expected_revision=item.revision), context=context(), authorize=allow)
    db.commit()
    with pytest.raises(CatalogError):
        media.add_entry(db, pid, AddEntry(expected_identity_revision=0, upload_id=ticket),
                         context=context(), authorize=allow)


def test_receipt_rechecks_reused_source_permissions(db):
    source, target = seed(db, "SOURCE-REPLAY"), seed(db, "TARGET-REPLAY")
    eid = entry(db, source)
    command = AddEntry(expected_identity_revision=0, asset_id=db.get(GalleryEntry, eid).asset_id, source_entry_id=eid)
    ctx = context()
    created = media.add_entry(db, target, command, context=ctx, authorize=allow)
    db.commit()
    assert UUID(created["data"]["entry_id"]) != eid
    def revoked(_db, _ctx, action, pid):
        if pid == source:
            raise CatalogError("auth.forbidden", "source permission revoked", status_code=403)
        return allow()
    with pytest.raises(CatalogError, match="source permission revoked"):
        api.get_receipt(db, ctx.request_id, context=ctx, authorize=revoked)

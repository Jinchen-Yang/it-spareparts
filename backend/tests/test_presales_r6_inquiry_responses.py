"""撤回编辑后的行处理结果必须与当前业务版本对应。"""

import pytest
from sqlalchemy import select

from app.models.presales import PresalesLineResponse, PresalesOfferLineLink
from app.services.presales import inquiries
from app.services.workflow import work_items
from tests.test_presales_inquiries import presales_parts, presales_users
from tests.test_presales_r6_existing import _publish, r6_actors


def _handled(db, users, parts):
    inquiry = inquiries.create_draft(db, users.sales.id,
        assignee_ids=[users.buyer.id],
        lines=[{"line_key": "original", "part_id": parts[0].id}])
    inquiries.submit(db, inquiry.id, users.sales.id, inquiry.version)
    line = inquiries.lines_for(db, inquiry.id)[0]
    item = work_items.list_by_subject(db, inquiries.subject(inquiry), users.sales.id)[0]
    response = PresalesLineResponse(line_id=line.id, created_by=users.buyer.id,
        work_item_id=item.id, line_version=line.version, mode="skip", reason="本次不报")
    db.add(response)
    db.flush()
    work_items.complete(db, item.id, users.buyer.id, item.version)
    return inquiry, line, item, response


@pytest.mark.parametrize("change", ["pn", "quantity", "delete"])
def test_material_revision_discards_previous_line_response(
        db, presales_users, presales_parts, change):
    users, parts = presales_users, presales_parts
    inquiry, line, item, response = _handled(db, users, parts)
    response_id = response.id
    inquiries.withdraw(db, inquiry.id, users.sales.id, inquiry.version)
    entry = {"line_key": line.line_key}
    if change == "pn":
        entry.update(part_id=parts[1].id, pn_raw=parts[1].pn_std)
    elif change == "quantity":
        entry["qty"] = 2
    lines = [{"part_id": parts[1].id}] if change == "delete" else [entry]
    inquiries.update_draft(db, inquiry.id, users.sales.id, inquiry.version, lines=lines)
    inquiries.resubmit(db, inquiry.id, users.sales.id, inquiry.version)
    assert db.scalar(select(PresalesLineResponse.id).where(
        PresalesLineResponse.id == response_id)) is None
    assert item.status == "pending" and not item.paused


def test_notes_only_revision_preserves_line_response_and_completed_task(
        db, presales_users, presales_parts):
    users, parts = presales_users, presales_parts
    inquiry, line, item, response = _handled(db, users, parts)
    previous_version = line.version
    inquiries.withdraw(db, inquiry.id, users.sales.id, inquiry.version)
    inquiries.update_draft(db, inquiry.id, users.sales.id, inquiry.version,
                           lines=[{"line_key": line.line_key, "notes": "补充备注"}])
    inquiries.resubmit(db, inquiry.id, users.sales.id, inquiry.version)
    assert line.version > previous_version
    assert db.get(PresalesLineResponse, response.id) is response
    assert response.line_version == line.version
    assert response.mode == "skip" and response.reason == "本次不报"
    assert item.status == "done"


@pytest.mark.parametrize("change", ["quantity", "pn", "delete", "notes"])
def test_original_line_revision_invalidates_only_material_moved_offer_links(db, r6_actors, change):
    users = r6_actors
    original, line, _, offer = _publish(db, users, kind="sales_inquiry")
    _, _, _, moved = _publish(db, users, on_existing="update", existing_offer_id=offer.id)
    link_key = (line.id, moved.id)
    assert db.get(PresalesOfferLineLink, link_key) is not None
    inquiries.withdraw(db, original.id, users.sales.id, original.version)
    entry = {"line_key": line.line_key}
    if change == "quantity":
        entry["qty"] = 2
    elif change == "pn":
        entry.update(part_id=users.other_part.id, pn_raw=users.other_part.pn_std)
    elif change == "notes":
        entry["notes"] = "补充说明"
    entries = [{"part_id": users.other_part.id}] if change == "delete" else [entry]
    inquiries.update_draft(db, original.id, users.sales.id, original.version, lines=entries)
    inquiries.resubmit(db, original.id, users.sales.id, original.version)
    assert (db.get(PresalesOfferLineLink, link_key) is not None) is (change == "notes")
    assert moved.status == "published" and moved.inquiry_id != original.id
    items = work_items.list_by_subject(db, inquiries.subject(original), users.sales.id)
    assert items[0].status == ("done" if change == "notes" else "pending")

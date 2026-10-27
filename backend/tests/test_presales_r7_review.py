"""独立复审：追加行回复不能重钉未改动的既有沿用行。"""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.presales import PresalesLineResponse
from app.services.presales import common, inquiries, offers
from tests.test_presales_r6_existing import _publish, r6_actors
from tests.test_presales_r6_responses import _pending, _reply, _response_view


@pytest.mark.parametrize("resend_original", [False, True], ids=["implicit-previous", "resend-same-offer"])
def test_appending_another_line_does_not_repin_existing_reuse(db, r6_actors, monkeypatch, resend_original):
    users = r6_actors
    _, _, _, source = _publish(db, users)
    inquiry, lines = _pending(db, users)
    old_line = lines[0]
    _reply(db, inquiry, users.buyer, [{"line_id": old_line.id, "mode": "reuse", "ref_offer_id": source.id}])
    original_until = source.valid_until
    monkeypatch.setattr(common, "now", lambda: original_until + timedelta(seconds=1))
    offers.update_offer(db, source.id, users.buyer.id,
        {"unit_price": "500.00", "validity_days": 30}, expected_version=source.version)
    inquiries.append_lines(db, inquiry.id, users.sales.id, inquiry.version,
        [{"part_id": users.other_part.id}])
    added_line = inquiries.lines_for(db, inquiry.id)[-1]
    choices = [{"line_id": added_line.id, "mode": "skip", "reason": "新行无货"}]
    if resend_original:
        choices.append({"line_id": old_line.id, "mode": "reuse", "ref_offer_id": source.id})
    _reply(db, inquiry, users.buyer, choices)
    response = db.scalar(select(PresalesLineResponse).where(PresalesLineResponse.line_id == old_line.id))
    assert response.ref_offer_revision_no == 1
    view = _response_view(db, inquiry, users.sales, old_line.id)
    assert view["offer"]["unit_price"] == Decimal("100.00")
    assert view["expired"] is True


def test_notes_only_edit_keeps_reference_pin_when_current_scope_is_narrower(db, r6_actors):
    from app.services.presales import visibility

    users = r6_actors
    _, _, _, source = _publish(db, users)
    inquiry = inquiries.create_draft(db, users.sales.id,
        lines=[{"part_id": users.part.id, "ref_offer_id": source.id}])
    inquiries.submit(db, inquiry.id, users.sales.id, inquiry.version)
    line = inquiries.lines_for(db, inquiry.id)[0]
    _publish(db, users, whole={"mode": "private"}, price="500.00",
        on_existing="update", existing_offer_id=source.id)
    inquiries.withdraw(db, inquiry.id, users.sales.id, inquiry.version)
    inquiries.update_draft(db, inquiry.id, users.sales.id, inquiry.version,
        lines=[{"line_key": line.line_key, "notes": "只改备注"}])
    inquiries.resubmit(db, inquiry.id, users.sales.id, inquiry.version)
    assert line.ref_offer_revision_no == 1
    view = visibility.inquiry_view(db, inquiry, common.resolve_actor(db, users.sales.id)[1])
    ref = view["lines"][0]["ref_offer"]
    assert ref["unit_price"] == Decimal("100.00") and "latest" not in ref


def test_each_previous_line_link_keeps_its_own_version_after_successive_moves(db, r6_actors):
    from app.models.presales import PresalesOfferLineLink
    from app.services.presales import visibility

    users = r6_actors
    first, first_line, _, source = _publish(db, users)
    second, second_line, _, _ = _publish(db, users, price="80.00",
        on_existing="update", existing_offer_id=source.id)
    _publish(db, users, price="60.00", whole={"mode": "private"},
        on_existing="update", existing_offer_id=source.id)
    assert db.get(PresalesOfferLineLink, (first_line.id, source.id)).offer_revision_no == 1
    assert db.get(PresalesOfferLineLink, (second_line.id, source.id)).offer_revision_no == 2
    ctx = common.resolve_actor(db, users.sales.id)[1]
    for inquiry, price, revision in ((first, "100.00", 1), (second, "80.00", 2)):
        old = visibility.inquiry_view(db, inquiry, ctx)["offers"][0]
        assert old["unit_price"] == Decimal(price) and old["revision_no"] == revision
        assert "latest" not in old


def test_close_pins_version_at_adoption_independently_from_earlier_reference(db, r6_actors):
    from app.services.presales import visibility

    users = r6_actors
    _, _, _, source = _publish(db, users)
    inquiry = inquiries.create_draft(db, users.sales.id,
        lines=[{"part_id": users.part.id, "ref_offer_id": source.id}])
    inquiries.submit(db, inquiry.id, users.sales.id, inquiry.version)
    line = inquiries.lines_for(db, inquiry.id)[0]
    offers.update_offer(db, source.id, users.buyer.id, {"unit_price": "80.00"},
        expected_version=source.version)
    inquiries.close(db, inquiry.id, users.sales.id, inquiry.version,
        outcome="won", won_offer_id=source.id)
    offers.update_offer(db, source.id, users.buyer.id, {"unit_price": "60.00"},
        expected_version=source.version)
    assert line.ref_offer_revision_no == 1 and inquiry.won_offer_revision_no == 2
    view = visibility.inquiry_view(db, inquiry, common.resolve_actor(db, users.sales.id)[1])
    assert view["lines"][0]["ref_offer"]["unit_price"] == Decimal("100.00")
    assert view["won_offer"]["unit_price"] == Decimal("80.00")
    assert view["won_offer"]["latest"]["unit_price"] == Decimal("60.00")

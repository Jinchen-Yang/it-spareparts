"""成交采纳支持本单关联的有效报价，并实时核验报价状态与受众（D-0046 起不看时间）。"""

from datetime import timedelta

import pytest
from sqlalchemy import delete

from app.models.presales import PresalesLineResponse
from app.services.presales import common, inquiries, offers
from app.services.workflow.registry import InvalidTransition
from tests.test_presales_r6_existing import _publish, r6_actors
from tests.test_presales_r6_responses import _pending, _reply


def _linked(db, users, kind):
    source, _, _, offer = _publish(db, users)
    if kind == "sales-reference":
        inquiry = inquiries.create_draft(db, users.sales.id,
            lines=[{"part_id": users.part.id, "ref_offer_id": offer.id}])
        inquiries.submit(db, inquiry.id, users.sales.id, inquiry.version)
    else:
        inquiry, lines = _pending(db, users)
        _reply(db, inquiry, users.buyer, [
            {"line_id": lines[0].id, "mode": "reuse", "ref_offer_id": offer.id}])
    return inquiry, source, offer


@pytest.mark.parametrize("kind", ["sales-reference", "buyer-reuse"])
def test_close_can_adopt_current_visible_linked_offer(db, r6_actors, kind):
    users = r6_actors
    inquiry, _, offer = _linked(db, users, kind)
    closed = inquiries.close(db, inquiry.id, users.sales.id, inquiry.version,
        outcome="won", won_offer_id=offer.id)
    assert closed.status == "closed" and closed.closed_outcome == "won"
    assert closed.won_offer_id == offer.id


@pytest.mark.parametrize("kind", ["sales-reference", "buyer-reuse"])
def test_close_can_adopt_linked_offer_long_after_publication(db, r6_actors, monkeypatch, kind):
    # D-0046：报价不设有效期，时间流逝不会让已关联的报价不可采纳。
    users = r6_actors
    inquiry, _, offer = _linked(db, users, kind)
    later = common.now() + timedelta(days=3650)
    monkeypatch.setattr(common, "now", lambda: later)
    closed = inquiries.close(db, inquiry.id, users.sales.id, inquiry.version,
        outcome="won", won_offer_id=offer.id)
    assert closed.won_offer_id == offer.id


@pytest.mark.parametrize("kind", ["sales-reference", "buyer-reuse"])
@pytest.mark.parametrize("invalid", ["invisible", "withdrawn", "no-stock"])
def test_close_rejects_linked_offer_that_is_no_longer_usable(
        db, r6_actors, kind, invalid):
    users = r6_actors
    inquiry, source, offer = _linked(db, users, kind)
    if invalid == "invisible":
        viewer = common.resolve_actor(db, users.buyer.id)[1]
        source.offer_audience_rule_id = common.rule_id(db, {"mode": "private"}, viewer)
        db.flush()
    elif invalid == "withdrawn":
        offers.withdraw(db, offer.id, users.buyer.id, "撤销错误价格", expected_version=offer.version)
    else:
        offers.update_offer(db, offer.id, users.buyer.id, {"no_stock": True, "notes": "已经无货"},
                            expected_version=offer.version)
    with pytest.raises(InvalidTransition):
        inquiries.close(db, inquiry.id, users.sales.id, inquiry.version,
                        outcome="won", won_offer_id=offer.id)
    assert inquiry.status == "submitted" and inquiry.won_offer_id is None


def test_close_cannot_adopt_unlinked_public_offer(db, r6_actors):
    users = r6_actors
    _, _, _, offer = _publish(db, users)
    inquiry, _ = _pending(db, users)
    with pytest.raises(InvalidTransition):
        inquiries.close(db, inquiry.id, users.sales.id, inquiry.version,
                        outcome="won", won_offer_id=offer.id)
    assert inquiry.status == "submitted"


@pytest.mark.parametrize("private", [False, True], ids=["visible", "invisible"])
def test_original_inquiry_adoption_uses_stable_moved_offer_link(db, r6_actors, private):
    users = r6_actors
    inquiry, _, _, original = _publish(db, users, kind="sales_inquiry")
    _, _, _, moved = _publish(db, users, on_existing="update", existing_offer_id=original.id,
        whole={"mode": "private" if private else "public"}, price="75.00")
    assert moved.id == original.id and moved.inquiry_id != inquiry.id
    db.execute(delete(PresalesLineResponse).where(PresalesLineResponse.ref_offer_id == moved.id))
    if private:
        with pytest.raises(InvalidTransition):
            inquiries.close(db, inquiry.id, users.sales.id, inquiry.version,
                            outcome="won", won_offer_id=moved.id)
        assert inquiry.status == "submitted"
    else:
        inquiries.close(db, inquiry.id, users.sales.id, inquiry.version,
                        outcome="won", won_offer_id=moved.id)
        assert inquiry.status == "closed" and inquiry.won_offer_id == moved.id

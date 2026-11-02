"""成交可采纳本单关联的有效报价，旧受众和经过的时间不影响采纳。"""

from datetime import timedelta

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models.presales import PresalesLineResponse, PresalesOffer, PresalesOfferLineLink
from app.services.presales import common, inquiries, offers
from app.services.workflow.registry import InvalidTransition
from tests.test_presales_r6_existing import _publish, r6_actors
from tests.test_presales_r6_responses import _pending, _reply
from tests.test_presales_api_visibility import _client


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


@pytest.mark.parametrize("kind", ["own", "sales-reference", "buyer-reuse"])
def test_close_http_rejects_repriced_selection_and_pins_only_after_reselection(db, r6_actors, kind):
    users = r6_actors
    if kind == "own":
        inquiry, _, _, offer = _publish(db, users, kind="sales_inquiry")
    else:
        inquiry, _, offer = _linked(db, users, kind)
    db.commit()
    http = _client(users.sales)
    path = f"/api/presales/inquiries/{inquiry.id}/close"
    lines = http.get("/api/presales/pool/lines", params={"tab": "all"}).json()["items"]
    price = next(row["price"] for row in lines if row["inquiry_id"] == inquiry.id and row["price"])
    assert (price["offer_id"], price["revision_no"], price["unit_price"]) == (offer.id, 1, "100.00")
    selection = {"expected_version": inquiry.version, "outcome": "won", "won_offer_id": offer.id}
    offers.update_offer(db, offer.id, users.buyer.id, {"unit_price": "200.00"}, expected_version=offer.version)
    db.commit()
    for body in (dict(selection, expected_offer_revision_no=1), selection):
        response = http.post(path, json=body)
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "offer_revision_conflict"
    db.refresh(inquiry)
    assert (inquiry.status, inquiry.won_offer_id, inquiry.won_offer_revision_no) == ("submitted", None, None)
    result = http.post(path, json=dict(selection, expected_offer_revision_no=2))
    assert result.status_code == 200, result.text
    assert result.json()["won_offer_revision_no"] == 2
    assert result.json()["won_offer"]["unit_price"] == 200


def test_close_keeps_external_offer_locked_until_adoption_commits(db, r6_actors):
    users = r6_actors
    inquiry, _, source = _linked(db, users, "sales-reference")
    db.commit()
    inquiries.close(db, inquiry.id, users.sales.id, inquiry.version, outcome="won", won_offer_id=source.id,
                    expected_offer_revision_no=source.revision_no)
    with Session(db.get_bind()) as competing, pytest.raises(OperationalError) as conflict:
        competing.scalar(select(PresalesOffer.id).where(PresalesOffer.id == source.id).with_for_update(nowait=True))
    assert conflict.value.orig.sqlstate == "55P03"
    db.commit()
    original_revision = inquiry.won_offer_revision_no
    offers.update_offer(db, source.id, users.buyer.id, {"unit_price": "200.00"}, expected_version=source.version)
    db.commit()
    assert source.revision_no > original_revision == inquiry.won_offer_revision_no


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


def test_close_cannot_adopt_unlinked_public_offer(db, r6_actors):
    users = r6_actors
    _, _, _, offer = _publish(db, users)
    inquiry, _ = _pending(db, users)
    with pytest.raises(InvalidTransition):
        inquiries.close(db, inquiry.id, users.sales.id, inquiry.version,
                        outcome="won", won_offer_id=offer.id)
    assert inquiry.status == "submitted"


@pytest.mark.parametrize("kind", ["sales-reference", "buyer-reuse"])
@pytest.mark.parametrize("invalid", ["withdrawn", "no-stock"])
def test_close_rejects_linked_offer_that_is_no_longer_usable(db, r6_actors, kind, invalid):
    users = r6_actors
    inquiry, _, offer = _linked(db, users, kind)
    if invalid == "withdrawn":
        offers.withdraw(db, offer.id, users.buyer.id, "撤销错误价格", expected_version=offer.version)
    else:
        offers.update_offer(db, offer.id, users.buyer.id, {"no_stock": True, "notes": "已经无货"},
                            expected_version=offer.version)
    with pytest.raises(InvalidTransition):
        inquiries.close(db, inquiry.id, users.sales.id, inquiry.version, outcome="won", won_offer_id=offer.id)
    assert inquiry.status == "submitted" and inquiry.won_offer_id is None and inquiry.won_offer_revision_no is None


@pytest.mark.parametrize("scope", ["public", "private"])
def test_original_inquiry_adoption_uses_stable_moved_offer_link(db, r6_actors, scope):
    users = r6_actors
    inquiry, line, _, original = _publish(db, users, kind="sales_inquiry")
    _, _, _, moved = _publish(db, users, on_existing="update", existing_offer_id=original.id,
        whole={"mode": scope}, price="75.00")
    assert moved.id == original.id and moved.inquiry_id != inquiry.id
    # 去掉其他关联入口，只通过迁移前所在行保存的稳定链接来采纳。
    db.execute(delete(PresalesLineResponse).where(PresalesLineResponse.ref_offer_id == moved.id))
    assert line.ref_offer_id is None
    assert db.get(PresalesOfferLineLink, (line.id, moved.id)).offer_revision_no == 1
    inquiries.close(db, inquiry.id, users.sales.id, inquiry.version, outcome="won", won_offer_id=moved.id, expected_offer_revision_no=moved.revision_no)
    assert (inquiry.status, inquiry.closed_outcome) == ("closed", "won")
    assert (inquiry.won_offer_id, inquiry.won_offer_revision_no) == (original.id, 2)

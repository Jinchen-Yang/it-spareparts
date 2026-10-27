"""每位采购逐行处理：沿用保持关联，拒报有原因，全部处理才完成。"""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.models.presales import PresalesLineResponse, PresalesOffer, PresalesOfferRevision
from app.services.presales import common, inquiries, library, offers, visibility
from app.services.workflow import work_items
from app.services.workflow.registry import InvalidTransition
from tests.test_presales_api_visibility import _client
from tests.test_presales_r6_existing import _publish, r6_actors


def _pending(db, users, *, multiple=False, lines=None):
    inquiry = inquiries.create_draft(db, users.sales.id,
        assignee_ids=[users.buyer.id, users.peer.id] if multiple else [users.buyer.id],
        lines=lines or [{"part_id": users.part.id}])
    inquiries.submit(db, inquiry.id, users.sales.id, inquiry.version)
    return inquiry, inquiries.lines_for(db, inquiry.id)


def _reply(db, inquiry, buyer, responses):
    return offers.reply(db, inquiry.id, buyer.id,
        expected_version=inquiry.version, line_responses=responses)


def _item(db, inquiry, users, buyer):
    return next(item for item in work_items.list_by_subject(
        db, inquiries.subject(inquiry), users.sales.id) if item.assignee_id == buyer.id)


def _response_view(db, inquiry, viewer, line_id):
    view = visibility.inquiry_view(db, inquiry, common.resolve_actor(db, viewer.id)[1])
    return next(row for row in view["line_responses"] if row["line_id"] == line_id)


def test_reuse_links_existing_offer_without_copying_price_and_expires_with_it(db, r6_actors, monkeypatch):
    users = r6_actors
    _, _, _, original = _publish(db, users)
    inquiry, lines = _pending(db, users)
    line = lines[0]
    _reply(db, inquiry, users.buyer, [{"line_id": line.id, "mode": "reuse", "ref_offer_id": original.id}])
    response = db.scalar(select(PresalesLineResponse).where(
        PresalesLineResponse.line_id == line.id, PresalesLineResponse.created_by == users.buyer.id))
    assert response is not None and response.mode == "reuse" and response.ref_offer_id == original.id
    assert line.ref_offer_id is None
    assert "unit_price" not in PresalesLineResponse.__table__.columns
    assert db.scalar(select(func.count()).select_from(PresalesOffer)) == 1
    assert db.scalar(select(func.count()).select_from(PresalesOfferRevision)) == 1
    assert _item(db, inquiry, users, users.buyer).status == "done"
    assert _response_view(db, inquiry, users.sales, line.id)["offer"]["unit_price"] == Decimal("100.00")
    assert _response_view(db, inquiry, users.sales, line.id)["expired"] is False
    monkeypatch.setattr(common, "now", lambda: original.valid_until - timedelta(microseconds=1))
    assert _response_view(db, inquiry, users.sales, line.id)["expired"] is False
    monkeypatch.setattr(common, "now", lambda: original.valid_until)
    assert _response_view(db, inquiry, users.sales, line.id)["expired"] is True
    assert library.batch(db, [users.part.id], users.sales.id)[users.part.id] == []


def test_reused_offer_reads_current_price_and_current_field_visibility(db, r6_actors):
    users = r6_actors
    _, _, _, original = _publish(db, users, fields={"notes_audience": {"mode": "public"}})
    inquiry, lines = _pending(db, users)
    _reply(db, inquiry, users.buyer, [{"line_id": lines[0].id, "mode": "reuse", "ref_offer_id": original.id}])
    offers.update_offer(db, original.id, users.buyer.id,
        {"unit_price": "75.00", "notes": "仅采购可看", "notes_audience": {"mode": "private"},
         "reporter_audience": {"mode": "private"}}, expected_version=original.version)
    result = _response_view(db, inquiry, users.sales, lines[0].id)
    assert result["offer"]["unit_price"] == Decimal("75.00")
    assert "notes" not in result["offer"] and "reporter" not in result["offer"]
    assert result["ref_offer_id"] == original.id
    assert db.scalar(select(func.count()).select_from(PresalesOffer)) == 1


def test_reused_offer_becomes_expired_when_source_is_updated_to_no_stock(db, r6_actors):
    users = r6_actors
    _, _, _, original = _publish(db, users)
    inquiry, lines = _pending(db, users)
    _reply(db, inquiry, users.buyer,
           [{"line_id": lines[0].id, "mode": "reuse", "ref_offer_id": original.id}])
    offers.update_offer(db, original.id, users.buyer.id,
        {"no_stock": True, "notes": "供应商确认库存售罄"}, expected_version=original.version)
    assert original.status == "published" and original.valid_until > common.now()
    assert original.unit_price is None and original.no_stock is True
    result = _response_view(db, inquiry, users.sales, lines[0].id)
    assert result["expired"] is True
    assert result["ref_offer_id"] == original.id
    assert library.batch(db, [users.part.id], users.sales.id)[users.part.id] == []


@pytest.mark.parametrize("reason", [None, "", "   "])
def test_skip_requires_nonblank_reason_before_completing_work(db, r6_actors, reason):
    users = r6_actors
    inquiry, lines = _pending(db, users)
    choice = {"line_id": lines[0].id, "mode": "skip"}
    if reason is not None:
        choice["reason"] = reason
    with pytest.raises(InvalidTransition):
        _reply(db, inquiry, users.buyer, [choice])
    assert _item(db, inquiry, users, users.buyer).status == "pending"
    assert db.scalar(select(func.count()).select_from(PresalesLineResponse)) == 0


@pytest.mark.parametrize("reason", [None, "", "   "])
def test_response_constraint_rejects_missing_skip_reason(db, r6_actors, reason):
    users = r6_actors
    inquiry, lines = _pending(db, users)
    with pytest.raises(IntegrityError), db.begin_nested():
        db.add(PresalesLineResponse(line_id=lines[0].id, created_by=users.buyer.id,
            line_version=lines[0].version, mode="skip", reason=reason,
            work_item_id=_item(db, inquiry, users, users.buyer).id))
        db.flush()


def test_all_lines_must_be_handled_then_reuse_and_skip_can_complete_together(db, r6_actors):
    users = r6_actors
    _, _, _, original = _publish(db, users)
    inquiry, lines = _pending(db, users, lines=[
        {"part_id": users.part.id}, {"part_id": users.other_part.id}])
    reuse = {"line_id": lines[0].id, "mode": "reuse", "ref_offer_id": original.id}
    with pytest.raises(InvalidTransition):
        _reply(db, inquiry, users.buyer, [reuse])
    assert _item(db, inquiry, users, users.buyer).status == "pending"
    assert db.scalar(select(func.count()).select_from(PresalesLineResponse)) == 0
    _reply(db, inquiry, users.buyer, [reuse,
        {"line_id": lines[1].id, "mode": "skip", "reason": "供应商确认无货"}])
    assert _item(db, inquiry, users, users.buyer).status == "done"
    own = visibility.inquiry_view(db, inquiry, common.resolve_actor(db, users.buyer.id)[1])
    assert {row["mode"] for row in own["line_responses"]} == {"reuse", "skip"}
    skip = next(row for row in own["line_responses"] if row["mode"] == "skip")
    assert skip["reason"] == "供应商确认无货"
    assert db.scalar(select(func.count()).select_from(PresalesOffer)) == 1


def test_each_buyer_handles_the_same_line_independently(db, r6_actors):
    users = r6_actors
    _, _, _, original = _publish(db, users)
    inquiry, lines = _pending(db, users, multiple=True)
    line = lines[0]
    _reply(db, inquiry, users.buyer, [{"line_id": line.id, "mode": "reuse", "ref_offer_id": original.id}])
    assert _item(db, inquiry, users, users.buyer).status == "done"
    assert _item(db, inquiry, users, users.peer).status == "pending"
    _reply(db, inquiry, users.peer, [{"line_id": line.id, "mode": "skip", "reason": "本渠道不报"}])
    assert _item(db, inquiry, users, users.peer).status == "done"
    responses = list(db.scalars(select(PresalesLineResponse).where(PresalesLineResponse.line_id == line.id)))
    assert len(responses) == 2
    assert {row.created_by for row in responses} == {users.buyer.id, users.peer.id}
    assert line.ref_offer_id is None
    owner = visibility.inquiry_view(db, inquiry, common.resolve_actor(db, users.sales.id)[1])
    assert len(owner["line_responses"]) == 2
    own = visibility.inquiry_view(db, inquiry, common.resolve_actor(db, users.buyer.id)[1])
    assert [row["created_by"] for row in own["line_responses"]] == [users.buyer.id]
    shared = visibility.inquiry_view(db, inquiry, common.resolve_actor(db, users.other.id)[1])
    assert not shared.get("line_responses")


@pytest.mark.parametrize("invalid", ["other-part", "expired", "withdrawn"])
def test_reuse_rejects_wrong_pn_or_invalid_offer(db, r6_actors, monkeypatch, invalid):
    users = r6_actors
    _, _, _, original = _publish(db, users)
    inquiry, lines = _pending(db, users, lines=[{
        "part_id": users.other_part.id if invalid == "other-part" else users.part.id}])
    if invalid == "expired":
        monkeypatch.setattr(common, "now", lambda: original.valid_until)
    if invalid == "withdrawn":
        offers.withdraw(db, original.id, users.buyer.id, "价格有误", expected_version=original.version)
    with pytest.raises(InvalidTransition):
        _reply(db, inquiry, users.buyer,
               [{"line_id": lines[0].id, "mode": "reuse", "ref_offer_id": original.id}])
    assert _item(db, inquiry, users, users.buyer).status == "pending"
    assert db.scalar(select(func.count()).select_from(PresalesLineResponse)) == 0


def test_reuse_rejects_invisible_offer_and_reference_list_uses_m8(db, r6_actors):
    users = r6_actors
    _, _, _, private = _publish(db, users, buyer=users.peer, whole={"mode": "private"}, price="1.00")
    _, _, _, public = _publish(db, users, price="100.00")
    inquiry, lines = _pending(db, users)
    assert [row["id"] for row in library.batch(db, [users.part.id], users.buyer.id)[users.part.id]] == [public.id]
    with pytest.raises(InvalidTransition):
        _reply(db, inquiry, users.buyer,
               [{"line_id": lines[0].id, "mode": "reuse", "ref_offer_id": private.id}])
    assert _item(db, inquiry, users, users.buyer).status == "pending"
    db.commit()
    response = _client(users.buyer).post("/api/presales/reference", json={"inquiry_id": inquiry.id})
    assert response.status_code == 200, response.text
    assert [row["id"] for row in response.json()["offers"][str(users.part.id)]] == [public.id]


def test_reuse_response_rechecks_record_audience_after_it_becomes_private(db, r6_actors):
    users = r6_actors
    source, _, _, original = _publish(db, users)
    inquiry, lines = _pending(db, users)
    _reply(db, inquiry, users.buyer,
           [{"line_id": lines[0].id, "mode": "reuse", "ref_offer_id": original.id}])
    _, buyer_ctx = common.resolve_actor(db, users.buyer.id)
    source.offer_audience_rule_id = common.rule_id(db, {"mode": "private"}, buyer_ctx)
    db.flush()
    result = _response_view(db, inquiry, users.sales, lines[0].id)
    assert result.get("offer") is None
    assert "unit_price" not in result


def test_http_reply_accepts_reuse_and_owner_sees_linked_price(db, r6_actors):
    users = r6_actors
    _, _, _, original = _publish(db, users)
    inquiry, lines = _pending(db, users)
    inquiry_id, line_id, offer_id, version = inquiry.id, lines[0].id, original.id, inquiry.version
    db.commit()
    response = _client(users.buyer).post(f"/api/presales/inquiries/{inquiry_id}/reply", json={
        "expected_version": version,
        "line_responses": [{"line_id": line_id, "mode": "reuse", "ref_offer_id": offer_id}],
    })
    assert response.status_code == 200, response.text
    detail = _client(users.sales).get(f"/api/presales/inquiries/{inquiry_id}")
    assert detail.status_code == 200, detail.text
    row = detail.json()["line_responses"][0]
    assert row["ref_offer_id"] == offer_id and Decimal(str(row["offer"]["unit_price"])) == Decimal("100.00")
    assert row["expired"] is False


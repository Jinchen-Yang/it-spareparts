"""草稿默认展示与用户明确输入分离：更新旧报价不得暗改税和价格性质。"""
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.presales import PresalesOffer, PresalesOfferRevision
from app.services.presales import inquiries, offers
from tests.test_presales_api_visibility import _client
from tests.test_presales_r6_existing import _publish, r6_actors


def _destination(db, users, kind):
    inquiry = inquiries.create_draft(
        db, users.buyer.id if kind == "proactive_offer" else users.sales.id,
        kind=kind, lines=[{"part_id": users.part.id}],
        assignee_ids=[users.buyer.id] if kind == "sales_inquiry" else [],
    )
    if kind == "sales_inquiry":
        inquiries.submit(db, inquiry.id, users.sales.id, inquiry.version)
    return inquiry, inquiries.lines_for(db, inquiry.id)[0]


@pytest.mark.parametrize("kind", ["sales_inquiry", "proactive_offer"])
@pytest.mark.parametrize("original_fields,explicit,expected", [
    ({"is_tax_inclusive": None, "price_basis": "cost"}, {}, (None, "cost")),
    ({"is_tax_inclusive": None, "price_basis": "cost"}, {"is_tax_inclusive": True}, (True, "cost")),
    ({"is_tax_inclusive": None, "price_basis": "cost"}, {"price_basis": "sale"}, (None, "sale")),
    ({"is_tax_inclusive": None, "price_basis": "cost"}, {"is_tax_inclusive": True, "price_basis": "sale"}, (True, "sale")),
    ({"is_tax_inclusive": True, "price_basis": "sale"}, {"is_tax_inclusive": None, "price_basis": "cost"}, (None, "cost")),
    ({"is_tax_inclusive": None, "price_basis": "cost"}, {"is_tax_inclusive": False}, (False, "cost")),
])
def test_http_save_draft_then_update_preserves_only_omitted_basis(db, r6_actors, kind, original_fields, explicit, expected):
    users = r6_actors
    _, _, _, target = _publish(db, users, fields=original_fields)
    target_id, original_revision = target.id, target.revision_no
    inquiry, line = _destination(db, users, kind)
    inquiry_id, inquiry_version = inquiry.id, inquiry.version
    path = f"/api/presales/inquiries/{inquiry_id}/lines/{line.id}/offers"
    db.commit()
    client = _client(users.buyer)
    created = client.post(path, json={"expected_version": inquiry_version, "unit_price": "22", **explicit})
    assert created.status_code == 200, created.text
    draft = created.json()
    assert "workbook_choice" not in draft
    # Separate HTTP saves must retain which values were explicitly chosen, even
    # when they equal the new-draft default and _apply sees no value change.
    edited = client.post(path, json={"offer_id": draft["id"], "expected_version": draft["version"], "unit_price": "33"})
    assert edited.status_code == 200, edited.text
    action = "reply" if kind == "sales_inquiry" else "publish"
    result = client.post(f"/api/presales/inquiries/{inquiry_id}/{action}", json={
        "expected_version": inquiry_version, "on_existing": "update", "existing_offer_id": target_id})
    assert result.status_code == 200, result.text
    row = result.json()[0]
    assert row["id"] == target_id and Decimal(row["unit_price"]) == Decimal("33")
    assert (row["is_tax_inclusive"], row["price_basis"]) == expected
    db.expire_all()
    old = db.scalar(select(PresalesOfferRevision).where(
        PresalesOfferRevision.offer_id == target_id, PresalesOfferRevision.revision_no == original_revision))
    assert (old.snapshot["is_tax_inclusive"], old.snapshot["price_basis"]) == (
        original_fields["is_tax_inclusive"], original_fields["price_basis"])
    assert db.get(PresalesOffer, target_id).workbook_choice == {}


@pytest.mark.parametrize("explicit_later,expected", [
    ({}, (None, "cost")),
    ({"is_tax_inclusive": True, "price_basis": "sale"}, (True, "sale")),
    ({"is_tax_inclusive": False}, (False, "cost")),
])
def test_existing_default_draft_then_line_decision_retains_input_intent(db, r6_actors, explicit_later, expected):
    users = r6_actors
    _, _, _, target = _publish(db, users, fields={"is_tax_inclusive": None, "price_basis": "cost"})
    inquiry, line = _destination(db, users, "sales_inquiry")
    draft = offers.save_draft(db, inquiry.id, line.id, users.buyer.id,
        {"unit_price": "22"}, expected_version=inquiry.version)
    draft_id, inquiry_id, line_id, actor_id = draft.id, inquiry.id, line.id, users.buyer.id
    assert (draft.is_tax_inclusive, draft.price_basis) == (True, "sale")
    db.commit()
    db.expire_all()
    draft = db.get(PresalesOffer, draft_id)
    offers.save_draft(db, inquiry_id, line_id, actor_id, {"unit_price": "33", **explicit_later},
        offer_id=draft.id, expected_version=draft.version)
    db.commit()
    db.expire_all()
    result = offers.save_line_decision(db, inquiry_id, line_id, actor_id,
        expected_version=inquiry.version, mode="quote", data={"unit_price": "44"},
        on_existing="update", existing_offer_id=target.id)
    draft = db.get(PresalesOffer, result["offer_id"])
    assert (draft.is_tax_inclusive, draft.price_basis) == expected
    db.commit()
    db.expire_all()
    published = offers.reply(db, inquiry_id, actor_id, expected_version=inquiry.version)[0]
    assert published.id == target.id and published.unit_price == Decimal("44")
    assert (published.is_tax_inclusive, published.price_basis) == expected
    assert published.workbook_choice == {}


@pytest.mark.parametrize("kind", ["sales_inquiry", "proactive_offer"])
def test_explicit_new_offer_keeps_new_defaults(db, r6_actors, kind):
    users = r6_actors
    _, _, _, original = _publish(db, users, fields={"is_tax_inclusive": None, "price_basis": "cost"})
    inquiry, line = _destination(db, users, kind)
    draft = offers.save_draft(db, inquiry.id, line.id, users.buyer.id,
        {"unit_price": "22"}, expected_version=inquiry.version)
    method = offers.reply if kind == "sales_inquiry" else offers.publish_proactive
    published = method(db, inquiry.id, users.buyer.id,
        expected_version=inquiry.version, on_existing="new")[0]
    assert published.id == draft.id and published.id != original.id
    assert (published.is_tax_inclusive, published.price_basis) == (True, "sale")
    assert published.workbook_choice == {}


def test_draft_input_metadata_preserves_other_keys_and_reconfirms_choice(db, r6_actors):
    users = r6_actors
    _, _, _, target = _publish(db, users, fields={"is_tax_inclusive": None, "price_basis": "cost"})
    inquiry, line = _destination(db, users, "sales_inquiry")
    result = offers.save_line_decision(db, inquiry.id, line.id, users.buyer.id,
        expected_version=inquiry.version, mode="quote", data={"unit_price": "22"},
        on_existing="update", existing_offer_id=target.id)
    draft = db.get(PresalesOffer, result["offer_id"])
    draft.workbook_choice = {**draft.workbook_choice, "other_namespace": {"keep": True},
        "_draft_input": {**draft.workbook_choice["_draft_input"], "other_key": "keep"}}
    db.commit()
    db.expire_all()
    offers.save_draft(db, inquiry.id, line.id, users.buyer.id, {"unit_price": "33"},
        offer_id=draft.id, expected_version=draft.version)
    assert draft.workbook_choice["other_namespace"] == {"keep": True}
    assert draft.workbook_choice["_draft_input"]["other_key"] == "keep"
    assert not {"on_existing", "existing_offer_id", "existing_version"}.intersection(draft.workbook_choice)
    offers.save_line_decision(db, inquiry.id, line.id, users.buyer.id,
        expected_version=inquiry.version, mode="quote", data={"unit_price": "44"},
        on_existing="update", existing_offer_id=target.id)
    assert draft.workbook_choice["other_namespace"] == {"keep": True}
    assert draft.workbook_choice["_draft_input"]["other_key"] == "keep"
    assert draft.workbook_choice["existing_version"] == target.version
    assert (draft.is_tax_inclusive, draft.price_basis) == (None, "cost")


@pytest.mark.parametrize("route", ["reply", "line_decision"])
@pytest.mark.parametrize("stored,explicit,live,expected", [
    ((True, "sale"), {}, (None, "cost"), (None, "cost")),
    ((False, "cost"), {}, (True, "sale"), (False, "cost")),
    ((None, "cost"), {}, (True, "sale"), (None, "cost")),
    ((True, "sale"), {"is_tax_inclusive": True, "price_basis": "sale"}, (None, "cost"), (True, "sale")),
    ((True, "sale"), {"is_tax_inclusive": True}, (None, "cost"), (True, "cost")),
    ((True, "sale"), {"price_basis": "sale"}, (None, "cost"), (None, "sale")),
])
def test_unmarked_legacy_draft_defaults_protect_live_basis(db, r6_actors, route, stored, explicit, live, expected):
    users = r6_actors
    _, _, _, target = _publish(db, users, fields={"is_tax_inclusive": live[0], "price_basis": live[1]})
    inquiry, line = _destination(db, users, "sales_inquiry")
    # A real pre-fix row, not a draft produced by the new save_draft helper.
    draft = PresalesOffer(inquiry_id=inquiry.id, line_id=line.id, part_id=line.part_id,
        created_by=users.buyer.id, unit_price=Decimal("22"), is_tax_inclusive=stored[0],
        price_basis=stored[1], status="draft", workbook_choice={})
    db.add(draft)
    db.commit()
    db.expire_all()
    offers.save_draft(db, inquiry.id, line.id, users.buyer.id, {"unit_price": "33", **explicit},
        offer_id=draft.id, expected_version=draft.version)
    db.commit()
    db.expire_all()
    # Explicit defaults must remain explicit after a subsequent price-only save.
    offers.save_draft(db, inquiry.id, line.id, users.buyer.id, {"unit_price": "44"},
        offer_id=draft.id, expected_version=draft.version)
    if route == "line_decision":
        offers.save_line_decision(db, inquiry.id, line.id, users.buyer.id,
            expected_version=inquiry.version, mode="quote", data={"unit_price": "55"},
            on_existing="update", existing_offer_id=target.id)
        assert (draft.is_tax_inclusive, draft.price_basis) == expected
    db.commit()
    db.expire_all()
    published = offers.reply(db, inquiry.id, users.buyer.id, expected_version=inquiry.version,
        on_existing="update", existing_offer_id=target.id)[0]
    assert (published.is_tax_inclusive, published.price_basis) == expected


def test_untouched_unmarked_legacy_draft_reply_protects_live_basis(db, r6_actors):
    users = r6_actors
    _, _, _, target = _publish(db, users, fields={"is_tax_inclusive": None, "price_basis": "cost"})
    inquiry, line = _destination(db, users, "sales_inquiry")
    db.add(PresalesOffer(inquiry_id=inquiry.id, line_id=line.id, part_id=line.part_id,
        created_by=users.buyer.id, unit_price=Decimal("22"), is_tax_inclusive=True,
        price_basis="sale", status="draft", workbook_choice={}))
    db.commit()
    db.expire_all()
    published = offers.reply(db, inquiry.id, users.buyer.id, expected_version=inquiry.version,
        on_existing="update", existing_offer_id=target.id)[0]
    assert (published.is_tax_inclusive, published.price_basis) == (None, "cost")

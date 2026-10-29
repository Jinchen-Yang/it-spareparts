"""旧单读取钉住的完整快照；候选沿用按所有人权限，报价库去重。"""
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.presales import PresalesLineResponse, PresalesOfferRevision
from app.services.presales import common, inquiries, library, offers, visibility
from app.services.workflow.registry import InvalidTransition
from tests.test_presales_api_visibility import _client
from tests.test_presales_r6_existing import _publish, r6_actors
from tests.test_presales_r6_responses import _pending, _reply, _response_view


def _ctx(db, user):
    return common.resolve_actor(db, user.id)[1]


def _refer(db, users, original):
    inquiry = inquiries.create_draft(db, users.sales.id,
        lines=[{"part_id": users.part.id, "ref_offer_id": original.id}])
    inquiries.submit(db, inquiry.id, users.sales.id, inquiry.version)
    return inquiry, inquiries.lines_for(db, inquiry.id)[0]


def test_reference_keeps_pinned_price_tax_source_and_field_audience_but_is_superseded_after_update(db, r6_actors):
    u = r6_actors
    _, _, _, original = _publish(db, u, fields={"is_tax_inclusive": True,
        "supplier_audience": {"mode": "public"}, "notes_audience": {"mode": "public"}})
    inquiry, line = _refer(db, u, original)
    assert line.ref_offer_revision_no == 1
    offers.update_offer(db, original.id, u.buyer.id,
        {"unit_price": "75.00", "is_tax_inclusive": False, "source_key": "new-source",
         "notes": "新的私密内容", "notes_audience": {"mode": "private"},
         "supplier_audience": {"mode": "private"}}, expected_version=original.version)
    ref = visibility.inquiry_view(db, inquiry, _ctx(db, u.sales))["lines"][0]["ref_offer"]
    assert ref["unit_price"] == Decimal("100.00") and ref["is_tax_inclusive"] is True
    assert ref["source_key"] == "source-A" and ref["notes"] == "报价备注"
    assert ref["latest"] == {"unit_price": Decimal("75.00"), "is_tax_inclusive": False, "revision_no": 2, "price_basis": "sale"}
    assert "_inquiry" not in ref
    assert ref["expired"] is True and ref["original_withdrawn"] is False


def test_reference_keeps_snapshot_scope_and_hides_latest_when_new_scope_denies_viewer(db, r6_actors):
    u = r6_actors
    _, _, _, original = _publish(db, u)
    inquiry, _ = _refer(db, u, original)
    _publish(db, u, whole={"mode": "private"}, price="1.00", on_existing="update", existing_offer_id=original.id)
    owner = visibility.inquiry_view(db, inquiry, _ctx(db, u.sales))["lines"][0]["ref_offer"]
    assert owner["unit_price"] == Decimal("100.00") and "latest" not in owner
    boss = visibility.inquiry_view(db, inquiry, _ctx(db, u.boss))["lines"][0]["ref_offer"]
    assert boss["unit_price"] == Decimal("100.00") and boss["latest"]["unit_price"] == Decimal("1.00")
    assert library.batch(db, [u.part.id], u.sales.id)[u.part.id] == []


def test_reference_is_superseded_by_price_update_never_by_time_and_withdrawn_is_separate(db, r6_actors, monkeypatch):
    # D-0046：钉住的引用不随时间失效；原报价改价即被取代且不会复活；撤回单独表达。
    u = r6_actors
    _, _, _, original = _publish(db, u)
    inquiry, _ = _refer(db, u, original)
    later = common.now() + timedelta(days=3650)
    monkeypatch.setattr(common, "now", lambda: later)
    ref = visibility.inquiry_view(db, inquiry, _ctx(db, u.sales))["lines"][0]["ref_offer"]
    assert ref["expired"] is False and "valid_until" not in ref
    offers.update_offer(db, original.id, u.buyer.id, {"notes": "只改备注"}, expected_version=original.version)
    ref = visibility.inquiry_view(db, inquiry, _ctx(db, u.sales))["lines"][0]["ref_offer"]
    assert ref["expired"] is False
    offers.update_offer(db, original.id, u.buyer.id, {"unit_price": "90.00"}, expected_version=original.version)
    ref = visibility.inquiry_view(db, inquiry, _ctx(db, u.sales))["lines"][0]["ref_offer"]
    assert ref["expired"] is True and ref["original_withdrawn"] is False
    offers.update_offer(db, original.id, u.buyer.id, {"unit_price": "100.00"}, expected_version=original.version)
    ref = visibility.inquiry_view(db, inquiry, _ctx(db, u.sales))["lines"][0]["ref_offer"]
    assert ref["expired"] is True
    offers.withdraw(db, original.id, u.buyer.id, "报错价", expected_version=original.version)
    ref = visibility.inquiry_view(db, inquiry, _ctx(db, u.sales))["lines"][0]["ref_offer"]
    assert ref["original_withdrawn"] is True


@pytest.mark.parametrize(("changed", "restored"), [
    ({"is_tax_inclusive": False}, {"is_tax_inclusive": True}),
    ({"no_stock": True, "notes": "暂时无货"}, {"no_stock": False, "unit_price": "100.00"}),
])
def test_superseded_reference_stays_superseded_after_quote_values_return(db, r6_actors, changed, restored):
    u = r6_actors
    _, _, _, original = _publish(db, u, fields={"is_tax_inclusive": True})
    inquiry, _ = _refer(db, u, original)
    offers.update_offer(db, original.id, u.buyer.id, changed, expected_version=original.version)
    offers.update_offer(db, original.id, u.buyer.id, restored, expected_version=original.version)
    ref = visibility.inquiry_view(db, inquiry, _ctx(db, u.sales))["lines"][0]["ref_offer"]
    assert ref["expired"] is True and ref["original_withdrawn"] is False


def test_legacy_revision_without_price_basis_defaults_to_sale_in_history_and_reference(db, r6_actors):
    u = r6_actors
    _, _, _, original = _publish(db, u)
    latest = db.scalar(select(PresalesOfferRevision).where(
        PresalesOfferRevision.offer_id == original.id,
        PresalesOfferRevision.revision_no == original.revision_no))
    # 只追加升级前格式的快照，既不改写已有历史，也不依赖迁移回填 JSON。
    original.revision_no += 1
    legacy_snapshot = {key: value for key, value in latest.snapshot.items() if key != "price_basis"}
    legacy_snapshot["revision_no"] = original.revision_no
    db.add(PresalesOfferRevision(offer_id=original.id, revision_no=original.revision_no,
        snapshot=legacy_snapshot, changed_fields=[], actor=u.buyer.id))
    db.flush()
    history = visibility.revision_views(db, original.id, _ctx(db, u.buyer))
    assert history[-1]["snapshot"]["price_basis"] == "sale"
    inquiry, _ = _refer(db, u, original)
    ref = visibility.inquiry_view(db, inquiry, _ctx(db, u.sales))["lines"][0]["ref_offer"]
    assert ref["price_basis"] == "sale" and ref["expired"] is False
    db.commit()
    response = _client(u.buyer).get(f"/api/presales/offers/{original.id}/revisions")
    assert response.status_code == 200
    assert response.json()[-1]["snapshot"]["price_basis"] == "sale"
    assert all("validity_days" not in row["snapshot"] and "valid_until" not in row["snapshot"]
               for row in response.json())


def test_reuse_requires_owner_visibility_even_when_purchaser_can_see_private_quote(db, r6_actors):
    u = r6_actors
    _, _, _, private = _publish(db, u, whole={"mode": "private"})
    inquiry, lines = _pending(db, u)
    assert visibility.can_view_offer(db, private, _ctx(db, u.buyer))
    with pytest.raises(InvalidTransition):
        _reply(db, inquiry, u.buyer, [{"line_id": lines[0].id, "mode": "reuse", "ref_offer_id": private.id}])
    assert db.scalar(select(PresalesLineResponse)) is None
    db.commit()
    response = _client(u.buyer).post("/api/presales/reference", json={"inquiry_id": inquiry.id})
    assert response.status_code == 200, response.text
    assert response.json()["offers"][str(u.part.id)] == []
    denied = _client(u.buyer).post(f"/api/presales/inquiries/{inquiry.id}/reply", json={
        "expected_version": inquiry.version,
        "line_responses": [{"line_id": lines[0].id, "mode": "reuse", "ref_offer_id": private.id}]})
    assert denied.status_code == 422


def test_reuse_pins_original_reporter_time_and_masks_name_for_owner(db, r6_actors):
    u = r6_actors
    _, _, _, original = _publish(db, u, fields={"reporter_audience": {"mode": "private"}})
    published = original.valid_from
    inquiry, lines = _pending(db, u)
    _reply(db, inquiry, u.buyer, [{"line_id": lines[0].id, "mode": "reuse", "ref_offer_id": original.id}])
    row = _response_view(db, inquiry, u.sales, lines[0].id)
    assert row["ref_offer_revision_no"] == 1 and row["offer"]["revision_no"] == 1
    assert row["offer"]["unit_price"] == Decimal("100.00") and row["offer"]["valid_from"] == published
    assert "reporter" not in row["offer"] and row["reporter_label"] == "其他采购"
    offers.update_offer(db, original.id, u.buyer.id, {"unit_price": "50.00"}, expected_version=original.version)
    row = _response_view(db, inquiry, u.sales, lines[0].id)
    assert row["offer"]["unit_price"] == Decimal("100.00") and row["offer"]["latest"]["unit_price"] == Decimal("50.00")


def test_won_offer_snapshot_survives_update_and_record_scope_change(db, r6_actors):
    u = r6_actors
    _, _, _, original = _publish(db, u)
    inquiry, _ = _refer(db, u, original)
    inquiries.close(db, inquiry.id, u.sales.id, inquiry.version, outcome="won", won_offer_id=original.id)
    assert inquiry.won_offer_revision_no == 1
    _publish(db, u, whole={"mode": "private"}, price="2.00", on_existing="update", existing_offer_id=original.id)
    won = visibility.inquiry_view(db, inquiry, _ctx(db, u.sales))["won_offer"]
    assert won["unit_price"] == Decimal("100.00") and won["revision_no"] == 1 and "latest" not in won


def test_missing_historical_pin_does_not_substitute_current_price(db, r6_actors):
    u = r6_actors
    _, _, _, original = _publish(db, u)
    inquiry, line = _refer(db, u, original)
    line.ref_offer_revision_no = None
    db.flush()
    view = visibility.inquiry_view(db, inquiry, _ctx(db, u.sales))
    assert view["lines"][0]["ref_offer"] is None and view["lines"][0]["expired"] is True
    assert not view["offers"]


def test_library_groups_latest_then_sorts_tax_buckets_and_filters_more(db, r6_actors):
    u = r6_actors
    _, _, _, old = _publish(db, u, price="5.00", fields={"is_tax_inclusive": True})
    _, _, _, main = _publish(db, u, price="50.00", fields={"is_tax_inclusive": False}, on_existing="new")
    _, _, _, hidden = _publish(db, u, price="1.00", whole={"mode": "private"}, on_existing="new")
    _, _, _, peer_old = _publish(db, u, buyer=u.peer, price="40.00", fields={"is_tax_inclusive": True})
    _, _, _, peer_main = _publish(db, u, buyer=u.peer, price="100.00", fields={"is_tax_inclusive": True}, on_existing="new")
    rows = library.batch(db, [u.part.id], u.sales.id)[u.part.id]
    assert [row["id"] for row in rows] == [peer_main.id, main.id]
    assert [[item["id"] for item in row["more"]] for row in rows] == [[peer_old.id], [old.id]]
    assert hidden.id not in {item["id"] for row in rows for item in [row, *row["more"]]}
    result = next(row for row in library.search(db, u.part.pn_std, u.sales.id) if row["part_id"] == u.part.id)
    assert result["offer_count"] == 2 and [row["id"] for row in result["offers"]] == [peer_main.id, main.id]
    offers.update_offer(db, peer_main.id, u.peer.id, {"is_tax_inclusive": None, "unit_price": "1.00"}, expected_version=peer_main.version)
    rows = library.batch(db, [u.part.id], u.sales.id)[u.part.id]
    assert [row["id"] for row in rows] == [main.id, peer_main.id]


def test_same_tax_group_orders_prices_low_to_high_after_dedup(db, r6_actors):
    u = r6_actors
    _publish(db, u, price="1.00", fields={"is_tax_inclusive": True})
    _, _, _, expensive = _publish(db, u, price="100.00", fields={"is_tax_inclusive": True}, on_existing="new")
    _, _, _, cheap = _publish(db, u, buyer=u.peer, price="20.00", fields={"is_tax_inclusive": True})
    rows = library.batch(db, [u.part.id], u.sales.id)[u.part.id]
    assert [row["id"] for row in rows] == [cheap.id, expensive.id] and len(rows[1]["more"]) == 1


def test_pre_r7_snapshot_without_record_scope_rejects_new_reference_and_reuse(db, r6_actors):
    from app.models.presales import PresalesOffer, PresalesOfferRevision
    u = r6_actors
    _, _, _, original = _publish(db, u)
    # 只追加一条旧格式历史，模拟升级前无整单受众元信息的当前版本。
    original.revision_no += 1
    db.flush()
    db.add(PresalesOfferRevision(offer_id=original.id, revision_no=original.revision_no,
        snapshot={column.name: offers._json(getattr(original, column.name))
                  for column in PresalesOffer.__table__.columns}, changed_fields=[], actor=u.buyer.id))
    db.flush()
    assert visibility.pinned_offer_view(db, original.id, original.revision_no, _ctx(db, u.sales)) is None
    with pytest.raises(InvalidTransition), db.begin_nested():
        _refer(db, u, original)
    inquiry, lines = _pending(db, u)
    with pytest.raises(InvalidTransition):
        _reply(db, inquiry, u.buyer, [{"line_id": lines[0].id, "mode": "reuse", "ref_offer_id": original.id}])
    offers.update_offer(db, original.id, u.buyer.id, {"unit_price": "80.00"}, expected_version=original.version)
    linked, line = _refer(db, u, original)
    assert line.ref_offer_revision_no == 3
    assert visibility.inquiry_view(db, linked, _ctx(db, u.sales))["lines"][0]["ref_offer"]["unit_price"] == Decimal("80.00")


def test_editing_legacy_unpinned_reference_cannot_implicitly_repin_current(db, r6_actors):
    u = r6_actors
    _, _, _, original = _publish(db, u)
    inquiry, line = _refer(db, u, original)
    line.ref_offer_revision_no = None
    db.flush()
    inquiries.withdraw(db, inquiry.id, u.sales.id, inquiry.version)
    with pytest.raises(InvalidTransition), db.begin_nested():
        inquiries.update_draft(db, inquiry.id, u.sales.id, inquiry.version,
            lines=[{"line_key": line.line_key, "part_id": u.part.id, "ref_offer_id": original.id,
                    "notes": "仅修改备注"}])
    assert line.ref_offer_revision_no is None


def test_same_offer_returning_to_inquiry_keeps_original_line_snapshot_distinct_from_new_line(db, r6_actors):
    u = r6_actors
    original, first_line, _, offer = _publish(db, u, kind="sales_inquiry")
    _publish(db, u, price="80.00", on_existing="update", existing_offer_id=offer.id)
    inquiries.append_lines(db, original.id, u.sales.id, original.version, [{"part_id": u.part.id}])
    new_line = inquiries.lines_for(db, original.id)[-1]
    offers.save_draft(db, original.id, new_line.id, u.buyer.id,
        {"unit_price": "60.00"}, expected_version=original.version)
    offers.reply(db, original.id, u.buyer.id, expected_version=original.version,
                 on_existing="update", existing_offer_id=offer.id)
    view = visibility.inquiry_view(db, original, _ctx(db, u.sales))
    old = next(row for row in view["lines"] if row["id"] == first_line.id)
    assert old["linked_offers"][0]["unit_price"] == Decimal("100.00")
    assert old["linked_offers"][0]["revision_no"] == 1
    assert next(row for row in view["offers"] if row["line_id"] == new_line.id)["unit_price"] == Decimal("60.00")


def test_more_items_keep_their_own_m8_field_rules_and_field_groups(db, r6_actors):
    u = r6_actors
    _, _, _, old = _publish(db, u, fields={name + "_audience": {"mode": "private"}
        for name in ("supplier", "reporter", "notes")})
    _, _, _, current = _publish(db, u, price="50.00", on_existing="new",
        fields={name + "_audience": {"mode": "public"} for name in ("supplier", "reporter", "notes")})
    row = library.batch(db, [u.part.id], u.sales.id)[u.part.id][0]
    assert row["id"] == current.id and row["supplier_id"] == u.supplier.id
    assert row["reporter"]["id"] == u.buyer.id and row["notes"] == "报价备注"
    nested = row["more"][0]
    assert nested["id"] == old.id
    assert all(key not in nested for key in ("supplier_id", "source_key", "reporter", "notes"))
    u.sales.perm_overrides = {"data_supplier": False}
    db.flush()
    row = library.batch(db, [u.part.id], u.sales.id)[u.part.id][0]
    assert "supplier_id" not in row and "source_key" not in row
    assert all("supplier_id" not in item and "source_key" not in item for item in row["more"])


def test_reuse_owner_visibility_matches_library_after_source_record_rule_changes(db, r6_actors):
    u = r6_actors
    source, _, _, quote = _publish(db, u)
    source.offer_audience_rule_id = common.rule_id(db, {"mode": "private"}, _ctx(db, u.buyer))
    db.flush()
    assert library.batch(db, [u.part.id], u.sales.id)[u.part.id] == []
    inquiry, lines = _pending(db, u)
    with pytest.raises(InvalidTransition):
        _reply(db, inquiry, u.buyer, [{"line_id": lines[0].id, "mode": "reuse", "ref_offer_id": quote.id}])
    assert library.batch(db, [u.part.id], u.buyer.id, owner_id=u.sales.id)[u.part.id] == []

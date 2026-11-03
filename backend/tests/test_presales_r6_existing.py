"""重复报价必须明确选择，最新整单及字段受众不能沿旧单泄漏。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Barrier
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app import permissions
from app.db import SessionLocal
from app.models.dimensions import DimPart, DimSupplier
from app.models.presales import PresalesOffer, PresalesOfferRevision
from app.models.system import SysUser
from app.services.presales import common, inquiries, library, offers, visibility
from app.services.presales.registration import register_subjects
from app.services.workflow import work_items
from app.services.workflow.registry import Conflict, Forbidden, InvalidTransition
from tests.test_presales_api_visibility import _client


FIXED = datetime(2026, 10, 26, 7, tzinfo=timezone.utc)


@pytest.fixture()
def r6_actors(db, monkeypatch):
    register_subjects()
    monkeypatch.setattr(common, "now", lambda: FIXED)
    users = {}
    for name, role in (("buyer", "purchaser"), ("peer", "purchaser"),
                       ("sales", "sales"), ("other", "sales"), ("boss", "boss")):
        graph = permissions.effective(role, None)
        graph["data_supplier"] = True
        user = SysUser(username="r6_" + name, role=role, display_name=name,
                       password_hash="测试账号", template_perms=graph, perm_overrides={})
        db.add(user)
        users[name] = user
    parts = [DimPart(pn_std=f"R6-PN-{index}", description="重复报价新契约") for index in (1, 2)]
    supplier = DimSupplier(name_raw="R6 供应商")
    db.add_all([*parts, supplier])
    db.flush()
    return SimpleNamespace(**users, part=parts[0], other_part=parts[1], supplier=supplier)


def _prepared(db, users, *, kind="proactive_offer", whole=None, price="100.00",
              source="source-A", fields=None, part=None, buyer=None):
    buyer = buyer or users.buyer
    inquiry = inquiries.create_draft(db,
        buyer.id if kind == "proactive_offer" else users.sales.id,
        kind=kind, lines=[{"part_id": (part or users.part).id}],
        assignee_ids=[buyer.id] if kind == "sales_inquiry" else [],
        offer_audience=whole)
    if kind == "sales_inquiry":
        inquiries.submit(db, inquiry.id, users.sales.id, inquiry.version)
    line = inquiries.lines_for(db, inquiry.id)[0]
    draft = offers.save_draft(db, inquiry.id, line.id, buyer.id,
        {"unit_price": price, "source_key": source, 
         "supplier_id": users.supplier.id, "notes": "报价备注", **(fields or {})},
        expected_version=inquiry.version)
    return inquiry, line, draft


def _publish(db, users, *, kind="proactive_offer", on_existing=None, existing_offer_id=None, **kwargs):
    inquiry, line, draft = _prepared(db, users, kind=kind, **kwargs)
    method = offers.publish_proactive if kind == "proactive_offer" else offers.reply
    published = method(db, inquiry.id, (kwargs.get("buyer") or users.buyer).id,
        expected_version=inquiry.version, on_existing=on_existing,
        existing_offer_id=existing_offer_id)
    return inquiry, line, draft.id, published[0]


@pytest.mark.parametrize("kind", ["proactive_offer", "sales_inquiry"])
@pytest.mark.parametrize("same_source", [True, False], ids=["same-source", "other-source"])
def test_same_buyer_same_pn_requires_explicit_existing_choice(db, r6_actors, kind, same_source):
    users = r6_actors
    _, _, _, first = _publish(db, users)
    inquiry, line, draft = _prepared(db, users, kind=kind,
        source=first.source_key if same_source else "source-B", price="1.00")
    method = offers.publish_proactive if kind == "proactive_offer" else offers.reply
    with pytest.raises(Conflict) as error:
        method(db, inquiry.id, users.buyer.id, expected_version=inquiry.version)
    detail = error.value.detail
    assert detail["code"] == "existing_offer"
    assert detail["draft_offer_id"] == draft.id and detail["line_id"] == line.id
    assert [row["id"] for row in detail["existing_offers"]] == [first.id]
    assert detail["existing_offers"][0]["unit_price"] == Decimal("100.00")
    assert draft.status == "draft" and first.revision_no == 1
    assert first.unit_price == Decimal("100.00")
    if kind == "sales_inquiry":
        assert work_items.list_by_subject(db, inquiries.subject(inquiry), users.sales.id)[0].status == "pending"


def test_multiple_existing_offers_require_explicit_update_target(db, r6_actors):
    users = r6_actors
    _, _, _, first = _publish(db, users)
    _, _, _, second = _publish(db, users, source="source-B", price="90.00", on_existing="new")
    inquiry, _, draft = _prepared(db, users, price="80.00")
    with pytest.raises(Conflict) as error:
        offers.publish_proactive(db, inquiry.id, users.buyer.id,
            expected_version=inquiry.version, on_existing="update")
    assert {row["id"] for row in error.value.detail["existing_offers"]} == {first.id, second.id}
    result = offers.publish_proactive(db, inquiry.id, users.buyer.id,
        expected_version=inquiry.version,
        existing_choices={draft.id: {"on_existing": "update", "existing_offer_id": second.id}})
    assert result[0].id == second.id
    assert first.unit_price == Decimal("100.00") and first.revision_no == 1
    assert second.unit_price == Decimal("80.00") and second.revision_no == 2


def test_original_line_keeps_each_source_link_after_separate_cross_inquiry_updates(db, r6_actors):
    users = r6_actors
    original, original_line, first = _prepared(db, users, source="source-A", price="100.00")
    second = offers.save_draft(db, original.id, original_line.id, users.buyer.id,
        {"unit_price": "200.00", "source_key": "source-B"}, expected_version=original.version)
    published = offers.publish_proactive(db, original.id, users.buyer.id,
        expected_version=original.version, on_existing="new")
    assert {row.id for row in published} == {first.id, second.id}

    next_a, line_a, _, _ = _publish(db, users, source="source-A-updated", price="80.00",
        on_existing="update", existing_offer_id=first.id)
    next_b, line_b, _, _ = _publish(db, users, source="source-B-updated", price="70.00",
        whole={"mode": "private"}, on_existing="update", existing_offer_id=second.id)
    assert len({original.id, next_a.id, next_b.id}) == 3
    assert (first.inquiry_id, first.line_id) == (next_a.id, line_a.id)
    assert (second.inquiry_id, second.line_id) == (next_b.id, line_b.id)
    expected = {
        first.id: ("source-A", Decimal("100"), Decimal("80")),
        second.id: ("source-B", Decimal("200"), Decimal("70")),
    }
    for reader in (users.other, users.buyer):
        view = visibility.inquiry_view(db, original, common.resolve_actor(db, reader.id)[1])
        line, = view["lines"]
        assert set(line["linked_offer_ids"]) == expected.keys()
        for quotes in (view["offers"], line["linked_offers"]):
            assert {quote["id"]: (quote["source_key"], quote["unit_price"], quote["latest"]["unit_price"])
                    for quote in quotes} == expected
            assert all(quote["line_id"] == original_line.id and quote["revision_no"] == 1
                       and quote["latest"]["revision_no"] == 2 for quote in quotes)


def test_old_quote_still_requires_repeat_choice_until_withdrawn(db, r6_actors, monkeypatch):
    # D-0046：报价不随时间失效，多年后再报同一 PN 仍须选择更新或另报；撤回后才无需选择。
    users = r6_actors
    _, _, _, first = _publish(db, users)
    later = common.now() + timedelta(days=3650)
    monkeypatch.setattr(common, "now", lambda: later)
    with pytest.raises(Conflict), db.begin_nested():
        _publish(db, users, price="80.00")
    offers.withdraw(db, first.id, users.buyer.id, "停止供货", expected_version=first.version)
    _, _, _, fresh = _publish(db, users, price="80.00")
    assert fresh.id != first.id and first.status == "withdrawn"


def test_batch_without_existing_quote_keeps_repeated_pn_as_independent_quotes(db, r6_actors):
    users = r6_actors
    inquiry = inquiries.create_draft(db, users.buyer.id, kind="proactive_offer",
        lines=[{"part_id": users.part.id}, {"part_id": users.part.id}])
    drafts = []
    for line, price in zip(inquiries.lines_for(db, inquiry.id), ("100.00", "80.00")):
        drafts.append(offers.save_draft(db, inquiry.id, line.id, users.buyer.id,
            {"unit_price": price, "source_key": "same-source"},
            expected_version=inquiry.version))
    result = offers.publish_proactive(db, inquiry.id, users.buyer.id,
        expected_version=inquiry.version, on_existing="new")
    assert {row.id for row in result} == {row.id for row in drafts}
    assert {row.unit_price for row in result} == {Decimal("100.00"), Decimal("80.00")}
    assert db.scalar(select(func.count()).select_from(PresalesOffer)) == 2
    assert all(row.revision_no == 1 for row in result)


def test_batch_repeat_conflict_does_not_publish_earlier_lines(db, r6_actors):
    users = r6_actors
    _, _, _, existing = _publish(db, users, part=users.other_part)
    inquiry = inquiries.create_draft(db, users.buyer.id, kind="proactive_offer",
        lines=[{"part_id": users.part.id}, {"part_id": users.other_part.id}])
    drafts = [offers.save_draft(db, inquiry.id, line.id, users.buyer.id,
        {"unit_price": "80.00"}, expected_version=inquiry.version)
        for line in inquiries.lines_for(db, inquiry.id)]
    with pytest.raises(Conflict):
        offers.publish_proactive(db, inquiry.id, users.buyer.id, expected_version=inquiry.version)
    assert inquiry.status == "draft" and inquiry.version == 1
    assert all(row.status == "draft" and row.revision_no == 0 for row in drafts)
    assert db.scalar(select(func.count()).select_from(PresalesOfferRevision)) == 1
    assert existing.unit_price == Decimal("100.00")


def test_batch_cannot_update_the_same_existing_quote_twice(db, r6_actors):
    users = r6_actors
    _, _, _, existing = _publish(db, users)
    inquiry = inquiries.create_draft(db, users.buyer.id, kind="proactive_offer",
        lines=[{"part_id": users.part.id}, {"part_id": users.part.id}])
    drafts = [offers.save_draft(db, inquiry.id, line.id, users.buyer.id,
        {"unit_price": "80.00"}, expected_version=inquiry.version)
        for line in inquiries.lines_for(db, inquiry.id)]
    with pytest.raises(InvalidTransition):
        offers.publish_proactive(db, inquiry.id, users.buyer.id,
            expected_version=inquiry.version, on_existing="update", existing_offer_id=existing.id)
    assert existing.revision_no == 1 and existing.unit_price == Decimal("100.00")
    assert all(row.status == "draft" and row.revision_no == 0 for row in drafts)


@pytest.mark.parametrize("choice", [None, "new"], ids=["requires-choice", "explicit-new"])
def test_concurrent_same_pn_publications_never_merge_automatically(db, r6_actors, choice):
    users = r6_actors
    pending = [_prepared(db, users, price=price) for price in ("100.00", "80.00")]
    identities = [(row[0].id, row[0].version) for row in pending]
    buyer_id = users.buyer.id
    db.commit()
    barrier = Barrier(2)

    def publish(info):
        with SessionLocal() as other_db:
            barrier.wait(timeout=10)
            try:
                result = offers.publish_proactive(other_db, info[0], buyer_id,
                    expected_version=info[1], on_existing=choice)
                offer_id = result[0].id
                other_db.commit()
                return "published", offer_id
            except Conflict as error:
                other_db.rollback()
                return "conflict", error.detail.get("code")

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(publish, identities))
    db.expire_all()
    if choice is None:
        assert sorted(row[0] for row in result) == ["conflict", "published"]
        assert next(row[1] for row in result if row[0] == "conflict") == "existing_offer"
        assert db.scalar(select(func.count()).select_from(PresalesOfferRevision)) == 1
    else:
        assert all(row[0] == "published" for row in result)
        assert len({row[1] for row in result}) == 2
        assert db.scalar(select(func.count()).select_from(PresalesOfferRevision)) == 2
    assert all(row.revision_no == 1 for row in db.scalars(select(PresalesOffer).where(
        PresalesOffer.status == "published")))


@pytest.mark.parametrize("kind", ["proactive_offer", "sales_inquiry"])
def test_http_repeat_conflict_returns_filtered_existing_offers(db, r6_actors, kind):
    users = r6_actors
    _, _, _, first = _publish(db, users)
    inquiry, line, draft = _prepared(db, users, kind=kind, source="source-B")
    inquiry_id, version, draft_id, line_id, first_id = inquiry.id, inquiry.version, draft.id, line.id, first.id
    db.commit()
    action = "publish" if kind == "proactive_offer" else "reply"
    client = _client(users.buyer)
    response = client.post(f"/api/presales/inquiries/{inquiry_id}/{action}",
                           json={"expected_version": version})
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "existing_offer"
    assert detail["draft_offer_id"] == draft_id and detail["line_id"] == line_id
    assert [row["id"] for row in detail["existing_offers"]] == [first_id]
    assert all("created_by" not in row and "work_item_id" not in row for row in detail["existing_offers"])
    retry = client.post(f"/api/presales/inquiries/{inquiry_id}/{action}",
        json={"expected_version": version, "on_existing": "new"})
    assert retry.status_code == 200, retry.text
    assert retry.json()[0]["id"] == draft_id


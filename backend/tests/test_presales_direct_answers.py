"""D-0048：无单据报价、真实权限、并发更新与历史/当前答复的契约。"""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier

import pytest
from sqlalchemy import func, select

from app.db import SessionLocal
from app.models.presales import OfferPriceDaily, PresalesInquiry, PresalesInquiryLine, PresalesOffer, PresalesOfferRevision
from app.models.workflow import WorkItem
from app.services.presales import common, daily, inquiries, library, offers, visibility
from app.services.workflow.registry import Conflict, Forbidden, InvalidTransition
from tests.test_presales_api_visibility import _client
from tests.test_presales_r6_existing import _publish, r6_actors
from tests.test_presales_r6_responses import _pending, _reply


def _view(db, inquiry, actor):
    return visibility.inquiry_view(db, inquiry, common.resolve_actor(db, actor.id)[1])


def test_http_direct_quote_creates_no_document_line_or_task_and_updates_same_identity(db, r6_actors):
    u = r6_actors
    part_id = u.part.id
    db.commit()
    client = _client(u.buyer)
    response = client.post("/api/presales/offers/direct", json={
        "part_id": part_id, "unit_price": "123.45", "price_basis": "cost"})
    assert response.status_code == 200, response.text
    quote = response.json()
    assert quote["inquiry_id"] is None and quote["line_id"] is None
    assert quote["is_tax_inclusive"] is True and quote["price_basis"] == "cost"
    assert "valid_until" not in quote and "offer_audience_rule_id" not in quote
    for model in (PresalesInquiry, PresalesInquiryLine, WorkItem):
        assert db.scalar(select(func.count()).select_from(model)) == 0
    conflict = client.post("/api/presales/offers/direct", json={"part_id": part_id, "unit_price": "200"})
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["detail"]["code"] == "existing_offer"
    updated = client.post("/api/presales/offers/direct", json={"part_id": part_id,
        "unit_price": "234.56", "existing_offer_id": quote["id"],
        "expected_version": quote["version"], "is_tax_inclusive": None})
    assert updated.status_code == 200, updated.text
    assert updated.json()["id"] == quote["id"] and updated.json()["is_tax_inclusive"] is None
    stale = client.post("/api/presales/offers/direct", json={"part_id": part_id,
        "unit_price": "1", "existing_offer_id": quote["id"], "expected_version": quote["version"]})
    assert stale.status_code == 409
    assert db.scalar(select(func.count()).select_from(PresalesOffer)) == 1
    assert db.scalar(select(func.count()).select_from(PresalesOfferRevision)) == 2
    for model in (PresalesInquiry, PresalesInquiryLine, WorkItem):
        assert db.scalar(select(func.count()).select_from(model)) == 0


def test_standalone_quote_preserves_audience_and_supplier_privacy(db, r6_actors):
    u = r6_actors
    hidden = offers.direct_offer(db, u.buyer.id, u.part.id,
        {"unit_price": "10", "notes": "秘密", "notes_audience": {"mode": "private"}},
        offer_audience={"mode": "private"})
    assert library.batch(db, [u.part.id], u.sales.id)[u.part.id] == []
    assert library.batch(db, [u.part.id], u.buyer.id)[u.part.id][0]["id"] == hidden.id
    assert visibility.pinned_offer_view(db, hidden.id, 1, common.resolve_actor(db, u.sales.id)[1]) is None
    public = offers.direct_offer(db, u.buyer.id, u.part.id,
        {"unit_price": "20", "supplier_id": u.supplier.id, "source_key": "secret-source",
         "notes": "private-note", "notes_audience": {"mode": "private"}}, on_existing="new")
    view = library.batch(db, [u.part.id], u.sales.id)[u.part.id][0]
    assert view["id"] == public.id and "supplier_id" not in view and "source_key" not in view
    assert "notes" not in view
    with pytest.raises(Forbidden):
        offers.update_offer(db, public.id, u.peer.id, {"unit_price": "1"}, expected_version=public.version)
    with pytest.raises(Forbidden):
        offers.direct_offer(db, u.sales.id, u.part.id, {"unit_price": "1"})
    history = visibility.revision_views(db, public.id, common.resolve_actor(db, u.buyer.id)[1])
    assert history[0]["snapshot"]["inquiry_id"] is None


def test_standalone_quote_can_be_referenced_and_closed_without_mutating_won_snapshot(db, r6_actors):
    u = r6_actors
    quote = offers.direct_offer(db, u.buyer.id, u.part.id, {"unit_price": "10"})
    inquiry = inquiries.create_draft(db, u.sales.id,
        lines=[{"part_id": u.part.id, "ref_offer_id": quote.id}])
    inquiries.submit(db, inquiry.id, u.sales.id, inquiry.version)
    inquiries.close(db, inquiry.id, u.sales.id, inquiry.version, outcome="won", won_offer_id=quote.id)
    offers.update_offer(db, quote.id, u.buyer.id, {"unit_price": "20"}, expected_version=quote.version)
    view = _view(db, inquiry, u.sales)
    assert view["won_offer"]["unit_price"] == Decimal("10")
    assert view["line_answers"][0]["offer"]["unit_price"] == Decimal("20")
    assert inquiry.won_offer_revision_no == 1


def test_daily_cache_handles_mixed_standalone_and_document_quotes(db, r6_actors):
    u = r6_actors
    offers.direct_offer(db, u.buyer.id, u.part.id, {"unit_price": "10"})
    _publish(db, u, part=u.other_part, price="20")
    assert daily.rebuild(db) == 2
    assert db.scalar(select(func.count()).select_from(OfferPriceDaily)) == 2


def test_legacy_reply_can_explicitly_update_standalone_quote(db, r6_actors):
    u = r6_actors
    original = offers.direct_offer(db, u.buyer.id, u.part.id, {"unit_price": "10"})
    inquiry, line, _, updated = _publish(db, u, kind="sales_inquiry", price="20",
        on_existing="update", existing_offer_id=original.id)
    assert updated.id == original.id and updated.inquiry_id == inquiry.id and updated.line_id == line.id
    assert visibility.pinned_offer_view(db, original.id, 1,
        common.resolve_actor(db, u.buyer.id)[1])["unit_price"] == Decimal("10")


@pytest.mark.parametrize("origin_status", ["closed", "revising"])
def test_direct_update_of_existing_quote_keeps_source_and_closed_state(db, r6_actors, origin_status):
    u = r6_actors
    inquiry, line, _, quote = _publish(db, u)
    inquiry.status = origin_status
    db.flush()
    current = offers.direct_offer(db, u.buyer.id, u.part.id, {"unit_price": "75"},
        existing_offer_id=quote.id, expected_version=quote.version)
    assert current.id == quote.id and current.inquiry_id == inquiry.id and current.line_id == line.id
    assert inquiry.status == origin_status
    offers.withdraw(db, quote.id, u.buyer.id, "撤回", expected_version=quote.version)
    assert inquiry.status == origin_status and quote.status == "withdrawn"


def test_reuse_current_answer_updates_but_old_snapshot_and_completed_task_do_not(db, r6_actors):
    u = r6_actors
    quote = offers.direct_offer(db, u.buyer.id, u.part.id, {"unit_price": "123.45"})
    inquiry, lines = _pending(db, u)
    assert _view(db, inquiry, u.sales)["can_urge"] is True
    _reply(db, inquiry, u.buyer, [{"line_id": lines[0].id, "mode": "reuse", "ref_offer_id": quote.id}])
    offers.direct_offer(db, u.buyer.id, u.part.id, {"unit_price": "234.56", "price_basis": "cost"},
        existing_offer_id=quote.id, expected_version=quote.version)
    for actor in (u.sales, u.buyer):
        view = _view(db, inquiry, actor)
        answer = view["line_answers"][0]
        assert answer["state"] == "quoted" and answer["responded"] is True
        assert answer["offer"]["unit_price"] == Decimal("234.56")
        assert answer["offer"]["price_basis"] == "cost"
        assert view["line_responses"][0]["offer"]["unit_price"] == Decimal("123.45")
        assert view["line_responses"][0]["offer"]["expired"] is True
        assert view["can_urge"] is False
    with pytest.raises(InvalidTransition):
        inquiries.urge(db, inquiry.id, u.sales.id)
    offers.withdraw(db, quote.id, u.buyer.id, "撤回报价", expected_version=quote.version)
    view = _view(db, inquiry, u.sales)
    assert view["line_answers"][0] == {"line_id": lines[0].id, "state": "unavailable", "responded": True}
    assert view["can_urge"] is False and view["progress"]["quoted_lines"] == 1


def test_current_answer_never_leaks_new_private_price_or_reporter(db, r6_actors):
    u = r6_actors
    _, _, _, quote = _publish(db, u)
    inquiry, lines = _pending(db, u)
    _reply(db, inquiry, u.buyer, [{"line_id": lines[0].id, "mode": "reuse", "ref_offer_id": quote.id}])
    _publish(db, u, whole={"mode": "private"}, price="987.65", on_existing="update", existing_offer_id=quote.id)
    view = _view(db, inquiry, u.sales)
    assert view["line_answers"][0]["state"] == "unavailable"
    assert "987.65" not in str(view)


def test_paused_tasks_cannot_be_urged_in_read_or_write_paths(db, r6_actors):
    u = r6_actors
    inquiry, _ = _pending(db, u)
    for item in db.scalars(select(WorkItem).where(
            WorkItem.subject_type == "presales_inquiry", WorkItem.subject_id == inquiry.id)):
        item.paused = True
    db.flush()
    assert _view(db, inquiry, u.sales)["can_urge"] is False
    with pytest.raises(InvalidTransition):
        inquiries.urge(db, inquiry.id, u.sales.id)


def test_concurrent_first_direct_quotes_create_only_one_offer(db, r6_actors):
    u = r6_actors
    actor_id, part_id = u.buyer.id, u.part.id
    db.commit()
    barrier = Barrier(2)
    def publish(price):
        with SessionLocal() as connection:
            barrier.wait(timeout=10)
            try:
                row = offers.direct_offer(connection, actor_id, part_id, {"unit_price": price})
                result = ("created", row.id)
                connection.commit()
                return result
            except Conflict as error:
                connection.rollback()
                return ("conflict", error.detail["code"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(publish, ("10", "20")))
    assert sorted(row[0] for row in results) == ["conflict", "created"]
    assert next(row[1] for row in results if row[0] == "conflict") == "existing_offer"
    assert db.scalar(select(func.count()).select_from(PresalesOffer)) == 1
    assert db.scalar(select(func.count()).select_from(PresalesInquiry)) == 0


def test_concurrent_direct_updates_require_same_offer_version(db, r6_actors):
    u = r6_actors
    original = offers.direct_offer(db, u.buyer.id, u.part.id, {"unit_price": "10"})
    actor_id, part_id, offer_id, version = u.buyer.id, u.part.id, original.id, original.version
    db.commit()
    barrier = Barrier(2)
    def update(price):
        with SessionLocal() as connection:
            barrier.wait(timeout=10)
            try:
                offers.direct_offer(connection, actor_id, part_id, {"unit_price": price},
                    existing_offer_id=offer_id, expected_version=version)
                connection.commit()
                return "updated"
            except Conflict:
                connection.rollback()
                return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, ("20", "30")))
    assert sorted(results) == ["conflict", "updated"]
    db.expire_all()
    assert db.get(PresalesOffer, offer_id).version == version + 1
    assert db.scalar(select(func.count()).select_from(PresalesOfferRevision)) == 2

"""报价发布历史、自然日边界、独立回复与报价库缓存契约。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from itertools import count
from threading import Barrier
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.db import SessionLocal
from app.models.dimensions import DimPart, DimSupplier
from app.models.presales import (
    OfferPriceDaily, PresalesInquiry, PresalesInquiryLine, PresalesOffer, PresalesOfferRevision,
)
from app.models.system import SysUser
from app.services.presales import common, daily, library, offers
from app.services.presales.registration import register_subjects
from app.services.workflow import work_items
from app.services.workflow.registry import Conflict, Forbidden, InvalidTransition, Subject

_numbers = count(1)
FIXED = datetime(2026, 10, 25, 7, 0, tzinfo=timezone.utc)


@pytest.fixture()
def actors(db):
    register_subjects()
    users = {}
    for name, role in (("sales", "sales"), ("other", "sales"),
                       ("buyer", "purchaser"), ("buyer2", "purchaser"), ("boss", "boss")):
        user = SysUser(username="presales_" + name, role=role, password_hash="测试账号",
                       display_name=name, is_active=True)
        db.add(user)
        users[name] = user
    part = DimPart(pn_std="PRESALES-PN", description="报价测试型号")
    supplier = DimSupplier(name_raw="报价供货商")
    db.add_all([part, supplier])
    db.flush()
    return SimpleNamespace(**users, part=part, supplier=supplier)


def _inquiry(db, actors, *, kind="proactive_offer", line_count=1):
    owner = actors.buyer if kind == "proactive_offer" else actors.sales
    inquiry = PresalesInquiry(kind=kind, inquiry_no=f"T{next(_numbers):012d}",
        status="draft", created_by=owner.id, version=1, revision=1)
    db.add(inquiry)
    db.flush()
    lines = [PresalesInquiryLine(inquiry_id=inquiry.id, line_key=str(i), seq=i + 1,
        part_id=actors.part.id, pn_raw=actors.part.pn_std, qty=1, status="active")
        for i in range(line_count)]
    db.add_all(lines)
    db.flush()
    return inquiry, lines


def _draft(db, inquiry, line, actor, **data):
    return offers.save_draft(db, inquiry.id, line.id, actor.id,
        {"unit_price": "100.25", "validity_days": 3, **data}, expected_version=inquiry.version)


def _published(db, actors, monkeypatch, **data):
    monkeypatch.setattr(common, "now", lambda: FIXED)
    inquiry, lines = _inquiry(db, actors)
    draft = _draft(db, inquiry, lines[0], actors.buyer, **data)
    result = offers.publish_proactive(db, inquiry.id, actors.buyer.id, expected_version=inquiry.version)
    return inquiry, result[0]


@pytest.mark.parametrize("days", [1, 3, 30])
def test_offer_validity_is_shanghai_natural_day_and_strict_boundary(days):
    # UTC 16:30 已经是上海次日，不能按 UTC 日期计算。
    published = datetime(2026, 10, 24, 16, 30, tzinfo=timezone.utc)
    expected_day = date(2026, 10, 25) + timedelta(days=days - 1)
    local = offers.offer_valid_until(published, days).astimezone(offers.SHANGHAI)
    assert local.date() == expected_day
    assert (local.hour, local.minute, local.second, local.microsecond) == (23, 59, 59, 999999)
    assert local.tzinfo == offers.SHANGHAI


@pytest.mark.parametrize("days", [0, -1, True, 1.5])
def test_offer_validity_rejects_invalid_day_counts(days):
    with pytest.raises(ValueError):
        offers.offer_valid_until(FIXED, days)


def test_offer_validity_requires_explicit_timezone():
    with pytest.raises(ValueError):
        offers.offer_valid_until(datetime(2026, 10, 25), 1)


def test_publish_update_renew_withdraw_appends_complete_snapshots(db, actors, monkeypatch):
    inquiry, offer = _published(db, actors, monkeypatch, supplier_id=actors.supplier.id,
                              notes="原备注", qty_available="3.500", is_tax_inclusive=True)
    assert offer.revision_no == 1 and offer.status == "published"
    first_until = offer.valid_until
    renewed = FIXED + timedelta(days=2)
    monkeypatch.setattr(common, "now", lambda: renewed)
    offers.update_offer(db, offer.id, actors.buyer.id,
        {"unit_price": "102.50", "validity_days": 3}, expected_version=offer.version)
    assert offer.revision_no == 2 and offer.valid_until == first_until + timedelta(days=2)
    history = list(db.scalars(select(PresalesOfferRevision).where(
        PresalesOfferRevision.offer_id == offer.id).order_by(PresalesOfferRevision.revision_no)))
    assert history[0].snapshot["unit_price"] == "100.25"
    assert history[1].snapshot["unit_price"] == "102.50"
    assert set(history[1].snapshot) == set(PresalesOffer.__table__.columns.keys())
    assert history[1].snapshot["supplier_id"] == actors.supplier.id
    assert history[1].snapshot["notes"] == "原备注"
    assert history[1].snapshot["is_tax_inclusive"] is True
    with pytest.raises(InvalidTransition):
        offers.withdraw(db, offer.id, actors.buyer.id, "  ", expected_version=offer.version)
    offers.withdraw(db, offer.id, actors.buyer.id, "价格填写有误", expected_version=offer.version)
    assert offer.status == "withdrawn" and offer.revision_no == 3
    assert library.batch(db, [actors.part.id], actors.sales.id, now=renewed)[actors.part.id] == []


@pytest.mark.parametrize("days", [1, 3, 30])
def test_library_expires_at_end_microsecond_and_next_midnight(db, actors, monkeypatch, days):
    _, offer = _published(db, actors, monkeypatch, validity_days=days)
    ids = [actors.part.id]
    assert library.batch(db, ids, actors.sales.id, now=offer.valid_until - timedelta(microseconds=1))[actors.part.id]
    assert library.batch(db, ids, actors.sales.id, now=offer.valid_until)[actors.part.id] == []
    assert library.batch(db, ids, actors.sales.id, now=offer.valid_until + timedelta(microseconds=1))[actors.part.id] == []


def test_repeat_proactive_quote_updates_existing_same_source_and_keeps_other_sources(db, actors, monkeypatch):
    _, first = _published(db, actors, monkeypatch, source_key="stock-A")
    inquiry, lines = _inquiry(db, actors)
    _draft(db, inquiry, lines[0], actors.buyer, source_key="stock-A", unit_price="90.00")
    repeated = offers.publish_proactive(db, inquiry.id, actors.buyer.id, expected_version=inquiry.version)
    assert repeated[0].id == first.id and first.revision_no == 2
    assert first.unit_price == Decimal("90.00") and lines[0].ref_offer_id == first.id
    assert db.scalar(select(func.count()).select_from(PresalesOffer)) == 1
    other, rows = _inquiry(db, actors)
    _draft(db, other, rows[0], actors.buyer, source_key="stock-B")
    second = offers.publish_proactive(db, other.id, actors.buyer.id, expected_version=other.version)[0]
    assert second.id != first.id


def test_concurrent_repeat_proactive_quotes_are_serialized(db, actors, monkeypatch):
    monkeypatch.setattr(common, "now", lambda: FIXED)
    pending = []
    for price in ("100.00", "110.00"):
        inquiry, lines = _inquiry(db, actors)
        _draft(db, inquiry, lines[0], actors.buyer, unit_price=price, source_key="same")
        pending.append((inquiry.id, inquiry.version))
    user_id = actors.buyer.id
    db.commit()
    barrier = Barrier(2)

    def publish(info):
        with SessionLocal() as other_db:
            barrier.wait(timeout=10)
            result = offers.publish_proactive(other_db, info[0], user_id, expected_version=info[1])
            offer_id = result[0].id
            other_db.commit()
            return offer_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(publish, pending))
    assert len(set(results)) == 1
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(PresalesOffer)) == 1
    current = db.get(PresalesOffer, results[0])
    assert current.revision_no == 2


def test_same_batch_repeated_part_and_source_merge_before_header_is_published(db, actors, monkeypatch):
    monkeypatch.setattr(common, "now", lambda: FIXED)
    inquiry, lines = _inquiry(db, actors, line_count=2)
    first = _draft(db, inquiry, lines[0], actors.buyer, source_key="same", unit_price="100.00")
    _draft(db, inquiry, lines[1], actors.buyer, source_key="same", unit_price="88.00")
    result = offers.publish_proactive(db, inquiry.id, actors.buyer.id, expected_version=inquiry.version)
    assert [offer.id for offer in result] == [first.id]
    assert first.unit_price == Decimal("88.00") and first.revision_no == 2
    assert lines[1].ref_offer_id == first.id
    assert db.scalar(select(func.count()).select_from(PresalesOffer)) == 1


def test_post_binding_multi_part_publish_uses_consistent_lock_order(db, actors, monkeypatch):
    monkeypatch.setattr(common, "now", lambda: FIXED)
    part2 = DimPart(pn_std="PRESALES-SECOND-PN")
    db.add(part2)
    db.flush()
    pending = []
    for ids in ((actors.part.id, part2.id), (part2.id, actors.part.id)):
        inquiry, lines = _inquiry(db, actors, line_count=2)
        for line in lines:
            line.part_id = None
        db.flush()
        for line in lines:
            _draft(db, inquiry, line, actors.buyer, source_key="same")
        for line, part_id in zip(lines, ids):
            line.part_id = part_id
        db.flush()
        pending.append((inquiry.id, inquiry.version))
    user_id = actors.buyer.id
    db.commit()
    barrier = Barrier(2)

    def publish(info):
        with SessionLocal() as other_db:
            other_db.execute(text("SET LOCAL lock_timeout = '5s'"))
            barrier.wait(timeout=10)
            result = offers.publish_proactive(other_db, info[0], user_id, expected_version=info[1])
            ids = {offer.id for offer in result}
            other_db.commit()
            return ids

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(publish, pending))
    assert results[0] == results[1]
    db.expire_all()
    current = list(db.scalars(select(PresalesOffer)))
    assert len(current) == 2 and all(offer.revision_no == 2 for offer in current)


def test_repeat_publish_conflicts_when_original_header_is_locked(db, actors, monkeypatch):
    original, old = _published(db, actors, monkeypatch, source_key="same")
    inquiry, lines = _inquiry(db, actors)
    draft = _draft(db, inquiry, lines[0], actors.buyer, source_key="same", unit_price="88.00")
    ids = (original.id, inquiry.id, inquiry.version, actors.buyer.id, draft.id, old.id)
    db.commit()
    with SessionLocal() as closer:
        common.lock_inquiry(closer, ids[0])
        with SessionLocal() as publisher:
            with pytest.raises(Conflict):
                offers.publish_proactive(publisher, ids[1], ids[3], expected_version=ids[2])
            publisher.rollback()
        closer.rollback()
    db.expire_all()
    assert db.get(PresalesInquiry, ids[1]).status == "draft"
    assert db.get(PresalesOffer, ids[4]).status == "draft"
    assert db.get(PresalesOffer, ids[5]).unit_price == Decimal("100.25")


def test_two_assignees_reply_independently_and_require_all_lines(db, actors, monkeypatch):
    monkeypatch.setattr(common, "now", lambda: FIXED)
    inquiry, lines = _inquiry(db, actors, kind="sales_inquiry", line_count=2)
    inquiry.status = "submitted"
    work_items.open_items(db, Subject("presales_inquiry", inquiry.id),
        [actors.buyer.id, actors.buyer2.id], "presales_inquiry", actors.sales.id)
    own = _draft(db, inquiry, lines[0], actors.buyer)
    _draft(db, inquiry, lines[0], actors.buyer2, unit_price="99.00")
    with pytest.raises(InvalidTransition):
        offers.reply(db, inquiry.id, actors.buyer.id, expected_version=inquiry.version)
    assert own.status == "draft" and own.revision_no == 0
    no_stock = _draft(db, inquiry, lines[1], actors.buyer, no_stock=True)
    first_version = inquiry.version
    offers.reply(db, inquiry.id, actors.buyer.id, expected_version=first_version)
    assert inquiry.version == first_version and no_stock.no_stock and no_stock.unit_price is None
    items = work_items.subject_items(db, Subject("presales_inquiry", inquiry.id), actors.sales.id)
    assert {item.assignee_id: item.status for item in items} == {
        actors.buyer.id: "done", actors.buyer2.id: "pending"}
    _draft(db, inquiry, lines[1], actors.buyer2, no_stock=True)
    offers.reply(db, inquiry.id, actors.buyer2.id, expected_version=first_version)
    assert all(item.status == "done" for item in work_items.subject_items(
        db, Subject("presales_inquiry", inquiry.id), actors.sales.id))
    assert own.revision_no == 1


def test_reopened_handler_reuses_own_line_and_source_quote(db, actors, monkeypatch):
    monkeypatch.setattr(common, "now", lambda: FIXED)
    inquiry, lines = _inquiry(db, actors, kind="sales_inquiry")
    inquiry.status = "submitted"
    work_items.open_items(db, Subject("presales_inquiry", inquiry.id),
        [actors.buyer.id], "presales_inquiry", actors.sales.id)
    original = _draft(db, inquiry, lines[0], actors.buyer, source_key="same")
    offers.reply(db, inquiry.id, actors.buyer.id, expected_version=inquiry.version)
    work_items.open_items(db, Subject("presales_inquiry", inquiry.id),
        [actors.buyer.id], "presales_inquiry", actors.sales.id)
    _draft(db, inquiry, lines[0], actors.buyer, source_key="same", unit_price="88.00")
    result = offers.reply(db, inquiry.id, actors.buyer.id, expected_version=inquiry.version)
    assert [row.id for row in result] == [original.id]
    assert original.unit_price == Decimal("88.00") and original.revision_no == 2
    assert db.scalar(select(func.count()).select_from(PresalesOffer)) == 1


def test_draft_follows_part_binding_done_after_draft_was_saved(db, actors, monkeypatch):
    monkeypatch.setattr(common, "now", lambda: FIXED)
    inquiry, lines = _inquiry(db, actors, kind="sales_inquiry")
    lines[0].part_id = None
    inquiry.status = "submitted"
    db.flush()
    work_items.open_items(db, Subject("presales_inquiry", inquiry.id),
        [actors.buyer.id], "presales_inquiry", actors.sales.id)
    draft = _draft(db, inquiry, lines[0], actors.buyer)
    assert draft.part_id is None
    lines[0].part_id = actors.part.id
    db.flush()
    offers.reply(db, inquiry.id, actors.buyer.id, expected_version=inquiry.version)
    assert draft.status == "published" and draft.part_id == actors.part.id


def test_default_source_separates_suppliers(db, actors, monkeypatch):
    _, initial = _published(db, actors, monkeypatch, supplier_id=actors.supplier.id)
    supplier2 = DimSupplier(name_raw="另一个货源")
    db.add(supplier2)
    db.flush()
    inquiry, lines = _inquiry(db, actors)
    _draft(db, inquiry, lines[0], actors.buyer, supplier_id=supplier2.id)
    second = offers.publish_proactive(db, inquiry.id, actors.buyer.id,
        expected_version=inquiry.version)[0]
    assert initial.source_key != second.source_key and initial.id != second.id


def test_source_update_cannot_create_a_duplicate_published_quote(db, actors, monkeypatch):
    _, initial = _published(db, actors, monkeypatch, source_key="A")
    _, second = _published(db, actors, monkeypatch, source_key="B")
    with pytest.raises(Conflict), db.begin_nested():
        offers.update_offer(db, second.id, actors.buyer.id,
            {"source_key": "A"}, expected_version=second.version)
    assert initial.source_key == "A" and second.source_key == "B"
    assert second.revision_no == 1


def test_updating_explicit_draft_requires_its_own_version(db, actors):
    inquiry, lines = _inquiry(db, actors)
    draft = _draft(db, inquiry, lines[0], actors.buyer)
    offers.save_draft(db, inquiry.id, lines[0].id, actors.buyer.id, {"unit_price": "88.00"},
        expected_version=draft.version, offer_id=draft.id)
    assert draft.version == 2
    with pytest.raises(Conflict):
        offers.save_draft(db, inquiry.id, lines[0].id, actors.buyer.id, {"unit_price": "1.00"},
            expected_version=1, offer_id=draft.id)


def test_implicit_draft_update_returns_existing_identity_without_overwriting(db, actors):
    inquiry, lines = _inquiry(db, actors)
    draft = _draft(db, inquiry, lines[0], actors.buyer)
    with pytest.raises(Conflict) as error:
        _draft(db, inquiry, lines[0], actors.buyer, unit_price="1.00")
    assert error.value.detail["offer_id"] == draft.id
    assert error.value.detail["version"] == draft.version
    assert draft.unit_price == Decimal("100.25")


def test_concurrent_draft_creates_do_not_overwrite_the_winner(db, actors):
    inquiry, lines = _inquiry(db, actors)
    ids = (inquiry.id, lines[0].id, actors.buyer.id, inquiry.version)
    db.commit()
    barrier = Barrier(2)

    def save(price):
        with SessionLocal() as other_db:
            barrier.wait(timeout=10)
            try:
                result = offers.save_draft(other_db, ids[0], ids[1], ids[2],
                    {"unit_price": price, "source_key": "same"}, expected_version=ids[3])
                result_id = result.id
                other_db.commit()
                return ("created", result_id)
            except Conflict as error:
                other_db.rollback()
                return ("conflict", error.detail["offer_id"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, ("100.00", "110.00")))
    assert sorted(row[0] for row in results) == ["conflict", "created"]
    assert len({row[1] for row in results}) == 1
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(PresalesOffer)) == 1
    assert db.get(PresalesOffer, results[0][1]).version == 1


def test_draft_other_actor_version_conflict_and_closed_update_are_rejected(db, actors, monkeypatch):
    inquiry, offer = _published(db, actors, monkeypatch)
    with pytest.raises(Forbidden):
        offers.update_offer(db, offer.id, actors.buyer2.id, {"unit_price": "1.00"}, expected_version=offer.version)
    with pytest.raises(Conflict):
        offers.update_offer(db, offer.id, actors.buyer.id, {"unit_price": "1.00"}, expected_version=1)
    inquiry.status = "closed"
    db.flush()
    with pytest.raises(InvalidTransition):
        offers.update_offer(db, offer.id, actors.buyer.id, {"unit_price": "1.00"}, expected_version=offer.version)
    with pytest.raises(InvalidTransition):
        offers.withdraw(db, offer.id, actors.buyer.id, "撤销", expected_version=offer.version)


def test_repeat_quote_does_not_change_closed_inquiry(db, actors, monkeypatch):
    previous, old = _published(db, actors, monkeypatch)
    previous.status = "closed"
    db.flush()
    inquiry, lines = _inquiry(db, actors)
    _draft(db, inquiry, lines[0], actors.buyer, unit_price="90.00")
    current = offers.publish_proactive(db, inquiry.id, actors.buyer.id,
        expected_version=inquiry.version)[0]
    assert current.id != old.id
    assert old.unit_price == Decimal("100.25") and old.revision_no == 1


def test_no_stock_not_in_library_and_not_boosted(db, actors, monkeypatch):
    _, offer = _published(db, actors, monkeypatch, no_stock=True)
    assert offer.status == "published" and offer.unit_price is None
    assert library.batch(db, [actors.part.id], actors.sales.id)[actors.part.id] == []
    assert library.boost_part_ids(db, "PRESALES", actors.sales.id) == []


def test_private_inquiry_has_no_library_or_search_weight_side_channel(db, actors, monkeypatch):
    inquiry, offer = _published(db, actors, monkeypatch)
    _, ctx = common.resolve_actor(db, actors.buyer.id)
    inquiry.offer_audience_rule_id = common.rule_id(db, {"mode": "private"}, ctx)
    db.flush()
    assert library.batch(db, [actors.part.id], actors.sales.id)[actors.part.id] == []
    assert library.boost_part_ids(db, "PRESALES", actors.sales.id) == []
    assert library.boost_part_ids(db, "PRESALES") == []
    assert library.boost_part_ids(db, "PRESALES", actors.buyer.id) == [actors.part.id]
    assert library.boost_part_ids(db, "PRESALES", actors.boss.id) == [actors.part.id]


def test_proactive_notice_targets_must_be_allowed_to_view_published_inquiry(db, actors, monkeypatch):
    monkeypatch.setattr(common, "now", lambda: FIXED)
    inquiry, lines = _inquiry(db, actors)
    draft = _draft(db, inquiry, lines[0], actors.buyer)
    _, ctx = common.resolve_actor(db, actors.buyer.id)
    inquiry.offer_audience_rule_id = common.rule_id(db, {"mode": "private"}, ctx)
    db.flush()
    with pytest.raises(InvalidTransition):
        offers.publish_proactive(db, inquiry.id, actors.buyer.id,
            expected_version=inquiry.version, notice_user_ids=[actors.sales.id])
    assert inquiry.status == "draft" and draft.status == "draft" and draft.revision_no == 0
    assert work_items.subject_items(db, Subject("presales_offer_notice", inquiry.id), actors.buyer.id) == []
    inquiry.offer_audience_rule_id = common.rule_id(db, {"mode": "public"}, ctx)
    db.flush()
    offers.publish_proactive(db, inquiry.id, actors.buyer.id,
        expected_version=inquiry.version, notice_user_ids=[actors.sales.id])
    items = work_items.subject_items(db, Subject("presales_offer_notice", inquiry.id), actors.sales.id)
    assert len(items) == 1 and items[0].assignee_id == actors.sales.id


@pytest.mark.parametrize("statement", [
    "UPDATE presales_offer_revision SET reason='篡改' WHERE id=:id",
    "DELETE FROM presales_offer_revision WHERE id=:id",
])
def test_revision_trigger_rejects_update_and_delete(db, actors, monkeypatch, statement):
    _, offer = _published(db, actors, monkeypatch)
    revision = db.scalar(select(PresalesOfferRevision).where(PresalesOfferRevision.offer_id == offer.id))
    with pytest.raises(DBAPIError), db.begin_nested():
        db.execute(text(statement), {"id": revision.id})


def test_daily_rebuild_uses_last_daily_snapshot_and_keeps_tax_bases_separate(db, actors, monkeypatch):
    _, first = _published(db, actors, monkeypatch, source_key="A", is_tax_inclusive=True)
    offers.update_offer(db, first.id, actors.buyer.id, {"unit_price": "120.00"}, expected_version=first.version)
    _, second = _published(db, actors, monkeypatch, source_key="B", is_tax_inclusive=False)
    assert daily.rebuild(db) == 2
    initial = {(row.part_id, row.date, row.source, row.min, row.max, row.avg, row.count)
        for row in db.scalars(select(OfferPriceDaily))}
    assert {row[2] for row in initial} == {"tax_inclusive", "tax_exclusive"}
    assert next(row for row in initial if row[2] == "tax_inclusive")[3:] == (
        Decimal("120.00"), Decimal("120.00"), Decimal("120.00"), 1)
    assert daily.rebuild(db) == 2
    assert initial == {(row.part_id, row.date, row.source, row.min, row.max, row.avg, row.count)
        for row in db.scalars(select(OfferPriceDaily))}
    tomorrow = FIXED + timedelta(days=1)
    monkeypatch.setattr(common, "now", lambda: tomorrow)
    offers.update_offer(db, second.id, actors.buyer.id, {"unit_price": "80.00"}, expected_version=second.version)
    assert daily.rebuild(db, since=tomorrow.astimezone(offers.SHANGHAI).date()) == 1
    assert len(list(db.scalars(select(OfferPriceDaily)))) == 3

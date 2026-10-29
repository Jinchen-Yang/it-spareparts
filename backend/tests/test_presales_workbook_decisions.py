"""工作簿只暂存采购选择，正式回复才发布并向销售展示。"""

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, select

from app.models.presales import PresalesLineResponse, PresalesOffer, PresalesOfferRevision
from app.services.presales import common, inquiries, offers, visibility
from app.services.workflow import work_items
from app.services.workflow.registry import Conflict, InvalidTransition
from tests.conftest import _alembic_cfg
from tests.test_presales_r6_existing import _publish, r6_actors
from tests.test_presales_r6_responses import _item, _pending


def _save(db, users, inquiry, line, **choice):
    return offers.save_line_decision(db, inquiry.id, line.id, users.buyer.id,
                                    expected_version=inquiry.version, **choice)


def _view(db, inquiry, user):
    return visibility.inquiry_view(db, inquiry, common.resolve_actor(db, user.id)[1])


def test_workbook_quote_stays_draft_until_explicit_reply(db, r6_actors):
    users = r6_actors
    inquiry, lines = _pending(db, users)
    result = _save(db, users, inquiry, lines[0], mode="quote",
                   data={"unit_price": "88.00"})
    draft = db.get(PresalesOffer, result["offer_id"])
    assert draft.status == "draft" and draft.revision_no == 0
    assert _item(db, inquiry, users, users.buyer).status == "pending"
    assert _view(db, inquiry, users.sales)["progress"]["quoted_lines"] == 0
    assert _view(db, inquiry, users.sales)["offers"] == []
    assert list(db.scalars(select(PresalesOfferRevision))) == []
    offers.reply(db, inquiry.id, users.buyer.id, expected_version=inquiry.version)
    assert draft.status == "published" and draft.workbook_choice == {}
    assert _item(db, inquiry, users, users.buyer).status == "done"


@pytest.mark.parametrize("mode", ["reuse", "skip"])
def test_workbook_line_decisions_are_private_and_do_not_complete_work(db, r6_actors, mode):
    users = r6_actors
    _, _, _, original = _publish(db, users)
    inquiry, lines = _pending(db, users)
    choice = {"ref_offer_id": original.id} if mode == "reuse" else {"reason": "本轮不报"}
    _save(db, users, inquiry, lines[0], mode=mode, **choice)
    saved = db.scalar(select(PresalesLineResponse).where(PresalesLineResponse.line_id == lines[0].id))
    assert saved.is_draft is True
    assert _view(db, inquiry, users.sales)["line_responses"] == []
    assert _view(db, inquiry, users.sales)["progress"]["quoted_lines"] == 0
    assert _view(db, inquiry, users.buyer)["line_responses"][0]["is_draft"] is True
    item = _item(db, inquiry, users, users.buyer)
    with pytest.raises(InvalidTransition):
        work_items.complete(db, item.id, users.buyer.id, expected_version=item.version)
    offers.reply(db, inquiry.id, users.buyer.id, expected_version=inquiry.version)
    assert saved.is_draft is False
    assert _view(db, inquiry, users.sales)["line_responses"][0]["mode"] == mode
    assert item.status == "done"


def test_workbook_cannot_adopt_an_unreplied_reuse(db, r6_actors):
    users = r6_actors
    _, _, _, original = _publish(db, users)
    inquiry, lines = _pending(db, users)
    _save(db, users, inquiry, lines[0], mode="reuse", ref_offer_id=original.id)
    with pytest.raises(InvalidTransition, match="采纳报价"):
        inquiries.close(db, inquiry.id, users.sales.id, inquiry.version,
                        outcome="won", won_offer_id=original.id)


def test_workbook_reuse_checks_owner_visibility_and_reply_validity(db, r6_actors, monkeypatch):
    users = r6_actors
    _, _, _, private = _publish(db, users, whole={"mode": "private"})
    inquiry, lines = _pending(db, users)
    with pytest.raises(InvalidTransition, match="不可见"):
        _save(db, users, inquiry, lines[0], mode="reuse", ref_offer_id=private.id)
    _, _, _, public = _publish(db, users, on_existing="new")
    _save(db, users, inquiry, lines[0], mode="reuse", ref_offer_id=public.id)
    offers.withdraw(db, public.id, users.buyer.id, "停止供货", expected_version=public.version)
    with pytest.raises(InvalidTransition, match="失效"):
        offers.reply(db, inquiry.id, users.buyer.id, expected_version=inquiry.version)
    assert _item(db, inquiry, users, users.buyer).status == "pending"


def test_workbook_reuse_does_not_silently_repin_updated_price(db, r6_actors):
    users = r6_actors
    _, _, _, original = _publish(db, users)
    inquiry, lines = _pending(db, users)
    _save(db, users, inquiry, lines[0], mode="reuse", ref_offer_id=original.id)
    offers.update_offer(db, original.id, users.buyer.id, {"unit_price": "77.00"},
                        expected_version=original.version)
    with pytest.raises(Conflict, match="沿用报价已更新"):
        offers.reply(db, inquiry.id, users.buyer.id, expected_version=inquiry.version)
    _save(db, users, inquiry, lines[0], mode="reuse", ref_offer_id=original.id)
    offers.reply(db, inquiry.id, users.buyer.id, expected_version=inquiry.version)
    saved = db.scalar(select(PresalesLineResponse).where(PresalesLineResponse.line_id == lines[0].id))
    assert saved.ref_offer_revision_no == original.revision_no


@pytest.mark.parametrize("action", ["new", "update"])
def test_workbook_remembers_explicit_existing_quote_choice(db, r6_actors, action):
    users = r6_actors
    _, _, _, original = _publish(db, users)
    inquiry, lines = _pending(db, users)
    result = _save(db, users, inquiry, lines[0], mode="quote", on_existing=action,
                   data={"unit_price": "88.00"})
    draft = db.get(PresalesOffer, result["offer_id"])
    assert draft.workbook_choice["on_existing"] == action
    assert "workbook_choice" not in _view(db, inquiry, users.buyer)["offers"][0]
    published = offers.reply(db, inquiry.id, users.buyer.id, expected_version=inquiry.version)[0]
    assert published.id == (original.id if action == "update" else draft.id)
    assert published.unit_price == Decimal("88.00")
    assert published.workbook_choice == {}


def test_workbook_update_refuses_changed_target_and_page_edit_clears_choice(db, r6_actors):
    users = r6_actors
    _, _, _, original = _publish(db, users)
    inquiry, lines = _pending(db, users)
    result = _save(db, users, inquiry, lines[0], mode="quote", on_existing="update",
                   data={"unit_price": "88.00"})
    draft = db.get(PresalesOffer, result["offer_id"])
    offers.update_offer(db, original.id, users.buyer.id, {"unit_price": "99.00"},
                        expected_version=original.version)
    with pytest.raises(Conflict, match="原报价已更新"):
        offers.reply(db, inquiry.id, users.buyer.id, expected_version=inquiry.version)
    offers.save_draft(db, inquiry.id, lines[0].id, users.buyer.id, {"unit_price": "66.00"},
                      offer_id=draft.id, expected_version=draft.version)
    assert draft.workbook_choice == {}
    with pytest.raises(Conflict, match="你已有有效报价"):
        offers.reply(db, inquiry.id, users.buyer.id, expected_version=inquiry.version)


@pytest.mark.parametrize("mode", ["reuse", "skip"])
def test_page_quote_replaces_unreplied_workbook_decision(db, r6_actors, mode):
    users = r6_actors
    _, _, _, original = _publish(db, users)
    inquiry, lines = _pending(db, users)
    choice = {"ref_offer_id": original.id} if mode == "reuse" else {"reason": "暂不报"}
    _save(db, users, inquiry, lines[0], mode=mode, **choice)
    draft = offers.save_draft(db, inquiry.id, lines[0].id, users.buyer.id,
        {"unit_price": "66.00"}, expected_version=inquiry.version)
    assert db.scalar(select(PresalesLineResponse).where(
        PresalesLineResponse.line_id == lines[0].id)) is None
    result = offers.reply(db, inquiry.id, users.buyer.id,
        expected_version=inquiry.version, on_existing="new")
    assert len(result) == 1 and result[0].id == draft.id
    assert draft.status == "published" and draft.unit_price == Decimal("66.00")


def test_expected_delivery_roundtrips_through_services_and_copy(db, r6_actors):
    users = r6_actors
    inquiry = inquiries.create_draft(db, users.sales.id, lines=[{
        "part_id": users.part.id, "expected_delivery": "2026-12-01"}])
    line = inquiries.lines_for(db, inquiry.id)[0]
    assert line.expected_delivery == date(2026, 12, 1)
    assert _view(db, inquiry, users.sales)["lines"][0]["expected_delivery"] == date(2026, 12, 1)
    copied = inquiries.copy_as_new(db, inquiry.id, users.sales.id, inquiry.version)
    assert inquiries.lines_for(db, copied.id)[0].expected_delivery == date(2026, 12, 1)
    inquiries.update_draft(db, inquiry.id, users.sales.id, inquiry.version, lines=[{
        "line_key": line.line_key, "expected_delivery": None}])
    assert line.expected_delivery is None


@pytest.mark.parametrize("value", ["2026-13-01", "20261201", "2026-12-01T12:00:00", 123,
                                   datetime(2026, 12, 1)])
def test_expected_delivery_rejects_non_date_values(db, r6_actors, value):
    with pytest.raises(InvalidTransition, match="期望交期"):
        inquiries.create_draft(db, r6_actors.sales.id, lines=[{
            "part_id": r6_actors.part.id, "expected_delivery": value}])


def test_workbook_fields_migration_roundtrip_and_zero_drift(db):
    cfg = _alembic_cfg()
    scripts = ScriptDirectory.from_config(cfg)
    assert scripts.get_revision("d6e8f0a2b4c6").down_revision == "c4e6f8a0b2d5"
    fields = {"presales_inquiry_line": "expected_delivery", "presales_offer": "workbook_choice",
              "presales_line_response": "is_draft"}
    engine = db.get_bind()
    db.close()
    try:
        command.downgrade(cfg, "c4e6f8a0b2d5")
        assert not inspect(engine).has_table("presales_workbook_export")
        for table, name in fields.items():
            assert name not in {column["name"] for column in inspect(engine).get_columns(table)}
        command.upgrade(cfg, "head")
        command.check(cfg)
        assert inspect(engine).has_table("presales_workbook_export")
        for table, name in fields.items():
            assert name in {column["name"] for column in inspect(engine).get_columns(table)}
    finally:
        command.upgrade(cfg, "head")

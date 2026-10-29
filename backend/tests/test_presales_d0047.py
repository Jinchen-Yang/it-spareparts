"""D-0047：询价优先级、催办（30 分钟限一次）、坏件是否返回与报价价格性质。"""

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models.workflow import SubjectEvent
from app.services.presales import common, inquiries, offers, visibility
from app.services.workflow.registry import Conflict, Forbidden, InvalidTransition
from tests.test_presales_r6_existing import FIXED, r6_actors  # noqa: F401
from tests.test_presales_r6_responses import _pending, _reply


def _view(db, inquiry, user):
    return visibility.inquiry_view(db, inquiry, common.resolve_actor(db, user.id)[1])


def test_new_fields_default_for_existing_style_payloads(db, r6_actors):
    users = r6_actors
    inquiry, lines = _pending(db, users)
    view = _view(db, inquiry, users.buyer)
    assert view["priority"] == "normal" and view["urge_count"] == 0 and view["last_urged_at"] is None
    assert view["lines"][0]["return_defective"] is True
    offer = offers.save_draft(db, inquiry.id, lines[0].id, users.buyer.id, {"unit_price": "10.00"},
                              expected_version=inquiry.version)
    assert offer.price_basis == "sale"


def test_priority_and_return_defective_are_written_and_editable(db, r6_actors):
    users = r6_actors
    draft = inquiries.create_draft(db, users.sales.id, priority="urgent",
        lines=[{"part_id": users.part.id, "return_defective": False}])
    assert draft.priority == "urgent"
    assert inquiries.lines_for(db, draft.id)[0].return_defective is False
    line_key = inquiries.lines_for(db, draft.id)[0].line_key
    updated = inquiries.update_draft(db, draft.id, users.sales.id, draft.version, priority="normal",
        lines=[{"line_key": line_key, "return_defective": True}])
    assert updated.priority == "normal"
    assert inquiries.lines_for(db, draft.id)[0].return_defective is True
    with pytest.raises(InvalidTransition):
        inquiries.create_draft(db, users.sales.id, priority="特急")
    with pytest.raises(InvalidTransition):
        inquiries.create_draft(db, users.sales.id, lines=[{"part_id": users.part.id, "return_defective": "是"}])
    copied = inquiries.copy_as_new(db, draft.id, users.sales.id, updated.version)
    assert copied.priority == "normal"


def test_price_basis_saved_validated_and_kept_through_reply(db, r6_actors):
    users = r6_actors
    inquiry, lines = _pending(db, users)
    offer = offers.save_draft(db, inquiry.id, lines[0].id, users.buyer.id,
        {"unit_price": "88.00", "is_tax_inclusive": True, "price_basis": "cost"}, expected_version=inquiry.version)
    with pytest.raises(InvalidTransition):
        offers.update_offer(db, offer.id, users.buyer.id, {"price_basis": "批发价"}, expected_version=offer.version)
    published = _reply(db, inquiry, users.buyer, [{"line_id": lines[0].id, "mode": "quote"}])
    assert published[0].price_basis == "cost"
    # 价格性质只是标注：发起销售照常看到这条报价与其价格性质。
    sales_view = next(row for row in _view(db, inquiry, users.sales)["offers"] if row["id"] == published[0].id)
    assert sales_view["price_basis"] == "cost" and str(sales_view["unit_price"]) == "88.00"
    updated = offers.update_offer(db, published[0].id, users.buyer.id, {"price_basis": "sale"},
                                  expected_version=published[0].version)
    assert updated.price_basis == "sale"


def test_owner_can_urge_open_inquiry_once_per_thirty_minutes(db, r6_actors, monkeypatch):
    users = r6_actors
    inquiry, _ = _pending(db, users)
    version = inquiry.version
    urged = inquiries.urge(db, inquiry.id, users.sales.id)
    assert urged.urge_count == 1 and urged.last_urged_at == FIXED
    # 催办不递增单据版本，采购手上的保存不会因此冲突。
    assert urged.version == version
    monkeypatch.setattr(common, "now", lambda: FIXED + timedelta(minutes=29))
    with pytest.raises(Conflict) as error:
        inquiries.urge(db, inquiry.id, users.sales.id)
    assert error.value.detail["code"] == "urge_too_soon" and "1 分钟" in str(error.value)
    monkeypatch.setattr(common, "now", lambda: FIXED + timedelta(minutes=30))
    assert inquiries.urge(db, inquiry.id, users.sales.id).urge_count == 2
    urges = db.scalars(select(SubjectEvent).where(SubjectEvent.subject_type == "presales_inquiry",
        SubjectEvent.subject_id == inquiry.id, SubjectEvent.kind == "inquiry.urged")).all()
    assert [row.payload["urge_count"] for row in urges] == [1, 2]


def test_urge_rejects_non_owner_draft_and_fully_answered(db, r6_actors):
    users = r6_actors
    inquiry, lines = _pending(db, users)
    for actor in (users.other, users.buyer):
        with pytest.raises(Forbidden):
            inquiries.urge(db, inquiry.id, actor.id)
    draft = inquiries.create_draft(db, users.sales.id, lines=[{"part_id": users.part.id}])
    with pytest.raises(InvalidTransition):
        inquiries.urge(db, draft.id, users.sales.id)
    _reply(db, inquiry, users.buyer, [{"line_id": lines[0].id, "mode": "skip", "reason": "停产"}])
    with pytest.raises(InvalidTransition):
        inquiries.urge(db, inquiry.id, users.sales.id)

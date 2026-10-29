"""旧行响应不能计入修改后的询价进度。"""

import pytest
from sqlalchemy import select

from app.models.presales import PresalesLineResponse
from app.services.presales import common, inquiries, offers, visibility
from app.services.workflow import work_items
from tests.test_presales_r6_existing import r6_actors
from tests.test_presales_r6_responses import _pending, _reply


def test_skipped_line_must_be_handled_again_after_withdraw_and_edit(db, r6_actors):
    users = r6_actors
    inquiry, lines = _pending(db, users)
    line = lines[0]
    _reply(db, inquiry, users.buyer,
           [{"line_id": line.id, "mode": "skip", "reason": "原数量暂无货"}])
    contexts = [common.resolve_actor(db, viewer.id)[1]
                for viewer in (users.sales, users.buyer, users.boss)]
    for ctx in contexts:
        assert visibility.inquiry_view(db, inquiry, ctx)["progress"] == {
            "quoted_lines": 1, "line_count": 1, "effective_quoted_lines": 0}

    inquiries.withdraw(db, inquiry.id, users.sales.id, inquiry.version)
    inquiries.update_draft(db, inquiry.id, users.sales.id, inquiry.version,
        lines=[{"line_key": line.line_key, "qty": 2}])
    response = db.scalar(select(PresalesLineResponse).where(PresalesLineResponse.line_id == line.id))
    assert response.line_version != line.version
    for ctx in contexts:
        for detail in (True, False):
            assert visibility.inquiry_view(db, inquiry, ctx, detail=detail)["progress"] == {
                "quoted_lines": 0, "line_count": 1, "effective_quoted_lines": 0}

    inquiries.resubmit(db, inquiry.id, users.sales.id, inquiry.version)
    for ctx in contexts:
        assert visibility.inquiry_view(db, inquiry, ctx)["progress"] == {
            "quoted_lines": 0, "line_count": 1, "effective_quoted_lines": 0}


@pytest.mark.parametrize("original", [True, False])
def test_return_defective_change_reopens_reply_and_invalidates_old_quote(db, r6_actors, original):
    u = r6_actors
    inquiry, lines = _pending(db, u, lines=[{"part_id": u.part.id, "return_defective": original}])
    line = lines[0]
    offers.save_draft(db, inquiry.id, line.id, u.buyer.id, {"unit_price": "10"}, expected_version=inquiry.version)
    quote = offers.reply(db, inquiry.id, u.buyer.id, expected_version=inquiry.version)[0]
    inquiries.withdraw(db, inquiry.id, u.sales.id, inquiry.version)
    inquiries.update_draft(db, inquiry.id, u.sales.id, inquiry.version,
        lines=[{"line_key": line.line_key, "return_defective": not original}])
    inquiries.resubmit(db, inquiry.id, u.sales.id, inquiry.version)
    assert quote.status == "stale"
    assert any(item.status in work_items.ACTIVE for item in
        work_items.list_by_subject(db, inquiries.subject(inquiry), u.sales.id))
    view = visibility.inquiry_view(db, inquiry, common.resolve_actor(db, u.sales.id)[1])
    assert view["line_answers"] == [{"line_id": line.id, "state": "pending", "responded": False}]
    assert view["can_urge"] is True


def test_legacy_snapshot_without_return_defective_keeps_default_true_reply_done(db, r6_actors):
    u = r6_actors
    inquiry, lines = _pending(db, u)
    line = lines[0]
    offers.save_draft(db, inquiry.id, line.id, u.buyer.id, {"unit_price": "10"}, expected_version=inquiry.version)
    quote = offers.reply(db, inquiry.id, u.buyer.id, expected_version=inquiry.version)[0]
    inquiry.submitted_snapshot = {**inquiry.submitted_snapshot, "lines": {
        key: {field: value for field, value in signature.items() if field != "return_defective"}
        for key, signature in inquiry.submitted_snapshot["lines"].items()}}
    db.flush()
    inquiries.withdraw(db, inquiry.id, u.sales.id, inquiry.version)
    inquiries.update_draft(db, inquiry.id, u.sales.id, inquiry.version,
        lines=[{"line_key": line.line_key, "notes": "只有备注变化", "return_defective": True}])
    inquiries.resubmit(db, inquiry.id, u.sales.id, inquiry.version)
    assert quote.status == "published"
    assert all(item.status == "done" for item in
        work_items.list_by_subject(db, inquiries.subject(inquiry), u.sales.id))
    view = visibility.inquiry_view(db, inquiry, common.resolve_actor(db, u.sales.id)[1])
    assert view["line_answers"][0]["state"] == "quoted" and view["can_urge"] is False

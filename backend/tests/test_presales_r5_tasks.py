"""引用状态和旧版本回复不会阻塞重新派单。"""

from datetime import datetime, timezone

import pytest

from app.services.presales import common, inquiries, offers
from app.services.workflow import work_items
from app.services.workflow.registry import Forbidden
from tests.test_presales_inquiries import presales_parts, presales_users


def _submitted(db, users, parts, *, pool=False, reference=None):
    inquiry = inquiries.create_draft(db, users.sales.id,
        assignee_ids=[] if pool else [users.buyer.id],
        lines=[{"line_key": "original", "part_id": parts[0].id, "ref_offer_id": reference}])
    inquiries.submit(db, inquiry.id, users.sales.id, inquiry.version)
    return inquiry


def _quote(db, inquiry, line, buyer):
    offer = offers.save_draft(db, inquiry.id, line.id, buyer.id,
        {"unit_price": "123.45"}, expected_version=inquiry.version)
    offers.reply(db, inquiry.id, buyer.id, expected_version=inquiry.version, on_existing="new")
    return offer


def _reference(db, users, parts):
    original = _submitted(db, users, parts)
    return _quote(db, original, inquiries.lines_for(db, original.id)[0], users.buyer)


def _assert_pending_has_handler(db, inquiry, users):
    assert inquiry.status == "submitted"
    assert any(line.ref_offer_id is None for line in inquiries.lines_for(db, inquiry.id))
    items = work_items.list_by_subject(db, inquiries.subject(inquiry), users.sales.id)
    active = [item for item in items if item.status in work_items.ACTIVE and not item.paused]
    assert active, "已提交且有待报价行的询价单必须至少有一个活动处理人或公共池待办"
    return active


@pytest.mark.parametrize("pool", [False, True], ids=["assigned", "pool"])
def test_removing_only_reference_resubmit_dispatches_and_buyer_can_quote(
        db, presales_users, presales_parts, pool):
    users, parts = presales_users, presales_parts
    reference = _reference(db, users, parts)
    inquiry = _submitted(db, users, parts, pool=pool, reference=reference.id)
    assert work_items.list_by_subject(db, inquiries.subject(inquiry), users.sales.id) == []
    inquiries.withdraw(db, inquiry.id, users.sales.id, inquiry.version)
    inquiries.update_draft(db, inquiry.id, users.sales.id, inquiry.version,
                           lines=[{"line_key": "original", "ref_offer_id": None}])
    inquiries.resubmit(db, inquiry.id, users.sales.id, inquiry.version)
    active = _assert_pending_has_handler(db, inquiry, users)
    assert len(active) == 1
    _, buyer_ctx = common.resolve_actor(db, users.buyer.id)
    visible = work_items.visible_items(db, buyer_ctx, "pool" if pool else "assigned")
    assert active[0].id in {item["id"] for item in visible["items"]}
    if pool:
        work_items.claim(db, active[0].id, users.buyer.id, active[0].version)
    quote = _quote(db, inquiry, inquiries.lines_for(db, inquiry.id)[0], users.buyer)
    assert quote.status == "published" and active[0].status == "done"


@pytest.mark.parametrize("pool", [False, True], ids=["assigned", "pool"])
def test_removing_reference_reopens_previously_completed_handler(
        db, presales_users, presales_parts, pool):
    users, parts = presales_users, presales_parts
    reference = _reference(db, users, parts)
    inquiry = _submitted(db, users, parts, pool=pool)
    item = _assert_pending_has_handler(db, inquiry, users)[0]
    if pool:
        work_items.claim(db, item.id, users.buyer.id, item.version)
    line = inquiries.lines_for(db, inquiry.id)[0]
    _quote(db, inquiry, line, users.buyer)
    inquiries.withdraw(db, inquiry.id, users.sales.id, inquiry.version)
    inquiries.update_draft(db, inquiry.id, users.sales.id, inquiry.version,
                           lines=[{"line_key": line.line_key, "ref_offer_id": reference.id}])
    inquiries.resubmit(db, inquiry.id, users.sales.id, inquiry.version)
    assert item.status == "done"
    inquiries.withdraw(db, inquiry.id, users.sales.id, inquiry.version)
    inquiries.update_draft(db, inquiry.id, users.sales.id, inquiry.version,
                           lines=[{"line_key": line.line_key, "ref_offer_id": None}])
    inquiries.resubmit(db, inquiry.id, users.sales.id, inquiry.version)
    assert [active.id for active in _assert_pending_has_handler(db, inquiry, users)] == [item.id]
    assert item.status == "pending"


def test_adding_reference_cancels_quote_task_without_staling_offer(
        db, presales_users, presales_parts):
    users, parts = presales_users, presales_parts
    inquiry = _submitted(db, users, parts)
    line = inquiries.lines_for(db, inquiry.id)[0]
    quote = _quote(db, inquiry, line, users.buyer)
    work_items.open_items(db, inquiries.subject(inquiry), [users.buyer.id], inquiries.SUBJECT_TYPE,
                          users.sales.id)
    inquiries.withdraw(db, inquiry.id, users.sales.id, inquiry.version)
    inquiries.update_draft(db, inquiry.id, users.sales.id, inquiry.version,
                           lines=[{"line_key": line.line_key, "ref_offer_id": quote.id}])
    inquiries.resubmit(db, inquiry.id, users.sales.id, inquiry.version)
    assert quote.status == "published"
    assert not [item for item in work_items.list_by_subject(db, inquiries.subject(inquiry), users.sales.id)
                if item.status in work_items.ACTIVE]
    with pytest.raises(Forbidden, match="待办"):
        offers.save_draft(db, inquiry.id, line.id, users.buyer.id,
            {"unit_price": "99.00"}, expected_version=inquiry.version)


def test_legacy_snapshot_notes_only_resubmit_keeps_completed_handler_closed(
        db, presales_users, presales_parts):
    users, parts = presales_users, presales_parts
    inquiry = _submitted(db, users, parts)
    line = inquiries.lines_for(db, inquiry.id)[0]
    quote = _quote(db, inquiry, line, users.buyer)
    inquiry.submitted_snapshot = {**inquiry.submitted_snapshot, "lines": {
        key: {field: value for field, value in signature.items() if field != "has_reference"}
        for key, signature in inquiry.submitted_snapshot["lines"].items()}}
    inquiries.withdraw(db, inquiry.id, users.sales.id, inquiry.version)
    inquiries.update_draft(db, inquiry.id, users.sales.id, inquiry.version,
                           lines=[{"line_key": line.line_key, "notes": "只补备注"}])
    inquiries.resubmit(db, inquiry.id, users.sales.id, inquiry.version)
    assert quote.status == "published"
    assert [item.status for item in work_items.list_by_subject(
        db, inquiries.subject(inquiry), users.sales.id)] == ["done"]


def test_legacy_reference_snapshot_without_tasks_dispatches_after_reference_removed(
        db, presales_users, presales_parts):
    users, parts = presales_users, presales_parts
    reference = _reference(db, users, parts)
    inquiry = _submitted(db, users, parts, reference=reference.id)
    inquiry.submitted_snapshot = {**inquiry.submitted_snapshot, "lines": {
        key: {field: value for field, value in signature.items() if field != "has_reference"}
        for key, signature in inquiry.submitted_snapshot["lines"].items()}}
    inquiries.withdraw(db, inquiry.id, users.sales.id, inquiry.version)
    inquiries.update_draft(db, inquiry.id, users.sales.id, inquiry.version,
                           lines=[{"line_key": "original", "ref_offer_id": None}])
    inquiries.resubmit(db, inquiry.id, users.sales.id, inquiry.version)
    active = _assert_pending_has_handler(db, inquiry, users)
    assert len(active) == 1 and active[0].assignee_id == users.buyer.id


@pytest.mark.parametrize("pool", [False, True], ids=["assigned", "claimed-pool"])
@pytest.mark.parametrize("status", ["done", "declined"])
@pytest.mark.parametrize("change", ["append", "quantity", "new-line"])
def test_new_version_reopens_done_or_declined_and_has_active_handler(
        db, presales_users, presales_parts, pool, status, change):
    users, parts = presales_users, presales_parts
    inquiry = _submitted(db, users, parts, pool=pool)
    item = _assert_pending_has_handler(db, inquiry, users)[0]
    if pool:
        work_items.claim(db, item.id, users.buyer.id, item.version)
    line = inquiries.lines_for(db, inquiry.id)[0]
    if status == "done":
        _quote(db, inquiry, line, users.buyer)
    else:
        work_items.decline(db, item.id, users.buyer.id, "旧版本无货", item.version)
    assert item.status == status
    if change == "append":
        inquiries.append_lines(db, inquiry.id, users.sales.id, inquiry.version,
                               [{"part_id": parts[1].id}])
    else:
        inquiries.withdraw(db, inquiry.id, users.sales.id, inquiry.version)
        lines = [{"line_key": line.line_key, **({"qty": 2} if change == "quantity" else {})}]
        if change == "new-line":
            lines.append({"part_id": parts[1].id})
        inquiries.update_draft(db, inquiry.id, users.sales.id, inquiry.version, lines=lines)
        inquiries.resubmit(db, inquiry.id, users.sales.id, inquiry.version)
    assert [active.id for active in _assert_pending_has_handler(db, inquiry, users)] == [item.id]
    assert item.status == "pending" and item.closed_at is None
    assert item.assignee_id == users.buyer.id
    assert item.claimed_by == (users.buyer.id if pool else None)


def test_legacy_declined_unclaimed_pool_does_not_block_new_pool_item(
        db, presales_users, presales_parts):
    users, parts = presales_users, presales_parts
    inquiry = _submitted(db, users, parts, pool=True)
    previous = _assert_pending_has_handler(db, inquiry, users)[0]
    # 兼容历史公共池终态记录；未领单的旧记录没有可恢复的实名处理人。
    previous.status, previous.closed_at = "declined", datetime.now(timezone.utc)
    db.flush()
    inquiries.append_lines(db, inquiry.id, users.sales.id, inquiry.version,
                           [{"part_id": parts[1].id}])
    active = _assert_pending_has_handler(db, inquiry, users)
    assert len(active) == 1 and active[0].id != previous.id
    assert active[0].assignee_id is None and active[0].claimed_by is None
    assert previous.status == "declined"


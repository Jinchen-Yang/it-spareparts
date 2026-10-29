"""销售询价生命周期与待建档行的服务契约。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Barrier
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app import permissions
from app.db import SessionLocal
from app.models.dimensions import DimPart
from app.models.presales import PresalesInquiry, PresalesInquiryLine, PresalesOffer, PresalesOfferRevision
from app.models.system import SysAccessLog, SysUser
from app.services import field_pool, master_edit
from app.services.presales import binding, common, inquiries
from app.services.presales.registration import register_subjects
from app.services.workflow import work_items
from app.services.workflow.registry import Conflict, Forbidden, InvalidTransition


@pytest.fixture()
def presales_users(db):
    register_subjects()
    output = {}
    for name, role in (("sales", "sales"), ("other_sales", "sales"),
                       ("buyer", "purchaser"), ("peer", "purchaser"), ("boss", "boss")):
        user = SysUser(username=f"presales-{name}", role=role, password_hash="测试不登录",
                       display_name=name, is_active=True, template_perms=permissions.effective(role, None),
                       perm_overrides={})
        db.add(user)
        output[name] = user
    db.flush()
    return SimpleNamespace(**output)


@pytest.fixture()
def presales_parts(db):
    parts = [DimPart(pn_std=f"PS-TEST-{index}", description=f"型号 {index}") for index in range(1, 6)]
    db.add_all(parts)
    db.flush()
    return parts


def _create(db, users, parts, *, count=1, pool=False):
    inquiry = inquiries.create_draft(db, users.sales.id,
        assignee_ids=[] if pool else [users.buyer.id, users.peer.id],
        lines=[{"line_key": f"line-{n}", "part_id": parts[n].id, "qty": n + 1}
               for n in range(count)])
    return inquiries.submit(db, inquiry.id, users.sales.id, inquiry.version)


def _items(db, inquiry, users):
    return work_items.list_by_subject(db, inquiries.subject(inquiry), users.sales.id)


def _offer(db, inquiry, line, buyer, *, valid_until=None, with_snapshot=False):
    offer = PresalesOffer(inquiry_id=inquiry.id, line_id=line.id, part_id=line.part_id,
        created_by=buyer.id, unit_price=Decimal("12.34"), is_tax_inclusive=True,
        status="published", no_stock=False, valid_from=datetime.now(timezone.utc),
        valid_until=valid_until,
        revision_no=0, version=1, source_key="test")
    db.add(offer)
    db.flush()
    if with_snapshot:
        from app.services.presales.offers import append_revision
        append_revision(db, offer, buyer.id, PresalesOffer.__table__.columns.keys(), "测试发布完整快照")
    return offer


def test_two_independent_tasks_append_close_and_copy(db, presales_users, presales_parts):
    users, parts = presales_users, presales_parts
    inquiry = _create(db, users, parts)
    assert inquiry.inquiry_no.startswith("XJ") and len(inquiry.inquiry_no) == 13
    items = _items(db, inquiry, users)
    assert {item.assignee_id for item in items} == {users.buyer.id, users.peer.id}
    for buyer in (users.buyer, users.peer):
        _offer(db, inquiry, inquiries.lines_for(db, inquiry.id)[0], buyer)
    work_items.complete(db, items[0].id, items[0].assignee_id, items[0].version)
    assert items[1].status == "pending"
    work_items.complete(db, items[1].id, items[1].assignee_id, items[1].version)
    inquiry = inquiries.append_lines(db, inquiry.id, users.sales.id, inquiry.version,
                                    [{"part_id": parts[1].id, "qty": 2}])
    assert inquiry.revision == 2
    assert all(item.status == "pending" for item in _items(db, inquiry, users))
    inquiry = inquiries.close(db, inquiry.id, users.sales.id, inquiry.version, outcome="lost")
    assert all(item.status == "cancelled" for item in _items(db, inquiry, users))
    copied = inquiries.copy_as_new(db, inquiry.id, users.sales.id, inquiry.version)
    assert copied.id != inquiry.id and copied.status == "draft" and copied.revision == 1
    assert copied.inquiry_no != inquiry.inquiry_no
    assert len(inquiries.lines_for(db, copied.id)) == 2
    assert not {line.line_key for line in inquiries.lines_for(db, copied.id)} & {
        line.line_key for line in inquiries.lines_for(db, inquiry.id)}
    with pytest.raises(InvalidTransition):
        inquiries.append_lines(db, inquiry.id, users.sales.id, inquiry.version, [{"part_id": parts[2].id}])


def test_withdraw_resubmit_diffs_and_notes_preserve_quotes(db, presales_users, presales_parts):
    users, parts = presales_users, presales_parts
    inquiry = _create(db, users, parts, count=4)
    old_lines = inquiries.lines_for(db, inquiry.id)
    offered = [_offer(db, inquiry, line, users.buyer) for line in old_lines]
    first = _items(db, inquiry, users)[0]
    work_items.complete(db, first.id, first.assignee_id, first.version)
    inquiries.withdraw(db, inquiry.id, users.sales.id, inquiry.version)
    assert inquiry.status == "revising"
    assert all(item.paused for item in _items(db, inquiry, users) if item.status in work_items.ACTIVE)
    inquiries.update_draft(db, inquiry.id, users.sales.id, inquiry.version, lines=[
        {"line_key": "line-0", "qty": 99},
        {"line_key": "line-2", "notes": "只改备注", "field_values": [{"new_name": "质保", "value": "一年"}]},
        {"line_key": "line-3", "require_exact_pn": True},
        {"line_key": "new", "part_id": parts[4].id},
    ])
    inquiries.resubmit(db, inquiry.id, users.sales.id, inquiry.version)
    assert inquiry.status == "submitted" and inquiry.revision == 2
    assert [offer.status for offer in offered] == ["stale", "stale", "published", "stale"]
    assert db.scalar(select(func.count()).select_from(PresalesOfferRevision)) == 3
    assert all(item.status == "pending" and not item.paused for item in _items(db, inquiry, users))
    assert db.scalar(select(PresalesInquiryLine.status).where(
        PresalesInquiryLine.inquiry_id == inquiry.id, PresalesInquiryLine.line_key == "line-1")) == "deleted"


def test_pn_change_stales_but_notes_only_resubmit_does_not_reopen_done(db, presales_users, presales_parts):
    users, parts = presales_users, presales_parts
    inquiry = _create(db, users, parts)
    line = inquiries.lines_for(db, inquiry.id)[0]
    offer = _offer(db, inquiry, line, users.buyer)
    _offer(db, inquiry, line, users.peer)
    for item in _items(db, inquiry, users):
        work_items.complete(db, item.id, item.assignee_id, item.version)
    inquiries.withdraw(db, inquiry.id, users.sales.id, inquiry.version)
    inquiries.update_draft(db, inquiry.id, users.sales.id, inquiry.version,
                           lines=[{"line_key": line.line_key, "notes": "备注更新"}])
    inquiries.resubmit(db, inquiry.id, users.sales.id, inquiry.version)
    assert offer.status == "published"
    assert all(item.status == "done" for item in _items(db, inquiry, users))
    inquiries.withdraw(db, inquiry.id, users.sales.id, inquiry.version)
    inquiries.update_draft(db, inquiry.id, users.sales.id, inquiry.version,
                           lines=[{"line_key": line.line_key, "part_id": parts[1].id, "pn_raw": parts[1].pn_std}])
    inquiries.resubmit(db, inquiry.id, users.sales.id, inquiry.version)
    assert offer.status == "stale"
    assert all(item.status == "pending" for item in _items(db, inquiry, users))


def test_effective_reference_skips_dispatch_and_withdrawn_is_rejected(db, presales_users, presales_parts):
    users, parts = presales_users, presales_parts
    original = _create(db, users, parts)
    line = inquiries.lines_for(db, original.id)[0]
    offer = _offer(db, original, line, users.buyer, with_snapshot=True)
    reused = inquiries.create_draft(db, users.sales.id, assignee_ids=[users.buyer.id],
        lines=[{"part_id": parts[0].id, "ref_offer_id": offer.id}])
    inquiries.submit(db, reused.id, users.sales.id, reused.version)
    assert _items(db, reused, users) == []
    inquiries.append_lines(db, reused.id, users.sales.id, reused.version, [{"part_id": parts[1].id}])
    assert len(_items(db, reused, users)) == 1
    # D-0046：已停用的 valid_until 即使早已过去，也不会让报价失效。
    offer.valid_until = datetime.now(timezone.utc) - timedelta(days=30)
    db.flush()
    inquiries.create_draft(db, users.sales.id, lines=[{"part_id": parts[0].id, "ref_offer_id": offer.id}])
    offer.status = "withdrawn"
    db.flush()
    with pytest.raises(InvalidTransition, match="有效"), db.begin_nested():
        inquiries.create_draft(db, users.sales.id, lines=[{"part_id": parts[0].id, "ref_offer_id": offer.id}])


def test_won_offer_must_belong_to_this_inquiry(db, presales_users, presales_parts):
    users, parts = presales_users, presales_parts
    first = _create(db, users, parts)
    other = _create(db, users, parts)
    own_offer = _offer(db, first, inquiries.lines_for(db, first.id)[0], users.buyer, with_snapshot=True)
    with pytest.raises(InvalidTransition):
        inquiries.close(db, other.id, users.sales.id, other.version, outcome="won", won_offer_id=own_offer.id)
    inquiries.close(db, first.id, users.sales.id, first.version, outcome="won", won_offer_id=own_offer.id)
    assert first.won_offer_id == own_offer.id


def test_draft_versions_ownership_and_originally_private_copy(db, presales_users, presales_parts):
    users, parts = presales_users, presales_parts
    inquiry = inquiries.create_draft(db, users.sales.id, notes="单头私密", notes_audience={"mode": "private"},
        offer_audience={"mode": "private"}, lines=[{"part_id": parts[0].id, "notes": "行私密",
                                                    "notes_audience": {"mode": "private"}}])
    version = inquiry.version
    inquiries.update_draft(db, inquiry.id, users.sales.id, version, notes="新备注")
    with pytest.raises(Conflict):
        inquiries.update_draft(db, inquiry.id, users.sales.id, version, notes="过期覆盖")
    with pytest.raises(InvalidTransition, match="expected_version"):
        inquiries.delete_draft(db, inquiry.id, users.sales.id, None)
    with pytest.raises(Forbidden):
        inquiries.update_draft(db, inquiry.id, users.other_sales.id, inquiry.version, notes="越权")
    copied = inquiries.copy_as_new(db, inquiry.id, users.sales.id, inquiry.version)
    assert copied.offer_audience_rule_id == inquiry.offer_audience_rule_id
    assert copied.notes_audience_rule_id == inquiry.notes_audience_rule_id
    assert inquiries.lines_for(db, copied.id)[0].notes_audience_rule_id == inquiries.lines_for(db, inquiry.id)[0].notes_audience_rule_id
    inquiries.delete_draft(db, inquiry.id, users.sales.id, inquiry.version)
    assert inquiry.closed_outcome == "cancelled" and inquiry.status == "closed"
    assert db.scalar(select(func.count()).select_from(SysAccessLog)) > 0


def test_field_pool_remembers_pn_with_empty_next_inputs(db, presales_users, presales_parts):
    users, parts = presales_users, presales_parts
    first = inquiries.create_draft(db, users.sales.id, lines=[{"part_id": parts[0].id,
        "field_values": [{"new_name": "质保要求", "value": "三年"}]}])
    _, ctx = common.resolve_actor(db, users.sales.id)
    definition = next(field for field in field_pool.list_defs(db, inquiries.SUBJECT_TYPE, "line", ctx)
                      if field.name == "质保要求")
    field_pool.update_field(db, definition, auto_include=False)
    second = inquiries.create_draft(db, users.sales.id, lines=[{"part_id": parts[0].id}])
    second_line = inquiries.lines_for(db, second.id)[0]
    assert definition.id in {field.id for field in field_pool.template_for(
        db, inquiries.SUBJECT_TYPE, "line", ctx, anchor=f"part:{parts[0].id}")}
    key = f"line:{second_line.id}"
    assert field_pool.read_values(db, [field_pool.Subject(inquiries.SUBJECT_TYPE, key, "line")], ctx, common.bypass)[key] == []
    assert first.id != second.id


def test_reassignment_does_not_reopen_old_recipients(db, presales_users, presales_parts):
    users, parts = presales_users, presales_parts
    inquiry = _create(db, users, parts)
    original = _items(db, inquiry, users)
    for buyer in (users.buyer, users.peer):
        _offer(db, inquiry, inquiries.lines_for(db, inquiry.id)[0], buyer)
    for item in original:
        work_items.complete(db, item.id, item.assignee_id, item.version)
    inquiries.withdraw(db, inquiry.id, users.sales.id, inquiry.version)
    inquiries.update_draft(db, inquiry.id, users.sales.id, inquiry.version, assignee_ids=[users.peer.id])
    inquiries.resubmit(db, inquiry.id, users.sales.id, inquiry.version)
    inquiries.append_lines(db, inquiry.id, users.sales.id, inquiry.version, [{"part_id": parts[1].id}])
    active = [item for item in _items(db, inquiry, users) if item.status in work_items.ACTIVE]
    assert len(active) == 1 and active[0].assignee_id == users.peer.id
    assert next(item for item in original if item.assignee_id == users.buyer.id).status == "done"


def test_binding_reuses_duplicates_and_only_once(db, presales_users, presales_parts):
    users, parts = presales_users, presales_parts
    inquiry = inquiries.create_draft(db, users.sales.id, assignee_ids=[users.buyer.id, users.peer.id],
                                    lines=[{"pn_raw": parts[0].pn_std}])
    inquiries.submit(db, inquiry.id, users.sales.id, inquiry.version)
    line = inquiries.lines_for(db, inquiry.id)[0]
    assert binding.check(db, line.id, users.buyer.id)["near_duplicates"]
    with pytest.raises(Conflict, match="绑定已有"):
        binding.create_and_bind(db, line.id, users.buyer.id, line.version)
    old_version = line.version
    binding.bind(db, line.id, users.buyer.id, old_version, part_id=parts[0].id)
    assert line.part_id == parts[0].id
    assert inquiry.submitted_snapshot["lines"][line.line_key]["part_id"] == parts[0].id
    with pytest.raises(Conflict):
        binding.bind(db, line.id, users.peer.id, old_version, part_id=parts[0].id)
    with pytest.raises(Conflict, match="已经绑定"):
        binding.create_and_bind(db, line.id, users.peer.id, line.version, pn_std="PS-NEW")


def test_binding_creation_is_transactional_and_requires_claim(db, presales_users):
    users = presales_users
    inquiry = inquiries.create_draft(db, users.sales.id, lines=[{"pn_raw": "DISTINCT-NEW-ALPHA"}])
    inquiries.submit(db, inquiry.id, users.sales.id, inquiry.version)
    line = inquiries.lines_for(db, inquiry.id)[0]
    with pytest.raises(Forbidden):
        binding.create_and_bind(db, line.id, users.buyer.id, line.version)
    item = _items(db, inquiry, users)[0]
    work_items.claim(db, item.id, users.buyer.id, item.version)
    binding.create_and_bind(db, line.id, users.buyer.id, line.version, description="新建说明")
    assert db.get(DimPart, line.part_id).description == "新建说明"
    created_id = line.part_id
    db.rollback()
    with SessionLocal() as fresh:
        assert fresh.get(DimPart, created_id) is None


def test_binding_requires_both_permissions(db, presales_users):
    users = presales_users
    inquiry = inquiries.create_draft(db, users.sales.id, assignee_ids=[users.buyer.id], lines=[{"pn_raw": "UNBOUND"}])
    inquiries.submit(db, inquiry.id, users.sales.id, inquiry.version)
    line = inquiries.lines_for(db, inquiry.id)[0]
    users.buyer.perm_overrides = {"page_master_data": False}
    db.flush()
    with pytest.raises(Forbidden):
        binding.check(db, line.id, users.buyer.id)


def test_public_pool_concurrent_claim_exactly_one_winner(db, presales_users, presales_parts):
    users, parts = presales_users, presales_parts
    inquiry = _create(db, users, parts, pool=True)
    item = _items(db, inquiry, users)[0]
    item_id, expected = item.id, item.version
    actor_ids = [users.buyer.id, users.peer.id]
    db.commit()
    barrier = Barrier(2)

    def claim(actor):
        with SessionLocal() as session:
            barrier.wait(timeout=10)
            try:
                claimed = work_items.claim(session, item_id, actor, expected)
                result = claimed.assignee_id
                session.commit()
                return result
            except Conflict:
                session.rollback()
                return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = list(pool.map(claim, actor_ids))
    assert len([actor for actor in claimed if actor is not None]) == 1


def test_null_field_values_and_future_reference_are_safe(db, presales_users, presales_parts):
    users, parts = presales_users, presales_parts
    inquiry = inquiries.create_draft(db, users.sales.id,
        lines=[{"part_id": parts[0].id, "field_values": None}])
    inquiries.submit(db, inquiry.id, users.sales.id, inquiry.version)
    offer = _offer(db, inquiry, inquiries.lines_for(db, inquiry.id)[0], users.buyer)
    offer.valid_from = datetime.now(timezone.utc) + timedelta(hours=1)
    db.flush()
    with pytest.raises(InvalidTransition, match="有效"), db.begin_nested():
        inquiries.create_draft(db, users.sales.id, lines=[{"part_id": parts[0].id, "ref_offer_id": offer.id}])


def test_all_references_after_withdraw_then_append_opens_fresh_tasks(db, presales_users, presales_parts):
    users, parts = presales_users, presales_parts
    inquiry = _create(db, users, parts)
    line = inquiries.lines_for(db, inquiry.id)[0]
    offer = _offer(db, inquiry, line, users.buyer, with_snapshot=True)
    inquiries.withdraw(db, inquiry.id, users.sales.id, inquiry.version)
    inquiries.update_draft(db, inquiry.id, users.sales.id, inquiry.version,
                           lines=[{"line_key": line.line_key, "ref_offer_id": offer.id}])
    inquiries.resubmit(db, inquiry.id, users.sales.id, inquiry.version)
    assert all(item.status == "cancelled" for item in _items(db, inquiry, users))
    inquiries.append_lines(db, inquiry.id, users.sales.id, inquiry.version, [{"part_id": parts[1].id}])
    assert {item.assignee_id for item in _items(db, inquiry, users) if item.status in work_items.ACTIVE} == {
        users.buyer.id, users.peer.id}


def test_number_allocation_is_concurrent_and_shanghai_based(db, presales_users, monkeypatch):
    users = presales_users
    user_id = users.sales.id
    db.commit()
    monkeypatch.setattr(common, "now", lambda: datetime(2026, 10, 24, 16, 0, tzinfo=timezone.utc))
    barrier = Barrier(3)

    def create(_):
        with SessionLocal() as session:
            barrier.wait(timeout=10)
            inquiry = inquiries.create_draft(session, user_id)
            number = inquiry.inquiry_no
            session.commit()
            return number

    with ThreadPoolExecutor(max_workers=3) as pool:
        numbers = list(pool.map(create, range(3)))
    assert len(set(numbers)) == 3
    assert set(numbers) == {f"XJ20261025{seq:03d}" for seq in range(1, 4)}


def test_same_unbound_line_concurrent_binding_has_one_winner(db, presales_users, presales_parts):
    users, parts = presales_users, presales_parts
    inquiry = inquiries.create_draft(db, users.sales.id, assignee_ids=[users.buyer.id, users.peer.id],
                                    lines=[{"pn_raw": parts[0].pn_std}])
    inquiries.submit(db, inquiry.id, users.sales.id, inquiry.version)
    line = inquiries.lines_for(db, inquiry.id)[0]
    line_id, version, part_id = line.id, line.version, parts[0].id
    actor_ids = [users.buyer.id, users.peer.id]
    db.commit()
    barrier = Barrier(2)

    def bind(actor):
        with SessionLocal() as session:
            barrier.wait(timeout=10)
            try:
                result = binding.bind(session, line_id, actor, version, part_id=part_id)
                session.commit()
                return result.part_id
            except Conflict:
                session.rollback()
                return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(bind, actor_ids))
    assert results.count(part_id) == 1 and results.count(None) == 1


def test_owner_can_manage_tasks_created_by_supervisor(db, presales_users, presales_parts):
    users, parts = presales_users, presales_parts
    inquiry = inquiries.create_draft(db, users.sales.id, assignee_ids=[users.buyer.id],
                                    lines=[{"part_id": parts[0].id}])
    inquiries.submit(db, inquiry.id, users.boss.id, inquiry.version)
    assert len(_items(db, inquiry, users)) == 1
    inquiries.withdraw(db, inquiry.id, users.sales.id, inquiry.version)
    assert all(item.paused for item in _items(db, inquiry, users))
    inquiries.resubmit(db, inquiry.id, users.sales.id, inquiry.version)
    assert inquiry.status == "submitted"
    assert all(not item.paused for item in _items(db, inquiry, users))


def test_closing_proactive_inquiry_cancels_notice_tasks(db, presales_users, presales_parts):
    from app.services.workflow.registry import Subject
    users, parts = presales_users, presales_parts
    inquiry = inquiries.create_draft(db, users.buyer.id, kind="proactive_offer",
                                    lines=[{"part_id": parts[0].id}])
    inquiry.status = "submitted"
    notice_subject = Subject("presales_offer_notice", inquiry.id)
    notices = work_items.open_items(db, notice_subject, [users.sales.id], "presales_offer_notice", users.buyer.id)
    inquiries.close(db, inquiry.id, users.buyer.id, inquiry.version, outcome="cancelled")
    assert notices[0].status == "cancelled"

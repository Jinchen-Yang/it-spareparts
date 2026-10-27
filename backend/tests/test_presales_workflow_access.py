"""售前接入通用待办后的页面撤权与动态受众准入。"""
from types import SimpleNamespace

import pytest

from app import permissions
from app.models.presales import PresalesInquiry, PresalesInquiryLine, PresalesOffer
from app.models.system import SysUser
from app.services import audience
from app.services.presales import common
from app.services.presales.registration import register_subjects
from app.services.workflow import events, work_items
from app.services.workflow.registry import Forbidden, Subject


@pytest.fixture()
def access_users(db):
    register_subjects()
    users = {}
    for name, role in (("sales", "sales"), ("buyer", "purchaser")):
        user = SysUser(username=f"presales-access-{name}", role=role, password_hash="unused",
                       template_perms=permissions.effective(role, None), perm_overrides={})
        db.add(user)
        users[name] = user
    db.flush()
    return SimpleNamespace(**users)


def _inquiry(db, owner, *, kind="proactive_offer", number=1, rule_id=None):
    inquiry = PresalesInquiry(kind=kind, inquiry_no=f"ACC{number:010d}", status="submitted",
                             created_by=owner.id, offer_audience_rule_id=rule_id,
                             submitted_at=common.now())
    db.add(inquiry)
    db.flush()
    return inquiry


@pytest.mark.parametrize("subject_type", ["presales_inquiry", "presales_offer_notice"])
def test_revoked_presales_page_hides_existing_assignment_and_blocks_actions(db, access_users, subject_type):
    users = access_users
    is_notice = subject_type == "presales_offer_notice"
    owner, target = (users.buyer, users.sales) if is_notice else (users.sales, users.buyer)
    inquiry = _inquiry(db, owner, kind="proactive_offer" if is_notice else "sales_inquiry")
    subject = Subject(subject_type, inquiry.id)
    item = work_items.open_items(db, subject, [target.id], subject_type, owner.id)[0]
    ctx = common.resolve_actor(db, target.id)[1]
    assert work_items.visible_items(db, ctx, "assigned", subject_type)["total"] == 1
    target.perm_overrides = {"page_presales": False}
    db.flush()

    # 传入撤权前的上下文，服务仍必须重新读取账号当前权限。
    listed = work_items.visible_items(db, ctx, "assigned", subject_type)
    assert listed["items"] == [] and listed["total"] == 0
    assert work_items.badge(db, ctx) == {"assigned": 0, "pool": 0, "claimed": 0}
    for action in (work_items.start, work_items.complete):
        with pytest.raises(Forbidden, match="无权访问"):
            action(db, item.id, target.id, expected_version=item.version)
    with pytest.raises(Forbidden):
        events.list_events(db, subject, common.resolve_actor(db, target.id)[1])
    assert item.status == "pending" and item.version == 1


def test_revoked_presales_page_blocks_public_pool_claim(db, access_users):
    users = access_users
    inquiry = _inquiry(db, users.sales, kind="sales_inquiry")
    item = work_items.open_items(db, Subject("presales_inquiry", inquiry.id), [],
                                 "presales_inquiry", users.sales.id)[0]
    users.buyer.perm_overrides = {"page_presales": False}
    db.flush()
    with pytest.raises(Forbidden, match="无权访问"):
        work_items.claim(db, item.id, users.buyer.id, expected_version=item.version)
    assert item.assignee_id is None and item.version == 1


def test_notice_group_membership_is_rechecked_before_total_and_pagination(db, access_users):
    users = access_users
    owner_ctx = common.resolve_actor(db, users.buyer.id)[1]
    group = audience.create_group(db, "售前通知受众", "personal", [users.sales.username],
                                  owner_ctx, bypass=common.bypass)
    rule = audience.create_rule(db, "include", [], [group.id], owner_ctx)
    public = _inquiry(db, users.buyer, number=1)
    grouped = _inquiry(db, users.buyer, number=2, rule_id=rule)
    notice = "presales_offer_notice"
    public_item = work_items.open_items(db, Subject(notice, public.id), [users.sales.id],
                                        notice, users.buyer.id)[0]
    grouped_item = work_items.open_items(db, Subject(notice, grouped.id), [users.sales.id],
                                         notice, users.buyer.id)[0]
    viewer = common.resolve_actor(db, users.sales.id)[1]
    before = work_items.visible_items(db, viewer, "assigned", notice, page_size=1)
    assert before["total"] == 2 and before["items"][0]["id"] == grouped_item.id
    assert work_items.badge(db, viewer)["assigned"] == 2

    audience.update_group(db, group.id, owner_ctx, user_ids=[], bypass=common.bypass)
    after = work_items.visible_items(db, viewer, "assigned", notice, page_size=1)
    assert after["total"] == 1
    assert work_items.badge(db, viewer)["assigned"] == 1
    assert [item["id"] for item in after["items"]] == [public_item.id]
    assert work_items.visible_items(db, viewer, "assigned", notice, page=2, page_size=1)["items"] == []
    with pytest.raises(Forbidden):
        events.list_events(db, Subject(notice, grouped.id), viewer)
    with pytest.raises(Forbidden):
        work_items.start(db, grouped_item.id, users.sales.id, expected_version=grouped_item.version)


def test_revoked_page_account_is_not_assignable_or_a_candidate(db, access_users):
    users = access_users
    inquiry = _inquiry(db, users.sales, kind="sales_inquiry")
    users.buyer.perm_overrides = {"page_presales": False}
    db.flush()
    viewer = common.resolve_actor(db, users.sales.id)[1]
    assert users.buyer.id not in {row["id"] for row in work_items.candidates(
        db, viewer, "presales_inquiry")["items"]}
    with pytest.raises(Forbidden):
        work_items.open_items(db, Subject("presales_inquiry", inquiry.id), [users.buyer.id],
                              "presales_inquiry", users.sales.id)


@pytest.mark.parametrize("modes, referenced, visible", [
    (["public"], False, True),
    (["public", "private"], False, False),
    (["public", "exclude"], False, False),
    (["private"], True, False),
])
def test_notice_creator_obeys_reporter_rules_including_referenced_quotes(
    db, access_users, modes, referenced, visible,
):
    users = access_users
    owner_ctx = common.resolve_actor(db, users.buyer.id)[1]
    inquiry = _inquiry(db, users.buyer)
    origin = _inquiry(db, users.buyer, number=2) if referenced else inquiry
    for seq, mode in enumerate(modes, start=1):
        line = PresalesInquiryLine(inquiry_id=origin.id, line_key=f"offer-{seq}", seq=seq,
                                   pn_raw=f"RAW-{seq}", qty=1)
        db.add(line)
        db.flush()
        rule = audience.create_rule(db, mode, [users.sales.username] if mode == "exclude" else [],
                                    [], owner_ctx)
        offer = PresalesOffer(inquiry_id=origin.id, line_id=line.id, created_by=users.buyer.id,
                              status="published", no_stock=True, reporter_audience_rule_id=rule)
        db.add(offer)
        db.flush()
        if referenced:
            db.add(PresalesInquiryLine(inquiry_id=inquiry.id, line_key=f"ref-{seq}", seq=seq,
                                       pn_raw=f"RAW-{seq}", qty=1, ref_offer_id=offer.id))
            db.flush()
    subject_type = "presales_offer_notice"
    item = work_items.open_items(db, Subject(subject_type, inquiry.id), [users.sales.id],
                                 subject_type, users.buyer.id)[0]
    viewer = common.resolve_actor(db, users.sales.id)[1]
    listed = work_items.visible_items(db, viewer, "assigned", subject_type)
    assert listed["total"] == 1
    result = listed["items"][0]
    assert ("created_by" in result) is visible
    assert "created_by_name" not in result
    if visible:
        assert result["created_by"] == users.buyer.id
    own_result = work_items.serialize_items(db, [item], users.buyer.id, owner_ctx)[0]
    assert own_result["created_by"] == users.buyer.id

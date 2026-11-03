"""D-0053 的整单权限、实时撤权、旧规则兼容及名单维护契约。"""

from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import aliased

from app import permissions
from app.models.fields_audience import AudienceGroup, AudienceGroupMember, AudienceRule
from app.models.presales import PresalesInquiry, PresalesInquiryLine, PresalesOffer, PresalesOfferLineLink
from app.models.system import SysUser
from app.services.presales import answers, common, inquiries, pool, visibility
from app.services.workflow.registry import Conflict, Forbidden
from tests.test_presales_api_visibility import _client, _ctx, users  # noqa: F401


def _create(db, users, *, visible_to=None):
    result = pool.create_inquiry(db, users.seller.id, visible_to=visible_to,
        lines=[{"part_id": users.part.id, "pn_raw": users.part.pn_std, "qty": Decimal("1")}])
    return db.get(PresalesInquiry, result["inquiry_id"])


def _admin(db, role):
    user = SysUser(username=f"acl_{role}", role=role, password_hash="test",
                  template_perms=permissions.effective(role, None), perm_overrides={})
    db.add(user)
    db.flush()
    return user


@pytest.mark.parametrize("legacy_mode", [None, "public", "private", "exclude"])
def test_default_and_legacy_open_rules_only_allow_owner_quoters_and_admins(db, users, legacy_mode):
    inquiry = _create(db, users)
    if legacy_mode is None:
        inquiry.offer_audience_rule_id = None
    else:
        rule = {"mode": legacy_mode}
        if legacy_mode == "exclude":
            rule["user_ids"] = [users.denied.username]
        inquiry.offer_audience_rule_id = common.rule_id(db, rule, _ctx(db, users.seller))
    for person, allowed in ((users.seller, True), (users.other, False), (users.buyer, True),
                            (users.peer, True), (users.supervisor, False), (users.denied, False),
                            (_admin(db, "admin"), True), (_admin(db, "boss"), True)):
        ctx = _ctx(db, person)
        assert visibility.can_view_inquiry(db, inquiry, ctx) is allowed
        model = aliased(PresalesInquiry)
        ids = list(db.scalars(select(model.id).where(visibility.inquiry_access_filter(db, ctx, model))))
        assert (inquiry.id in ids) is allowed


def test_explicit_people_and_groups_apply_only_after_submission(db, users):
    group = AudienceGroup(name="历史销售组", scope="personal", owner_id=users.seller.username)
    db.add(group)
    db.flush()
    db.add(AudienceGroupMember(group_id=group.id, user_id=users.other.username))
    inquiry = inquiries.create_draft(db, users.seller.id,
        offer_audience={"mode": "include", "group_ids": [group.id]},
        lines=[{"part_id": users.part.id}])
    for person in (users.other, users.buyer, users.supervisor):
        assert not visibility.can_view_inquiry(db, inquiry, _ctx(db, person))
    assert visibility.can_view_inquiry(db, inquiry, _ctx(db, _admin(db, "admin")))
    assert inquiries.visibility_settings(db, inquiry.id, users.seller.id)["visible_to"] == [
        {"id": users.other.username, "name": "other"}]
    inquiries.submit(db, inquiry.id, users.seller.id, inquiry.version)
    assert visibility.can_view_inquiry(db, inquiry, _ctx(db, users.other))
    inquiries.update_visibility(db, inquiry.id, users.seller.id, inquiry.version, [])
    assert not visibility.can_view_inquiry(db, inquiry, _ctx(db, users.other))


def test_quote_permission_and_menu_are_refreshed_for_every_access(db, users):
    inquiry = _create(db, users)
    stale = _ctx(db, users.other)
    users.other.perm_overrides = {"action_presales_quote": True}
    db.flush()
    assert visibility.can_view_inquiry(db, inquiry, stale)
    users.other.perm_overrides = {"action_presales_quote": False}
    db.flush()
    assert not visibility.can_view_inquiry(db, inquiry, stale)
    inquiries.update_visibility(db, inquiry.id, users.seller.id, inquiry.version, [users.other.username])
    assert visibility.can_view_inquiry(db, inquiry, stale)
    users.other.perm_overrides = {"page_presales": False}
    db.flush()
    assert not visibility.can_view_inquiry(db, inquiry, stale)


def test_owner_admin_manage_shares_and_versions_without_changing_closed_snapshot(db, users):
    inquiry = _create(db, users, visible_to=[users.other.username, users.supervisor.username])
    for person in (users.other, users.buyer, users.supervisor):
        with pytest.raises(Forbidden):
            inquiries.update_visibility(db, inquiry.id, person.id, inquiry.version, [])
    old_version = inquiry.version
    result = inquiries.update_visibility(db, inquiry.id, users.seller.id, old_version, [])
    assert result == {"version": old_version + 1, "visible_to": []}
    with pytest.raises(Conflict):
        inquiries.update_visibility(db, inquiry.id, users.seller.id, old_version, [users.other.username])
    inquiries.close(db, inquiry.id, users.seller.id, inquiry.version, outcome="lost")
    saved = dict(inquiry.submitted_snapshot)
    result = inquiries.update_visibility(db, inquiry.id, _admin(db, "admin").id,
        inquiry.version, [users.other.username])
    assert result["visible_to"] == [{"id": users.other.username, "name": "other"}]
    assert inquiry.status == "closed" and inquiry.submitted_snapshot == saved
    copied = inquiries.copy_as_new(db, inquiry.id, users.seller.id, inquiry.version)
    assert inquiries.visibility_settings(db, copied.id, users.seller.id)["visible_to"] == []
    assert db.get(AudienceRule, copied.offer_audience_rule_id).mode == "private"


def test_shared_supervisor_can_maintain_order_but_cannot_change_visibility_in_old_editor(db, users):
    inquiry = _create(db, users, visible_to=[users.supervisor.username])
    inquiries.withdraw(db, inquiry.id, users.supervisor.id, inquiry.version)
    inquiries.update_draft(db, inquiry.id, users.supervisor.id, inquiry.version, notes="维护备注")
    with pytest.raises(Forbidden):
        inquiries.update_draft(db, inquiry.id, users.supervisor.id, inquiry.version,
            offer_audience={"mode": "include", "user_ids": [users.other.username]})
    assert not visibility.can_view_inquiry(db, inquiry, _ctx(db, users.other))


def test_revocation_hides_authored_quotes_history_and_pool_conflict_payload(db, users):
    inquiry = _create(db, users, visible_to=[users.other.username])
    line = inquiries.lines_for(db, inquiry.id)[0]
    priced = pool.set_price(db, line.id, users.other.id, {"unit_price": Decimal("42")},
        expected_version=line.version)
    offer = db.get(PresalesOffer, priced["price"]["offer_id"])
    assert visibility.pinned_offer_view(db, offer.id, 1, _ctx(db, users.other)) is not None
    inquiries.update_visibility(db, inquiry.id, users.seller.id, inquiry.version, [])
    assert visibility.offer_view(db, offer, _ctx(db, users.other)) is None
    assert visibility.pinned_offer_view(db, offer.id, 1, _ctx(db, users.other)) is None
    assert list(db.scalars(select(PresalesOffer.id).where(
        visibility.offer_access_filter(db, _ctx(db, users.other))))) == []
    for operation in (
        lambda: pool.set_price(db, line.id, users.other.id, {"unit_price": Decimal("99")}, expected_version=1),
        lambda: pool.history(db, line.id, users.other.id),
        lambda: visibility.revision_views(db, offer.id, _ctx(db, users.other)),
    ):
        with pytest.raises(Forbidden):
            operation()
    assert offer.unit_price == Decimal("42")


def test_pool_and_legacy_list_counts_search_and_pagination_are_acl_scoped(db, users):
    hidden = _create(db, users)
    shown = _create(db, users, visible_to=[users.other.username])
    own_result = pool.create_inquiry(db, users.other.id, lines=[{"pn_raw": "OWN"}])
    db.commit()
    client = _client(users.other)
    for path in ("/api/presales/pool/lines", "/api/presales/inquiries"):
        response = client.get(path, params={"tab": "all", "scope": "all", "page_size": 1})
        assert response.status_code == 200, response.text
        assert response.json()["total"] == 2
        assert len(response.json()["items"]) == 1
        assert client.get(path, params={"q": hidden.inquiry_no, "scope": "all"}).json()["total"] == 0
    assert client.get("/api/presales/pool/counts").json()["all"] == 2
    assert client.get(f"/api/presales/inquiries/{hidden.id}").status_code == 403
    assert client.get(f"/api/presales/inquiries/{shown.id}").status_code == 200
    assert client.get(f"/api/presales/inquiries/{own_result['inquiry_id']}").status_code == 200


def test_pool_current_price_history_and_same_pn_hint_respect_source_acl(db, users):
    source = _create(db, users)
    source_line = inquiries.lines_for(db, source.id)[0]
    price = pool.set_price(db, source_line.id, users.buyer.id, {"unit_price": Decimal("76")},
        expected_version=source_line.version)
    target = pool.create_inquiry(db, users.other.id, lines=[
        {"part_id": users.part.id, "pn_raw": users.part.pn_std},
        {"part_id": users.part.id, "pn_raw": users.part.pn_std}])
    linked, pending = inquiries.lines_for(db, target["inquiry_id"])
    db.add(PresalesOfferLineLink(line_id=linked.id, offer_id=price["price"]["offer_id"], offer_revision_no=1))
    answers.refresh(db, [linked.id])
    shown = pool.line_view(db, linked.id, users.other.id)
    assert shown["answer_state"] == "quoted"
    assert shown["price"] is None and shown["answered_by"] is None and shown["answered_at"] is None
    assert pool.counts(db, users.other.id)["quoted"] == 1
    assert pool.line_view(db, pending.id, users.other.id)["latest_same_pn"] is None
    assert not [row for row in pool.history(db, linked.id, users.other.id) if row["kind"] == "price"]


def test_visibility_http_contract_and_invalid_members_are_atomic(db, users):
    inquiry = _create(db, users)
    db.commit()
    client = _client(users.seller)
    path = f"/api/presales/inquiries/{inquiry.id}/visibility"
    original = client.get(path).json()
    assert original == {"version": inquiry.version, "visible_to": []}
    invalid = client.put(path, json={"expected_version": inquiry.version, "visible_to": ["no_such_account"]})
    assert invalid.status_code == 422
    assert client.get(path).json() == original
    changed = client.put(path, json={"expected_version": inquiry.version, "visible_to": [users.other.username]})
    assert changed.status_code == 200 and changed.json()["visible_to"] == [
        {"id": users.other.username, "name": "other"}]
    assert _client(users.other).get(path).status_code == 403
    assert client.put(path, json={"expected_version": original["version"], "visible_to": []}).status_code == 409

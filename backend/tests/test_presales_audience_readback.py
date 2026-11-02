"""受众规则只回读给各字段作者和主管，不能沿记录准入泄漏给其他人。"""

import pytest

from app.services import audience, field_pool
from app.services.presales import common, inquiries, offers, visibility
from tests.test_presales_api_visibility import _client, _ctx, _quoted, users


def _rule(mode, users, group):
    result = {"mode": mode}
    if mode in {"include", "exclude"}:
        result.update(user_ids=[users.other.username], group_ids=[group.id])
    return result


def test_shared_values_do_not_disclose_personal_audience_lists(db, users):
    buyer, seller = _ctx(db, users.buyer), _ctx(db, users.seller)
    group = audience.create_group(db, "采购的个人组", "personal", [users.other.username],
                                  buyer, bypass=common.bypass)
    inquiry, offer = _quoted(db, users, rule=_rule("include", users, group))
    line = inquiries.lines_for(db, inquiry.id)[0]
    for target, level in ((f"inquiry:{inquiry.id}", "header"), (f"line:{line.id}", "line")):
        field_pool.save_values(db, field_pool.Subject("presales_inquiry", target, level), [
            {"new_name": "历史备注", "value": "全员可见", "rule": {"mode": "private"}}], seller)
    db.commit()
    for viewer in (users.other, users.seller, users.buyer, users.supervisor):
        client = _client(viewer)
        detail = client.get(f"/api/presales/inquiries/{inquiry.id}")
        history = client.get(f"/api/presales/offers/{offer.id}/revisions")
        assert detail.status_code == history.status_code == 200
        header = detail.json()
        for row in (header, header["lines"][0]):
            field = next(f for f in row["field_values"] if f["name"] == "历史备注")
            assert field["value"] == "全员可见"
            assert ("rule" in field) == (viewer in (users.seller, users.supervisor))
            assert ("notes_audience" in row) == (viewer in (users.seller, users.supervisor))
        for quote in (header["offers"][0], history.json()[-1]["snapshot"]):
            assert quote["notes"] == "采购机密" and quote["reporter"]["name"] == "buyer"
            assert ("notes_audience" in quote) == (viewer in (users.buyer, users.supervisor))
            if "notes_audience" in quote:
                assert quote["notes_audience"]["groups"] == [{"id": group.id, "name": "采购的个人组"}]


def test_inquiry_and_line_rules_read_back_selected_names_for_author_and_bypass(db, users):
    ctx = _ctx(db, users.seller)
    group = audience.create_group(db, "采购协作", "personal", [users.buyer.username],
                                  ctx, bypass=common.bypass)
    rule = {"mode": "exclude", "user_ids": [users.other.username], "group_ids": [group.id]}
    inquiry = inquiries.create_draft(db, users.seller.id, offer_audience=rule,
        notes_audience=rule, lines=[{"part_id": users.part.id, "notes_audience": rule}])
    expected = {"mode": "exclude", "users": [{"id": users.other.username, "name": "other"}],
                "groups": [{"id": group.id, "name": group.name}]}
    for viewer in (users.seller, users.supervisor):
        view = visibility.inquiry_view(db, inquiry, _ctx(db, viewer))
        assert view["offer_audience"] == view["notes_audience"] == expected
        assert view["lines"][0]["notes_audience"] == expected
    assert "offer_audience" not in visibility.inquiry_view(db, inquiry, ctx, detail=False)


def test_field_pool_read_rules_use_effective_default_and_value_author(db, users):
    author, other = _ctx(db, users.seller), _ctx(db, users.other)
    rule = audience.create_rule(db, "exclude", [users.peer.username], [], author)
    field = field_pool.define_field(db, subject_type="presales_inquiry", level="line",
        name="团队字段", owner_scope="team", viewer=author, default_rule_id=rule)
    subject = field_pool.Subject("presales_inquiry", "line:123", "line")
    field_pool.save_values(db, subject, [{"field_def_id": field.id, "value": "原值"}], author)
    read = lambda viewer, bypass=common.bypass: field_pool.read_values(
        db, [subject], viewer, bypass, include_rules=True)[subject.subject_id]
    expected = {"mode": "exclude", "users": [{"id": users.peer.username, "name": "peer"}], "groups": []}
    assert read(author)[0]["rule"] == expected
    assert "rule" not in read(other)[0]
    assert read(_ctx(db, users.supervisor))[0]["rule"] == expected
    assert read(_ctx(db, users.peer)) == []
    field_pool.save_values(db, subject, [{"field_def_id": field.id, "value": "新值"}], other)
    assert "rule" not in read(author)[0]
    assert read(other)[0]["rule"] == expected
    field_pool.save_values(db, subject, [{"field_def_id": field.id, "value": "覆盖", "rule": {
        "mode": "private"}}], other)
    assert read(author) == []
    assert read(other)[0]["rule"] == {"mode": "private", "users": [], "groups": []}
    assert "rule" not in field_pool.read_values(db, [subject], other, common.bypass)[subject.subject_id][0]


def test_rule_readback_uses_current_names_and_falls_back_to_username(db, users):
    ctx = _ctx(db, users.buyer)
    group = audience.create_group(db, "旧分组名", "personal", [users.other.username],
                                  ctx, bypass=common.bypass)
    inquiry, offer = _quoted(db, users, rule=_rule("include", users, group))
    group.name = "新分组名"
    users.other.display_name = None
    db.flush()
    view = visibility.offer_view(db, offer, ctx)
    assert view["supplier_audience"] == {"mode": "include", "users": [
        {"id": users.other.username, "name": users.other.username}],
        "groups": [{"id": group.id, "name": "新分组名"}]}

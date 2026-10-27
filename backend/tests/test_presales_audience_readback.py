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


@pytest.mark.parametrize("mode", ["public", "private", "include", "exclude"])
@pytest.mark.parametrize("viewer", ["seller", "buyer", "other", "peer", "supervisor"])
def test_http_rules_follow_each_field_author_without_exposing_hidden_values(db, users, mode, viewer):
    buyer_ctx, seller_ctx = _ctx(db, users.buyer), _ctx(db, users.seller)
    group = audience.create_group(db, "可见销售", "personal", [users.other.username],
                                  buyer_ctx, bypass=common.bypass)
    inquiry, offer = _quoted(db, users, rule=_rule(mode, users, group))
    line = inquiries.lines_for(db, inquiry.id)[0]
    inquiry.notes = "单头私密备注"
    line.notes = "行私密备注"
    private = common.rule_id(db, {"mode": "private"}, seller_ctx)
    inquiry.notes_audience_rule_id = line.notes_audience_rule_id = private
    for subject_id, level in ((f"inquiry:{inquiry.id}", "header"), (f"line:{line.id}", "line")):
        field_pool.save_values(db, field_pool.Subject("presales_inquiry", subject_id, level), [
            {"new_name": "公开字段", "value": "可以看"},
            {"new_name": "私密字段", "value": "不可泄漏", "rule": {"mode": "private"}},
        ], seller_ctx)
    db.commit()

    client = _client(getattr(users, viewer))
    response = client.get(f"/api/presales/inquiries/{inquiry.id}")
    assert response.status_code == 200, response.text
    detail = response.json()
    manages_header = viewer in {"seller", "supervisor"}
    for row in (detail, detail["lines"][0]):
        assert ("notes_audience" in row) is manages_header
        assert ("notes" in row) is manages_header
        if manages_header:
            assert row["notes_audience"] == {"mode": "private", "users": [], "groups": []}
        values = {value["name"]: value for value in row["field_values"]}
        assert ("私密字段" in values) is manages_header
        assert ("rule" in values["公开字段"]) is manages_header
        if manages_header:
            assert values["公开字段"]["rule"] == {"mode": "public", "users": [], "groups": []}
            assert values["私密字段"]["rule"] == {"mode": "private", "users": [], "groups": []}
    assert ("offer_audience" in detail) is manages_header
    if manages_header:
        assert detail["offer_audience"] == {"mode": "public", "users": [], "groups": []}

    library = client.get(f"/api/presales/library?part_ids={users.part.id}")
    assert library.status_code == 200, library.text
    quote = library.json()[str(users.part.id)][0]
    assert quote == {**detail["offers"][0], "more": []}
    manages_offer = viewer in {"buyer", "supervisor"}
    sees_offer_values = manages_offer or mode == "public" or (
        mode == "include" and viewer == "other") or (mode == "exclude" and viewer != "other")
    expected = {"mode": mode, "users": [], "groups": []}
    if mode in {"include", "exclude"}:
        expected.update(users=[{"id": users.other.username, "name": users.other.display_name}],
                        groups=[{"id": group.id, "name": group.name}])
    for field in ("supplier_audience", "reporter_audience", "notes_audience"):
        assert (field in quote) is manages_offer
        if manages_offer:
            assert quote[field] == expected
    for field in ("supplier_id", "source_key", "reporter", "notes"):
        assert (field in quote) is sees_offer_values
    if not manages_header:
        assert "单头私密备注" not in response.text and "不可泄漏" not in response.text


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


def test_revision_rule_changes_are_only_disclosed_to_offer_author_and_bypass(db, users):
    inquiry, offer = _quoted(db, users)
    offers.update_offer(db, offer.id, users.buyer.id, {"unit_price": "60.00",
        "supplier_audience": {"mode": "public"}, "notes_audience": {"mode": "private"}},
        expected_version=offer.version)
    db.commit()
    rule_keys = {"supplier_audience", "reporter_audience", "notes_audience"}
    for viewer in (users.seller, users.buyer, users.supervisor):
        response = _client(viewer).get(f"/api/presales/offers/{offer.id}/revisions")
        assert response.status_code == 200, response.text
        rows = response.json()
        if viewer == users.seller:
            assert all(not rule_keys.intersection(row["snapshot"]) for row in rows)
            assert all(not rule_keys.intersection(row["changed_fields"]) for row in rows)
            assert all("notes" not in row["snapshot"] for row in rows)
        else:
            assert all(rule_keys <= row["snapshot"].keys() for row in rows)
            assert rows[-1]["snapshot"]["supplier_audience"]["mode"] == "public"
            assert rows[-1]["snapshot"]["notes_audience"]["mode"] == "private"

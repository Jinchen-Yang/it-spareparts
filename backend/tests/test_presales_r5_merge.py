"""重复主动报价只有整单和字段受众均等价时才更新原记录。"""

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.models.dimensions import DimPart
from app.models.presales import PresalesOffer
from app.models.system import SysUser
from app.services import audience
from app.services.presales import common, inquiries, library, offers, visibility
from app.services.presales.registration import register_subjects
from app.services.workflow.registry import Forbidden


@pytest.fixture()
def actors(db, monkeypatch):
    register_subjects()
    monkeypatch.setattr(common, "now", lambda: datetime(2026, 10, 25, 7, tzinfo=timezone.utc))
    users = {}
    for name, role in (("buyer", "purchaser"), ("sales", "sales"), ("other", "sales")):
        user = SysUser(username="r5_merge_" + name, role=role, password_hash="测试账号")
        db.add(user)
        users[name] = user
    part = DimPart(pn_std="R5-MERGE-PN", description="受众归并回归")
    db.add(part)
    db.flush()
    return SimpleNamespace(**users, part=part)


def _publish(db, actors, *, whole=None, fields=None, price="100.00"):
    inquiry = inquiries.create_draft(db, actors.buyer.id, kind="proactive_offer",
        lines=[{"part_id": actors.part.id}], offer_audience=whole)
    line = inquiries.lines_for(db, inquiry.id)[0]
    draft = offers.save_draft(db, inquiry.id, line.id, actors.buyer.id,
        {"unit_price": price, "source_key": "same-source", **(fields or {})},
        expected_version=inquiry.version)
    draft_id = draft.id
    result = offers.publish_proactive(db, inquiry.id, actors.buyer.id,
                                      expected_version=inquiry.version)
    return inquiry, line, draft_id, result[0]


@pytest.mark.parametrize("mode", ["private", "include"])
def test_restricted_repeat_keeps_public_price_in_library_original_inquiry_and_search(db, actors, mode):
    original, _, _, first = _publish(db, actors)
    rule = {"mode": mode}
    if mode == "include":
        rule["user_ids"] = [actors.sales.username]
    restricted, line, draft_id, second = _publish(db, actors, whole=rule, price="1.00")
    assert second.id == draft_id and second.id != first.id
    assert db.scalar(select(func.count()).select_from(PresalesOffer)) == 2
    assert line.ref_offer_id is None
    assert first.unit_price == Decimal("100.00") and first.revision_no == 1
    ctx = common.resolve_actor(db, actors.other.id)[1]
    assert [row["unit_price"] for row in library.batch(db, [actors.part.id], ctx)[actors.part.id]] == [Decimal("100.00")]
    assert [row["unit_price"] for row in visibility.inquiry_view(db, original, ctx)["offers"]] == [Decimal("100.00")]
    with pytest.raises(Forbidden):
        visibility.inquiry_view(db, restricted, ctx)
    results = library.search(db, actors.part.pn_std, ctx)
    assert len(results) == 1 and results[0]["offer_count"] == 1
    assert [row["unit_price"] for row in results[0]["offers"]] == [Decimal("100.00")]
    assert len(library.batch(db, [actors.part.id], actors.buyer.id)[actors.part.id]) == 2


@pytest.mark.parametrize("field", ["supplier", "reporter", "notes"])
@pytest.mark.parametrize("mode", ["private", "include"])
def test_any_field_audience_difference_preserves_independent_quote(db, actors, field, mode):
    _, _, _, first = _publish(db, actors, fields={field + "_audience": {"mode": "public"}})
    rule = {"mode": mode}
    if mode == "include":
        rule["user_ids"] = [actors.sales.username]
    _, line, draft_id, second = _publish(db, actors,
        fields={field + "_audience": rule}, price="1.00")
    assert second.id == draft_id and second.id != first.id
    assert line.ref_offer_id is None
    assert first.unit_price == Decimal("100.00") and first.revision_no == 1
    assert db.scalar(select(func.count()).select_from(PresalesOffer)) == 2


@pytest.mark.parametrize("scope", ["whole", "supplier", "reporter", "notes"])
@pytest.mark.parametrize("target", ["user", "group"])
def test_same_mode_different_member_sets_keep_quotes_separate(db, actors, scope, target):
    if target == "user":
        first_rule = {"mode": "include", "user_ids": [actors.sales.username]}
        second_rule = {"mode": "include", "user_ids": [actors.other.username]}
    else:
        ctx = common.resolve_actor(db, actors.buyer.id)[1]
        # 当前成员相同的两个组仍是不同规则，后续成员可以独立变化。
        groups = [audience.create_group(db, name, "personal", [actors.sales.username], ctx,
                                        bypass=common.bypass) for name in ("组一", "组二")]
        first_rule = {"mode": "include", "group_ids": [groups[0].id]}
        second_rule = {"mode": "include", "group_ids": [groups[1].id]}
    first_data = {"whole": first_rule} if scope == "whole" else {
        "fields": {scope + "_audience": first_rule}}
    second_data = {"whole": second_rule} if scope == "whole" else {
        "fields": {scope + "_audience": second_rule}}
    _, _, _, first = _publish(db, actors, **first_data)
    _, line, draft_id, second = _publish(db, actors, price="1.00", **second_data)
    assert second.id == draft_id and second.id != first.id
    assert line.ref_offer_id is None
    assert first.unit_price == Decimal("100.00") and first.revision_no == 1
    assert db.scalar(select(func.count()).select_from(PresalesOffer)) == 2


@pytest.mark.parametrize("mode", ["public", "private", "include", "exclude"])
def test_equivalent_rule_content_updates_despite_new_ids_and_reordered_members(db, actors, mode):
    ctx = common.resolve_actor(db, actors.buyer.id)[1]
    groups = [audience.create_group(db, name, "personal", [], ctx, bypass=common.bypass)
              for name in ("组一", "组二")]
    rule = {"mode": mode}
    reversed_rule = {"mode": mode}
    if mode in {"include", "exclude"}:
        rule.update(user_ids=[actors.sales.username, actors.other.username],
                    group_ids=[group.id for group in groups])
        reversed_rule.update(user_ids=list(reversed(rule["user_ids"])),
                             group_ids=list(reversed(rule["group_ids"])))
    _, _, _, first = _publish(db, actors, whole=rule,
        fields={field + "_audience": rule for field in ("supplier", "reporter", "notes")})
    _, line, draft_id, repeated = _publish(db, actors, whole=reversed_rule, price="90.00",
        fields={field + "_audience": reversed_rule for field in ("supplier", "reporter", "notes")})
    assert repeated.id == first.id and repeated.revision_no == 2
    assert repeated.unit_price == Decimal("90.00") and line.ref_offer_id == first.id
    assert db.get(PresalesOffer, draft_id) is None
    assert db.scalar(select(func.count()).select_from(PresalesOffer)) == 1


def test_repeat_finds_equivalent_candidate_after_different_audience(db, actors):
    _, _, _, public = _publish(db, actors)
    _, _, _, private = _publish(db, actors, whole={"mode": "private"}, price="50.00")
    _, line, _, repeated = _publish(db, actors, whole={"mode": "private"}, price="40.00")
    assert public.id != private.id
    assert repeated.id == private.id and repeated.revision_no == 2
    assert public.unit_price == Decimal("100.00") and public.revision_no == 1
    assert line.ref_offer_id == private.id
    assert db.scalar(select(func.count()).select_from(PresalesOffer)) == 2


def test_same_batch_different_field_audiences_do_not_merge(db, actors):
    inquiry = inquiries.create_draft(db, actors.buyer.id, kind="proactive_offer",
        lines=[{"part_id": actors.part.id}, {"part_id": actors.part.id}])
    for line, mode in zip(inquiries.lines_for(db, inquiry.id), ("public", "private")):
        offers.save_draft(db, inquiry.id, line.id, actors.buyer.id,
            {"unit_price": "100.00", "source_key": "same-source",
             "reporter_audience": {"mode": mode}}, expected_version=inquiry.version)
    result = offers.publish_proactive(db, inquiry.id, actors.buyer.id,
                                      expected_version=inquiry.version)
    assert len(result) == 2
    assert db.scalar(select(func.count()).select_from(PresalesOffer)) == 2


def test_default_public_and_explicit_public_are_equivalent(db, actors):
    _, _, _, first = _publish(db, actors)
    _, line, _, repeated = _publish(db, actors, whole={"mode": "public"},
        fields={"reporter_audience": {"mode": "public"}, "notes_audience": {"mode": "public"}})
    assert repeated.id == first.id and repeated.revision_no == 2
    assert line.ref_offer_id == first.id

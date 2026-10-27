"""重复报价按采购的显式选择更新或独立发布，保留受众参数矩阵。"""

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.models.dimensions import DimPart
from app.models.presales import PresalesOffer
from app.models.system import SysUser
from app.services import audience
from app.services.presales import common, inquiries, offers
from app.services.presales.registration import register_subjects


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


def _publish(db, actors, *, whole=None, fields=None, price="100.00",
             on_existing="new", existing_offer_id=None):
    inquiry = inquiries.create_draft(db, actors.buyer.id, kind="proactive_offer",
        lines=[{"part_id": actors.part.id}], offer_audience=whole)
    line = inquiries.lines_for(db, inquiry.id)[0]
    draft = offers.save_draft(db, inquiry.id, line.id, actors.buyer.id,
        {"unit_price": price, "source_key": "same-source", **(fields or {})},
        expected_version=inquiry.version)
    draft_id = draft.id
    result = offers.publish_proactive(db, inquiry.id, actors.buyer.id,
                                      expected_version=inquiry.version, on_existing=on_existing,
                                      existing_offer_id=existing_offer_id)
    return inquiry, line, draft_id, result[0]


@pytest.mark.parametrize("field", ["supplier", "reporter", "notes"])
@pytest.mark.parametrize("mode", ["private", "include"])
def test_explicit_new_preserves_independent_quote_with_field_audience_difference(db, actors, field, mode):
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
def test_explicit_new_keeps_different_member_sets_separate(db, actors, scope, target):
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
def test_explicit_update_accepts_equivalent_rules_with_new_ids_and_reordered_members(db, actors, mode):
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
        fields={field + "_audience": reversed_rule for field in ("supplier", "reporter", "notes")},
        on_existing="update", existing_offer_id=first.id)
    assert repeated.id == first.id and repeated.revision_no == 2
    assert repeated.unit_price == Decimal("90.00") and repeated.line_id == line.id
    assert db.get(PresalesOffer, draft_id) is None
    assert db.scalar(select(func.count()).select_from(PresalesOffer)) == 1


def test_explicit_update_targets_selected_private_quote_among_different_audiences(db, actors):
    _, _, _, public = _publish(db, actors)
    _, _, _, private = _publish(db, actors, whole={"mode": "private"}, price="50.00")
    _, line, _, repeated = _publish(db, actors, whole={"mode": "private"}, price="40.00",
        on_existing="update", existing_offer_id=private.id)
    assert public.id != private.id
    assert repeated.id == private.id and repeated.revision_no == 2
    assert public.unit_price == Decimal("100.00") and public.revision_no == 1
    assert repeated.line_id == line.id
    assert db.scalar(select(func.count()).select_from(PresalesOffer)) == 2


def test_explicit_new_keeps_default_and_explicit_public_quotes_independent(db, actors):
    _, _, _, first = _publish(db, actors)
    _, line, _, repeated = _publish(db, actors, whole={"mode": "public"},
        fields={"reporter_audience": {"mode": "public"}, "notes_audience": {"mode": "public"}})
    assert repeated.id != first.id and repeated.revision_no == 1
    assert first.revision_no == 1 and line.ref_offer_id is None
    assert db.scalar(select(func.count()).select_from(PresalesOffer)) == 2

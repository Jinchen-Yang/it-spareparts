"""批量报价输出与单条契约等价，查询次数不随有效报价条数增加。"""

from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import event

from app.db import engine
from app.models.presales import PresalesInquiry, PresalesInquiryLine, PresalesOffer
from app.models.system import SysUser
from app.services import audience
from app.services.presales import common, inquiries, library, offers, visibility
from app.services.workflow import work_items
from app.services.workflow.registry import Subject
from tests.test_presales_api_visibility import _ctx, _quoted, users


@contextmanager
def _selects():
    statements = []

    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", capture)


@pytest.mark.parametrize("viewer", ["buyer", "other", "peer", "supervisor"])
def test_batch_and_single_views_share_field_matrix_without_cross_record_rules(db, users, monkeypatch, viewer):
    original_filter = audience.filter_fields
    calls = []

    def track(*args, **kwargs):
        calls.append(True)
        return original_filter(*args, **kwargs)

    monkeypatch.setattr(audience, "filter_fields", track)
    for mode in ("public", "private", "include", "exclude"):
        rule = {"mode": mode}
        if mode in {"include", "exclude"}:
            rule["user_ids"] = [users.other.username]
        inquiry, first = _quoted(db, users, rule=rule)
        second = offers.save_draft(db, inquiry.id, inquiries.lines_for(db, inquiry.id)[0].id,
            users.peer.id, {"unit_price": "51.00", "supplier_id": users.supplier.id,
                "notes": "另一条公开备注", "supplier_audience": {"mode": "public"}},
            expected_version=inquiry.version)
        offers.reply(db, inquiry.id, users.peer.id,
            expected_version=inquiry.version, on_existing="new")
        ctx = _ctx(db, getattr(users, viewer))
        before = len(calls)
        result = visibility.offer_views(db, [first, second], ctx)
        assert len(calls) == before + 1
        assert result == [visibility.offer_view(db, first, ctx), visibility.offer_view(db, second, ctx)]
        assert [row["id"] for row in result] == [first.id, second.id]
        expected = viewer in {"buyer", "supervisor"} or mode == "public" or (
            mode == "include" and viewer == "other") or (mode == "exclude" and viewer != "other")
        for field in ("supplier_id", "source_key", "reporter", "notes"):
            assert (field in result[0]) is expected, (mode, viewer, field)
            assert field in result[1]
        assert all("created_by" not in row and "work_item_id" not in row and "customer_id" not in row
                   for row in result)


def test_batch_record_access_and_field_groups_keep_the_strict_intersection(db, users):
    public, first = _quoted(db, users)
    private, second = _quoted(db, users, whole={"mode": "private"})
    ctx = _ctx(db, users.other)
    assert [row["id"] for row in visibility.offer_views(db, [first, second], ctx)] == [first.id]
    assert visibility.visible_offers(db, [first, second], ctx) == [first]
    assert not visibility.can_view_inquiry(db, private, ctx)
    graph = dict(users.buyer.template_perms)
    graph["data_supplier"] = False
    users.buyer.template_perms = graph
    db.flush()
    own = visibility.offer_views(db, [first, second], _ctx(db, users.buyer))
    assert len(own) == 2
    assert all("supplier_id" not in row and "source_key" not in row for row in own)


def test_workflow_batch_participation_matches_single_and_refreshes_after_claim(db, users):
    subjects = []
    for assignees in ([], [users.buyer.id], [users.peer.id]):
        inquiry = inquiries.create_draft(db, users.seller.id,
            lines=[{"part_id": users.part.id}], assignee_ids=assignees)
        inquiries.submit(db, inquiry.id, users.seller.id, inquiry.version)
        subjects.append(inquiry.id)
    user_id = users.buyer.id
    with _selects() as statements:
        result = work_items.participation_batch(db, "presales_inquiry", subjects, user_id)
    assert len(statements) == 2
    assert result == {subject_id: work_items.subject_participation(
        db, Subject("presales_inquiry", subject_id), user_id) for subject_id in subjects}
    assert result[subjects[0]]["unclaimed"] and result[subjects[1]]["assigned"]
    assert not result[subjects[2]]["assigned"] and not result[subjects[2]]["unclaimed"]
    item = work_items.list_by_subject(db, Subject("presales_inquiry", subjects[0]), user_id)[0]
    work_items.claim(db, item.id, user_id, item.version)
    refreshed = work_items.participation_batch(db, "presales_inquiry", subjects, user_id)
    assert refreshed[subjects[0]]["assigned"] and not refreshed[subjects[0]]["unclaimed"]


def test_1500_effective_quotes_have_bounded_library_and_boost_queries(db, users):
    moment = common.now()
    owner, writer = _ctx(db, users.seller), _ctx(db, users.buyer)
    public_rule = common.rule_id(db, {"mode": "public"}, owner)
    private_rule = common.rule_id(db, {"mode": "private"}, writer)
    # 每位报价人一条主报价，保留大结果集的查询数与字段矩阵契约。
    reporters = [SysUser(username=f"batch_buyer_{index}", role="purchaser",
        password_hash="测试账号") for index in range(1500)]
    db.add_all(reporters)
    db.flush()
    headers = [PresalesInquiry(kind="sales_inquiry", inquiry_no=f"BATCH{i:010d}",
        status="submitted", submitted_at=moment, created_by=users.seller.id,
        offer_audience_rule_id=public_rule) for i in range(1500)]
    db.add_all(headers)
    db.flush()
    lines = [PresalesInquiryLine(inquiry_id=header.id, line_key="one", seq=1,
        part_id=users.part.id, pn_raw=users.part.pn_std, qty=1) for header in headers]
    db.add_all(lines)
    db.flush()
    db.add_all([PresalesOffer(inquiry_id=line.inquiry_id, line_id=line.id,
        created_by=reporter.id, part_id=users.part.id, status="published",
        unit_price=Decimal("20.25"), source_key="supplier", supplier_id=users.supplier.id,
        supplier_audience_rule_id=private_rule, notes_audience_rule_id=private_rule,
        valid_from=moment - timedelta(hours=1), valid_until=moment + timedelta(days=30))
        for line, reporter in zip(lines, reporters)])
    db.flush()
    viewer_id, part_id = users.other.id, users.part.id
    db.expire_all()
    with _selects() as statements:
        result = library.batch(db, [part_id], viewer_id, now=moment)
    assert len(result[part_id]) == 1500
    assert len(statements) <= 8, len(statements)
    assert all("supplier_id" not in row and "notes" not in row for row in result[part_id])
    db.expire_all()
    with _selects() as statements:
        boost = library.boost_part_ids(db, "任意匹配文本", viewer_id)
    assert boost == [part_id]
    assert len(statements) <= 8, len(statements)

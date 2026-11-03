"""批量报价输出与单条契约等价，查询次数不随有效报价条数增加。"""

from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import event

from app.db import engine
from app.models.presales import PresalesInquiry, PresalesInquiryLine, PresalesOffer
from app.models.system import SysUser
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


def test_shared_offer_fields_match_across_readers_and_omit_internal_fields(db, users):
    private = {"mode": "private"}
    inquiry, first = _quoted(db, users, whole=private, rule=private)
    line = inquiries.lines_for(db, inquiry.id)[0]
    second = offers.save_draft(db, inquiry.id, line.id, users.peer.id,
        {"unit_price": "51.00", "supplier_id": users.supplier.id, "source_key": "peer",
         "notes": "另一条备注"}, expected_version=inquiry.version)
    offers.reply(db, inquiry.id, users.peer.id, expected_version=inquiry.version, on_existing="new")
    users.other.perm_overrides = {"data_supplier": False, "data_customer": False}
    db.flush()
    viewer = _ctx(db, users.other)
    batch = visibility.offer_views(db, [first, second], viewer)
    assert batch == [visibility.offer_view(db, row, viewer) for row in (first, second)]
    detail = visibility.inquiry_view(db, inquiry, viewer)
    assert "customer_id" not in detail and "customer_name" not in detail
    library_rows = library.batch(db, [users.part.id], viewer)[users.part.id]
    for rows in (batch, detail["offers"], library_rows):
        by_id = {row["id"]: row for row in rows}
        assert set(by_id) == {first.id, second.id}
        for offer, writer in ((first, users.buyer), (second, users.peer)):
            row = by_id[offer.id]
            assert row["unit_price"] == offer.unit_price and row["notes"] == offer.notes
            assert row["supplier_id"] == users.supplier.id and row["source_key"] == offer.source_key
            assert row["reporter"] == {"id": writer.id, "name": writer.display_name}
            assert not {"created_by", "work_item_id", "customer_id", "workbook_choice",
                        "offer_audience_rule_id", "supplier_audience_rule_id",
                        "notes_audience_rule_id", "reporter_audience_rule_id"}.intersection(row)


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
    db.expire_all()
    with _selects() as statements:
        boost = library.boost_part_ids(db, "任意匹配文本", viewer_id)
    assert boost == [part_id]
    assert len(statements) <= 8, len(statements)

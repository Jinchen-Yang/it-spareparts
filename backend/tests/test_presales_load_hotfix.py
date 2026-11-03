"""售前热修：页外历史增加不增加准入 SQL，权限仍与逐条规则一致。"""

from contextlib import contextmanager
from datetime import date

import pytest
from sqlalchemy import event, select

from app.db import engine
from app.models.fields_audience import FieldValue
from app.models.presales import PresalesInquiry
from app.models.workflow import WorkItem
from app.services import field_pool
from app.services.presales import common, inquiries, pool, visibility
from app.services.workflow import work_items
from tests.test_presales_api_visibility import _client, _ctx, _quoted, users  # noqa: F401


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


@pytest.mark.parametrize("viewer", ["seller", "other", "buyer", "peer", "supervisor", "denied"])
def test_batch_inquiry_admission_matches_single_for_audiences_and_old_participation(db, users, viewer):
    rows = []
    for mode in ("public", "private", "include", "exclude"):
        rule = {"mode": mode}
        if mode in {"include", "exclude"}:
            rule["user_ids"] = [users.other.username]
        rule_id = common.rule_id(db, rule, _ctx(db, users.seller))
        for relation in ("none", "assigned", "done", "pool", "draft"):
            row = PresalesInquiry(kind="sales_inquiry", inquiry_no=f"HF{len(rows):010d}",
                status="draft" if relation == "draft" else "submitted",
                submitted_at=None if relation == "draft" else common.now(),
                created_by=users.seller.id, offer_audience_rule_id=rule_id)
            db.add(row)
            db.flush()
            if relation != "none":
                db.add(WorkItem(subject_type="presales_inquiry", subject_id=row.id,
                    pool_key="presales_inquiry", created_by=users.seller.id,
                    assignee_id=None if relation == "pool" else users.buyer.id,
                    status="done" if relation == "done" else "pending"))
            rows.append(row)
    db.flush()
    ctx = _ctx(db, getattr(users, viewer))
    expected = {row.id: visibility.can_view_inquiry(db, row, ctx) for row in rows}
    with _selects() as statements:
        result = visibility.inquiry_access_batch(db, rows, ctx)
    assert {key: flags["visible"] for key, flags in result.items()} == expected
    assert len(statements) <= 5
    for row in rows:
        assigned = work_items.subject_participation(
            db, ("presales_inquiry", row.id), ctx)["assigned"]
        if row.created_by != getattr(users, viewer).id and viewer != "denied":
            assert result[row.id]["assigned"] == assigned


def test_inquiry_list_total_select_count_is_independent_of_page_outside_rows(db, users):
    def add_rows(start, count):
        for index in range(start, start + count):
            inquiries.create_draft(db, users.seller.id, lines=[{"pn_raw": f"VISIBLE-{index}"}])
            inquiries.create_draft(db, users.other.id, lines=[{"pn_raw": f"HIDDEN-{index}"}])
        db.commit()

    add_rows(0, 5)
    client = _client(users.seller)
    params = {"scope": "mine", "sort": "inquiry_no", "dir": "asc", "page_size": 2}
    with _selects() as baseline_queries:
        response = client.get("/api/presales/inquiries", params=params)
    assert response.status_code == 200, response.text
    baseline = response.json()
    assert baseline["total"] == 5 and len(baseline["items"]) == 2
    add_rows(5, 25)
    with _selects() as expanded_queries:
        response = client.get("/api/presales/inquiries", params=params)
    assert response.status_code == 200, response.text
    expanded = response.json()
    assert expanded["total"] == 30
    assert [row["id"] for row in expanded["items"]] == [row["id"] for row in baseline["items"]]
    assert len(expanded_queries) == len(baseline_queries)


def test_work_item_admission_select_count_is_independent_of_unshown_rows(db, users):
    def add_pool_rows(start, count):
        for index in range(start, start + count):
            row = PresalesInquiry(kind="sales_inquiry", inquiry_no=f"HFW{index:010d}",
                status="submitted", submitted_at=common.now(), created_by=users.seller.id)
            db.add(row)
            db.flush()
            db.add(WorkItem(subject_type="presales_inquiry", subject_id=row.id,
                pool_key="presales_inquiry", status="pending", created_by=users.seller.id))
        db.commit()

    add_pool_rows(0, 5)
    ctx = _ctx(db, users.buyer)
    db.expire_all()
    with _selects() as baseline_queries:
        before = work_items.visible_items(db, ctx, "pool", "presales_inquiry", page_size=1)
    assert before["total"] == 5 and len(before["items"]) == 1
    add_pool_rows(5, 25)
    db.expire_all()
    with _selects() as expanded_queries:
        after = work_items.visible_items(db, ctx, "pool", "presales_inquiry", page_size=1)
    assert after["total"] == 30 and len(after["items"]) == 1
    assert len(expanded_queries) == len(baseline_queries)


@pytest.mark.parametrize("viewer", ["seller", "buyer", "peer", "other", "supervisor"])
def test_list_summary_preserves_detail_business_fields_and_permissions(db, users, viewer):
    inquiry, _ = _quoted(db, users, rule={"mode": "private"},
        whole={"mode": "include", "user_ids": [users.other.username, users.supervisor.username]})
    ctx = _ctx(db, getattr(users, viewer))
    detail = visibility.inquiry_view(db, inquiry, ctx)
    summary = visibility.inquiry_view(db, inquiry, ctx, detail=False, summary=True)
    for key in ("offers", "work_items", "line_responses", "line_answers", "progress", "can_urge"):
        assert summary[key] == detail[key], key
    assert "field_values" not in summary
    for line, full in zip(summary["lines"], detail["lines"], strict=True):
        assert line == {key: value for key, value in full.items()
                        if key not in {"field_values", "field_template", "notes_audience"}}


def test_shared_reads_keep_legacy_delivery_without_loading_list_templates_or_writing_back(db, users, monkeypatch):
    inquiry, _ = _quoted(db, users)
    line = inquiries.lines_for(db, inquiry.id)[0]
    subject = field_pool.Subject(inquiries.SUBJECT_TYPE, f"line:{line.id}", "line")
    field_pool.save_values(db, subject,
        [{"new_name": "期望交期", "value": "2026-12-03", "rule": {"mode": "private"}}],
        _ctx(db, users.seller))
    viewers = [_ctx(db, user) for user in (users.seller, users.other)]
    expected = date(2026, 12, 3)
    for viewer in viewers:
        assert visibility.inquiry_view(db, inquiry, viewer)["lines"][0]["expected_delivery"] == expected

    def no_template(*args, **kwargs):
        pytest.fail("摘要和共享池行读出不应加载字段模板")

    monkeypatch.setattr(field_pool, "template_for", no_template)
    for viewer in viewers:
        summary = visibility.inquiry_view(db, inquiry, viewer, detail=False, summary=True)
        assert summary["lines"][0]["expected_delivery"] == expected
        assert pool.list_lines(db, viewer, tab="all")["items"][0]["expected_delivery"] == expected
        assert pool.line_view(db, line.id, viewer)["expected_delivery"] == expected
    db.flush()
    db.refresh(line)
    assert line.expected_delivery is None
    legacy_value = select(FieldValue.value).where(FieldValue.subject_id == subject.subject_id)
    assert db.scalar(legacy_value) == "2026-12-03"

    version = line.version
    cleared = pool.edit_line(db, line.id, users.other.id, {"expected_delivery": None}, expected_version=version)
    assert cleared["expected_delivery"] is None and cleared["line_version"] == version + 1
    assert db.scalar(legacy_value) is None
    summary = visibility.inquiry_view(db, inquiry, viewers[0], detail=False, summary=True)
    assert summary["lines"][0]["expected_delivery"] is None
    assert pool.list_lines(db, users.seller.id, tab="all")["items"][0]["expected_delivery"] is None

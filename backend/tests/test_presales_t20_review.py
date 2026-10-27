"""T20 交期迁移不能撤销工作簿授权，普通 M7 字段仍须复查。"""

from datetime import date
from io import BytesIO

from openpyxl import load_workbook
import pytest
from sqlalchemy import select

from app.models.fields_audience import FieldValue
from app.models.presales import PresalesWorkbookExport
from app.services import field_pool
from app.services.presales import inquiries, inquiry_workbook, quote_workbook
from app.services.workbook import operations
from app.services.workbook.protocol import read_metadata
from app.services.workbook.registry import Conflict, Forbidden
from tests.test_presales_inquiry_workbook import _ctx, _preview, _apply, _edit, _set
from tests.test_presales_quote_workbook import quote_data, _submitted, _edit as _edit_quote


def _legacy(db, inquiry, author, *, name="期望交期"):
    line = inquiries.lines_for(db, inquiry.id)[0]
    subject = field_pool.Subject(inquiries.SUBJECT_TYPE, f"line:{line.id}", "line")
    field_pool.save_values(db, subject, [{"new_name": name, "value": "2026-12-10"}], _ctx(db, author))
    value = db.scalar(select(FieldValue).where(FieldValue.subject_id == subject.subject_id))
    return line, value.field_def_id


def _export_record(db, content):
    metadata, _ = read_metadata(load_workbook(BytesIO(content)))
    return db.scalar(select(PresalesWorkbookExport).where(
        PresalesWorkbookExport.export_id == metadata["export_id"]))


def _historical_quote_scope(db, content, line, field_id):
    record = _export_record(db, content)
    record.scope = {**record.scope, "rows": [
        {**row, "delivery_fields": [[line.id, field_id]]} for row in record.scope["rows"]]}
    db.flush()


def _historical_inquiry_scope(db, content, line, field_id, author):
    record = _export_record(db, content)
    record.scope = {**record.scope, "fields": [*record.scope["fields"], [line.id, field_id]],
                    "field_authors": {**record.scope["field_authors"], f"{line.id}:{field_id}": author.id}}
    db.flush()


@pytest.mark.parametrize("clear", [False, True])
def test_quote_historical_delivery_migration_allows_old_new_and_replay(db, quote_data, clear):
    data = quote_data
    inquiry = _submitted(db, data)
    line, field_id = _legacy(db, inquiry, data.sales)
    old = quote_workbook.export_quote_sheet(db, [inquiry.id], data.buyer.id)
    _historical_quote_scope(db, old, line, field_id)
    # 执行保存共用的迁移原语，隔离迁移本身与正常版本冲突。
    inquiries._migrate_delivery(db, line, _ctx(db, data.sales), explicit=clear)
    db.flush()
    assert line.expected_delivery == (None if clear else date(2026, 12, 10))
    assert db.scalar(select(FieldValue.id)) is None
    new = quote_workbook.export_quote_sheet(db, [inquiry.id], data.buyer.id)
    for content in (old, new):
        preview = operations.preview(db, quote_workbook.QUOTE_SCHEMA.name, content, ctx=_ctx(db, data.buyer))
        assert preview["plan"]["counts"]["conflict"] == 0
        result = operations.apply(db, preview["operation_id"], data.buyer.id, preview["plan_digest"])
        assert result["status"] == "applied"
        replay = operations.preview(db, quote_workbook.QUOTE_SCHEMA.name, content, ctx=_ctx(db, data.buyer))
        assert replay["replayed"] and replay["response"] == result["response"]
        assert operations.apply(db, preview["operation_id"], data.buyer.id, preview["plan_digest"])["replayed"]


def test_new_exports_do_not_authorize_delivery_as_a_field(db, quote_data):
    data = quote_data
    inquiry = inquiries.create_draft(db, data.sales.id, assignee_ids=[data.buyer.id],
        lines=[{"part_id": data.parts[0].id}])
    line, field_id = _legacy(db, inquiry, data.sales)
    content = inquiry_workbook.export_inquiry(db, inquiry.id, _ctx(db, data.sales))
    scope = _export_record(db, content).scope
    assert [line.id, field_id] not in scope["fields"]
    assert f"{line.id}:{field_id}" not in scope["field_authors"]
    inquiries.submit(db, inquiry.id, data.sales.id, inquiry.version)
    content = quote_workbook.export_quote_sheet(db, [inquiry.id], data.buyer.id)
    assert all("delivery_fields" not in row for row in _export_record(db, content).scope["rows"])


@pytest.mark.parametrize("clear", [False, True])
def test_inquiry_other_author_deleted_delivery_allows_reimport_and_replay(db, quote_data, clear):
    data = quote_data
    inquiry = inquiries.create_draft(db, data.sales.id, lines=[{"part_id": data.parts[0].id}])
    line, field_id = _legacy(db, inquiry, data.other)
    old = inquiry_workbook.export_inquiry(db, inquiry.id, _ctx(db, data.sales))
    _historical_inquiry_scope(db, old, line, field_id, data.other)
    inquiries._migrate_delivery(db, line, _ctx(db, data.sales), explicit=clear)
    db.flush()
    assert db.scalar(select(FieldValue.id)) is None
    assert line.expected_delivery == (None if clear else date(2026, 12, 10))
    content = inquiry_workbook.export_inquiry(db, inquiry.id, _ctx(db, data.sales))
    content = _edit(content, lambda sheet, cols: _set(sheet, cols, 2, **{"数量": 3}))
    preview = _preview(db, data.sales, content, inquiry)
    result = _apply(db, data.sales, preview)
    assert line.qty == 3
    replay = _preview(db, data.sales, content, inquiry)
    assert replay["replayed"] and replay["response"] == result["response"]
    assert _apply(db, data.sales, preview)["replayed"]


def test_real_draft_save_does_not_poison_quote_exports(db, quote_data):
    data = quote_data
    inquiry = _submitted(db, data)
    line, field_id = _legacy(db, inquiry, data.sales)
    old = quote_workbook.export_quote_sheet(db, [inquiry.id], data.buyer.id)
    _historical_quote_scope(db, old, line, field_id)
    inquiries.withdraw(db, inquiry.id, data.sales.id, inquiry.version)
    inquiries.update_draft(db, inquiry.id, data.sales.id, inquiry.version, lines=[{"line_key": line.line_key}])
    inquiries.resubmit(db, inquiry.id, data.sales.id, inquiry.version)
    assert line.expected_delivery == date(2026, 12, 10)
    assert db.scalar(select(FieldValue.id)) is None
    # 旧文件仍有正常版本保护，但不再被历史交期永久判为失权。
    preview = operations.preview(db, quote_workbook.QUOTE_SCHEMA.name, old, ctx=_ctx(db, data.buyer))
    assert preview["plan"]["counts"]["conflict"] == 1
    with pytest.raises(Conflict):
        operations.apply(db, preview["operation_id"], data.buyer.id, preview["plan_digest"])
    new = quote_workbook.export_quote_sheet(db, [inquiry.id], data.buyer.id)
    changed = _edit_quote(new, {2: {"处理方式": "报价", "单价": 12}})
    preview = operations.preview(db, quote_workbook.QUOTE_SCHEMA.name, changed, ctx=_ctx(db, data.buyer))
    result = operations.apply(db, preview["operation_id"], data.buyer.id, preview["plan_digest"])
    assert result["status"] == "applied" and result["response"]["saved"][0]["mode"] == "quote"


@pytest.mark.parametrize("deleted", [False, True])
def test_non_delivery_field_still_denies_inquiry_preview_and_replay(db, quote_data, deleted):
    data = quote_data
    inquiry = inquiries.create_draft(db, data.sales.id, lines=[{"part_id": data.parts[0].id}])
    line, field_id = _legacy(db, inquiry, data.other, name="质保要求")
    content = inquiry_workbook.export_inquiry(db, inquiry.id, _ctx(db, data.sales))
    preview = _preview(db, data.sales, content, inquiry)
    _apply(db, data.sales, preview)
    value = db.scalar(select(FieldValue))
    if deleted:
        db.delete(value)
        db.flush()
    else:
        field_pool.save_values(db, field_pool.Subject(inquiries.SUBJECT_TYPE, f"line:{line.id}", "line"),
            [{"field_def_id": field_id, "value": "2026-12-10", "rule": {"mode": "private"}}], _ctx(db, data.other))
    with pytest.raises(Forbidden):
        _preview(db, data.sales, content, inquiry)
    with pytest.raises(Forbidden):
        _apply(db, data.sales, preview)


@pytest.mark.parametrize("recipient", ["buyer", "peer"])
@pytest.mark.parametrize("new_export", [False, True])
def test_quote_migrated_delivery_allows_edited_file_and_related_export_recipient(db, quote_data, recipient, new_export):
    data = quote_data
    inquiry = inquiries.create_draft(db, data.sales.id, assignee_ids=[data.buyer.id, data.peer.id],
        lines=[{"part_id": data.parts[0].id}])
    inquiries.submit(db, inquiry.id, data.sales.id, inquiry.version)
    line, field_id = _legacy(db, inquiry, data.sales)
    old = quote_workbook.export_quote_sheet(db, [inquiry.id], data.buyer.id)
    _historical_quote_scope(db, old, line, field_id)
    actor = getattr(data, recipient)
    # 每个采购的签名还包含各自待办版本；用各自的旧文件验证应用，保留另一人的关联旧范围。
    if recipient == "peer":
        old = quote_workbook.export_quote_sheet(db, [inquiry.id], actor.id)
        _historical_quote_scope(db, old, line, field_id)
    inquiries._migrate_delivery(db, line, _ctx(db, data.sales))
    db.flush()
    content = quote_workbook.export_quote_sheet(db, [inquiry.id], actor.id) if new_export else old
    content = _edit_quote(content, {2: {"处理方式": "报价", "单价": 12}})
    preview = operations.preview(db, quote_workbook.QUOTE_SCHEMA.name, content, ctx=_ctx(db, actor))
    result = operations.apply(db, preview["operation_id"], actor.id, preview["plan_digest"])
    assert result["status"] == "applied" and result["response"]["saved"][0]["mode"] == "quote"


@pytest.mark.parametrize("kind", ["inquiry", "quote"])
def test_receipt_created_before_delivery_migration_still_replays(db, quote_data, kind):
    data = quote_data
    inquiry = inquiries.create_draft(db, data.sales.id, assignee_ids=[data.buyer.id],
        lines=[{"part_id": data.parts[0].id}])
    line, field_id = _legacy(db, inquiry, data.other)
    if kind == "quote":
        inquiries.submit(db, inquiry.id, data.sales.id, inquiry.version)
        content = quote_workbook.export_quote_sheet(db, [inquiry.id], data.buyer.id)
        _historical_quote_scope(db, content, line, field_id)
        preview = operations.preview(db, quote_workbook.QUOTE_SCHEMA.name, content, ctx=_ctx(db, data.buyer))
        result = operations.apply(db, preview["operation_id"], data.buyer.id, preview["plan_digest"])
    else:
        content = inquiry_workbook.export_inquiry(db, inquiry.id, _ctx(db, data.sales))
        _historical_inquiry_scope(db, content, line, field_id, data.other)
        preview = _preview(db, data.sales, content, inquiry)
        result = _apply(db, data.sales, preview)
    inquiries._migrate_delivery(db, line, _ctx(db, data.sales))
    db.flush()
    assert db.scalar(select(FieldValue.id)) is None
    if kind == "quote":
        replay = operations.preview(db, quote_workbook.QUOTE_SCHEMA.name, content, ctx=_ctx(db, data.buyer))
        applied = operations.apply(db, preview["operation_id"], data.buyer.id, preview["plan_digest"])
    else:
        replay = _preview(db, data.sales, content, inquiry)
        applied = _apply(db, data.sales, preview)
    assert replay["replayed"] and replay["response"] == result["response"]
    assert applied["replayed"] and applied["response"] == result["response"]

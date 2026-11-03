"""共享池报价表的合并分发、立即生效、草稿隔离与回执契约。"""

from datetime import datetime, timezone
from decimal import Decimal
from io import BytesIO
import os
import shutil
import subprocess
from types import SimpleNamespace

from openpyxl import load_workbook
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app import permissions
from app.db import SessionLocal
from app.models.dimensions import DimPart, DimSupplier
from app.models.presales import PresalesLineResponse, PresalesOffer, PresalesWorkbookExport
from app.models.system import SysUser
from app.services.presales import common, inquiries, offers, pool, quote_workbook, visibility
from app.services.presales.workbook_registration import register_workbooks
from app.services.workbook import operations
from app.services.workbook.registry import ApplyFailed, Conflict, Forbidden
from app.services.workbook.protocol import read_metadata
from app.services.workflow.registry import InvalidTransition


@pytest.fixture()
def quote_data(db, monkeypatch):
    register_workbooks()
    monkeypatch.setattr(common, "now", lambda: datetime(2026, 10, 25, 7, tzinfo=timezone.utc))
    users = {}
    for name, role in (("sales", "sales"), ("other", "sales"), ("buyer", "purchaser"), ("peer", "purchaser")):
        user = SysUser(username=f"workbook-{name}", role=role, display_name=name,
            password_hash="测试不登录", template_perms=permissions.effective(role, None), perm_overrides={})
        db.add(user)
        users[name] = user
    parts = [DimPart(pn_std=f"QUOTE-{i}", description=f"型号{i}") for i in range(4)]
    supplier = DimSupplier(name_raw="精确供应商")
    db.add_all([*parts, supplier])
    db.flush()
    return SimpleNamespace(**users, parts=parts, supplier=supplier)


def _submitted(db, data, *, owner=None, lines=None, exact=False, assigned=None):
    inquiry = inquiries.create_draft(db, (owner or data.sales).id,
        assignee_ids=[(assigned or data.buyer).id],
        lines=lines or [{"part_id": data.parts[0].id, "qty": 2, "require_exact_pn": exact}])
    inquiries.submit(db, inquiry.id, (owner or data.sales).id, inquiry.version)
    return inquiry


def _edit(content, changes):
    book = load_workbook(BytesIO(content))
    sheet = book["采购报价表"]
    columns = {cell.value: cell.column for cell in sheet[1]}
    for row_number, values in changes.items():
        for key, value in values.items():
            sheet.cell(row_number, columns[key], value)
    out = BytesIO()
    book.save(out)
    return out.getvalue()


def _apply(db, data, content):
    preview = operations.preview(db, quote_workbook.QUOTE_SCHEMA.name, content,
        ctx=common.resolve_actor(db, data.buyer.id)[1])
    return operations.apply(db, preview["operation_id"], data.buyer.id, preview["plan_digest"])


def _published(db, data, *, private=False):
    inquiry = inquiries.create_draft(db, data.buyer.id, kind="proactive_offer",
        offer_audience={"mode": "private"} if private else None,
        lines=[{"part_id": data.parts[0].id, "qty": 1}])
    line = inquiries.lines_for(db, inquiry.id)[0]
    offers.save_draft(db, inquiry.id, line.id, data.buyer.id,
        {"unit_price": "19.00"}, expected_version=inquiry.version)
    return offers.publish_proactive(db, inquiry.id, data.buyer.id,
        expected_version=inquiry.version, on_existing="new")[0]


def test_same_pn_merges_and_distributes_without_reply(db, quote_data):
    data = quote_data
    original = _published(db, data)
    original_revision = original.revision_no
    first = _submitted(db, data)
    second = _submitted(db, data, owner=data.other)
    content = quote_workbook.export_quote_sheet(db, [first.id, second.id], data.buyer.id)
    sheet = load_workbook(BytesIO(content))["采购报价表"]
    headers = {cell.value: cell.column for cell in sheet[1]}
    assert sheet.max_row == 2
    assert sheet.cell(2, headers["合计数量"]).value == 4
    assert first.inquiry_no in sheet.cell(2, headers["来源单号"]).value
    assert second.inquiry_no in sheet.cell(2, headers["来源单号"]).value
    changed = _edit(content, {2: {"处理方式": "报价", "单价": "１２.５０", "含税": "是", "供应商": data.supplier.name_raw}})
    _apply(db, data, changed)
    rows = list(db.scalars(select(PresalesOffer).where(PresalesOffer.id != original.id)))
    assert len(rows) == 2 and {row.inquiry_id for row in rows} == {first.id, second.id}
    # D-0052：Excel 与页面同一写入口，上传即生效，每个来源行一个当前价。
    assert all(row.status == "published" and row.unit_price == Decimal("12.50") for row in rows)
    assert all(row.supplier_id == data.supplier.id for row in rows)
    assert {line.answer_state for inquiry in (first, second) for line in inquiries.lines_for(db, inquiry.id)} == {"quoted"}
    replay = operations.preview(db, quote_workbook.QUOTE_SCHEMA.name, changed,
        ctx=common.resolve_actor(db, data.buyer.id)[1])
    assert replay["replayed"] and len(list(db.scalars(select(PresalesOffer)))) == 3
    assert original.revision_no == original_revision and original.unit_price == Decimal("19")


@pytest.mark.parametrize("bound", [True, False], ids=["part", "raw-pn"])
def test_opposite_return_defective_terms_split_and_are_readonly(db, quote_data, bound):
    data = quote_data
    identity = {"part_id": data.parts[0].id} if bound else {"pn_raw": "SAME-RAW-PN"}
    sources = [_submitted(db, data, lines=[{**identity, "return_defective": value}]) for value in (True, False)]
    content = quote_workbook.export_quote_sheet(db, [row.id for row in sources], data.buyer.id)
    sheet = load_workbook(BytesIO(content))["采购报价表"]
    columns = {cell.value: cell.column for cell in sheet[1]}
    assert sheet.max_row == 3
    cells = [sheet.cell(row, columns["坏件是否返回"]) for row in (2, 3)]
    assert {cell.value for cell in cells} == {True, False}
    assert all(cell.protection.locked for cell in cells)
    changed = _edit(content, {2: {"坏件是否返回": not cells[0].value}})
    preview = operations.preview(db, quote_workbook.QUOTE_SCHEMA.name, changed,
        ctx=common.resolve_actor(db, data.buyer.id)[1])
    assert not preview["plan"]["errors"] and preview["plan"]["counts"]["unchanged"] == 2
    assert any("锁定" in warning["message"] for warning in preview["plan"]["warnings"])
    assert [inquiries.lines_for(db, source.id)[0].return_defective for source in sources] == [True, False]


@pytest.mark.parametrize("label,basis", [("成本价", "cost"), ("售价", "sale")])
def test_workbook_price_basis_import_export_and_price_only_patch(db, quote_data, label, basis):
    data = quote_data
    inquiry = _submitted(db, data)
    content = quote_workbook.export_quote_sheet(db, [inquiry.id], data.buyer.id)
    _apply(db, data, _edit(content, {2: {"处理方式": "报价", "单价": 25, "价格性质": label}}))
    line = inquiries.lines_for(db, inquiry.id)[0]
    current = db.get(PresalesOffer, line.current_offer_id)
    assert current.price_basis == basis and current.is_tax_inclusive is True
    offers.update_offer(db, current.id, data.buyer.id, {"is_tax_inclusive": None}, expected_version=current.version)
    exported = quote_workbook.export_quote_sheet(db, [inquiry.id], data.buyer.id)
    sheet = load_workbook(BytesIO(exported))["采购报价表"]
    columns = {cell.value: cell.column for cell in sheet[1]}
    assert sheet.cell(2, columns["价格性质"]).value == label
    _apply(db, data, _edit(exported, {2: {"单价": 30}}))
    assert line.current_offer_id == current.id and current.status == "published"
    assert current.price_basis == basis and current.is_tax_inclusive is None and current.unit_price == Decimal("30")


def test_exact_requirement_splits_and_unknown_pn_can_skip(db, quote_data):
    data = quote_data
    sources = [_submitted(db, data), _submitted(db, data, exact=True),
               _submitted(db, data, lines=[{"pn_raw": "新型号", "qty": 1}])]
    content = quote_workbook.export_quote_sheet(db, [inquiry.id for inquiry in sources], data.buyer.id)
    sheet = load_workbook(BytesIO(content))["采购报价表"]
    columns = {cell.value: cell.column for cell in sheet[1]}
    rows = {(sheet.cell(n, columns["来源单号"]).value, sheet.cell(n, columns["PN"]).value,
             sheet.cell(n, columns["要求 PN 一致"]).value) for n in range(2, sheet.max_row + 1)}
    assert rows == {(sources[0].inquiry_no, data.parts[0].pn_std, False),
                    (sources[1].inquiry_no, data.parts[0].pn_std, True),
                    (sources[2].inquiry_no, "新型号", False)}
    result = _apply(db, data, _edit(content, {n: {"处理方式": "不报", "不报原因": "待确认"} for n in range(2, 5)}))
    lines = [inquiries.lines_for(db, inquiry.id)[0] for inquiry in sources]
    assert {row["line_id"] for row in result["response"]["saved"]} == {line.id for line in lines}
    responses = list(db.scalars(select(PresalesLineResponse)))
    assert len(responses) == 3 and all(row.mode == "skip" and not row.is_draft for row in responses)
    assert all(line.answer_state == "skip" and line.answer_reason == "待确认" and line.current_offer_id is None
               for line in lines)
    assert lines[2].part_id is None
    for inquiry in sources:
        view = visibility.inquiry_view(db, inquiry, common.resolve_actor(db, data.sales.id)[1])
        assert view["progress"]["quoted_lines"] == 1 and view["progress"]["effective_quoted_lines"] == 0
    peer_sheet = load_workbook(BytesIO(quote_workbook.export_quote_sheet(
        db, [inquiry.id for inquiry in sources], data.peer.id)))["采购报价表"]
    assert all(peer_sheet.cell(n, columns["处理方式"]).value == "不报" and
               peer_sheet.cell(n, columns["不报原因"]).value == "待确认" for n in range(2, 5))


def test_price_only_patch_preserves_different_source_drafts_and_hidden_columns(db, quote_data):
    data = quote_data
    sources = [_submitted(db, data), _submitted(db, data)]
    lines = [inquiries.lines_for(db, inquiry.id)[0] for inquiry in sources]
    for index, (inquiry, line) in enumerate(zip(sources, lines)):
        offers.save_draft(db, inquiry.id, line.id, data.buyer.id, {
            "unit_price": str(10 + index), "supplier_id": data.supplier.id,
            "price_basis": ("cost", "sale")[index], "is_tax_inclusive": (None, False)[index],
            "notes": f"来源备注{index}", "stock_location": f"仓库{index}",
        }, expected_version=inquiry.version)
    content = quote_workbook.export_quote_sheet(db, [row.id for row in sources], data.buyer.id,
        visible_columns={"lines": ["unit_price"]})
    sheet = load_workbook(BytesIO(content))["采购报价表"]
    assert sheet.max_row == 2 and [cell.value for cell in sheet[1]] == ["_row_token", "来源单号", "PN", "单价"]
    _apply(db, data, _edit(content, {2: {"单价": "25.00"}}))
    current = [db.get(PresalesOffer, line.current_offer_id) for line in lines]
    assert current[0].id != current[1].id
    assert all(offer.status == "published" and offer.unit_price == Decimal("25.00")
               and offer.supplier_id == data.supplier.id for offer in current)
    assert [offer.notes for offer in current] == ["来源备注0", "来源备注1"]
    assert [offer.stock_location for offer in current] == ["仓库0", "仓库1"]
    assert [offer.price_basis for offer in current] == ["cost", "sale"]
    assert [offer.is_tax_inclusive for offer in current] == [None, False]
    sheet = load_workbook(BytesIO(quote_workbook.export_quote_sheet(
        db, [row.id for row in sources], data.buyer.id)))["采购报价表"]
    columns = {cell.value: cell.column for cell in sheet[1]}
    assert sheet.cell(2, columns["单价"]).value == 25


def test_deleted_own_draft_does_not_block_first_receipt_replay(db, quote_data):
    data = quote_data
    inquiry = _submitted(db, data)
    line = inquiries.lines_for(db, inquiry.id)[0]
    draft = offers.save_draft(db, inquiry.id, line.id, data.buyer.id,
        {"unit_price": "12.00", "notes": "本人草稿备注"}, expected_version=inquiry.version)
    content = _edit(quote_workbook.export_quote_sheet(db, [inquiry.id], data.buyer.id),
        {2: {"处理方式": "不报", "不报原因": "本轮不报"}})
    result = _apply(db, data, content)
    assert line.answer_state == "skip" and line.current_offer_id is None
    assert draft.status == "draft"
    # 模拟本人随后清掉未发布草稿；回执不能依赖草稿实体仍存在。
    db.delete(draft)
    db.flush()
    version = line.version
    ctx = common.resolve_actor(db, data.buyer.id)[1]
    replay = operations.preview(db, quote_workbook.QUOTE_SCHEMA.name, content, ctx=ctx)
    assert replay["replayed"] and replay["response"] == result["response"]
    assert line.version == version and len(list(db.scalars(select(PresalesLineResponse)))) == 1
    data.buyer.perm_overrides = {"page_presales": False}
    db.flush()
    with pytest.raises(Forbidden):
        operations.preview(db, quote_workbook.QUOTE_SCHEMA.name, content, ctx=ctx)
    with pytest.raises(Forbidden):
        operations.apply(db, result["operation_id"], data.buyer.id, result["plan_digest"])


def test_another_assignees_line_can_export_but_stale_version_conflicts(db, quote_data):
    data = quote_data
    inquiry = _submitted(db, data, assigned=data.peer)
    line = inquiries.lines_for(db, inquiry.id)[0]
    content = _edit(quote_workbook.export_quote_sheet(db, [inquiry.id], data.buyer.id),
        {2: {"处理方式": "不报", "不报原因": "无货"}})
    preview = operations.preview(db, quote_workbook.QUOTE_SCHEMA.name, content,
        ctx=common.resolve_actor(db, data.buyer.id)[1])
    pool.set_price(db, line.id, data.peer.id, {"unit_price": 15}, expected_version=line.version)
    with pytest.raises(Conflict):
        operations.apply(db, preview["operation_id"], data.buyer.id, preview["plan_digest"])
    assert line.answer_state == "quoted" and db.get(PresalesOffer, line.current_offer_id).unit_price == Decimal("15")


def test_reuse_copies_shared_price_and_historical_tax_to_each_source(db, quote_data):
    data = quote_data
    original = _published(db, data, private=True)
    offers.update_offer(db, original.id, data.buyer.id,
        {"price_basis": "cost", "is_tax_inclusive": None}, expected_version=original.version)
    sources = [_submitted(db, data), _submitted(db, data, owner=data.other)]
    content = quote_workbook.export_quote_sheet(db, [inquiry.id for inquiry in sources], data.peer.id)
    sheet = load_workbook(BytesIO(content))["采购报价表"]
    columns = {cell.value: cell.column for cell in sheet[1]}
    assert f"#{original.id}：" in sheet.cell(2, columns["已有有效报价"]).value
    content = _edit(content, {2: {"处理方式": "沿用", "沿用报价编号": original.id}})
    preview = operations.preview(db, quote_workbook.QUOTE_SCHEMA.name, content,
        ctx=common.resolve_actor(db, data.peer.id)[1])
    operations.apply(db, preview["operation_id"], data.peer.id, preview["plan_digest"])
    lines = [inquiries.lines_for(db, inquiry.id)[0] for inquiry in sources]
    copies = [db.get(PresalesOffer, line.current_offer_id) for line in lines]
    assert len({original.id, *(offer.id for offer in copies)}) == 3
    assert all(offer.unit_price == Decimal("19") and offer.price_basis == "cost"
               and offer.is_tax_inclusive is None and offer.updated_by == data.peer.id for offer in copies)
    assert [offer.line_id for offer in copies] == [line.id for line in lines]
    pool.set_price(db, lines[0].id, data.sales.id, {"unit_price": 28}, expected_version=lines[0].version)
    assert [offer.unit_price for offer in (original, *copies)] == [Decimal("19"), Decimal("28"), Decimal("19")]


@pytest.mark.parametrize("invalid", ["other_pn", "stale", "draft"])
def test_reuse_rejects_wrong_pn_and_ineffective_offers(db, quote_data, invalid):
    data = quote_data
    if invalid == "draft":
        source = _submitted(db, data)
        line = inquiries.lines_for(db, source.id)[0]
        original = offers.save_draft(db, source.id, line.id, data.buyer.id, {"unit_price": 19},
            expected_version=source.version)
    else:
        original = _published(db, data)
        if invalid == "stale":
            offers.mark_stale(db, original, data.buyer.id, "来源价格失效")
    part = data.parts[1] if invalid == "other_pn" else data.parts[0]
    target = _submitted(db, data, lines=[{"part_id": part.id}])
    content = quote_workbook.export_quote_sheet(db, [target.id], data.buyer.id)
    with pytest.raises(ApplyFailed) as error:
        _apply(db, data, _edit(content, {2: {"处理方式": "沿用", "沿用报价编号": original.id}}))
    assert "沿用" in str(error.value.__cause__)
    assert inquiries.lines_for(db, target.id)[0].answer_state == "pending"
    assert db.scalar(select(PresalesOffer).where(PresalesOffer.inquiry_id == target.id)) is None


def test_missing_supplier_fails_without_persisting_draft(db, quote_data):
    data = quote_data
    inquiry = _submitted(db, data)
    content = quote_workbook.export_quote_sheet(db, [inquiry.id], data.buyer.id)
    with pytest.raises(ApplyFailed) as error:
        _apply(db, data, _edit(content, {2: {"处理方式": "报价", "单价": 22, "供应商": "不存在"}}))
    assert "精确匹配唯一" in str(error.value.__cause__)
    assert not list(db.scalars(select(PresalesOffer)))


def test_ambiguous_supplier_never_chooses_a_candidate():
    # 当前数据库 name_raw 有唯一约束，模拟兼容旧档案或未来适配器返回重复候选。
    db = SimpleNamespace(scalars=lambda statement: [SimpleNamespace(id=1), SimpleNamespace(id=2)])
    with pytest.raises(InvalidTransition, match="精确匹配唯一"):
        quote_workbook._supplier_id(db, "重复供应商")


def test_late_source_failure_rolls_back_earlier_distribution(db, quote_data, monkeypatch):
    data = quote_data
    first = _submitted(db, data)
    second = _submitted(db, data)
    content = quote_workbook.export_quote_sheet(db, [first.id, second.id], data.buyer.id)
    second_line = inquiries.lines_for(db, second.id)[0].id
    actual = pool.set_price
    def reject_second(db, line_id, *args, **kwargs):
        if line_id == second_line:
            raise ValueError("第二张来源单校验失败")
        return actual(db, line_id, *args, **kwargs)
    monkeypatch.setattr(pool, "set_price", reject_second)
    with pytest.raises(ApplyFailed):
        _apply(db, data, _edit(content, {2: {"处理方式": "报价", "单价": 22}}))
    assert not list(db.scalars(select(PresalesOffer)))


def test_reuse_offer_updated_after_preview_requires_fresh_export(db, quote_data):
    data = quote_data
    original = _published(db, data)
    inquiry = _submitted(db, data)
    content = _edit(quote_workbook.export_quote_sheet(db, [inquiry.id], data.buyer.id),
        {2: {"处理方式": "沿用", "沿用报价编号": original.id}})
    preview = operations.preview(db, quote_workbook.QUOTE_SCHEMA.name, content,
        ctx=common.resolve_actor(db, data.buyer.id)[1])
    offers.update_offer(db, original.id, data.buyer.id, {"unit_price": "100.00"},
        expected_version=original.version)
    with pytest.raises(Conflict):
        operations.apply(db, preview["operation_id"], data.buyer.id, preview["plan_digest"])
    assert not list(db.scalars(select(PresalesLineResponse)))


def test_reference_lock_is_nowait_and_survives_until_outer_commit(db, quote_data):
    data = quote_data
    original = _published(db, data)
    inquiry = _submitted(db, data)
    content = quote_workbook.export_quote_sheet(db, [inquiry.id], data.buyer.id)
    _, manifest = read_metadata(load_workbook(BytesIO(content)))
    entity = manifest["sheets"]["lines"]["rows"][0]["entity_id"]
    original_id = original.id
    ctx = common.resolve_actor(db, data.buyer.id)[1]
    db.commit()
    with SessionLocal() as other:
        other.scalar(select(PresalesOffer).where(PresalesOffer.id == original_id).with_for_update())
        with pytest.raises(Conflict, match="有效报价正在更新"):
            quote_workbook.load_current(db, "lines", [entity], ctx)
        assert db.scalar(text("SELECT 1")) == 1
        other.rollback()
        assert entity in quote_workbook.load_current(db, "lines", [entity], ctx)
        with pytest.raises(DBAPIError):
            other.scalar(select(PresalesOffer).where(PresalesOffer.id == original_id).with_for_update(nowait=True))
        other.rollback()
    db.commit()


def test_identity_columns_survive_selection_and_rows_cannot_be_added_or_deleted(db, quote_data):
    data = quote_data
    inquiry = _submitted(db, data)
    content = quote_workbook.export_quote_sheet(db, [inquiry.id], data.buyer.id,
        visible_columns={"lines": ["mode", "reason"]})
    sheet = load_workbook(BytesIO(content))["采购报价表"]
    assert [cell.value for cell in sheet[1]] == ["_row_token", "来源单号", "PN", "处理方式", "不报原因"]
    book = load_workbook(BytesIO(content))
    book["采购报价表"].delete_rows(2)
    output = BytesIO()
    book.save(output)
    preview = operations.preview(db, quote_workbook.QUOTE_SCHEMA.name, output.getvalue(),
        ctx=common.resolve_actor(db, data.buyer.id)[1])
    assert preview["plan"]["errors"]
    added = _edit(content, {3: {"处理方式": "不报", "不报原因": "无货"}})
    preview = operations.preview(db, quote_workbook.QUOTE_SCHEMA.name, added,
        ctx=common.resolve_actor(db, data.buyer.id)[1])
    assert preview["plan"]["errors"]
    corrected = operations.preview(db, quote_workbook.QUOTE_SCHEMA.name, content,
        ctx=common.resolve_actor(db, data.buyer.id)[1])
    assert corrected["plan"]["counts"]["unchanged"] == 1


def test_export_scope_only_tracks_selected_visible_fields_and_contains_no_values(db, quote_data):
    data = quote_data
    inquiry = _submitted(db, data)
    inquiry.notes = "受众控制的客户要求"
    db.flush()
    content = quote_workbook.export_quote_sheet(db, [inquiry.id], data.buyer.id,
        visible_columns={"lines": ["mode", "reason"]})
    scope = db.scalar(select(PresalesWorkbookExport)).scope
    assert scope["rows"][0]["notes"] == []
    assert scope["rows"][0]["active_offer_ids"] == []
    assert "受众控制的客户要求" not in str(scope)
    inquiry.notes_audience_rule_id = common.rule_id(db, {"mode": "private"},
        common.resolve_actor(db, data.sales.id)[1])
    db.flush()
    preview = operations.preview(db, quote_workbook.QUOTE_SCHEMA.name, content,
        ctx=common.resolve_actor(db, data.buyer.id)[1])
    assert preview["plan"]["counts"]["unchanged"] == 1


def test_other_rows_in_another_export_do_not_expand_authorization_scope(db, quote_data):
    data = quote_data
    data.peer.perm_overrides = {"action_presales_supervise": True}
    db.flush()
    first = inquiries.create_draft(db, data.sales.id, assignee_ids=[data.buyer.id, data.peer.id],
        lines=[{"part_id": data.parts[0].id}])
    inquiries.submit(db, first.id, data.sales.id, first.version)
    second = _submitted(db, data, assigned=data.peer,
        lines=[{"part_id": data.parts[1].id, "notes": "无关来源的私密要求",
                "notes_audience": {"mode": "private"}}])
    quote_workbook.export_quote_sheet(db, [first.id, second.id], data.peer.id)
    content = quote_workbook.export_quote_sheet(db, [first.id], data.buyer.id,
        visible_columns={"lines": ["mode", "reason"]})
    preview = operations.preview(db, quote_workbook.QUOTE_SCHEMA.name, content,
        ctx=common.resolve_actor(db, data.buyer.id)[1])
    assert preview["plan"]["counts"]["unchanged"] == 1


def test_forwarded_buyer_draft_cannot_leak_private_purchase_notes(db, quote_data):
    data = quote_data
    inquiry = inquiries.create_draft(db, data.sales.id, assignee_ids=[data.buyer.id, data.peer.id],
        lines=[{"part_id": data.parts[0].id}])
    inquiries.submit(db, inquiry.id, data.sales.id, inquiry.version)
    line = inquiries.lines_for(db, inquiry.id)[0]
    offers.save_draft(db, inquiry.id, line.id, data.peer.id,
        {"unit_price": "12.00", "notes": "另一采购的私密备注", "notes_audience": {"mode": "private"}},
        expected_version=inquiry.version)
    content = quote_workbook.export_quote_sheet(db, [inquiry.id], data.peer.id)
    with pytest.raises(Forbidden):
        operations.preview(db, quote_workbook.QUOTE_SCHEMA.name, content,
            ctx=common.resolve_actor(db, data.buyer.id)[1])


def _remove_quote_row(content):
    book = load_workbook(BytesIO(content))
    book["采购报价表"].delete_rows(2)
    output = BytesIO()
    book.save(output)
    return output.getvalue()


@pytest.mark.parametrize(("mode", "field"), [("skip", "reason"), ("reuse", "ref_offer_id")])
def test_forwarded_draft_response_stays_private_after_delete_and_permission_revocation(db, quote_data, mode, field):
    data = quote_data
    inquiry = _submitted(db, data)
    line = inquiries.lines_for(db, inquiry.id)[0]
    value = "采购暂存的原因" if mode == "skip" else _published(db, data).id
    draft = PresalesLineResponse(line_id=line.id, line_version=line.version, created_by=data.buyer.id,
        mode=mode, is_draft=True, **{field: value})
    db.add(draft)
    db.flush()
    schema = quote_workbook.QUOTE_SCHEMA.name
    buyer = common.resolve_actor(db, data.buyer.id)[1]
    peer = common.resolve_actor(db, data.peer.id)[1]
    content = quote_workbook.export_quote_sheet(db, [inquiry.id], buyer, visible_columns={"lines": [field]})
    removed = _remove_quote_row(content)
    assert operations.preview(db, schema, removed, ctx=buyer)["plan"]["rows"][0]["values"][field] == value
    with pytest.raises(Forbidden):
        operations.preview(db, schema, removed, ctx=peer)

    data.peer.perm_overrides = {"action_presales_supervise": True}
    db.flush()
    allowed = operations.preview(db, schema, removed, ctx=peer)
    assert allowed["plan"]["rows"][0]["values"][field] == value
    db.delete(draft)
    data.peer.perm_overrides = {}
    db.flush()
    with pytest.raises(Forbidden):
        operations.preview(db, schema, removed, ctx=peer)
    with pytest.raises(Forbidden):
        operations.apply(db, allowed["operation_id"], data.peer.id, allowed["plan_digest"])
    with pytest.raises(Forbidden):
        operations.preview(db, schema, content, ctx=peer)


def test_only_exported_private_fields_restrict_the_file_for_a_shared_line(db, quote_data):
    data = quote_data
    inquiry = _submitted(db, data)
    line = inquiries.lines_for(db, inquiry.id)[0]
    db.add(PresalesLineResponse(line_id=line.id, line_version=line.version, created_by=data.buyer.id,
        mode="skip", reason="私有原因", is_draft=True))
    db.flush()
    def export(keys):
        return quote_workbook.export_quote_sheet(db, [inquiry.id], data.buyer.id, visible_columns={"lines": keys})
    private = export(["mode"])
    public = export(["qty"])
    schema = quote_workbook.QUOTE_SCHEMA.name
    peer = common.resolve_actor(db, data.peer.id)[1]
    assert operations.preview(db, schema, _remove_quote_row(public), ctx=peer)["plan"]["rows"][0]["values"]["qty"] == "2.000"
    pool.skip(db, line.id, data.peer.id, "公开的不报原因", expected_version=line.version)
    published = export(["mode", "reason"])
    row = operations.preview(db, schema, _remove_quote_row(published), ctx=peer)["plan"]["rows"][0]
    assert (row["values"]["mode"], row["values"]["reason"]) == ("skip", "公开的不报原因")
    with pytest.raises(Forbidden):
        operations.preview(db, schema, _remove_quote_row(private), ctx=peer)


def test_legacy_export_scope_requires_owner_or_supervisor_on_applied_replay(db, quote_data):
    data = quote_data
    inquiry = _submitted(db, data)
    schema = quote_workbook.QUOTE_SCHEMA.name
    content = quote_workbook.export_quote_sheet(db, [inquiry.id], data.buyer.id)
    stored = db.scalar(select(PresalesWorkbookExport))
    stored.scope = {"rows": stored.scope["rows"]}  # 升级前只记录来源范围，不记录是否带出未发布答复。
    db.flush()
    buyer = common.resolve_actor(db, data.buyer.id)[1]
    peer = common.resolve_actor(db, data.peer.id)[1]
    assert operations.preview(db, schema, content, ctx=buyer)["plan"]["counts"]["unchanged"] == 1
    changed = _edit(content, {2: {"处理方式": "报价", "单价": 14}})
    with pytest.raises(Forbidden):
        operations.preview(db, schema, changed, ctx=peer)
    data.peer.perm_overrides = {"action_presales_supervise": True}
    db.flush()
    invalid = _edit(content, {2: {"处理方式": "不报"}})
    rejected = operations.preview(db, schema, invalid, ctx=peer)
    with pytest.raises(ApplyFailed) as failed:
        operations.apply(db, rejected["operation_id"], data.peer.id, rejected["plan_digest"])
    assert failed.value.result["status"] == "failed"
    preview = operations.preview(db, schema, changed, ctx=peer)
    first = operations.apply(db, preview["operation_id"], data.peer.id, preview["plan_digest"])
    assert operations.preview(db, schema, changed, ctx=peer)["response"] == first["response"]
    data.peer.perm_overrides = {}
    db.flush()
    with pytest.raises(Forbidden):
        operations.preview(db, schema, changed, ctx=peer)
    with pytest.raises(Forbidden):
        operations.apply(db, preview["operation_id"], data.peer.id, preview["plan_digest"])
    with pytest.raises(Forbidden):
        operations.preview(db, schema, invalid, ctx=peer)
    with pytest.raises(Forbidden):
        operations.apply(db, rejected["operation_id"], data.peer.id, rejected["plan_digest"])


def test_optional_source_can_be_cleared_without_clearing_supplier(db, quote_data):
    data = quote_data
    inquiry = _submitted(db, data)
    line = inquiries.lines_for(db, inquiry.id)[0]
    offers.save_draft(db, inquiry.id, line.id, data.buyer.id,
        {"unit_price": "12.00", "source_key": "仓库A", "supplier_id": data.supplier.id},
        expected_version=inquiry.version)
    content = quote_workbook.export_quote_sheet(db, [inquiry.id], data.buyer.id)
    book = load_workbook(BytesIO(content))
    sheet = book["采购报价表"]
    columns = {cell.value: cell.column for cell in sheet[1]}
    sheet.cell(2, columns["货源"]).value = None
    output = BytesIO()
    book.save(output)
    _apply(db, data, output.getvalue())
    db.refresh(line)
    current = db.get(PresalesOffer, line.current_offer_id)
    assert current.source_key == "" and current.supplier_id == data.supplier.id and current.unit_price == Decimal("12.00")


@pytest.mark.skipif(os.getenv("RUN_SOFFICE_ROUNDTRIP") != "1", reason="显式开启 LibreOffice 往返")
def test_quote_libreoffice_roundtrip(db, quote_data, tmp_path):
    binary = shutil.which("soffice")
    assert binary, "RUN_SOFFICE_ROUNDTRIP=1 需要 soffice"
    inquiry = _submitted(db, quote_data)
    source = tmp_path / "quote.xlsx"
    source.write_bytes(_edit(quote_workbook.export_quote_sheet(db, [inquiry.id], quote_data.buyer.id),
        {2: {"处理方式": "报价", "单价": "12.50", "含税": "是"}}))
    destination = tmp_path / "converted"
    destination.mkdir()
    result = subprocess.run([binary, "-env:UserInstallation=file:///tmp/t16-lo-profile", "--headless",
        "--convert-to", "xlsx", "--outdir", str(destination), str(source)],
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    output = destination / source.name
    assert output.exists(), result.stdout + result.stderr
    result = _apply(db, quote_data, output.read_bytes())
    assert result["response"]["saved"][0]["mode"] == "quote"

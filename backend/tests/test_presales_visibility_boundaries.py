"""D-0053 的关联报价、SQL 分页和已签名工作簿现时授权。"""

from io import BytesIO

from openpyxl import load_workbook
import pytest
from sqlalchemy import select

from app.models.dimensions import DimPart
from app.models.presales import (
    PresalesInquiry, PresalesLineResponse, PresalesOffer, PresalesOfferLineLink, PresalesWorkbookExport,
)
from app.services.presales import (
    binding, common, inquiries, inquiry_workbook, library, offers, part_history, pool,
    quote_workbook, visibility,
)
from app.services.presales.workbook_registration import register_workbooks
from app.services.workbook import operations
from app.services.workbook.registry import Forbidden
from app.services.workflow.registry import Conflict, Forbidden as WorkflowForbidden
from tests.test_presales_api_visibility import _ctx, _quoted, users
from tests.test_presales_quote_workbook import _edit, _submitted, quote_data


def _share(db, inquiry, usernames):
    inquiry.offer_audience_rule_id = common.rule_id(db,
        {"mode": "include", "user_ids": usernames} if usernames else {"mode": "private"},
        common.resolve_actor(db, inquiry.created_by)[1])
    db.flush()


def _source_quote(db, data):
    inquiry = _submitted(db, data, owner=data.other)
    _share(db, inquiry, [data.sales.username])
    line = inquiries.lines_for(db, inquiry.id)[0]
    result = pool.set_price(db, line.id, data.buyer.id, {"unit_price": "19.00"},
                            expected_version=line.version)
    return db.get(PresalesOffer, result["price"]["offer_id"])


def test_part_history_filters_hidden_rows_before_count_and_pagination(db, users, monkeypatch):
    shared = [_quoted(db, users, whole={"mode": "include", "user_ids": [users.other.username]})[0]
              for _ in range(2)]
    for _ in range(4):
        _quoted(db, users, whole={"mode": "private"})
    inquiries.create_draft(db, users.seller.id,
        offer_audience={"mode": "include", "user_ids": [users.other.username]},
        lines=[{"part_id": users.part.id}])
    calls = []
    original = part_history.visibility.inquiry_view

    def view(db, inquiry, ctx):
        calls.append(inquiry.id)
        return original(db, inquiry, ctx)

    monkeypatch.setattr(part_history.visibility, "inquiry_view", view)
    result = part_history.list_lines(db, users.part.id, _ctx(db, users.other), page=2, page_size=1)
    assert result["total"] == 2
    assert [row["inquiry_id"] for row in result["items"]] == [shared[0].id]
    assert calls == [shared[0].id]


def test_library_rechecks_source_grants_for_latest_more_search_and_stale_context(db, users):
    rule = {"mode": "include", "user_ids": [users.other.username]}
    older, first = _quoted(db, users, whole=rule)
    newer, second = _quoted(db, users, whole=rule)
    ctx = _ctx(db, users.other)
    result = library.batch(db, [users.part.id], ctx)[users.part.id]
    assert result[0]["id"] == second.id and result[0]["more"][0]["id"] == first.id
    _share(db, newer, [])
    result = library.batch(db, [users.part.id], ctx)[users.part.id]
    assert [row["id"] for row in result] == [first.id] and result[0]["more"] == []
    _share(db, older, [])
    assert library.batch(db, [users.part.id], ctx)[users.part.id] == []
    assert library.boost_part_ids(db, users.part.pn_std, ctx) == []
    rows = library.search(db, users.part.pn_std, ctx)
    assert rows and all(row["offer_count"] == 0 and row["offers"] == [] for row in rows)
    buyer = _ctx(db, users.buyer)
    assert library.batch(db, [users.part.id], buyer)[users.part.id]
    users.buyer.perm_overrides = {"action_presales_quote": False}
    db.flush()
    assert library.batch(db, [users.part.id], buyer)[users.part.id] == []


@pytest.mark.parametrize("phase", ["unopened", "previewed", "applied"])
def test_mixed_source_workbook_revocation_blocks_preview_apply_and_receipt(db, quote_data, phase):
    data = quote_data
    own = _submitted(db, data, owner=data.other)
    shared = _submitted(db, data)
    _share(db, shared, [data.other.username])
    content = _edit(quote_workbook.export_quote_sheet(db, [own.id, shared.id], data.buyer.id),
                    {2: {"处理方式": "报价", "单价": 17}})
    ctx = common.resolve_actor(db, data.other.id)[1]
    schema = quote_workbook.QUOTE_SCHEMA.name
    preview = None
    if phase != "unopened":
        preview = operations.preview(db, schema, content, ctx=ctx)
        assert not preview["plan"]["errors"]
        if phase == "applied":
            operations.apply(db, preview["operation_id"], data.other.id, preview["plan_digest"])
    _share(db, shared, [])
    lines = [inquiries.lines_for(db, inquiry.id)[0] for inquiry in (own, shared)]
    versions = [line.version for line in lines]
    with pytest.raises(Forbidden):
        operations.preview(db, schema, content, ctx=ctx)
    if preview is not None:
        with pytest.raises(Forbidden):
            operations.apply(db, preview["operation_id"], data.other.id, preview["plan_digest"])
    assert [line.version for line in lines] == versions


def test_exported_price_retains_source_acl_when_reference_column_was_omitted(db, quote_data):
    data = quote_data
    source = _source_quote(db, data)
    inquiry = _submitted(db, data)
    line = inquiries.lines_for(db, inquiry.id)[0]
    # 兼容旧共享池迁移产生的跨单 current_offer_id。
    line.current_offer_id, line.answer_state = source.id, "quoted"
    db.flush()
    content = quote_workbook.export_quote_sheet(db, [inquiry.id], data.sales.id,
        visible_columns={"lines": ["unit_price"]})
    exported = db.scalar(select(PresalesWorkbookExport))
    assert exported.scope["rows"][0]["active_offer_ids"] == []
    assert exported.scope["rows"][0]["state_offer_ids"] == [source.id]
    ctx = common.resolve_actor(db, data.sales.id)[1]
    preview = operations.preview(db, quote_workbook.QUOTE_SCHEMA.name, content, ctx=ctx)
    _share(db, db.get(PresalesInquiry, source.inquiry_id), [])
    with pytest.raises(Forbidden):
        operations.preview(db, quote_workbook.QUOTE_SCHEMA.name, content, ctx=ctx)
    with pytest.raises(Forbidden):
        operations.apply(db, preview["operation_id"], data.sales.id, preview["plan_digest"])
    fresh = quote_workbook.export_quote_sheet(db, [inquiry.id], data.sales.id,
        visible_columns={"lines": ["unit_price"]})
    sheet = load_workbook(BytesIO(fresh))["采购报价表"]
    price_column = next(cell.column for cell in sheet[1] if cell.value == "单价")
    assert sheet.cell(2, price_column).value is None


def test_saved_workbook_plan_rechecks_manually_selected_reuse_source(db, quote_data):
    data = quote_data
    source = _source_quote(db, data)
    inquiry = _submitted(db, data)
    content = _edit(quote_workbook.export_quote_sheet(db, [inquiry.id], data.sales.id,
        visible_columns={"lines": ["mode", "ref_offer_id"]}),
        {2: {"处理方式": "沿用", "沿用报价编号": source.id}})
    ctx = common.resolve_actor(db, data.sales.id)[1]
    preview = operations.preview(db, quote_workbook.QUOTE_SCHEMA.name, content, ctx=ctx)
    assert not preview["plan"]["errors"]
    _share(db, db.get(PresalesInquiry, source.inquiry_id), [])
    with pytest.raises(Forbidden):
        operations.preview(db, quote_workbook.QUOTE_SCHEMA.name, content, ctx=ctx)
    with pytest.raises(Forbidden):
        operations.apply(db, preview["operation_id"], data.sales.id, preview["plan_digest"])


@pytest.mark.parametrize("action", ["update", "withdraw", "save_draft", "decision", "reply", "direct"])
def test_original_quote_author_cannot_write_after_losing_inquiry_access(db, users, action):
    inquiry, offer = _quoted(db, users, whole={"mode": "private"})
    line = inquiries.lines_for(db, inquiry.id)[0]
    previous = (offer.version, offer.revision_no, offer.unit_price, offer.status, line.version)
    users.buyer.perm_overrides = {"action_presales_quote": False}
    db.flush()
    calls = {
        "update": lambda: offers.update_offer(db, offer.id, users.buyer.id,
            {"unit_price": "1"}, expected_version=999),
        "withdraw": lambda: offers.withdraw(db, offer.id, users.buyer.id, "不可再改",
            expected_version=999),
        "save_draft": lambda: offers.save_draft(db, inquiry.id, line.id, users.buyer.id,
            {"unit_price": "1"}, expected_version=999),
        "decision": lambda: offers.save_line_decision(db, inquiry.id, line.id, users.buyer.id,
            mode="skip", reason="不可再改", expected_version=999),
        "reply": lambda: offers.reply(db, inquiry.id, users.buyer.id, expected_version=999),
        "direct": lambda: offers.direct_offer(db, users.buyer.id, users.part.id,
            {"unit_price": "1"}, existing_offer_id=offer.id, expected_version=999),
    }
    expected = Conflict if action == "direct" else WorkflowForbidden
    with pytest.raises(expected):
        calls[action]()
    db.flush()
    assert (offer.version, offer.revision_no, offer.unit_price, offer.status, line.version) == previous


def test_old_quote_cannot_be_moved_into_new_proactive_inquiry_after_source_revocation(db, users):
    _, original = _quoted(db, users, whole={"mode": "private"})
    users.buyer.perm_overrides = {"action_presales_quote": False}
    db.flush()
    inquiry = inquiries.create_draft(db, users.buyer.id, kind="proactive_offer",
        lines=[{"part_id": users.part.id}])
    line = inquiries.lines_for(db, inquiry.id)[0]
    draft = offers.save_draft(db, inquiry.id, line.id, users.buyer.id,
        {"unit_price": "1"}, expected_version=inquiry.version)
    source_id = original.inquiry_id
    with pytest.raises(Conflict):
        offers.publish_proactive(db, inquiry.id, users.buyer.id, expected_version=inquiry.version,
            on_existing="update", existing_offer_id=original.id)
    assert original.inquiry_id == source_id and original.unit_price != draft.unit_price
    assert inquiry.status == "draft" and draft.status == "draft"


def test_public_proactive_inquiry_does_not_expose_private_sales_reference(db, users):
    _, source = _quoted(db, users, whole={"mode": "private"})
    wrapper = inquiries.create_draft(db, users.buyer.id, kind="proactive_offer",
        lines=[{"part_id": users.part.id}])
    line = inquiries.lines_for(db, wrapper.id)[0]
    own = offers.save_draft(db, wrapper.id, line.id, users.buyer.id,
        {"unit_price": "7"}, expected_version=wrapper.version)
    offers.publish_proactive(db, wrapper.id, users.buyer.id, expected_version=wrapper.version,
                              on_existing="new")
    line.ref_offer_id, line.ref_offer_revision_no = source.id, source.revision_no
    line.current_offer_id, line.answer_state = source.id, "quoted"
    db.add(PresalesOfferLineLink(line_id=line.id, offer_id=source.id,
                               offer_revision_no=source.revision_no))
    db.flush()
    result = visibility.inquiry_view(db, wrapper, _ctx(db, users.other))
    assert [offer["id"] for offer in result["offers"]] == [own.id]
    row = result["lines"][0]
    assert row.get("ref_offer_id") is None and row.get("ref_offer_revision_no") is None
    assert row["reference_restricted"] is True
    assert row.get("current_offer_id") is None and row.get("ref_offer") is None
    assert row["linked_offer_ids"] == [] and row["linked_offers"] == []


def test_revoked_sales_source_hides_won_and_response_ids_without_rewriting_references(db, users):
    source_inquiry, source = _quoted(db, users,
        whole={"mode": "include", "user_ids": [users.other.username]})
    inquiry = inquiries.create_draft(db, users.other.id, lines=[{
        "part_id": users.part.id, "ref_offer_id": source.id}])
    inquiries.submit(db, inquiry.id, users.other.id, inquiry.version)
    line = inquiries.lines_for(db, inquiry.id)[0]
    inquiries.close(db, inquiry.id, users.other.id, inquiry.version, outcome="won",
        won_offer_id=source.id, expected_offer_revision_no=source.revision_no)
    db.add(PresalesLineResponse(line_id=line.id, line_version=line.version,
        created_by=users.other.id, mode="reuse", ref_offer_id=source.id,
        ref_offer_revision_no=source.revision_no, is_draft=False))
    db.flush()
    _share(db, source_inquiry, [])
    result = visibility.inquiry_view(db, inquiry, _ctx(db, users.other))
    assert result["won_offer"] is None and result["won_offer_id"] is None
    assert result["won_offer_revision_no"] is None
    assert result["lines"][0]["ref_offer_id"] is None
    assert result["lines"][0]["reference_restricted"] is True
    response = result["line_responses"][0]
    assert response["ref_offer_id"] is None and response["ref_offer_revision_no"] is None
    assert "offer" not in response and "current_offer" not in response
    assert inquiry.won_offer_id == source.id and line.ref_offer_id == source.id
    for summary in (False, True):
        compact = visibility.inquiry_view(db, inquiry, _ctx(db, users.other), detail=False, summary=summary)
        assert compact["won_offer_id"] is None and compact["won_offer_revision_no"] is None


@pytest.mark.parametrize("change", [{"qty": 3}, {"ref_offer_id": None}])
def test_notes_only_edit_keeps_restricted_reference_when_payload_omits_it(db, users, change):
    source_inquiry, source = _quoted(db, users,
        whole={"mode": "include", "user_ids": [users.other.username]})
    inquiry = inquiries.create_draft(db, users.other.id, lines=[{
        "part_id": users.part.id, "ref_offer_id": source.id}])
    inquiries.submit(db, inquiry.id, users.other.id, inquiry.version)
    inquiries.withdraw(db, inquiry.id, users.other.id, inquiry.version)
    line = inquiries.lines_for(db, inquiry.id)[0]
    original = (line.ref_offer_id, line.ref_offer_revision_no)
    _share(db, source_inquiry, [])
    view = visibility.inquiry_view(db, inquiry, _ctx(db, users.other))
    assert view["lines"][0]["reference_restricted"] is True
    inquiries.update_draft(db, inquiry.id, users.other.id, inquiry.version,
        lines=[{"line_key": line.line_key, "notes": "仅补充客户备注"}],
        expected_line_versions={line.line_key: line.version})
    assert line.notes == "仅补充客户备注"
    assert (line.ref_offer_id, line.ref_offer_revision_no) == original
    inquiries.update_draft(db, inquiry.id, users.other.id, inquiry.version,
        lines=[{"line_key": line.line_key, **change}],
        expected_line_versions={line.line_key: line.version}, confirm_reset=True)
    assert line.ref_offer_id is None and line.ref_offer_revision_no is None
    assert line.current_offer_id is None and line.answer_state == "pending"
    assert source.status == "published"
    if "ref_offer_id" in change:
        assert line.qty == 1
        inquiries.resubmit(db, inquiry.id, users.other.id, inquiry.version)
        assert inquiry.status == "submitted"


@pytest.mark.parametrize("action", ["check", "bind", "create"])
def test_binding_denies_hidden_inquiry_before_versions_or_master_mutations(db, users, action):
    inquiry = inquiries.create_draft(db, users.seller.id, lines=[{"pn_raw": "PRIVATE-NEW-PN"}])
    inquiries.submit(db, inquiry.id, users.seller.id, inquiry.version)
    line = inquiries.lines_for(db, inquiry.id)[0]
    users.buyer.perm_overrides = {"action_presales_quote": False,
        "action_presales_bind_part": True, "page_master_data": True}
    db.flush()
    calls = {
        "check": lambda: binding.check(db, line.id, users.buyer.id),
        "bind": lambda: binding.bind(db, line.id, users.buyer.id, 999, part_id=users.part.id),
        "create": lambda: binding.create_and_bind(db, line.id, users.buyer.id, 999),
    }
    with pytest.raises(WorkflowForbidden):
        calls[action]()
    assert line.part_id is None
    assert db.scalar(select(DimPart.id).where(DimPart.pn_std == "PRIVATE-NEW-PN")) is None


def test_supervisor_inquiry_workbook_requires_current_shared_access_and_denies_drafts(db, users):
    register_workbooks()
    inquiry, _ = _quoted(db, users, whole={"mode": "private"})
    inquiries.withdraw(db, inquiry.id, users.seller.id, inquiry.version)
    supervisor = _ctx(db, users.supervisor)
    with pytest.raises(WorkflowForbidden):
        inquiry_workbook.export_inquiry(db, inquiry.id, supervisor)
    _share(db, inquiry, [users.supervisor.username])
    content = inquiry_workbook.export_inquiry(db, inquiry.id, supervisor)
    preview = operations.preview(db, inquiry_workbook.INQUIRY_SCHEMA.name, content, ctx=supervisor,
        subject_type=inquiries.SUBJECT_TYPE, subject_id=inquiry.id)
    _share(db, inquiry, [])
    with pytest.raises(Forbidden):
        operations.apply(db, preview["operation_id"], supervisor, preview["plan_digest"])
    draft = inquiries.create_draft(db, users.seller.id, lines=[{"part_id": users.part.id}],
        offer_audience={"mode": "include", "user_ids": [users.supervisor.username]})
    with pytest.raises(WorkflowForbidden):
        inquiry_workbook.export_inquiry(db, draft.id, supervisor)


@pytest.mark.parametrize("role", ["sales", "buyer", "admin"])
@pytest.mark.parametrize("phase", ["previewed", "applied"])
def test_old_quote_workbook_without_price_source_proof_requires_redownload(db, quote_data, role, phase):
    data = quote_data
    actor = data.peer if role == "admin" else getattr(data, role)
    if role == "admin":
        actor.role = "admin"
        db.flush()
    inquiry = _submitted(db, data)
    content = _edit(quote_workbook.export_quote_sheet(db, [inquiry.id], actor.id),
        {2: {"处理方式": "报价", "单价": 19}})
    ctx = common.resolve_actor(db, actor.id)[1]
    schema = quote_workbook.QUOTE_SCHEMA.name
    preview = operations.preview(db, schema, content, ctx=ctx)
    if phase == "applied":
        operations.apply(db, preview["operation_id"], actor.id, preview["plan_digest"])
    exported = db.scalar(select(PresalesWorkbookExport))
    exported.scope = {**exported.scope, "rows": [
        {key: value for key, value in row.items() if key != "state_offer_ids"}
        for row in exported.scope["rows"]]}
    db.flush()
    with pytest.raises(Forbidden, match="报价表权限范围已更新，请重新下载"):
        operations.preview(db, schema, content, ctx=ctx)
    with pytest.raises(Forbidden, match="报价表权限范围已更新，请重新下载"):
        operations.apply(db, preview["operation_id"], actor.id, preview["plan_digest"])

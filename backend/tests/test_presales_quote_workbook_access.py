"""报价预览实际读取的价格来源必须随缓存计划保存并在回放时重新授权。"""

from copy import deepcopy
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.presales import PresalesOffer, PresalesWorkbookExport
from app.models.workbook import WorkbookOperation
from app.services.presales import common, inquiries, pool, quote_workbook
from app.services.workbook import operations
from app.services.workbook.registry import Forbidden
from tests.test_presales_quote_workbook import _edit, _submitted, quote_data


def _source(db, data, price):
    inquiry = _submitted(db, data, owner=data.other)
    inquiries.update_visibility(db, inquiry.id, data.other.id, inquiry.version, [data.sales.username])
    line = inquiries.lines_for(db, inquiry.id)[0]
    priced = pool.set_price(db, line.id, data.buyer.id, {"unit_price": price}, expected_version=line.version)
    return inquiry, db.get(PresalesOffer, priced["price"]["offer_id"])


def _link_current(db, line, offer):
    # 兼容存量跨单 current_offer_id；计算结果刷新不修改业务输入的行版本。
    line.current_offer_id, line.answer_state = offer.id, "quoted"
    db.flush()


@pytest.mark.parametrize("phase", ["conflict", "applied"])
def test_preview_dynamic_price_source_is_rechecked_after_current_source_changes(db, quote_data, phase):
    data = quote_data
    inquiry = _submitted(db, data)
    line = inquiries.lines_for(db, inquiry.id)[0]
    if phase == "applied":
        source, offer = _source(db, data, "47")
    content = _edit(quote_workbook.export_quote_sheet(db, [inquiry.id], data.sales.id,
        visible_columns={"lines": ["unit_price"]}), {2: {"单价": 51}})
    exported = db.scalar(select(PresalesWorkbookExport))
    original_scope = deepcopy(exported.scope)
    assert original_scope["rows"][0]["state_offer_ids"] == []
    assert original_scope["rows"][0]["active_offer_ids"] == []
    if phase == "conflict":
        source, offer = _source(db, data, "47")
    _link_current(db, line, offer)
    ctx = common.resolve_actor(db, data.sales.id)[1]
    schema = quote_workbook.QUOTE_SCHEMA.name
    preview = operations.preview(db, schema, content, ctx=ctx)
    row = preview["plan"]["rows"][0]
    assert row["status"] == ("conflict" if phase == "conflict" else "modified")
    recorded_access = deepcopy(row["access"])
    assert offer.id in recorded_access["state_offer_ids"]
    assert source.id in recorded_access["source_inquiry_ids"]
    price_change = next(change for change in row["changes"] if change["key"] == "unit_price")
    assert Decimal(price_change["before"]) == Decimal("47")
    if phase == "applied":
        operations.apply(db, preview["operation_id"], data.sales.id, preview["plan_digest"])
    _, replacement = _source(db, data, "83")
    _link_current(db, line, replacement)
    assert replacement.id not in recorded_access["state_offer_ids"]
    inquiries.update_visibility(db, source.id, data.other.id, source.version, [])
    with pytest.raises(Forbidden):
        operations.preview(db, schema, content, ctx=ctx)
    with pytest.raises(Forbidden):
        operations.apply(db, preview["operation_id"], data.sales.id, preview["plan_digest"])
    db.refresh(exported)
    assert exported.scope == original_scope
    assert db.get(WorkbookOperation, preview["operation_id"]).plan["rows"][0]["access"] == recorded_access


@pytest.mark.parametrize("phase", ["previewed", "applied"])
def test_legacy_plan_without_server_price_source_metadata_fails_closed(db, quote_data, phase):
    data = quote_data
    inquiry = _submitted(db, data)
    content = _edit(quote_workbook.export_quote_sheet(db, [inquiry.id], data.sales.id),
        {2: {"处理方式": "报价", "单价": 17}})
    ctx = common.resolve_actor(db, data.sales.id)[1]
    schema = quote_workbook.QUOTE_SCHEMA.name
    preview = operations.preview(db, schema, content, ctx=ctx)
    if phase == "applied":
        operations.apply(db, preview["operation_id"], data.sales.id, preview["plan_digest"])
    stored = db.get(WorkbookOperation, preview["operation_id"])
    legacy = deepcopy(stored.plan)
    for row in legacy["rows"]:
        row.pop("access", None)
    stored.plan = legacy
    db.flush()
    with pytest.raises(Forbidden):
        operations.preview(db, schema, content, ctx=ctx)
    with pytest.raises(Forbidden):
        operations.apply(db, preview["operation_id"], data.sales.id, preview["plan_digest"])

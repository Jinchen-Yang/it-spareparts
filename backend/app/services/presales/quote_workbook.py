"""采购按 PN 合并往返；签名行身份保存来源，所有写入复用报价服务。"""

import hashlib
import json
import re
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.models.dimensions import DimPart, DimSupplier
from app.models.fields_audience import FieldValue
from app.models.presales import (
    PresalesInquiry, PresalesInquiryLine, PresalesLineResponse, PresalesOffer, PresalesWorkbookExport,
)
from app.models.workbook import WorkbookOperation
from app.models.workflow import WorkItem
from app.services import field_pool, price_reference
from app.services.presales import common, library, offers, visibility
from app.services.workbook.export import export_workbook
from app.services.workbook.registry import Conflict as WorkbookConflict
from app.services.workbook.registry import Forbidden as WorkbookForbidden
from app.services.workbook.schema import ColumnSpec, SheetSpec, WorkbookError, WorkbookSchema, column_allowed
from app.services.workbook.validation import validate_by_applying
from app.services.workflow import work_items
from app.services.workflow.registry import Forbidden, InvalidTransition, WorkflowError

QUOTE_SCHEMA = WorkbookSchema("presales_quote_sheet", 1, [
    SheetSpec("lines", "采购报价表", [
        ColumnSpec("inquiry_nos", "来源单号", "code", "locked", "逐张列出来源单；不可修改", width=28),
        ColumnSpec("pn", "PN", "code", "locked", "绑定型号或待建档 PN 原文；不可修改"),
        ColumnSpec("description", "描述", "text", "locked", "来源描述合并显示"),
        ColumnSpec("qty", "合计数量", "decimal", "locked", "按来源行合计", decimals=3),
        ColumnSpec("require_exact_pn", "要求 PN 一致", "bool", "locked", "一致要求不同的来源会拆行"),
        ColumnSpec("return_defective", "坏件是否返回", "bool", "locked", "坏件返回条件不同的来源会拆行；不可修改"),
        ColumnSpec("customer_requirements", "客户要求", "text", "locked", "仅包含当前账号可见的备注和期望交期", width=35),
        ColumnSpec("purchase_reference", "参考采购均价", "text", "reference", "近 30 天；参考价不等于定价", field_group="data_purchase_cost"),
        ColumnSpec("sale_reference", "参考销售价", "text", "reference", "近 30 天；参考价不等于定价"),
        ColumnSpec("active_offers", "已有有效报价", "text", "reference", "仅列当前采购与所有来源单所有人均可见的有效报价", width=45),
        ColumnSpec("mode", "处理方式", "enum", "optional", "选择报价、沿用或不报；应用只保存草稿", choices={"quote": "报价", "reuse": "沿用", "skip": "不报"}),
        ColumnSpec("unit_price", "单价", "decimal", "optional", "报价时填写，最多两位小数", decimals=2),
        ColumnSpec("price_basis", "价格性质", "enum", "optional", "成本价或售价；未修改时保留原草稿/更新目标的口径", choices={"cost": "成本价", "sale": "售价"}),
        ColumnSpec("is_tax_inclusive", "含税", "bool", "optional", "税口径；空白表示未填写"),
        ColumnSpec("qty_available", "可供数量", "decimal", "optional", "最多三位小数", decimals=3),
        ColumnSpec("source_key", "货源", "text", "optional", "货源标识，最多 128 字", max_len=128, field_group="data_supplier"),
        ColumnSpec("stock_location", "所在地", "text", "optional", "所在地，最多 256 字", max_len=256),
        ColumnSpec("lead_time", "交期", "text", "optional", "交期说明，最多 256 字", max_len=256),
        ColumnSpec("supplier", "供应商", "text", "optional", "按供应商档案名称精确匹配；不唯一或找不到时报错", field_group="data_supplier"),
        ColumnSpec("notes", "采购备注", "text", "optional", "沿用草稿中已有的可见性规则"),
        ColumnSpec("ref_offer_id", "沿用报价编号", "int", "optional", "沿用时必填，必须是所有来源单所有人可见的有效报价"),
        ColumnSpec("reason", "不报原因", "text", "optional", "不报时必填"),
        ColumnSpec("on_existing", "已有报价时的选择", "enum", "optional", "本人同 PN 已有有效报价时必须显式选择", choices={"update": "更新", "new": "另报"}),
        ColumnSpec("existing_offer_id", "目标报价编号", "int", "optional", "选择更新且有多条有效报价时必须填写"),
    ]),
], instructions=[
    "同一 PN 按要求 PN 一致、坏件是否返回分别合并；任一条件不同会拆行，避免放宽要求。",
    "一行会分发至全部来源单对应行；任何来源校验失败，整份回滚；不新建询价单。",
    "不允许新增或删除行。应用只保存草稿报价及处理选择；须回到页面点击回复给销售。",
    "合并来源已有草稿的值不一致时显示空白；未改动的字段按每个来源保留，修改后统一分发。",
])

_DATA_KEYS = {"unit_price", "price_basis", "is_tax_inclusive", "qty_available", "source_key", "stock_location", "lead_time", "notes"}


def _source_ids(entity_id):
    if not isinstance(entity_id, str) or not entity_id.startswith("quote:"):
        raise WorkbookError("采购表行身份无效，请重新导出")
    try:
        ids = [int(value) for value in entity_id[6:].split(",")]
    except ValueError:
        raise WorkbookError("采购表行身份无效，请重新导出") from None
    if not ids or len(ids) > 20000 or any(not 0 < value < 2**31 for value in ids) or ids != sorted(set(ids)):
        raise WorkbookError("采购表行身份无效，请重新导出")
    return ids


def _entity_id(lines):
    return "quote:" + ",".join(str(line.id) for line in sorted(lines, key=lambda row: row.id))


def _ids(values):
    if (not isinstance(values, list) or not 1 <= len(values) <= 200
            or any(type(value) is not int or value < 1 for value in values)):
        raise InvalidTransition("请选择 1–200 张询价单")
    return sorted(set(values))


def _task(db, inquiry, ctx, user, *, history=False):
    candidates = list(db.scalars(select(WorkItem).where(
        WorkItem.subject_type == "presales_inquiry", WorkItem.subject_id == inquiry.id,
        WorkItem.assignee_id == user.id,
    ).order_by(WorkItem.id).with_for_update().execution_options(populate_existing=True)))
    eligible = [row for row in candidates if not row.paused and row.status in work_items.ACTIVE]
    if len(eligible) == 1:
        return eligible[0]
    if history and candidates and any(row.status in {"done", "declined"} for row in candidates):
        return candidates[-1]
    raise Forbidden("请先领取或接受自己的未暂停待办")


def can_access(db, ctx, subject_type, subject_id):
    try:
        user, ctx = common.resolve_actor(db, ctx)
        common.require(ctx, "page_presales")
        common.require(ctx, "action_presales_quote")
        if subject_type is not None or subject_id is not None:
            return False
        # M4 无法把当前计划传给授权回调。保守重查本人的全部历史来源，
        # 任一来源失权就拒绝无范围预览/回放，避免旧回执成为权限旁路。
        operations = list(db.scalars(select(WorkbookOperation).where(
            WorkbookOperation.schema_name == QUOTE_SCHEMA.name,
            WorkbookOperation.actor == ctx.user_id)))
        exports = list(db.scalars(select(PresalesWorkbookExport).where(
            PresalesWorkbookExport.schema_name == QUOTE_SCHEMA.name,
            PresalesWorkbookExport.actor == ctx.user_id)))
        line_ids = set()
        for operation in operations:
            for row in operation.plan.get("rows", []):
                # 非法新增行没有来源身份和历史值，不能永久封禁后续修正上传。
                if row.get("entity_id") is not None:
                    line_ids.update(_source_ids(row["entity_id"]))
        for exported in exports:
            for row in exported.scope.get("rows", []):
                ids = row.get("line_ids", [])
                if any(type(value) is not int or value < 1 for value in ids):
                    return False
                line_ids.update(ids)
        lines = list(db.scalars(select(PresalesInquiryLine).where(PresalesInquiryLine.id.in_(line_ids))))
        if len(lines) != len(line_ids):
            return False
        for inquiry_id in sorted({line.inquiry_id for line in lines}):
            inquiry = common.lock_inquiry(db, inquiry_id)
            if not visibility.can_view_inquiry(db, inquiry, ctx):
                return False
            _task(db, inquiry, ctx, user, history=True)
        return (_related_exports_accessible(db, line_ids, ctx)
                and _exports_accessible(db, exports, lines, ctx)
                and _history_accessible(db, operations, lines, ctx))
    except (WorkflowError, WorkbookError):
        return False


def _related_exports_accessible(db, source_ids, ctx):
    related = []
    all_ids = set()
    for exported in db.scalars(select(PresalesWorkbookExport).where(
            PresalesWorkbookExport.schema_name == QUOTE_SCHEMA.name)):
        rows = [row for row in exported.scope.get("rows", [])
                if source_ids.intersection(row.get("line_ids", []))]
        if rows:
            related.append(SimpleNamespace(actor=exported.actor, scope={"rows": rows}))
            all_ids.update(value for row in rows for value in row["line_ids"])
    lines = list(db.scalars(select(PresalesInquiryLine).where(PresalesInquiryLine.id.in_(all_ids))))
    return len(lines) == len(all_ids) and _exports_accessible(db, related, lines, ctx)


def _exports_accessible(db, exports, lines, ctx):
    by_id = {line.id: line for line in lines}
    for exported in exports:
        for row in exported.scope.get("rows", []):
            members = [by_id[value] for value in row["line_ids"]]
            inquiries = {line.inquiry_id: db.get(PresalesInquiry, line.inquiry_id) for line in members}
            if any(not visibility.can_view_inquiry(db, inquiry, ctx) for inquiry in inquiries.values()):
                return False
            if any(not work_items.has_permission(ctx, key) for key in row.get("field_groups", [])):
                return False
            for note in row.get("notes", []):
                if note["level"] == "header":
                    record = inquiries.get(note["id"])
                    author = record.created_by if record is not None else None
                elif note["level"] == "line":
                    record = by_id.get(note["id"])
                    author = inquiries[record.inquiry_id].created_by if record is not None else None
                else:
                    return False
                if record is None or not visibility._allowed(db, record.notes_audience_rule_id, ctx, author):
                    return False
            for line_id, field_id in row.get("delivery_fields", []):
                stored = db.scalar(select(FieldValue.id).where(
                    FieldValue.subject_type == "presales_inquiry",
                    FieldValue.subject_id == f"line:{line_id}", FieldValue.field_def_id == field_id))
                # 旧交期迁移到行列后会删除 M7 值，不能据此撤销历史工作簿授权。
                if stored is None:
                    continue
                subject = field_pool.Subject("presales_inquiry", f"line:{line_id}", "line")
                visible = field_pool.read_values(db, [subject], ctx, common.bypass)[subject.subject_id]
                if field_id not in {field["field_def_id"] for field in visible}:
                    return False
            for offer_id in row.get("active_offer_ids", []):
                offer = db.get(PresalesOffer, offer_id)
                viewers = [ctx, *(common.resolve_actor(db, inquiry.created_by)[1]
                                 for inquiry in inquiries.values())]
                if offer is None or any(visibility.offer_view(db, offer, viewer) is None for viewer in viewers):
                    return False
            if exported.actor != ctx.user_id and not common.bypass(ctx):
                for draft in row.get("draft_fields", []):
                    offer = db.get(PresalesOffer, draft["id"])
                    view = visibility.offer_view(db, offer, ctx) if offer is not None else None
                    if view is None or any(key not in view for key in draft["keys"]):
                        return False
    return True


def _history_accessible(db, operations, lines, ctx):
    # 禁删的采购表仍会保存带基线的错误计划；回放也不能返回后来失权的字段。
    by_id = {line.id: line for line in lines}
    for operation in operations:
        for row in operation.plan.get("rows", []):
            if row.get("entity_id") is None:
                continue
            members = [by_id[value] for value in _source_ids(row["entity_id"])]
            inquiries = {line.inquiry_id: db.get(PresalesInquiry, line.inquiry_id) for line in members}
            values = dict(row.get("values", {}))
            for change in row.get("changes", []):
                if values.get(change["key"]) in (None, "") and change.get("before") not in (None, ""):
                    values[change["key"]] = change["before"]
            if any(values.get(key) not in (None, "") for key in ("supplier", "source_key")):
                if not work_items.has_permission(ctx, "data_supplier"):
                    return False
            if values.get("customer_requirements"):
                if any(not visibility._allowed(db, inquiry.notes_audience_rule_id, ctx, inquiry.created_by)
                       for inquiry in inquiries.values()):
                    return False
                if any(not visibility._allowed(db, line.notes_audience_rule_id, ctx,
                                               inquiries[line.inquiry_id].created_by) for line in members):
                    return False
            if values.get("active_offers"):
                summaries = values["active_offers"].splitlines()
                for summary in summaries:
                    match = re.match(r"^#([0-9]+)：", summary)
                    if match is None:
                        return False
                    offer = db.get(PresalesOffer, int(match[1]))
                    viewers = [ctx, *(common.resolve_actor(db, inquiry.created_by)[1]
                                     for inquiry in inquiries.values())]
                    if offer is None or any(visibility.offer_view(db, offer, viewer) is None for viewer in viewers):
                        return False
    return True


def _load(db, ids, ctx):
    user, ctx = common.resolve_actor(db, ctx)
    common.require(ctx, "page_presales")
    common.require(ctx, "action_presales_quote")
    initial = list(db.scalars(select(PresalesInquiryLine).where(PresalesInquiryLine.id.in_(ids))))
    if len(initial) != len(ids):
        raise Forbidden("来源询价行不存在或不可处理")
    inquiries = {}
    tasks = {}
    for inquiry_id in sorted({line.inquiry_id for line in initial}):
        inquiry = common.lock_inquiry(db, inquiry_id)
        if inquiry.kind != "sales_inquiry" or inquiry.status != "submitted":
            raise Forbidden("只能导出已提交且本人有待办的销售询价")
        tasks[inquiry.id] = _task(db, inquiry, ctx, user)
        inquiries[inquiry.id] = inquiry
    lines = list(db.scalars(select(PresalesInquiryLine).where(PresalesInquiryLine.id.in_(ids))
        .order_by(PresalesInquiryLine.id).with_for_update().execution_options(populate_existing=True)))
    if any(line.status != "active" or line.ref_offer_id is not None for line in lines):
        raise Forbidden("已删除或已引用报价的来源行不可处理")
    for line in lines:
        field_pool._lock(db, f"field-values:presales_inquiry:line:{line.id}:line")
    drafts = list(db.scalars(select(PresalesOffer).where(
        PresalesOffer.line_id.in_(ids), PresalesOffer.created_by == user.id,
        PresalesOffer.status == "draft").order_by(PresalesOffer.id)
        .with_for_update().execution_options(populate_existing=True)))
    responses = list(db.scalars(select(PresalesLineResponse).where(
        PresalesLineResponse.line_id.in_(ids), PresalesLineResponse.created_by == user.id)
        .order_by(PresalesLineResponse.id).with_for_update().execution_options(populate_existing=True)))
    return user, ctx, inquiries, tasks, lines, drafts, responses


def _state(db, line, drafts, responses, ctx):
    own = [draft for draft in drafts if draft.line_id == line.id]
    response = next((row for row in responses if row.line_id == line.id and row.line_version == line.version), None)
    result = {}
    if len(own) == 1:
        draft = own[0]
        view = visibility.offer_view(db, draft, ctx) or {}
        result.update({key: view.get(key) for key in _DATA_KEYS})
        supplier = db.get(DimSupplier, view["supplier_id"]) if view.get("supplier_id") is not None else None
        result.update(mode="quote", supplier=supplier.name_raw if supplier else None)
        result.update({key: (draft.workbook_choice or {}).get(key) for key in ("on_existing", "existing_offer_id")})
    elif response is not None:
        result.update(mode=response.mode, ref_offer_id=response.ref_offer_id, reason=response.reason)
    return result


def _same(states, key):
    values = [state.get(key) for state in states]
    return values[0] if values and all(value == values[0] for value in values) else None


def _reference_text(value):
    if not value:
        return None
    return "；".join(f"{label} {value[key]:.2f}" for key, label in (
        ("avg_inc_tax", "含税"), ("avg_ex_tax", "不含税")) if value.get(key) is not None) or None


def _group_key(line):
    return ("part", line.part_id, line.require_exact_pn, line.return_defective) if line.part_id is not None else (
        "raw", line.pn_raw, line.require_exact_pn, line.return_defective)


def _lock_active_offers(db, part_id):
    active = library._active(db, [part_id])
    if not active:
        return set()
    db.flush()
    try:
        # 报价更新先拿 PN 锁再拿行锁；此处不等待，避免和正在更新的报价反向等待。
        with db.connection().begin_nested():
            list(db.scalars(select(PresalesOffer).where(
                PresalesOffer.id.in_([offer.id for offer in active])).order_by(PresalesOffer.id)
                .with_for_update(nowait=True).execution_options(populate_existing=True)))
        return {offer.id for offer in active}
    except OperationalError as exc:
        if getattr(exc.orig, "sqlstate", None) == "55P03":
            raise WorkbookConflict("有效报价正在更新，请稍后重新预览") from exc
        raise


def _row(db, data, refs=None):
    user, ctx, inquiries, tasks, lines, drafts, responses = data
    if len({_group_key(line) for line in lines}) != 1:
        raise WorkbookError("合并行来源的 PN、一致要求或坏件返回条件已改变，请重新导出")
    states = [_state(db, line, drafts, responses, ctx) for line in lines]
    values = {key: _same(states, key) for key in {column.key for column in QUOTE_SCHEMA.sheets[0].columns if column.editable}}
    part = db.get(DimPart, lines[0].part_id) if lines[0].part_id is not None else None
    requirements = []
    descriptions = []
    for inquiry in inquiries.values():
        view = visibility.inquiry_view(db, inquiry, ctx)
        for line_view in view["lines"]:
            if line_view["id"] not in {line.id for line in lines}:
                continue
            if line_view.get("description") and line_view["description"] not in descriptions:
                descriptions.append(line_view["description"])
            text = "；".join(str(value) for value in (
                view.get("notes"), line_view.get("notes"),
                f"期望交期 {line_view['expected_delivery']}" if line_view.get("expected_delivery") else None,
            ) if value)
            if text:
                requirements.append(f"{inquiry.inquiry_no}：{text}")
    active = None
    active_versions = []
    if part is not None:
        locked_offer_ids = _lock_active_offers(db, part.id)
        for owner_id in sorted({inquiry.created_by for inquiry in inquiries.values()}):
            by_part = library.batch(db, [part.id], ctx, owner_id=owner_id)[part.id]
            visible = {entry["id"]: entry for row in by_part for entry in [row, *row.get("more", [])]
                       if entry["id"] in locked_offer_ids}
            active = visible if active is None else {key: value for key, value in active.items() if key in visible}
        # 预览后沿用价或可更新的本人报价发生变化时，必须重新确认。
        active_versions = [(offer.id, offer.version, offer.revision_no)
            for offer in library._active(db, [part.id])
            if offer.id in locked_offer_ids and (offer.created_by == user.id or offer.id in (active or {}))]
        # 摘要不显示报价人，避免不同来源所有人的报价人字段可见性不一致。
        values["active_offers"] = "\n".join(
            f"#{item['id']}：{item['unit_price']:.2f}（{'成本价' if item.get('price_basis') == 'cost' else '售价'}；{'含税' if item.get('is_tax_inclusive') is True else '不含税' if item.get('is_tax_inclusive') is False else '税口径未填'}）"
            for item in sorted((active or {}).values(), key=lambda item: item["id"])) or None
        ref = (refs if refs is not None else price_reference.batch(db, [part.id], ctx)).get(part.id, {})
        values["purchase_reference"] = _reference_text(ref.get("purchase"))
        values["sale_reference"] = _reference_text(ref.get("sale"))
    values.update(inquiry_nos="\n".join(row.inquiry_no for row in inquiries.values()),
        pn=part.pn_std if part else lines[0].pn_raw, qty=sum((line.qty for line in lines), Decimal("0")),
        description="\n".join(descriptions) or None,
        require_exact_pn=lines[0].require_exact_pn, return_defective=lines[0].return_defective,
        customer_requirements="\n".join(requirements) or None)
    version_data = [
        [(row.id, row.version, row.status) for row in inquiries.values()],
        [(row.id, row.version, row.part_id, row.pn_raw, row.require_exact_pn, row.return_defective) for row in lines],
        [(row.id, row.version, row.status, row.paused, row.assignee_id, row.claimed_by) for row in tasks.values()],
        [(row.id, row.version) for row in drafts],
        [(row.id, row.line_version, row.mode, row.ref_offer_id, row.ref_offer_revision_no, row.reason,
          getattr(row, "is_draft", False)) for row in responses],
        active_versions,
    ]
    version = int(hashlib.sha256(json.dumps(version_data, ensure_ascii=False).encode()).hexdigest()[:13], 16)
    return version, values


def load_current(db, sheet, ids, ctx):
    if sheet != "lines":
        return {}
    sources = {}
    for entity_id in ids:
        try:
            sources[entity_id] = _source_ids(entity_id)
        except WorkbookError:
            continue
    # 文件可自由调整行顺序；始终按单号锁定，避免不同上传顺序形成反向锁。
    all_ids = {value for values in sources.values() for value in values}
    # M4 不把文件导出者传给回调；按来源追溯所有相关导出，阻止转交文件绕过 M8。
    # 必须直接拒绝，不能转为 conflict 行，否则禁删行仍会把旧基线带回预览。
    if not _related_exports_accessible(db, all_ids, ctx):
        raise WorkbookForbidden("来源导出字段当前不可见，不能预览或应用该采购表")
    inquiry_ids = db.scalars(select(PresalesInquiryLine.inquiry_id).where(
        PresalesInquiryLine.id.in_(all_ids)).distinct().order_by(PresalesInquiryLine.inquiry_id))
    for inquiry_id in inquiry_ids:
        common.lock_inquiry(db, inquiry_id)
    result = {}
    for entity_id, values in sources.items():
        try:
            data = _load(db, values, ctx)
            result[entity_id] = _row(db, data)
        except WorkbookConflict:
            raise
        except (WorkflowError, WorkbookError):
            continue
    return result


def export_quote_sheet(db, inquiry_ids, ctx, visible_columns=None):
    ids = _ids(inquiry_ids)
    user, ctx = common.resolve_actor(db, ctx)
    common.require(ctx, "page_presales")
    common.require(ctx, "action_presales_quote")
    for inquiry_id in ids:
        inquiry = common.lock_inquiry(db, inquiry_id)
        if inquiry.kind != "sales_inquiry" or inquiry.status != "submitted":
            raise Forbidden("只能导出已提交且本人有待办的销售询价")
        _task(db, inquiry, ctx, user)
    lines = list(db.scalars(select(PresalesInquiryLine).where(
        PresalesInquiryLine.inquiry_id.in_(ids), PresalesInquiryLine.status == "active",
        PresalesInquiryLine.ref_offer_id.is_(None)).order_by(PresalesInquiryLine.id)))
    groups = {}
    for line in lines:
        groups.setdefault(_group_key(line), []).append(line)
    refs = price_reference.batch(db, list({line.part_id for line in lines if line.part_id is not None}), ctx)
    rows = []
    scopes = []
    selected = None if visible_columns is None else visible_columns.get("lines", [])
    columns = [column for column in QUOTE_SCHEMA.sheets[0].columns
               if column_allowed(column, ctx) and (selected is None or column.key in selected or column.mandatory)]
    for members in groups.values():
        entity_id = _entity_id(members)
        data = _load(db, _source_ids(entity_id), ctx)
        version, values = _row(db, data, refs)
        rows.append({"entity_id": entity_id, "base_version": version, "values": values})
        scopes.append(_export_scope(db, data, values, columns))
    export_id = str(uuid4())
    content = export_workbook(QUOTE_SCHEMA, {"lines": rows}, export_id=export_id, actor=ctx.user_id,
                              visible_columns=visible_columns, ctx=ctx)
    db.add(PresalesWorkbookExport(schema_name=QUOTE_SCHEMA.name, export_id=export_id,
                                 actor=ctx.user_id, scope={"rows": scopes}))
    db.flush()
    return content


def _export_scope(db, data, values, columns):
    _, ctx, inquiries, _, lines, drafts, _ = data
    present = {column.key for column in columns if values.get(column.key) not in (None, "")}
    notes = []
    if "customer_requirements" in present:
        notes.extend({"level": "header", "id": inquiry.id} for inquiry in inquiries.values()
                     if inquiry.notes and visibility._allowed(db, inquiry.notes_audience_rule_id, ctx, inquiry.created_by))
        notes.extend({"level": "line", "id": line.id} for line in lines
                     if line.notes and visibility._allowed(db, line.notes_audience_rule_id, ctx,
                                                          inquiries[line.inquiry_id].created_by))
    draft_keys = sorted(present.intersection(_DATA_KEYS) | ({"supplier_id"} if "supplier" in present else set()))
    return {"line_ids": [line.id for line in lines], "notes": notes,
            "active_offer_ids": [int(match) for match in re.findall(r"^#([0-9]+)：", values["active_offers"], re.MULTILINE)]
                if "active_offers" in present else [],
            "field_groups": sorted({column.field_group for column in columns
                                    if column.field_group and column.key in present}),
            "draft_fields": [{"id": draft.id, "keys": draft_keys} for draft in drafts] if draft_keys else []}


def _supplier_id(db, name):
    if not name:
        return None
    suppliers = list(db.scalars(select(DimSupplier).where(DimSupplier.name_raw == name)))
    if len(suppliers) != 1:
        raise InvalidTransition(f"供应商名称须精确匹配唯一档案：{name}")
    return suppliers[0].id


def apply_quotes(db, plan, ctx):
    if plan.subject_type is not None or plan.subject_id is not None:
        raise WorkbookError("采购报价表不接受额外业务范围")
    applied = []
    used = set()
    updating = set()
    # 先锁定并验证全部来源，再调用逐行服务，防止一行来源重复分发。
    loaded = []
    for row in plan.rows:
        if row["sheet"] != "lines" or row["status"] != "modified":
            raise WorkbookError("采购报价表不允许新增或删除行")
        ids = _source_ids(row["entity_id"])
        if used.intersection(ids):
            raise WorkbookError("来源询价行重复，请重新导出")
        used.update(ids)
        data = _load(db, ids, ctx)
        if _row(db, data)[0] != row["base_version"]:
            raise WorkbookError("来源数据已改变，请重新导出")
        loaded.append((row, data))
    # 报价服务按采购人/PN 加事务锁；按 PN 排序后写入，避免文件行序反转锁序。
    loaded.sort(key=lambda item: (item[1][4][0].part_id or 0, item[0]["entity_id"]))
    for row, data in loaded:
        user, ctx, inquiries, tasks, lines, drafts, responses = data
        changes = {change["key"] for change in row["changes"]}
        values = row["values"]
        supplier_id = _supplier_id(db, values.get("supplier")) if "supplier" in changes else None
        for line in lines:
            state = _state(db, line, drafts, responses, ctx)
            def chosen(key):
                return values.get(key) if key in changes else state.get(key)
            mode = chosen("mode")
            payload = {key: values.get(key) for key in _DATA_KEYS if key in changes}
            if "source_key" in payload and payload["source_key"] is None:
                payload["source_key"] = ""
            if "supplier" in changes:
                payload["supplier_id"] = supplier_id
            if mode == "quote":
                payload["no_stock"] = False
            result = offers.save_line_decision(db, line.inquiry_id, line.id, ctx,
                expected_version=inquiries[line.inquiry_id].version, work_item_id=tasks[line.inquiry_id].id,
                mode=mode, data=payload if mode == "quote" else None,
                ref_offer_id=chosen("ref_offer_id"), reason=chosen("reason"),
                on_existing=chosen("on_existing"), existing_offer_id=chosen("existing_offer_id"))
            if result.get("offer_id") is not None:
                draft = db.get(PresalesOffer, result["offer_id"])
                target = (draft.workbook_choice or {}).get("existing_offer_id")
                if target is not None:
                    if target in updating:
                        raise InvalidTransition("同一批次不能多次更新同一有效报价，请改为另报或逐单处理")
                    updating.add(target)
            applied.append({"inquiry_id": line.inquiry_id, **result})
    return {"saved": applied, "replied": False}


def _error_column(exc):
    message = str(exc)
    if "供应商" in message:
        return "supplier"
    if "沿用" in message or "不可见" in message:
        return "ref_offer_id"
    if "PN" in message and "一致" in message:
        return "pn"
    return None


def validate_quotes(db, plan, ctx):
    return validate_by_applying(db, plan, ctx, apply_quotes, _error_column)

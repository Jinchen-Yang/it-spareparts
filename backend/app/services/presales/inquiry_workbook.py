"""销售询价工作簿；行变更沿用草稿服务，发布仍由页面确认。"""

from contextlib import nullcontext
from hashlib import sha256
import json
from uuid import uuid4

from sqlalchemy import select

from app.models.dimensions import DimPart
from app.models.fields_audience import FieldDef, FieldValue
from app.models.presales import PresalesInquiry, PresalesInquiryLine, PresalesOffer, PresalesWorkbookExport
from app.models.workbook import WorkbookOperation
from app.services import audience, field_pool, price_reference
from app.services.presales import common, inquiries, library, visibility
from app.services.workbook.export import export_workbook
from app.services.workbook.registry import Forbidden as WorkbookForbidden
from app.services.workbook.schema import ColumnSpec, SheetSpec, WorkbookSchema, WorkbookError, json_value
from app.services.workbook.validation import validate_by_applying
from app.services.workflow.registry import Forbidden, InvalidTransition, WorkflowError


INQUIRY_SCHEMA = WorkbookSchema("presales_inquiry_lines", 1, [
    SheetSpec("lines", "询价明细", [
        ColumnSpec("pn", "PN", "code", "locked", "已绑定的型号不可改；如需换型号请删除后新增", max_len=256),
        ColumnSpec("pn_raw", "PN 原文", "code", "required", "新增行必填，按原文精确匹配主数据", max_len=256),
        ColumnSpec("qty", "数量", "decimal", "required", "正数，最多 3 位小数", decimals=3),
        ColumnSpec("description", "描述", "text", "optional", "产品描述"),
        ColumnSpec("require_exact_pn", "要求 PN 一致", "bool", "optional", "是或否；空白表示不要求"),
        ColumnSpec("expected_delivery", "期望交期", "date", "optional", "YYYY-MM-DD"),
        ColumnSpec("notes", "行备注", "text", "optional", "按原有受众规则保存"),
        ColumnSpec("purchase_reference", "参考采购价", "text", "reference", "近 30 天参考价，不等于定价", field_group="data_purchase_cost"),
        ColumnSpec("sale_reference", "参考销售价", "text", "reference", "近 30 天参考价，不等于定价"),
        ColumnSpec("offer_reference", "有效报价最低价", "text", "reference", "仅显示当前可见有效报价，含税口径分别列示"),
    ], allow_insert=True, allow_delete=True, custom_columns=True),
], instructions=[
    "仅草稿或撤回中的销售询价可修改；导入只保存草稿，不提交询价。",
    "空模板导入创建新草稿；修改现有单据时请选择对应询价单。",
    "新增行留空令牌和 PN，填写 PN 原文及数量；找不到精确型号时保存为待建档。",
    "已绑定行不能修改 PN 原文；改型号请删除原行并新增。自定义字段不填则留空。",
])

_EDITABLE = {column.key for column in INQUIRY_SCHEMA.sheets[0].columns if column.editable}


class _WorkbookSession:
    """复用 M4 已持有的 savepoint；真实 commit 仍由原 Session 拦截。"""

    def __init__(self, db):
        if not db.in_nested_transaction():
            raise InvalidTransition("工作簿回调必须在引擎事务中运行")
        self._db = db

    def __getattr__(self, name):
        return getattr(self._db, name)

    def begin_nested(self):
        # M7 内层 savepoint 的释放也触发 before_commit；整份原子性已由 M4 保证。
        return nullcontext()


def _editable_inquiry(db, inquiry_id, ctx, *, lock=False):
    user, ctx = common.resolve_actor(db, ctx)
    common.require(ctx, "page_presales")
    common.require(ctx, "action_presales_inquire")
    query = select(PresalesInquiry).where(PresalesInquiry.id == inquiry_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    inquiry = db.scalar(query)
    if inquiry is None or (inquiry.created_by != user.id and not common.bypass(ctx)):
        raise Forbidden("无权编辑该询价单")
    if inquiry.kind != "sales_inquiry" or inquiry.status not in {"draft", "revising"}:
        raise InvalidTransition("仅草稿或撤回中的销售询价可导入导出")
    return inquiry


def can_access(db, ctx, subject_type, subject_id):
    try:
        _, ctx = common.resolve_actor(db, ctx)
        common.require(ctx, "page_presales")
        common.require(ctx, "action_presales_inquire")
        if not _exports_accessible(db, ctx, subject_id):
            return False
        if subject_type is None and subject_id is None:
            # 空模板回执没有 subject；用已创建单据复查，避免绕过历史字段授权。
            operations = db.scalars(select(WorkbookOperation).where(
                WorkbookOperation.schema_name == INQUIRY_SCHEMA.name,
                WorkbookOperation.subject_type.is_(None), WorkbookOperation.actor == ctx.user_id,
                WorkbookOperation.status == "applied"))
            for operation in operations:
                inquiry_id = (operation.response or {}).get("inquiry_id")
                inquiry = db.get(PresalesInquiry, inquiry_id) if type(inquiry_id) is int else None
                if inquiry is None or not visibility.can_view_inquiry(db, inquiry, ctx):
                    return False
                if not _history_accessible(db, inquiry, ctx, plans=[operation.plan]):
                    return False
            return True
        if subject_type != inquiries.SUBJECT_TYPE or type(subject_id) is not int:
            return False
        inquiry = _editable_inquiry(db, subject_id, ctx)
        return _history_accessible(db, inquiry, ctx)
    except WorkflowError:
        return False


def _exports_accessible(db, ctx, inquiry_id):
    if inquiry_id is None:
        plans = db.scalars(select(WorkbookOperation.plan).where(
            WorkbookOperation.schema_name == INQUIRY_SCHEMA.name, WorkbookOperation.actor == ctx.user_id))
        line_ids = {row["entity_id"] for plan in plans for row in plan.get("rows", [])
                    if type(row.get("entity_id")) is int}
        parents = set(db.scalars(select(PresalesInquiryLine.inquiry_id).where(PresalesInquiryLine.id.in_(line_ids))))
        if any(not _exports_accessible(db, ctx, parent) for parent in parents):
            return False
    query = select(PresalesWorkbookExport).where(PresalesWorkbookExport.schema_name == INQUIRY_SCHEMA.name)
    query = query.where(PresalesWorkbookExport.actor == ctx.user_id) if inquiry_id is None else query.where(
        PresalesWorkbookExport.scope["inquiry_id"].as_integer() == inquiry_id)
    exports = db.scalars(query)
    for exported in exports:
        scope = exported.scope
        if inquiry_id is not None and scope["inquiry_id"] != inquiry_id:
            continue
        inquiry = db.get(PresalesInquiry, scope["inquiry_id"])
        if inquiry is None or not visibility.can_view_inquiry(db, inquiry, ctx):
            return False
        field_pairs = scope.get("fields", [])
        subjects = [field_pool.Subject(inquiries.SUBJECT_TYPE, f"line:{line_id}", "line")
                    for line_id in sorted({pair[0] for pair in field_pairs})]
        visible = field_pool.read_values(db, subjects, ctx, common.bypass)
        for line_id, field_id in field_pairs:
            if field_id in {item["field_def_id"] for item in visible[f"line:{line_id}"]}:
                continue
            stored = db.scalar(select(FieldValue.id).where(
                FieldValue.subject_type == inquiries.SUBJECT_TYPE,
                FieldValue.subject_id == f"line:{line_id}", FieldValue.field_def_id == field_id))
            # 旧模板把交期混记在 fields 中；仅豁免已迁移删除的交期，其他字段保持原判权。
            if stored is None and db.scalar(select(FieldDef.name).where(FieldDef.id == field_id)) == "期望交期":
                continue
            author = scope.get("field_authors", {}).get(f"{line_id}:{field_id}")
            # 作者清空自己的值不撤销其历史可见性；被他人改成私密仍须拒绝。
            if stored is not None or (author != ctx.user_id and not common.bypass(ctx)):
                return False
        for line_id in scope.get("notes", []):
            line = db.get(PresalesInquiryLine, line_id)
            if line is None or not visibility._allowed(db, line.notes_audience_rule_id, ctx, inquiry.created_by):
                return False
        for offer_id in scope.get("offers", []):
            offer = db.get(PresalesOffer, offer_id)
            if offer is None or not visibility.can_view_offer(db, offer, ctx):
                return False
    return True


def _history_accessible(db, inquiry, ctx, *, plans=None):
    # 通用 can_access 没有 operation_id；按本单历史计划保守拦截已失权字段的回放。
    lines = inquiries.lines_for(db, inquiry.id, active=False)
    subjects = [field_pool.Subject(inquiries.SUBJECT_TYPE, f"line:{line.id}", "line") for line in lines]
    if not subjects:
        return True
    visible = field_pool.read_values(db, subjects, ctx, common.bypass)
    visible_ids = {key: {item["field_def_id"] for item in values} for key, values in visible.items()}
    hidden = {}
    for subject_id, field_id, name in db.execute(select(
            FieldValue.subject_id, FieldValue.field_def_id, FieldDef.name).join(FieldDef).where(
                FieldValue.subject_type == inquiries.SUBJECT_TYPE,
                FieldValue.subject_id.in_(visible_ids))):
        if field_id not in visible_ids[subject_id]:
            hidden.setdefault(int(subject_id.removeprefix("line:")), set()).add(name)
    hidden_notes = {line.id for line in lines if not visibility._allowed(
        db, line.notes_audience_rule_id, ctx, inquiry.created_by)}
    if not hidden and not hidden_notes:
        return True
    if plans is None:
        plans = db.scalars(select(WorkbookOperation.plan).where(
            WorkbookOperation.schema_name == INQUIRY_SCHEMA.name,
            WorkbookOperation.subject_type == inquiries.SUBJECT_TYPE,
            WorkbookOperation.subject_id == inquiry.id, WorkbookOperation.actor == ctx.user_id))
    for plan in plans:
        for row in plan.get("rows", []):
            entity_id = row.get("entity_id")
            # 历史计划只记字段名；改名后不能证明对应关系，保守拒绝该行的有值字段。
            has_hidden_fields = bool(hidden) if entity_id is None else entity_id in hidden
            if has_hidden_fields:
                if any(value not in (None, "") for value in row.get("custom_fields", {}).values()):
                    return False
                if any(change.get("key", "").startswith("+")
                       and (change.get("before") not in (None, "") or change.get("after") not in (None, ""))
                       for change in row.get("changes", [])):
                    return False
            has_hidden_notes = bool(hidden_notes) if entity_id is None else entity_id in hidden_notes
            if has_hidden_notes:
                if row.get("values", {}).get("notes") not in (None, ""):
                    return False
                if any(change.get("key") == "notes"
                       and (change.get("before") not in (None, "") or change.get("after") not in (None, ""))
                       for change in row.get("changes", [])):
                    return False
    return True


def _line_values(db, lines, ctx, *, fields=None):
    if fields is None:
        subjects = [field_pool.Subject(inquiries.SUBJECT_TYPE, f"line:{line.id}", "line") for line in lines]
        fields = field_pool.read_values(db, subjects, ctx, common.bypass)
    values = {}
    for line in lines:
        note = visibility._allowed(db, line.notes_audience_rule_id, ctx,
                                   db.get(PresalesInquiry, line.inquiry_id).created_by)
        part = db.get(DimPart, line.part_id) if line.part_id is not None else None
        values[line.id] = {
            "pn": part.pn_std if part else None, "pn_raw": line.pn_raw, "qty": line.qty,
            "description": line.description, "require_exact_pn": line.require_exact_pn,
            "expected_delivery": inquiries.delivery_value(line, fields[f"line:{line.id}"]), "notes": line.notes if note else None,
            "custom_fields": {item["name"]: item["value"] for item in fields[f"line:{line.id}"] if item["name"] != "期望交期"},
        }
    return values


def _version(inquiry, line, values):
    # 字段池独立于行版本；一起计算签名，防止外部字段修改后被旧表覆盖。
    payload = json.dumps(json_value([inquiry.version, line.version, values]),
                         sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return int(sha256(payload.encode()).hexdigest()[:13], 16)


def load_current(db, sheet, ids, ctx):
    if sheet != "lines" or any(type(value) is not int for value in ids):
        return {}
    candidates = list(db.scalars(select(PresalesInquiryLine).where(PresalesInquiryLine.id.in_(ids))))
    parents = {}
    for inquiry_id in sorted({line.inquiry_id for line in candidates}):
        try:
            parents[inquiry_id] = _editable_inquiry(db, inquiry_id, ctx, lock=True)
        except WorkflowError:
            continue
    lines = list(db.scalars(select(PresalesInquiryLine).where(
        PresalesInquiryLine.id.in_(ids), PresalesInquiryLine.inquiry_id.in_(parents),
        PresalesInquiryLine.status == "active").order_by(PresalesInquiryLine.id)
        .with_for_update().execution_options(populate_existing=True)))
    # 与 M7 写值共享锁；先锁全体行再锁定义，避免读版本后自定义字段被并发覆盖。
    for line in lines:
        field_pool._lock(db, f"field-values:{inquiries.SUBJECT_TYPE}:line:{line.id}:line")
    for scope, owner in (("team", None), ("personal", ctx.user_id)):
        field_pool._lock(db, f"field-def:{scope}:{owner}:{inquiries.SUBJECT_TYPE}:line")
    for inquiry_id in sorted({line.inquiry_id for line in candidates}):
        if not _exports_accessible(db, ctx, inquiry_id):
            # 不能转成 conflict 行：删行计划仍会回显导出基线。
            raise WorkbookForbidden("来源工作簿字段权限已变更，请恢复权限后重试")
    values = _line_values(db, lines, ctx)
    return {line.id: (_version(parents[line.inquiry_id], line, values[line.id]), values[line.id])
            for line in lines}


def _reference(value):
    if not value:
        return None
    return "；".join(f"{label} {value[key]}" for key, label in
                    (("avg_inc_tax", "含税"), ("avg_ex_tax", "不含税")) if value.get(key) is not None) or None


def _offer_reference(rows):
    prices = {}
    for row in rows:
        for offer in [row, *row.get("more", [])]:
            price = offer.get("unit_price")
            if price is not None:
                tax = offer.get("is_tax_inclusive")
                prices[tax] = min(prices.get(tax, price), price)
    return "；".join(f"{'含税' if tax is True else '不含税' if tax is False else '税口径未填'} {price}"
                    for tax, price in prices.items()) or None


def _custom_names(db, lines, ctx, values):
    fields = {field.name: field for field in field_pool.template_for(db, inquiries.SUBJECT_TYPE, "line", ctx)}
    for line in lines:
        if line.part_id is not None:
            fields.update({field.name: field for field in field_pool.template_for(
                db, inquiries.SUBJECT_TYPE, "line", ctx, anchor=f"part:{line.part_id}")})
    visible_names = {name for value in values.values() for name in value["custom_fields"]}
    stored_names = set(db.scalars(select(FieldDef.name).join(FieldValue).where(
        FieldValue.subject_type == inquiries.SUBJECT_TYPE,
        FieldValue.subject_id.in_([f"line:{line.id}" for line in lines]))))
    names = [field.name for field in sorted(fields.values(), key=lambda item: (item.sort, item.name, item.id))
             if field.name != "期望交期" and (field.name in visible_names or (
                 field.name not in stored_names and audience.filter_fields(
                     db, [{"visible": True}], ctx, field_rules={"visible":
                         (field.default_rule_id, field.owner_id)}, bypass=common.bypass)[0]))]
    for value in values.values():
        for name in value["custom_fields"]:
            if name not in names:
                names.append(name)
    return names


def export_schema(db, ctx, inquiry_id=None):
    _, ctx = common.resolve_actor(db, ctx)
    if inquiry_id is None:
        if not can_access(db, ctx, None, None):
            raise Forbidden("无权下载销售询价模板")
        lines = []
    else:
        inquiry = _editable_inquiry(db, inquiry_id, ctx)
        lines = inquiries.lines_for(db, inquiry.id)
    names = _custom_names(db, lines, ctx, _line_values(db, lines, ctx))
    result = INQUIRY_SCHEMA.to_dict(ctx=ctx)
    result["sheets"][0]["columns"].extend({"key": "+" + name, "header": "+" + name,
        "type": "text", "edit": "optional", "hint": "自定义字段", "custom": True} for name in names)
    return result


def _export(db, inquiry, ctx, visible_columns):
    if visible_columns is not None and (not isinstance(visible_columns, dict)
            or any(not isinstance(columns, list) or any(not isinstance(column, str) for column in columns)
                   for columns in visible_columns.values())):
        raise WorkbookError("visible_columns 必须按工作表列出列键")
    lines = inquiries.lines_for(db, inquiry.id) if inquiry is not None else []
    subjects = [field_pool.Subject(inquiries.SUBJECT_TYPE, f"line:{line.id}", "line") for line in lines]
    for line in sorted(lines, key=lambda row: row.id):
        field_pool._lock(db, f"field-values:{inquiries.SUBJECT_TYPE}:line:{line.id}:line")
    for scope, owner in (("team", None), ("personal", ctx.user_id)):
        field_pool._lock(db, f"field-def:{scope}:{owner}:{inquiries.SUBJECT_TYPE}:line")
    fields = field_pool.read_values(db, subjects, ctx, common.bypass)
    authors = {f"{subject_id.removeprefix('line:')}:{field_id}": author
               for subject_id, field_id, author in db.execute(select(
                   FieldValue.subject_id, FieldValue.field_def_id, FieldValue.updated_by).where(
                       FieldValue.subject_type == inquiries.SUBJECT_TYPE,
                       FieldValue.subject_id.in_([subject.subject_id for subject in subjects])))}
    values = _line_values(db, lines, ctx, fields=fields)
    part_ids = list({line.part_id for line in lines if line.part_id is not None})
    references = price_reference.batch(db, part_ids, ctx)
    offers = library.batch(db, part_ids, ctx)
    rows = []
    for line in lines:
        value = dict(values[line.id])
        custom = value.pop("custom_fields")
        reference = references.get(line.part_id, {})
        value.update(purchase_reference=_reference(reference.get("purchase")),
                     sale_reference=_reference(reference.get("sale")),
                     offer_reference=_offer_reference(offers.get(line.part_id, [])))
        rows.append({"entity_id": line.id, "base_version": _version(inquiry, line, values[line.id]),
                     "values": value, "custom_fields": custom})
    names = _custom_names(db, lines, ctx, values)
    if visible_columns is not None:
        selected = visible_columns.get("lines", [])
        names = [name for name in names if "+" + name in selected]
    export_id = uuid4().hex
    content = export_workbook(INQUIRY_SCHEMA, {"lines": rows}, export_id=export_id, actor=ctx.user_id,
                              ctx=ctx, visible_columns=visible_columns, custom_fields={"lines": names})
    if inquiry is not None:
        selected = None if visible_columns is None else visible_columns.get("lines", [])
        def exported_field(line, item):
            return item["name"] != "期望交期" and item["name"] in names
        scope = {"inquiry_id": inquiry.id,
                 "fields": [[line.id, item["field_def_id"]] for line in lines
                            for item in fields[f"line:{line.id}"] if exported_field(line, item)],
                 "field_authors": {f"{line.id}:{item['field_def_id']}": authors[f"{line.id}:{item['field_def_id']}"]
                                   for line in lines for item in fields[f"line:{line.id}"] if exported_field(line, item)},
                 "notes": [line.id for line in lines if values[line.id].get("notes")
                           and (selected is None or "notes" in selected)],
                 "offers": sorted({offer["id"] for rows in offers.values() for row in rows
                                   for offer in [row, *row.get("more", [])]})
                           if selected is None or "offer_reference" in selected else []}
        # M4 授权回调不接收本次文件；保存导出范围，首次删行预览也能复查旧基线。
        db.add(PresalesWorkbookExport(schema_name=INQUIRY_SCHEMA.name, export_id=export_id,
                                     actor=ctx.user_id, scope=scope))
        db.flush()
    return content


def export_template(db, ctx, visible_columns=None):
    _, ctx = common.resolve_actor(db, ctx)
    if not can_access(db, ctx, None, None):
        raise Forbidden("无权下载销售询价模板")
    return _export(db, None, ctx, visible_columns)


def export_inquiry(db, inquiry_id, ctx, visible_columns=None):
    _, ctx = common.resolve_actor(db, ctx)
    return _export(db, _editable_inquiry(db, inquiry_id, ctx), ctx, visible_columns)


def _exact_part(db, pn):
    if not isinstance(pn, str) or not pn.strip():
        raise InvalidTransition("新增行必须填写 PN 原文")
    matches = list(db.scalars(select(DimPart).where(DimPart.pn_std == pn.strip(), DimPart.status == "active")))
    if len(matches) > 1:
        raise InvalidTransition("PN 原文精确匹配多个型号，请先在主数据处理重复")
    return matches[0].id if matches else None


def _changes(db, row, old, ctx):
    changed = {entry["key"] for entry in row["changes"]}
    payload = {key: value for key, value in row["values"].items()
               if key in _EDITABLE and (old is None or key in changed)}
    if old is None and payload.get("qty") is None:
        raise InvalidTransition("新增行必须填写数量，请在导出时保留数量列")
    if old is not None and old.part_id is not None and "pn_raw" in payload and payload["pn_raw"] != old.pn_raw:
        raise InvalidTransition("已绑定行不能修改 PN 原文，请删除后新增")
    if old is None or "pn_raw" in payload:
        payload["part_id"] = _exact_part(db, payload.get("pn_raw"))
    if "require_exact_pn" in payload and payload["require_exact_pn"] is None:
        payload["require_exact_pn"] = False
    if old is not None and "notes" in payload and not visibility._allowed(
            db, old.notes_audience_rule_id, ctx, db.get(PresalesInquiry, old.inquiry_id).created_by):
        raise Forbidden("无权修改该行备注")
    fields = [{"new_name": name, "value": value} for name, value in row["custom_fields"].items()
              if (old is None and value is not None) or "+" + name in changed]
    if fields and old is not None:
        visible = _line_values(db, [old], ctx)[old.id]["custom_fields"]
        available = {field.id for field in field_pool.list_defs(db, inquiries.SUBJECT_TYPE, "line", ctx)}
        stored = dict(db.execute(select(FieldDef.name, FieldDef.id).join(FieldValue).where(
            FieldValue.subject_type == inquiries.SUBJECT_TYPE,
            FieldValue.subject_id == f"line:{old.id}")).all())
        if any(entry["new_name"] in stored and (entry["new_name"] not in visible
                or stored[entry["new_name"]] not in available) for entry in fields):
            raise Forbidden("无权修改该自定义字段，请保留原值")
    if fields:
        payload["field_values"] = fields
    return payload


def apply_inquiry(db, plan, ctx):
    db = _WorkbookSession(db)
    _, ctx = common.resolve_actor(db, ctx)
    if any(row["sheet"] != "lines" for row in plan.rows):
        raise InvalidTransition("工作表无效")
    if plan.subject_type is None and plan.subject_id is None:
        if any(row["status"] != "added" for row in plan.rows):
            raise InvalidTransition("修改现有询价必须指定对应询价单")
        payload = [_changes(db, row, None, ctx) for row in plan.rows]
        inquiry = inquiries.create_draft(db, ctx, lines=payload)
    else:
        inquiry = _editable_inquiry(db, plan.subject_id, ctx, lock=True)
        if plan.subject_type != inquiries.SUBJECT_TYPE:
            raise InvalidTransition("询价单类型无效")
        existing = inquiries.lines_for(db, inquiry.id, lock=True)
        indexed = {line.id: line for line in existing}
        changes = {}
        added = []
        for row in plan.rows:
            if row["status"] == "added":
                added.append(_changes(db, row, None, ctx))
                continue
            old = indexed.get(row["entity_id"])
            if old is None:
                raise Forbidden("工作簿包含其他询价单的行")
            changes[old.id] = None if row["status"] == "deleted" else _changes(db, row, old, ctx)
        payload = [{"line_key": line.line_key, **(changes.get(line.id) or {})}
                   for line in existing if line.id not in changes or changes[line.id] is not None]
        if plan.rows:
            inquiry = inquiries.update_draft(db, inquiry.id, ctx, inquiry.version, lines=[*payload, *added])
    return {"inquiry_id": inquiry.id, "inquiry_no": inquiry.inquiry_no, "status": inquiry.status,
            "version": inquiry.version, "line_count": len(inquiries.lines_for(db, inquiry.id))}


def validate_inquiry(db, plan, ctx):
    return validate_by_applying(db, plan, ctx, apply_inquiry)

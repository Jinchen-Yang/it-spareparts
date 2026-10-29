"""报价草稿、独立回复和只追加的发布历史；事务由调用方持有。"""

import hashlib
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app.models.dimensions import DimPart, DimSupplier
from app.models.presales import (
    PresalesInquiry, PresalesInquiryLine, PresalesLineResponse, PresalesOffer, PresalesOfferLineLink,
    PresalesOfferRevision,
)
from app.services.presales import common
from app.services.workflow import work_items
from app.services.workflow.registry import Conflict, Forbidden, InvalidTransition, NotFound, Subject

SHANGHAI = ZoneInfo("Asia/Shanghai")
EDITABLE = {
    "unit_price", "is_tax_inclusive", "no_stock", "qty_available", "stock_location",
    "lead_time", "source_key", "supplier_id", "notes", "price_basis",
    "supplier_audience", "reporter_audience", "notes_audience",
}


def _json(value):
    if isinstance(value, (datetime, Decimal)):
        return value.isoformat() if isinstance(value, datetime) else str(value)
    return value


def append_revision(db, offer, actor, changed_fields, reason=None):
    user, _ = common.resolve_actor(db, actor)
    offer.revision_no += 1
    db.flush()
    snapshot = {column.name: _json(getattr(offer, column.name))
                for column in PresalesOffer.__table__.columns}
    inquiry = common.get_inquiry(db, offer.inquiry_id)
    snapshot["_inquiry"] = {key: _json(getattr(inquiry, key)) for key in (
        "id", "kind", "created_by", "offer_audience_rule_id", "status", "submitted_at")}
    if offer.status == "published":
        snapshot["_inquiry"].update(status="submitted", submitted_at=_json(offer.valid_from))
    revision = PresalesOfferRevision(
        offer_id=offer.id, revision_no=offer.revision_no, snapshot=snapshot,
        changed_fields=sorted(set(changed_fields)), reason=reason, actor=user.id,
        created_at=common.now(),
    )
    db.add(revision)
    db.flush()
    return revision


def mark_stale(db, offer, actor, reason):
    if offer.status == "stale":
        return offer
    offer.status = "stale"
    offer.version += 1
    offer.updated_at = common.now()
    append_revision(db, offer, actor, ["status"], reason)
    inquiry = common.get_inquiry(db, offer.inquiry_id)
    common.event(db, inquiry, actor, "offer.stale", {"offer_id": offer.id})
    common.audit(db, actor, "presales.offer.stale", offer.id)
    return offer


def _line(db, inquiry_id, line_id):
    line = db.scalar(select(PresalesInquiryLine).where(
        PresalesInquiryLine.id == line_id, PresalesInquiryLine.inquiry_id == inquiry_id,
    ).with_for_update().execution_options(populate_existing=True))
    if line is None or line.status != "active":
        raise NotFound("询价行不存在或已删除")
    if line.ref_offer_id is not None:
        raise InvalidTransition("已引用报价的行无需再次报价")
    return line


def _task(db, inquiry, actor, user_id, work_item_id=None):
    items = work_items.subject_items(db, Subject("presales_inquiry", inquiry.id), actor, lock=True)
    candidates = [item for item in items if item.assignee_id == user_id
                  and item.status in work_items.ACTIVE and not item.paused
                  and (work_item_id is None or item.id == work_item_id)]
    if len(candidates) != 1:
        raise Forbidden("请先领取或接受自己的未暂停待办")
    return candidates[0]


def _serialize_source(db, user_id, part_id, source_key):
    # 同采购同 PN 的检测与发布串行，避免并发绕过重复报价的明确选择。
    digest = hashlib.sha256(f"presales:{user_id}:{part_id}".encode()).digest()
    lock_key = int.from_bytes(digest[:8], "big", signed=True)
    db.execute(select(func.pg_advisory_xact_lock(lock_key)))


def _decimal(value, name, places):
    if isinstance(value, bool):
        raise InvalidTransition(f"{name}必须为非负数字")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise InvalidTransition(f"{name}必须为非负数字") from exc
    if not result.is_finite() or result < 0:
        raise InvalidTransition(f"{name}必须为非负数字，最多 {places} 位小数")
    if result >= Decimal(10) ** (14 - places):
        raise InvalidTransition(f"{name}超出范围")
    if result != result.quantize(Decimal(1).scaleb(-places)):
        raise InvalidTransition(f"{name}最多 {places} 位小数")
    return result


def _apply(db, offer, data, ctx):
    if not isinstance(data, dict) or set(data) - EDITABLE:
        raise InvalidTransition("包含不可修改的报价字段")
    changed = []
    for key, value in data.items():
        field = key
        if key.endswith("_audience"):
            field = key + "_rule_id"
            value = common.rule_id(db, value, ctx, default="private" if key == "supplier_audience" else "public")
        elif key in {"unit_price", "qty_available"}:
            value = _decimal(value, "单价" if key == "unit_price" else "可供数量", 2 if key == "unit_price" else 3) if value is not None else None
        elif key == "no_stock":
            if type(value) is not bool:
                raise InvalidTransition("无货标记必须为布尔值")
        elif key == "is_tax_inclusive":
            if value is not None and type(value) is not bool:
                raise InvalidTransition("含税口径必须为布尔值或空")
        elif key == "supplier_id":
            if value is not None and (type(value) is not int or db.get(DimSupplier, value) is None):
                raise InvalidTransition("供应商不存在")
        elif key == "source_key":
            if not isinstance(value, str) or len(value) > 128:
                raise InvalidTransition("货源标识最多 128 字")
            value = value.strip()
        elif key in {"stock_location", "lead_time"}:
            if value is not None and (not isinstance(value, str) or len(value) > 256):
                raise InvalidTransition("所在地和交期最多 256 字")
        elif key == "price_basis":
            if value not in {"sale", "cost"}:
                raise InvalidTransition("价格性质只能是售价或成本价")
        elif key == "notes" and value is not None and not isinstance(value, str):
            raise InvalidTransition("备注必须为文本")
        if getattr(offer, field) != value:
            setattr(offer, field, value)
            changed.append(field)
    if offer.no_stock:
        if offer.unit_price is not None:
            offer.unit_price = None
            changed.append("unit_price")
    return changed


def _publishable(db, offer):
    if offer.no_stock and (not offer.notes or not offer.notes.strip()):
        raise InvalidTransition("无货或不报必须填写原因")
    if not offer.no_stock:
        if offer.part_id is None:
            raise InvalidTransition("请先绑定型号，再回复有货报价")
        part = db.get(DimPart, offer.part_id)
        if part is None or part.status != "active":
            raise InvalidTransition("报价型号不存在或已合并")
        if offer.unit_price is None:
            raise InvalidTransition("每行必须填写报价或标记无货")


def save_draft(db, inquiry_id, line_id, actor, data, *, expected_version,
               offer_id=None, work_item_id=None):
    user, ctx = common.resolve_actor(db, actor)
    common.require(ctx, "page_presales")
    inquiry = common.lock_inquiry(db, inquiry_id)
    if inquiry.kind == "proactive_offer":
        common.require(ctx, "action_presales_offer")
        if inquiry.created_by != user.id and not common.bypass(ctx):
            raise Forbidden("只能编辑自己发起的主动报价")
        if inquiry.status != "draft":
            raise InvalidTransition("发布后请直接更新报价")
        task = None
    else:
        common.require(ctx, "action_presales_quote")
        if inquiry.status != "submitted":
            raise InvalidTransition("询价未提交或已撤回、关单")
        task = _task(db, inquiry, actor, user.id, work_item_id)
    line = _line(db, inquiry.id, line_id)
    if not isinstance(data, dict):
        raise InvalidTransition("报价必须为对象")
    data = dict(data)
    if "source_key" not in data and offer_id is None:
        data["source_key"] = f"supplier:{data['supplier_id']}" if data.get("supplier_id") else ""
    source = data.get("source_key", "")
    if not isinstance(source, str) or len(source) > 128:
        raise InvalidTransition("货源标识最多 128 字")
    source = source.strip()
    if "source_key" in data:
        data["source_key"] = source
    _serialize_source(db, user.id, line.part_id, source)
    offer = None
    if offer_id is not None:
        offer = db.scalar(select(PresalesOffer).where(PresalesOffer.id == offer_id)
                          .with_for_update().execution_options(populate_existing=True))
        if offer is None or offer.inquiry_id != inquiry.id or offer.line_id != line.id:
            raise NotFound("草稿报价不存在")
        if offer.created_by != user.id or offer.status != "draft":
            raise Forbidden("只能修改自己的草稿报价")
        common.check_version(offer, expected_version)
    else:
        offer = db.scalar(select(PresalesOffer).where(
            PresalesOffer.inquiry_id == inquiry.id, PresalesOffer.line_id == line.id,
            PresalesOffer.created_by == user.id, PresalesOffer.source_key == source,
            PresalesOffer.status == "draft",
        ).order_by(PresalesOffer.id).with_for_update())
        if offer is not None:
            raise Conflict("该行货源已有草稿，请指定 offer_id 和报价版本进行更新",
                           offer_id=offer.id, version=offer.version)
        common.check_version(inquiry, expected_version)
    if offer is None:
        offer = PresalesOffer(
            inquiry_id=inquiry.id, line_id=line.id, part_id=line.part_id,
            work_item_id=task.id if task else None, created_by=user.id,
            source_key="", status="draft", no_stock=False,
            version=1, revision_no=0,
            supplier_audience_rule_id=common.rule_id(db, None, ctx, default="private"),
        )
        db.add(offer)
    else:
        offer.version += 1
    _apply(db, offer, data, ctx)
    # 页面重新编辑草稿后，旧 Excel 的发布选择须重新确认。
    offer.workbook_choice = {}
    previous = db.scalar(select(PresalesLineResponse).where(
        PresalesLineResponse.line_id == line.id, PresalesLineResponse.created_by == user.id,
        PresalesLineResponse.is_draft.is_(True)))
    if previous is not None:
        # 页面改为填写报价后，不能在回复时继续沿用 Excel 暂存的沿用或不报选择。
        db.delete(previous)
    offer.part_id = line.part_id
    offer.work_item_id = task.id if task else None
    offer.updated_at = common.now()
    db.flush()
    common.event(db, inquiry, actor, "offer.draft_saved", {"offer_id": offer.id})
    common.audit(db, actor, "presales.offer.draft_saved", offer.id)
    return offer


def _publish(db, offer, actor, published_at, reason="发布报价"):
    _publishable(db, offer)
    offer.status = "published"
    offer.valid_from = published_at
    offer.valid_until = None
    offer.version += 1
    offer.updated_at = published_at
    offer.workbook_choice = {}
    append_revision(db, offer, actor, list(PresalesOffer.__table__.columns.keys()), reason)


def _copy_quote(target, source):
    target.part_id = source.part_id
    for field in EDITABLE - {"supplier_audience", "reporter_audience", "notes_audience"}:
        setattr(target, field, getattr(source, field))
    for field in ("supplier_audience_rule_id", "reporter_audience_rule_id", "notes_audience_rule_id"):
        setattr(target, field, getattr(source, field))


def _response(db, line, user_id, task_id, mode, *, ref_offer_id=None, reason=None,
              ref_offer_revision_no=None, is_draft=False):
    row = db.scalar(select(PresalesLineResponse).where(
        PresalesLineResponse.line_id == line.id, PresalesLineResponse.created_by == user_id))
    if row is None:
        row = PresalesLineResponse(line_id=line.id, created_by=user_id)
        db.add(row)
    row.line_version, row.work_item_id = line.version, task_id
    row.mode, row.ref_offer_id, row.reason = mode, ref_offer_id, reason
    row.ref_offer_revision_no = ref_offer_revision_no
    row.is_draft = is_draft
    return row


def _valid_reference(db, line, offer_id, ctx, owner_id=None):
    from app.services.presales import library, visibility

    if owner_id is None:
        owner_id = common.get_inquiry(db, line.inquiry_id).created_by
    if type(offer_id) is not int or line.part_id is None:
        raise InvalidTransition("请选择同 PN 的有效报价")
    offer = next((offer for offer in library._active(db, [line.part_id]) if offer.id == offer_id), None)
    owner_ctx = common.resolve_actor(db, owner_id)[1]
    if (offer is None or not visibility.can_view_offer(db, offer, ctx)
            or visibility.offer_view(db, offer, owner_ctx) is None
            or visibility.pinned_offer_view(db, offer.id, offer.revision_no, owner_ctx) is None):
        raise InvalidTransition("沿用报价已失效、不可见或与本行 PN 不同")
    return offer


def save_line_decision(db, inquiry_id, line_id, actor, *, expected_version, mode,
                       data=None, ref_offer_id=None, reason=None, on_existing=None,
                       existing_offer_id=None, work_item_id=None):
    """保存采购逐行草稿选择；共享回复校验，但不发布报价或完成待办。"""
    user, ctx = common.resolve_actor(db, actor)
    common.require(ctx, "page_presales")
    common.require(ctx, "action_presales_quote")
    inquiry = common.lock_inquiry(db, inquiry_id, expected_version)
    if inquiry.kind != "sales_inquiry" or inquiry.status != "submitted":
        raise InvalidTransition("只能处理已提交的销售询价")
    task = _task(db, inquiry, actor, user.id, work_item_id)
    line = _line(db, inquiry.id, line_id)
    drafts = list(db.scalars(select(PresalesOffer).where(
        PresalesOffer.inquiry_id == inquiry.id, PresalesOffer.line_id == line.id,
        PresalesOffer.created_by == user.id, PresalesOffer.status == "draft",
    ).order_by(PresalesOffer.id).with_for_update().execution_options(populate_existing=True)))
    if mode == "quote":
        if ref_offer_id is not None or reason:
            raise InvalidTransition("报新价不能同时选择沿用或不报")
        if len(drafts) > 1:
            raise Conflict("该行有多个货源草稿，请在页面逐条处理")
        old = drafts[0] if drafts else None
        draft = save_draft(db, inquiry.id, line.id, actor, data or {},
            expected_version=old.version if old else inquiry.version,
            offer_id=old.id if old else None, work_item_id=task.id)
        if draft.no_stock or draft.unit_price is None:
            raise InvalidTransition("报价必须填写单价；无货请选择不报并填写原因")
        targets = _publication_plans(db, [draft], inquiry, ctx, on_existing, existing_offer_id, None)
        target = targets[0][1]
        draft.workbook_choice = ({"on_existing": "update", "existing_offer_id": target.id,
                                  "existing_version": target.version} if target else
                                 {"on_existing": "new"} if on_existing == "new" else {})
        response = db.scalar(select(PresalesLineResponse).where(
            PresalesLineResponse.line_id == line.id, PresalesLineResponse.created_by == user.id))
        if response is not None:
            db.delete(response)
        db.flush()
        return {"line_id": line.id, "mode": "quote", "offer_id": draft.id}
    if mode == "reuse":
        if reason:
            raise InvalidTransition("沿用报价不能同时填写不报原因")
        if type(ref_offer_id) is not int:
            raise InvalidTransition("沿用必须填写报价编号")
        db.scalar(select(PresalesOffer).where(PresalesOffer.id == ref_offer_id).with_for_update()
                  .execution_options(populate_existing=True))
        reference = _valid_reference(db, line, ref_offer_id, ctx, inquiry.created_by)
        _response(db, line, user.id, task.id, mode, ref_offer_id=reference.id,
                  ref_offer_revision_no=reference.revision_no, is_draft=True)
    elif mode == "skip":
        if ref_offer_id is not None or not isinstance(reason, str) or not reason.strip():
            raise InvalidTransition("无货或不报必须填写原因，不能同时沿用报价")
        _response(db, line, user.id, task.id, mode, reason=reason.strip(), is_draft=True)
    else:
        raise InvalidTransition("请选择报价、沿用或不报")
    for draft in drafts:
        db.delete(draft)
    db.flush()
    common.event(db, inquiry, actor, "offer.decision_saved", {"line_id": line.id, "mode": mode})
    common.audit(db, actor, "presales.offer.decision_saved", line.id)
    return {"line_id": line.id, "mode": mode}


def reply(db, inquiry_id, actor, *, expected_version, work_item_id=None,
          expected_work_item_version=None, on_existing=None, existing_offer_id=None,
          existing_choices=None, line_responses=None):
    user, ctx = common.resolve_actor(db, actor)
    common.require(ctx, "page_presales")
    common.require(ctx, "action_presales_quote")
    inquiry = common.lock_inquiry(db, inquiry_id, expected_version)
    if inquiry.kind != "sales_inquiry" or inquiry.status != "submitted":
        raise InvalidTransition("只能回复已提交的销售询价")
    task = _task(db, inquiry, actor, user.id, work_item_id)
    if expected_work_item_version is not None:
        common.check_version(task, expected_work_item_version)
    lines = list(db.scalars(select(PresalesInquiryLine).where(
        PresalesInquiryLine.inquiry_id == inquiry.id, PresalesInquiryLine.status == "active",
        PresalesInquiryLine.ref_offer_id.is_(None),
    ).order_by(PresalesInquiryLine.id).with_for_update()))
    own = list(db.scalars(select(PresalesOffer).where(
        PresalesOffer.inquiry_id == inquiry.id, PresalesOffer.created_by == user.id,
        PresalesOffer.line_id.in_([line.id for line in lines]),
        PresalesOffer.status.in_(["draft", "published"]),
    ).order_by(PresalesOffer.id).with_for_update().execution_options(populate_existing=True)))
    by_line = {line.id: line for line in lines}
    previous = list(db.scalars(select(PresalesLineResponse).where(
        PresalesLineResponse.line_id.in_(by_line), PresalesLineResponse.created_by == user.id)))
    previous_by_line = {row.line_id: row for row in previous}
    def unchanged_reuse(line_id, data):
        row = previous_by_line.get(line_id)
        return (row is not None and not row.is_draft and row.line_version == by_line[line_id].version
                and row.mode == data["mode"] == "reuse" and row.ref_offer_id == data.get("ref_offer_id")
                and row.ref_offer_revision_no is not None)
    def validate_reuse(line, data):
        if unchanged_reuse(line.id, data):
            return
        # Excel 只保存待回复选择；正式回复前重新检查有效期、受众和版本。
        reference = db.scalar(select(PresalesOffer).where(PresalesOffer.id == data.get("ref_offer_id"))
                              .with_for_update().execution_options(populate_existing=True))
        reference = _valid_reference(db, line, data.get("ref_offer_id"), ctx, inquiry.created_by)
        previous_response = previous_by_line.get(line.id)
        if (previous_response is not None and previous_response.is_draft
                and previous_response.ref_offer_id == reference.id
                and previous_response.ref_offer_revision_no != reference.revision_no):
            raise Conflict("Excel 选择的沿用报价已更新，请重新选择")
    decisions = {}
    for data in line_responses or []:
        if (not isinstance(data, dict) or data.get("line_id") not in by_line
                or data["line_id"] in decisions or data.get("mode") not in {"quote", "reuse", "skip"}):
            raise InvalidTransition("行处理选择无效或重复")
        line = by_line[data["line_id"]]
        mode = data["mode"]
        if mode == "reuse":
            if data.get("reason"):
                raise InvalidTransition("沿用报价不能同时填写不报原因")
            validate_reuse(line, data)
        elif mode == "skip":
            if data.get("ref_offer_id") is not None or not isinstance(data.get("reason"), str) or not data["reason"].strip():
                raise InvalidTransition("无货或不报必须填写原因")
        elif data.get("ref_offer_id") is not None or data.get("reason"):
            raise InvalidTransition("报新价不能同时选择沿用或不报")
        decisions[line.id] = data
    for row in previous:
        if row.line_id not in decisions and row.line_version == by_line[row.line_id].version:
            # 旧沿用没有钉住版本时不能猜成当前价，必须由采购显式重选。
            if row.mode == "reuse" and row.ref_offer_revision_no is None:
                continue
            decisions[row.line_id] = {"mode": row.mode, "ref_offer_id": row.ref_offer_id, "reason": row.reason}
            if row.mode == "reuse":
                validate_reuse(by_line[row.line_id], decisions[row.line_id])
    quoted = {offer.line_id for offer in own}
    if any(line.id not in quoted and decisions.get(line.id, {}).get("mode", "quote") == "quote"
           for line in lines):
        raise InvalidTransition("每行必须填写报价、沿用有效报价或说明不报原因")
    drafts = [offer for offer in own if offer.status == "draft"
              and decisions.get(offer.line_id, {}).get("mode", "quote") == "quote"]
    part_ids = {line.id: line.part_id for line in lines}
    for offer in drafts:
        offer.part_id = part_ids[offer.line_id]
        _publishable(db, offer)
    plans = _publication_plans(db, drafts, inquiry, ctx, on_existing, existing_offer_id, existing_choices)
    # 先验证整单，再落处理结果与报价，任何一行错误都不会完成待办。
    for row in previous:
        if decisions.get(row.line_id, {}).get("mode", "quote") == "quote":
            db.delete(row)
    for line_id, data in decisions.items():
        if data["mode"] != "quote" and not unchanged_reuse(line_id, data):
            _response(db, by_line[line_id], user.id, task.id, data["mode"],
                      ref_offer_id=data.get("ref_offer_id"),
                      ref_offer_revision_no=(db.get(PresalesOffer, data["ref_offer_id"]).revision_no
                                             if data["mode"] == "reuse" else None),
                      reason=data["reason"].strip() if data["mode"] == "skip" else None)
    for offer in own:
        if offer.status == "draft" and decisions.get(offer.line_id, {}).get("mode", "quote") != "quote":
            db.delete(offer)
    published_at = common.now()
    published = [_publish_choice(db, draft, target, inquiry, actor, published_at, task.id)
                 for draft, target in plans]
    db.flush()
    work_items.complete(db, task.id, actor, expected_version=task.version)
    db.flush()
    common.event(db, inquiry, actor, "offer.replied", {"offer_ids": [o.id for o in published]})
    common.audit(db, actor, "presales.offer.replied", inquiry.id)
    current = {offer.id: offer for offer in own if offer.status == "published"}
    current.update({offer.id: offer for offer in published})
    return list(current.values())


def _editable(db, offer_id, actor, expected_version):
    user, ctx = common.resolve_actor(db, actor)
    common.require(ctx, "page_presales")
    initial = db.get(PresalesOffer, offer_id)
    if initial is None:
        raise NotFound("报价不存在")
    inquiry = common.lock_inquiry(db, initial.inquiry_id)
    _serialize_source(db, initial.created_by, initial.part_id, initial.source_key)
    offer = db.scalar(select(PresalesOffer).where(PresalesOffer.id == offer_id)
                      .with_for_update().execution_options(populate_existing=True))
    common.check_version(offer, expected_version)
    common.require(ctx, "action_presales_offer" if inquiry.kind == "proactive_offer" else "action_presales_quote")
    if offer.created_by != user.id and not common.bypass(ctx):
        raise Forbidden("只能修改自己的报价")
    if inquiry.status != "submitted":
        raise InvalidTransition("询价已撤回或关单，不能更新报价")
    if offer.status != "published":
        raise InvalidTransition("只能更新或撤销已发布报价")
    return offer, inquiry, ctx


def update_offer(db, offer_id, actor, data, *, expected_version):
    offer, inquiry, ctx = _editable(db, offer_id, actor, expected_version)
    if not isinstance(data, dict):
        raise InvalidTransition("报价必须为对象")
    data = dict(data)
    old_source, old_supplier = offer.source_key, offer.supplier_id
    implicit_source = f"supplier:{old_supplier}" if old_supplier else ""
    if "supplier_id" in data and "source_key" not in data and old_source == implicit_source:
        data["source_key"] = f"supplier:{data['supplier_id']}" if data["supplier_id"] else ""
    changed = _apply(db, offer, data, ctx)
    _publishable(db, offer)
    published_at = common.now()
    offer.valid_from = published_at
    offer.valid_until = None
    offer.updated_at = published_at
    offer.version += 1
    append_revision(db, offer, actor, changed + ["valid_from"], "更新报价")
    common.event(db, inquiry, actor, "offer.updated", {"offer_id": offer.id})
    common.audit(db, actor, "presales.offer.updated", offer.id)
    return offer


def withdraw(db, offer_id, actor, reason, *, expected_version):
    if not isinstance(reason, str) or not reason.strip():
        raise InvalidTransition("撤销报价必须填写原因")
    offer, inquiry, _ = _editable(db, offer_id, actor, expected_version)
    offer.status = "withdrawn"
    offer.version += 1
    offer.updated_at = common.now()
    append_revision(db, offer, actor, ["status"], reason.strip())
    common.event(db, inquiry, actor, "offer.withdrawn", {"offer_id": offer.id})
    common.audit(db, actor, "presales.offer.withdrawn", offer.id)
    return offer


def _notice_targets(db, inquiry, user_ids):
    from app.services.presales.visibility import can_view_inquiry

    if user_ids is None:
        return []
    if not isinstance(user_ids, list) or any(type(value) is not int or value < 1 for value in user_ids):
        raise InvalidTransition("通知对象必须为账号 ID 列表")
    targets = list(dict.fromkeys(user_ids))
    # 只按发布后的准入判定，不让草稿状态本身挡住合法销售；验证不落库。
    original_status = inquiry.status
    original_submitted_at = inquiry.submitted_at
    inquiry.status = "submitted"
    inquiry.submitted_at = common.now()
    try:
        for target in targets:
            _, ctx = common.resolve_actor(db, target)
            if not can_view_inquiry(db, inquiry, ctx):
                raise InvalidTransition("通知对象无权查看该主动报价")
    finally:
        inquiry.status = original_status
        inquiry.submitted_at = original_submitted_at
    return targets


def _lock_merge_inquiry(db, inquiry_id):
    # 发布者已经持有新单和 PN 锁；旧单用 NOWAIT 防止与关单、直接更新形成反向锁序。
    # 显式刷新后使用同连接保存点，避免释放内部保存点触发 M4 的 Session 提交保护。
    db.flush()
    try:
        with db.connection().begin_nested():
            return db.scalar(select(PresalesInquiry).where(PresalesInquiry.id == inquiry_id)
                .with_for_update(nowait=True).execution_options(populate_existing=True))
    except OperationalError as exc:
        if getattr(exc.orig, "sqlstate", None) == "55P03":
            raise Conflict("原报价单正在处理，请刷新后重试") from exc
        raise


def _publication_plans(db, drafts, inquiry, ctx, on_existing, existing_offer_id, existing_choices):
    from app.services.presales import library, visibility

    if on_existing not in {None, "update", "new"}:
        raise InvalidTransition("重复报价必须选择更新或另报一条")
    choices = existing_choices or {}
    if not isinstance(choices, dict) or any(int(key) not in {row.id for row in drafts} for key in choices):
        raise InvalidTransition("重复报价选择不属于本次发布的草稿")
    for owner_id, part_id in sorted({(row.created_by, row.part_id or 0) for row in drafts}):
        _serialize_source(db, owner_id, part_id or None, "")
    plans, updating = [], set()
    for draft in drafts:
        choice = choices.get(draft.id, choices.get(str(draft.id), {}))
        if not isinstance(choice, dict) or set(choice) - {"on_existing", "existing_offer_id"}:
            raise InvalidTransition("重复报价选择无效")
        action = choice.get("on_existing", on_existing)
        target_id = choice.get("existing_offer_id", existing_offer_id)
        saved = draft.workbook_choice or {}
        use_saved = not choice and on_existing is None and existing_offer_id is None
        if use_saved:
            action, target_id = saved.get("on_existing"), saved.get("existing_offer_id")
        if action not in {None, "update", "new"}:
            raise InvalidTransition("重复报价必须选择更新或另报一条")
        candidates = [row for row in library._active(db, [draft.part_id])
                      if row.created_by == draft.created_by] if draft.part_id and not draft.no_stock else []
        if not candidates:
            if action == "update":
                raise Conflict("原报价已失效或变化，请刷新后重新选择")
            plans.append((draft, None))
            continue
        if action is None or (action == "update" and target_id is None and len(candidates) != 1):
            raise Conflict("你已有有效报价，请选择更新那条或另报一条", code="existing_offer",
                           draft_offer_id=draft.id, line_id=draft.line_id,
                           existing_offers=visibility.offer_views(db, candidates, ctx))
        if action == "new":
            plans.append((draft, None))
            continue
        target = next((row for row in candidates if row.id == target_id), None) if target_id else candidates[0]
        if target is None:
            raise InvalidTransition("只能更新本人对同一 PN 的有效报价")
        if target.id in updating:
            raise InvalidTransition("同一有效报价不能在一批中被更新两次，请分别选择或另报一条")
        origin = _lock_merge_inquiry(db, target.inquiry_id)
        if origin is None or origin.status != "submitted":
            raise InvalidTransition("原报价单已撤回或关单，请另报一条")
        target = db.scalar(select(PresalesOffer).where(PresalesOffer.id == target.id)
                           .with_for_update().execution_options(populate_existing=True))
        # 旧单锁取得后重新核验，避免等待时旧单撤回使报价变更失效。
        if target.inquiry_id != origin.id or not common.offer_effective(target):
            raise Conflict("原报价已变化，请刷新后重试")
        if use_saved and saved.get("existing_version") is not None and target.version != saved["existing_version"]:
            raise Conflict("Excel 选择的原报价已更新，请重新选择更新或另报")
        updating.add(target.id)
        plans.append((draft, target))
    return plans


def _publish_choice(db, draft, target, inquiry, actor, published_at, task_id=None):
    if target is None:
        draft.work_item_id = task_id
        _publish(db, draft, actor, published_at)
        return draft
    # 报价归属迁至本次单据，整单 M8 立即使用最新规则；旧单只保留实时受控关联。
    old_line = db.get(PresalesInquiryLine, target.line_id)
    if old_line is not None and old_line.id != draft.line_id:
        if db.get(PresalesOfferLineLink, (old_line.id, target.id)) is None:
            db.add(PresalesOfferLineLink(line_id=old_line.id, offer_id=target.id,
                                        offer_revision_no=target.revision_no))
        _response(db, old_line, target.created_by, target.work_item_id, "reuse", ref_offer_id=target.id,
                  ref_offer_revision_no=target.revision_no)
    _copy_quote(target, draft)
    target.inquiry_id, target.line_id, target.work_item_id = inquiry.id, draft.line_id, task_id
    _publish(db, target, actor, published_at, "采购明确选择更新原报价，按本次单据覆盖受众")
    db.delete(draft)
    return target


def publish_proactive(db, inquiry_id, actor, *, expected_version, notice_user_ids=None,
                      on_existing=None, existing_offer_id=None, existing_choices=None):
    user, ctx = common.resolve_actor(db, actor)
    common.require(ctx, "page_presales")
    common.require(ctx, "action_presales_offer")
    inquiry = common.lock_inquiry(db, inquiry_id, expected_version)
    if inquiry.kind != "proactive_offer" or inquiry.status != "draft":
        raise InvalidTransition("只能发布主动报价草稿单")
    if inquiry.created_by != user.id and not common.bypass(ctx):
        raise Forbidden("只能发布自己的主动报价")
    targets = _notice_targets(db, inquiry, notice_user_ids)
    lines = list(db.scalars(select(PresalesInquiryLine).where(
        PresalesInquiryLine.inquiry_id == inquiry.id, PresalesInquiryLine.status == "active",
    ).order_by(PresalesInquiryLine.id).with_for_update()))
    drafts = list(db.scalars(select(PresalesOffer).where(
        PresalesOffer.inquiry_id == inquiry.id, PresalesOffer.status == "draft",
        PresalesOffer.created_by == user.id, PresalesOffer.line_id.in_([line.id for line in lines]),
    ).order_by(PresalesOffer.part_id, PresalesOffer.source_key, PresalesOffer.id)
       .with_for_update().execution_options(populate_existing=True)))
    if not lines or {line.id for line in lines} - {offer.line_id for offer in drafts}:
        raise InvalidTransition("每行必须填写报价或标记无货")
    part_ids = {line.id: line.part_id for line in lines}
    for offer in drafts:
        offer.part_id = part_ids[offer.line_id]
        _publishable(db, offer)
    plans = _publication_plans(db, drafts, inquiry, ctx, on_existing, existing_offer_id, existing_choices)
    published_at = common.now()
    result = [_publish_choice(db, draft, target, inquiry, actor, published_at)
              for draft, target in plans]
    inquiry.status = "submitted"
    inquiry.submitted_at = published_at
    inquiry.version += 1
    inquiry.updated_at = published_at
    db.flush()
    if targets:
        from app.services.presales.registration import register_subjects
        register_subjects()
        work_items.open_items(db, Subject("presales_offer_notice", inquiry.id),
                              targets, "presales_offer_notice", actor)
    common.event(db, inquiry, actor, "offer.published", {"offer_ids": [o.id for o in result]})
    common.audit(db, actor, "presales.offer.published", inquiry.id)
    return result

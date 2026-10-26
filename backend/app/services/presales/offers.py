"""报价草稿、独立回复和只追加的发布历史；事务由调用方持有。"""

import hashlib
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app.models.dimensions import DimPart, DimSupplier
from app.models.presales import PresalesInquiry, PresalesInquiryLine, PresalesOffer, PresalesOfferRevision
from app.services.presales import common
from app.services.workflow import work_items
from app.services.workflow.registry import Conflict, Forbidden, InvalidTransition, NotFound, Subject

SHANGHAI = ZoneInfo("Asia/Shanghai")
EDITABLE = {
    "unit_price", "is_tax_inclusive", "no_stock", "qty_available", "stock_location",
    "lead_time", "source_key", "supplier_id", "validity_days", "notes",
    "supplier_audience", "reporter_audience", "notes_audience",
}


def offer_valid_until(published_at: datetime, days: int) -> datetime:
    """上海自然日末尾转换为 UTC；截止瞬间本身已失效。"""
    if published_at.tzinfo is None or published_at.utcoffset() is None:
        raise ValueError("published_at 必须包含时区")
    if type(days) is not int or days < 1:
        raise ValueError("有效天数必须为正整数")
    day = published_at.astimezone(SHANGHAI).date() + timedelta(days=days - 1)
    return datetime.combine(day, time.max, SHANGHAI).astimezone(timezone.utc)


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
    # 同一报价人、PN 的货源修改也须与跨单发布串行，防止两个货源撞成同一条。
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
        elif key == "validity_days":
            if type(value) is not int or not 1 <= value <= 36500:
                raise InvalidTransition("有效天数必须为 1–36500 的整数")
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
            source_key="", status="draft", no_stock=False, validity_days=1,
            version=1, revision_no=0,
            supplier_audience_rule_id=common.rule_id(db, None, ctx, default="private"),
        )
        db.add(offer)
    else:
        offer.version += 1
    _apply(db, offer, data, ctx)
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
    offer.valid_until = offer_valid_until(published_at, offer.validity_days)
    offer.version += 1
    offer.updated_at = published_at
    append_revision(db, offer, actor, list(PresalesOffer.__table__.columns.keys()), reason)


def _copy_quote(target, source):
    target.part_id = source.part_id
    for field in EDITABLE - {"supplier_audience", "reporter_audience", "notes_audience"}:
        setattr(target, field, getattr(source, field))
    for field in ("supplier_audience_rule_id", "reporter_audience_rule_id", "notes_audience_rule_id"):
        setattr(target, field, getattr(source, field))


def reply(db, inquiry_id, actor, *, expected_version, work_item_id=None,
          expected_work_item_version=None):
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
    if {line.id for line in lines} - {offer.line_id for offer in own}:
        raise InvalidTransition("每行必须填写报价或标记无货")
    # 先验证所有行，避免调用方捕获异常后错误地提交了一半回复。
    drafts = [offer for offer in own if offer.status == "draft"]
    part_ids = {line.id: line.part_id for line in lines}
    for offer in drafts:
        offer.part_id = part_ids[offer.line_id]
        _publishable(db, offer)
    published_at = common.now()
    current = {(offer.line_id, offer.source_key): offer for offer in own if offer.status == "published"}
    published = []
    for offer in drafts:
        key = (offer.line_id, offer.source_key)
        existing = current.get(key)
        target = existing or offer
        if existing is not None:
            _copy_quote(existing, offer)
            db.delete(offer)
        target.work_item_id = task.id
        _publish(db, target, actor, published_at)
        current[key] = target
        published.append(target)
    work_items.complete(db, task.id, actor, expected_version=task.version)
    db.flush()
    common.event(db, inquiry, actor, "offer.replied", {"offer_ids": [o.id for o in published]})
    common.audit(db, actor, "presales.offer.replied", inquiry.id)
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
    if offer.source_key != old_source or offer.supplier_id != old_supplier:
        duplicate = db.scalar(select(PresalesOffer.id).where(
            PresalesOffer.id != offer.id, PresalesOffer.created_by == offer.created_by,
            PresalesOffer.part_id == offer.part_id, PresalesOffer.source_key == offer.source_key,
            PresalesOffer.status == "published",
        ).limit(1))
        if duplicate is not None:
            raise Conflict("该货源已有同 PN 报价，请更新已有报价", offer_id=duplicate)
    _publishable(db, offer)
    published_at = common.now()
    offer.valid_from = published_at
    offer.valid_until = offer_valid_until(published_at, offer.validity_days)
    offer.updated_at = published_at
    offer.version += 1
    append_revision(db, offer, actor, changed + ["valid_from", "valid_until"], "更新报价")
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
    try:
        with db.begin_nested():
            return db.scalar(select(PresalesInquiry).where(PresalesInquiry.id == inquiry_id)
                .with_for_update(nowait=True).execution_options(populate_existing=True))
    except OperationalError as exc:
        if getattr(exc.orig, "sqlstate", None) == "55P03":
            raise Conflict("原报价单正在处理，请刷新后重试") from exc
        raise


def publish_proactive(db, inquiry_id, actor, *, expected_version, notice_user_ids=None):
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
    drafts.sort(key=lambda offer: (offer.created_by, offer.part_id or 0, offer.source_key, offer.id))
    for owner_id, part_id in dict.fromkeys((offer.created_by, offer.part_id) for offer in drafts):
        _serialize_source(db, owner_id, part_id, "")
    published_at = common.now()
    result = []
    published_by_source = {}
    for draft in drafts:
        key = (draft.created_by, draft.part_id, draft.source_key)
        existing = published_by_source.get(key) if draft.part_id is not None else None
        if existing is None and draft.part_id is not None:
            existing = db.scalar(select(PresalesOffer).join(
                PresalesInquiry, PresalesInquiry.id == PresalesOffer.inquiry_id,
            ).where(
                PresalesOffer.created_by == draft.created_by,
                PresalesOffer.part_id == draft.part_id, PresalesOffer.source_key == draft.source_key,
                PresalesOffer.status == "published", PresalesOffer.id != draft.id,
                PresalesInquiry.kind == "proactive_offer", PresalesInquiry.status == "submitted",
            ).order_by(PresalesOffer.id).with_for_update(of=PresalesOffer)
               .execution_options(populate_existing=True))
            if existing is not None:
                origin = _lock_merge_inquiry(db, existing.inquiry_id)
                if origin is None or origin.kind != "proactive_offer" or origin.status != "submitted":
                    existing = None
        if existing is not None:
            _copy_quote(existing, draft)
            _publish(db, existing, actor, published_at, "同 PN 同货源重复报价，更新原报价")
            line = next(line for line in lines if line.id == draft.line_id)
            line.ref_offer_id = existing.id
            db.delete(draft)
            target = existing
        else:
            _publish(db, draft, actor, published_at)
            target = draft
        if draft.part_id is not None:
            published_by_source[key] = target
        if target.id not in {offer.id for offer in result}:
            result.append(target)
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

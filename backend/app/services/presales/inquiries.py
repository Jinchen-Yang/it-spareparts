"""询价单生命周期；待办、时间线和自定义字段仅走通用服务。"""

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select, text

from app.models.dimensions import DimCustomer, DimPart
from app.models.fields_audience import FieldValue
from app.models.presales import (
    PresalesInquiry, PresalesInquiryLine, PresalesLineResponse, PresalesOffer, PresalesOfferLineLink,
)
from app.services import field_pool
from app.services.presales import common
from app.services.workflow import work_items
from app.services.workflow.registry import Conflict, Forbidden, InvalidTransition, Subject

SUBJECT_TYPE = "presales_inquiry"
_LINE_FIELDS = {"line_key", "part_id", "pn_raw", "description", "qty", "require_exact_pn",
                "ref_offer_id", "notes", "notes_audience", "field_values", "expected_delivery",
                "return_defective"}
PRIORITIES = {"normal", "urgent"}
URGE_INTERVAL = timedelta(minutes=30)


def subject(inquiry):
    from app.services.presales.registration import register_subjects
    register_subjects()
    return Subject(SUBJECT_TYPE, inquiry.id)


def lines_for(db, inquiry_id, *, active=True, lock=False):
    query = select(PresalesInquiryLine).where(PresalesInquiryLine.inquiry_id == inquiry_id)
    if active:
        query = query.where(PresalesInquiryLine.status == "active")
    if lock:
        query = query.with_for_update()
    return list(db.scalars(query.order_by(PresalesInquiryLine.seq, PresalesInquiryLine.id)
                           .execution_options(populate_existing=True)))


def _permission(ctx, kind):
    common.require(ctx, "page_presales")
    common.require(ctx, "action_presales_inquire" if kind == "sales_inquiry" else "action_presales_offer")


def _owner(db, inquiry, actor):
    user, ctx = common.resolve_actor(db, actor)
    _permission(ctx, inquiry.kind)
    if inquiry.created_by != user.id and not common.bypass(ctx):
        raise Forbidden("只有发起人或主管可以修改询价单")
    return user, ctx


def _locked(db, inquiry_id, actor, expected_version):
    inquiry = common.lock_inquiry(db, inquiry_id)
    common.check_version(inquiry, expected_version)
    user, ctx = _owner(db, inquiry, actor)
    if inquiry.status == "closed":
        raise InvalidTransition("询价单已关单，请复制为新单")
    return inquiry, user, ctx


def _touch(inquiry):
    inquiry.version += 1
    inquiry.updated_at = common.now()


def _record(db, inquiry, actor, kind, payload=None):
    db.flush()
    common.event(db, inquiry, actor, kind, payload)
    common.audit(db, actor, kind, inquiry.id)
    return inquiry


def _number(db, kind):
    prefix = ("XJ" if kind == "sales_inquiry" else "BJ") + common.now().astimezone(
        ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
    # 同日同类编号在事务内串行分配；唯一约束仍作最后一道保护。
    db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
               {"key": f"presales-number:{prefix}"})
    last = db.scalar(select(PresalesInquiry.inquiry_no).where(
        PresalesInquiry.inquiry_no.like(prefix + "%")).order_by(PresalesInquiry.inquiry_no.desc()).limit(1))
    seq = int(last[len(prefix):]) + 1 if last else 1
    if seq > 999:
        raise InvalidTransition("当日单据编号已满 999，请联系主管")
    return f"{prefix}{seq:03d}"


def _assignees(db, ids):
    if not isinstance(ids, list) or any(type(value) is not int or value < 1 for value in ids):
        raise InvalidTransition("处理人必须是账号 ID 列表")
    result = list(dict.fromkeys(ids))
    for value in result:
        _, ctx = common.resolve_actor(db, value)
        common.require(ctx, "action_presales_quote")
    return result


def _customer(db, value):
    if value is not None and (type(value) is not int or db.get(DimCustomer, value) is None):
        raise InvalidTransition("客户不存在")
    return value


def _text(value, name, limit=None):
    if value is not None and (not isinstance(value, str) or (limit and len(value) > limit)):
        raise InvalidTransition(f"{name}格式无效")
    return value


def _date(value):
    if isinstance(value, str):
        try:
            parsed = date.fromisoformat(value)
            if parsed.isoformat() != value:
                raise ValueError
            return parsed
        except ValueError:
            raise InvalidTransition("期望交期必须为 YYYY-MM-DD 日期") from None
    if value is not None and type(value) is not date:
        raise InvalidTransition("期望交期必须为 YYYY-MM-DD 日期")
    return value


def delivery_value(line, visible_fields):
    """旧 M7 值仅在当前用户可见时兜底；读取不写库。"""
    if line.expected_delivery is not None:
        return line.expected_delivery
    for field in visible_fields:
        if field["name"] == "期望交期" and field["value"]:
            return _date(field["value"])
    return None


def _migrate_delivery(db, line, ctx, *, explicit=False):
    subject = field_pool.Subject(SUBJECT_TYPE, f"line:{line.id}", "line")
    field_pool._lock(db, f"field-values:{SUBJECT_TYPE}:{subject.subject_id}:line")
    values = field_pool.read_values(db, [subject], ctx, common.bypass)[subject.subject_id]
    legacy = [value for value in values if value["name"] == "期望交期"]
    if not explicit:
        line.expected_delivery = delivery_value(line, legacy)
    # 不删除当前账号看不到的旧值，也不删除字段定义。
    ids = [value["field_def_id"] for value in legacy]
    if ids:
        db.execute(delete(FieldValue).where(FieldValue.subject_type == SUBJECT_TYPE,
            FieldValue.subject_id == subject.subject_id, FieldValue.field_def_id.in_(ids)))


def _reference(db, line, ctx, *, allow_new=False):
    if line.ref_offer_id is None:
        line.ref_offer_revision_no = None
        return
    if line.ref_offer_revision_no is not None:
        from app.services.presales.visibility import pinned_offer_view
        view = pinned_offer_view(db, line.ref_offer_id, line.ref_offer_revision_no, ctx)
        if view is None or view["expired"] or view["original_withdrawn"]:
            raise InvalidTransition("只能引用有效且可见的报价版本")
        if line.part_id != view["part_id"]:
            raise InvalidTransition("引用报价的 PN 与询价行不一致")
        return
    if not allow_new:
        raise InvalidTransition("原引用缺少历史版本，请先取消引用再重新选择")
    offer = db.scalar(select(PresalesOffer).where(PresalesOffer.id == line.ref_offer_id)
                      .with_for_update().execution_options(populate_existing=True))
    if offer is None or not common.offer_effective(offer):
        raise InvalidTransition("只能引用有效的报价")
    if line.part_id is None or offer.part_id != line.part_id:
        raise InvalidTransition("引用报价的 PN 与询价行不一致")
    from app.services.presales.visibility import can_view_offer
    if not can_view_offer(db, offer, ctx):
        raise Forbidden("无权引用该报价")
    from app.services.presales.visibility import pinned_offer_view
    if pinned_offer_view(db, offer.id, offer.revision_no, ctx) is None:
        raise InvalidTransition("报价历史版本不可用，请由原报价人更新后重新选择")
    line.ref_offer_revision_no = offer.revision_no


def _signature(line):
    return {"part_id": line.part_id, "pn_raw": line.pn_raw, "qty": str(Decimal(line.qty).normalize()),
            "require_exact_pn": line.require_exact_pn, "return_defective": line.return_defective,
            "has_reference": line.ref_offer_id is not None}


def _snapshot(db, inquiry):
    return {"lines": {line.line_key: _signature(line) for line in lines_for(db, inquiry.id)},
            "assignee_ids": list(inquiry.assignee_ids)}


def _save_fields(db, inquiry, values, ctx):
    if values is not None:
        field_pool.save_values(db, field_pool.Subject(SUBJECT_TYPE, f"inquiry:{inquiry.id}", "header"),
                               values, ctx)


def _write_line(db, inquiry, data, seq, ctx, existing=None):
    if not isinstance(data, dict) or set(data) - _LINE_FIELDS:
        raise InvalidTransition("询价行包含未知字段")
    line = existing or PresalesInquiryLine(inquiry_id=inquiry.id, line_key=data.get("line_key") or uuid4().hex,
                                          version=1, status="active")
    if not isinstance(line.line_key, str) or not line.line_key.strip() or len(line.line_key) > 64:
        raise InvalidTransition("line_key 必须为 1–64 字")
    part_id = data.get("part_id", line.part_id)
    if part_id is not None and (type(part_id) is not int or part_id < 1):
        raise InvalidTransition("型号 ID 无效")
    part = db.get(DimPart, part_id) if part_id is not None else None
    if part_id is not None and (part is None or part.status != "active"):
        raise InvalidTransition("型号不存在或已合并")
    pn = data.get("pn_raw", line.pn_raw or (part.pn_std if part else ""))
    if not isinstance(pn, str) or not pn.strip() or len(pn) > 256:
        raise InvalidTransition("请填写 PN 原文（最多 256 字）")
    try:
        raw_qty = data.get("qty", line.qty if line.qty is not None else 1)
        if isinstance(raw_qty, bool):
            raise InvalidOperation
        qty = Decimal(str(raw_qty))
        if (not qty.is_finite() or qty <= 0 or qty > Decimal("99999999999.999")
                or qty != qty.quantize(Decimal("0.001"))):
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        raise InvalidTransition("数量必须是正数，最多 3 位小数") from None
    exact = data.get("require_exact_pn", line.require_exact_pn if line.require_exact_pn is not None else False)
    if type(exact) is not bool:
        raise InvalidTransition("require_exact_pn 必须为布尔值")
    return_defective = data.get("return_defective", line.return_defective if line.return_defective is not None else True)
    if type(return_defective) is not bool:
        raise InvalidTransition("坏件是否返回必须为布尔值")
    line.return_defective = return_defective
    ref = data.get("ref_offer_id", line.ref_offer_id)
    if ref is not None and (type(ref) is not int or ref < 1):
        raise InvalidTransition("引用报价 ID 无效")
    line.seq, line.part_id, line.pn_raw, line.qty = seq, part_id, pn.strip(), qty
    new_reference = existing is None or ref != line.ref_offer_id
    if ref != line.ref_offer_id:
        line.ref_offer_revision_no = None
    line.require_exact_pn, line.ref_offer_id = exact, ref
    line.description = _text(data.get("description", line.description), "描述")
    line.expected_delivery = _date(data.get("expected_delivery", line.expected_delivery))
    line.notes = _text(data.get("notes", line.notes), "备注")
    if "notes_audience" in data:
        line.notes_audience_rule_id = common.rule_id(db, data["notes_audience"], ctx)
    if existing is not None:
        line.version += 1
        line.updated_at = common.now()
    db.add(line)
    _reference(db, line, ctx, allow_new=new_reference)
    db.flush()
    anchor = f"part:{part_id}" if part_id is not None else None
    if existing is None:
        field_pool.template_for(db, SUBJECT_TYPE, "line", ctx, anchor=anchor)
    if data.get("field_values") is not None:
        field_pool.save_values(db, field_pool.Subject(SUBJECT_TYPE, f"line:{line.id}", "line"),
                               data["field_values"], ctx, anchor=anchor)
    _migrate_delivery(db, line, ctx, explicit="expected_delivery" in data)
    return line


def _replace_lines(db, inquiry, data, ctx):
    if not isinstance(data, list):
        raise InvalidTransition("lines 必须为列表")
    existing = {line.line_key: line for line in lines_for(db, inquiry.id, active=False, lock=True)}
    keys = set()
    for seq, entry in enumerate(data, 1):
        if not isinstance(entry, dict):
            raise InvalidTransition("询价行格式无效")
        key = entry.get("line_key")
        if key is not None and (not isinstance(key, str) or key in keys):
            raise InvalidTransition("line_key 无效或重复")
        old = existing.get(key)
        if old is not None and old.status == "deleted":
            raise InvalidTransition("已删除的行不能复用 line_key，请新增一行")
        line = _write_line(db, inquiry, entry, seq, ctx, old)
        if line.line_key in keys:
            raise InvalidTransition("line_key 重复")
        keys.add(line.line_key)
    for key, line in existing.items():
        if key not in keys and line.status == "active":
            line.status = "deleted"
            line.version += 1
    db.flush()


def _priority(value):
    if value not in PRIORITIES:
        raise InvalidTransition("优先级只能是普通或紧急")
    return value


def create_draft(db, actor, *, kind="sales_inquiry", customer_id=None, notes=None, lines=None,
                 assignee_ids=None, offer_audience=None, notes_audience=None, field_values=None,
                 priority="normal"):
    user, ctx = common.resolve_actor(db, actor)
    if kind not in {"sales_inquiry", "proactive_offer"}:
        raise InvalidTransition("单据类型无效")
    _permission(ctx, kind)
    inquiry = PresalesInquiry(kind=kind, inquiry_no=_number(db, kind), customer_id=_customer(db, customer_id),
        status="draft", revision=1, version=1, created_by=user.id, assignee_ids=_assignees(db, assignee_ids or []),
        notes=_text(notes, "备注"), offer_audience_rule_id=common.rule_id(db, offer_audience, ctx, default="public"),
        notes_audience_rule_id=common.rule_id(db, notes_audience, ctx), submitted_snapshot={},
        priority=_priority(priority))
    db.add(inquiry)
    db.flush()
    field_pool.template_for(db, SUBJECT_TYPE, "header", ctx)
    _replace_lines(db, inquiry, lines or [], ctx)
    _save_fields(db, inquiry, field_values, ctx)
    return _record(db, inquiry, user.id, "inquiry.created")


def update_draft(db, inquiry_id, actor, expected_version, *, lines=None, field_values=None, **changes):
    inquiry, user, ctx = _locked(db, inquiry_id, actor, expected_version)
    if inquiry.status not in {"draft", "revising"}:
        raise InvalidTransition("请先撤回再修改询价单")
    if set(changes) - {"customer_id", "notes", "assignee_ids", "offer_audience", "notes_audience", "priority"}:
        raise InvalidTransition("询价单包含未知字段")
    if "priority" in changes:
        inquiry.priority = _priority(changes["priority"])
    if "customer_id" in changes:
        inquiry.customer_id = _customer(db, changes["customer_id"])
    if "notes" in changes:
        inquiry.notes = _text(changes["notes"], "备注")
    if "assignee_ids" in changes:
        inquiry.assignee_ids = _assignees(db, changes["assignee_ids"])
    for input_key, column in (("offer_audience", "offer_audience_rule_id"),
                              ("notes_audience", "notes_audience_rule_id")):
        if input_key in changes:
            setattr(inquiry, column, common.rule_id(db, changes[input_key], ctx))
    if lines is not None:
        _replace_lines(db, inquiry, lines, ctx)
    _save_fields(db, inquiry, field_values, ctx)
    _touch(inquiry)
    return _record(db, inquiry, user.id, "inquiry.updated")


def delete_draft(db, inquiry_id, actor, expected_version):
    inquiry, user, _ = _locked(db, inquiry_id, actor, expected_version)
    if inquiry.status != "draft":
        raise InvalidTransition("只能删除草稿")
    inquiry.status, inquiry.closed_outcome, inquiry.closed_at = "closed", "cancelled", common.now()
    _touch(inquiry)
    return _record(db, inquiry, user.id, "inquiry.deleted")


def _validate_submit(db, inquiry, ctx):
    rows = lines_for(db, inquiry.id)
    if not rows:
        raise InvalidTransition("至少填写一行询价")
    for line in rows:
        _reference(db, line, ctx)
    return rows


def _versions(items):
    return {item.id: item.version for item in items if item.status in work_items.ACTIVE}


def submit(db, inquiry_id, actor, expected_version):
    inquiry, user, ctx = _locked(db, inquiry_id, actor, expected_version)
    if inquiry.kind != "sales_inquiry" or inquiry.status != "draft":
        raise InvalidTransition("只能提交销售询价草稿；主动报价请使用发布")
    rows = _validate_submit(db, inquiry, ctx)
    inquiry.status, inquiry.submitted_at = "submitted", common.now()
    inquiry.submitted_snapshot = _snapshot(db, inquiry)
    if any(line.ref_offer_id is None for line in rows):
        work_items.open_items(db, subject(inquiry), inquiry.assignee_ids, SUBJECT_TYPE, user.id)
    _touch(inquiry)
    return _record(db, inquiry, user.id, "inquiry.submitted")


def withdraw(db, inquiry_id, actor, expected_version):
    inquiry, user, _ = _locked(db, inquiry_id, actor, expected_version)
    if inquiry.kind != "sales_inquiry" or inquiry.status != "submitted":
        raise InvalidTransition("只能撤回已提交的销售询价")
    items = work_items.list_by_subject(db, subject(inquiry), user.id, lock=True)
    work_items.pause_subject(db, subject(inquiry), user.id, expected_versions=_versions(items))
    inquiry.status = "revising"
    _touch(inquiry)
    return _record(db, inquiry, user.id, "inquiry.withdrawn")


def _reopen(db, inquiry, user, items):
    active = {item.assignee_id for item in items if item.status in work_items.ACTIVE}
    latest = {}
    for item in items:
        latest[item.assignee_id] = item
    targets = set(inquiry.assignee_ids) if inquiry.assignee_ids else set(latest) - {None}
    for assignee_id in targets:
        item = latest.get(assignee_id)
        if item is not None and assignee_id not in active and item.status in {"done", "declined"}:
            work_items.reopen(db, item.id, user.id, expected_version=item.version)
            active.add(assignee_id)
    missing = [uid for uid in inquiry.assignee_ids if uid not in active and
               (uid not in latest or latest[uid].status == "cancelled")]
    if missing:
        work_items.open_items(db, subject(inquiry), missing, SUBJECT_TYPE, user.id)
    elif not inquiry.assignee_ids and not active:
        work_items.open_items(db, subject(inquiry), [], SUBJECT_TYPE, user.id)


def resubmit(db, inquiry_id, actor, expected_version):
    inquiry, user, ctx = _locked(db, inquiry_id, actor, expected_version)
    if inquiry.kind != "sales_inquiry" or inquiry.status != "revising":
        raise InvalidTransition("只能重新提交已撤回的销售询价")
    rows = _validate_submit(db, inquiry, ctx)
    # 旧快照没有引用标记；不能仅因升级了签名就重开已经回复的待办。
    previous = {key: {"has_reference": False, "return_defective": True, **signature}
                for key, signature in inquiry.submitted_snapshot.get("lines", {}).items()}
    current = {line.line_key: _signature(line) for line in rows}
    changed = {key for key in previous if key not in current or previous[key] != current[key]}
    added = set(current) - set(previous)
    # 引用切换影响派单，不改变原来的 PN、数量或报价内容。
    stale = {key for key in changed if key not in current or any(
        previous[key].get(field) != current[key][field]
        for field in current[key] if field != "has_reference")}
    all_rows = lines_for(db, inquiry.id, active=False)
    changed_ids = [line.id for line in all_rows if line.line_key in changed]
    if changed_ids:
        # 新版本要重新逐行处理，沿用与不报的旧结果不能冒充本次回复。
        db.execute(delete(PresalesLineResponse).where(PresalesLineResponse.line_id.in_(changed_ids)))
        db.execute(delete(PresalesOfferLineLink).where(PresalesOfferLineLink.line_id.in_(changed_ids)))
    versions = {line.id: line.version for line in rows if line.line_key not in changed}
    if versions:
        for response in db.scalars(select(PresalesLineResponse).where(
                PresalesLineResponse.line_id.in_(versions))):
            response.line_version = versions[response.line_id]
    from app.services.presales.offers import mark_stale
    if stale:
        ids = [line.id for line in all_rows if line.line_key in stale]
        offers = list(db.scalars(select(PresalesOffer).where(PresalesOffer.line_id.in_(ids),
            PresalesOffer.status.in_(("draft", "published"))).order_by(PresalesOffer.id).with_for_update()))
        for offer in offers:
            mark_stale(db, offer, user.id, "询价行删除或 PN、数量、要求 PN 一致、坏件返回条件发生变化")
    items = work_items.list_by_subject(db, subject(inquiry), user.id, lock=True)
    old_assignees = inquiry.submitted_snapshot.get("assignee_ids", [])
    inquiry.status = "submitted"
    if old_assignees != inquiry.assignee_ids:
        work_items.cancel_subject(db, subject(inquiry), user.id, "询价重新指派", expected_versions=_versions(items))
        if any(line.ref_offer_id is None for line in rows):
            work_items.open_items(db, subject(inquiry), inquiry.assignee_ids, SUBJECT_TYPE, user.id)
    else:
        work_items.resume_subject(db, subject(inquiry), user.id, expected_versions=_versions(items))
        needs_reply = added | (changed & set(current))
        if any(line.ref_offer_id is None and (line.line_key in needs_reply or not items)
               for line in rows):
            _reopen(db, inquiry, user, items)
        elif not any(line.ref_offer_id is None for line in rows):
            work_items.cancel_subject(db, subject(inquiry), user.id, "询价行已全部引用报价", expected_versions=_versions(items))
    inquiry.revision += 1
    inquiry.submitted_at, inquiry.submitted_snapshot = common.now(), _snapshot(db, inquiry)
    _touch(inquiry)
    return _record(db, inquiry, user.id, "inquiry.resubmitted", {"changed_line_keys": sorted(changed),
        "added_line_keys": sorted(added), "revision": inquiry.revision})


def append_lines(db, inquiry_id, actor, expected_version, lines):
    inquiry, user, ctx = _locked(db, inquiry_id, actor, expected_version)
    if inquiry.kind != "sales_inquiry" or inquiry.status != "submitted":
        raise InvalidTransition("只能向已提交的销售询价追加行")
    if not isinstance(lines, list) or not lines:
        raise InvalidTransition("追加行不能为空")
    existing = lines_for(db, inquiry.id, active=False, lock=True)
    keys = {line.line_key for line in existing}
    seq = max((line.seq for line in existing), default=0)
    added = []
    for offset, data in enumerate(lines, 1):
        if not isinstance(data, dict) or data.get("line_key") in keys:
            raise InvalidTransition("追加只能新增 line_key")
        line = _write_line(db, inquiry, data, seq + offset, ctx)
        keys.add(line.line_key)
        added.append(line)
    items = work_items.list_by_subject(db, subject(inquiry), user.id, lock=True)
    if any(line.ref_offer_id is None for line in added):
        _reopen(db, inquiry, user, items)
    inquiry.revision += 1
    inquiry.submitted_snapshot = _snapshot(db, inquiry)
    _touch(inquiry)
    return _record(db, inquiry, user.id, "inquiry.lines_appended", {"line_keys": [line.line_key for line in added]})


def close(db, inquiry_id, actor, expected_version, *, outcome, won_offer_id=None):
    inquiry, user, ctx = _locked(db, inquiry_id, actor, expected_version)
    if inquiry.status not in {"submitted", "revising"} or outcome not in {"won", "lost", "cancelled"}:
        raise InvalidTransition("关单状态或结果无效")
    if won_offer_id is not None:
        offer = db.get(PresalesOffer, won_offer_id)
        line = db.get(PresalesInquiryLine, offer.line_id) if offer and offer.line_id is not None else None
        own = (offer is not None and offer.inquiry_id == inquiry.id and
               offer.status == "published" and not offer.no_stock and
               line is not None and line.status == "active")
        linked = False
        if offer is not None and not own:
            rows = lines_for(db, inquiry.id)
            by_id = {row.id: row for row in rows}
            linked = any(row.ref_offer_id == offer.id and row.part_id == offer.part_id for row in rows)
            if not linked and by_id:
                linked = any(response.line_version == by_id[response.line_id].version and
                             by_id[response.line_id].part_id == offer.part_id
                             for response in db.scalars(select(PresalesLineResponse).where(
                                 PresalesLineResponse.line_id.in_(by_id),
                                 PresalesLineResponse.is_draft.is_(False),
                                 PresalesLineResponse.mode == "reuse",
                                 PresalesLineResponse.ref_offer_id == offer.id)))
                if not linked:
                    linked = any(by_id[link.line_id].part_id == offer.part_id
                                 for link in db.scalars(select(PresalesOfferLineLink).where(
                                     PresalesOfferLineLink.line_id.in_(by_id),
                                     PresalesOfferLineLink.offer_id == offer.id)))
            if linked:
                from app.services.presales import library, visibility
                linked = (offer in library._active(db, [offer.part_id]) and
                          visibility.can_view_offer(db, offer, ctx))
        if outcome != "won" or not (own or linked):
            raise InvalidTransition("采纳报价必须是本单有效行内的已发布报价")
        from app.services.presales.visibility import pinned_offer_view
        if pinned_offer_view(db, offer.id, offer.revision_no, ctx) is None:
            raise InvalidTransition("报价历史版本不可用，请由原报价人更新后重新选择")
    items = work_items.list_by_subject(db, subject(inquiry), user.id, lock=True)
    work_items.cancel_subject(db, subject(inquiry), user.id, "询价关单", expected_versions=_versions(items))
    if inquiry.kind == "proactive_offer":
        notices = Subject("presales_offer_notice", inquiry.id)
        notice_items = work_items.list_by_subject(db, notices, user.id, lock=True)
        work_items.cancel_subject(db, notices, user.id, "主动报价关单", expected_versions=_versions(notice_items))
    inquiry.status, inquiry.closed_outcome = "closed", outcome
    inquiry.won_offer_id, inquiry.closed_at = won_offer_id, common.now()
    inquiry.won_offer_revision_no = offer.revision_no if won_offer_id is not None else None
    _touch(inquiry)
    return _record(db, inquiry, user.id, "inquiry.closed", {"outcome": outcome, "won_offer_id": won_offer_id})


def copy_as_new(db, inquiry_id, actor, expected_version):
    inquiry = common.lock_inquiry(db, inquiry_id)
    common.check_version(inquiry, expected_version)
    user, ctx = _owner(db, inquiry, actor)
    source_lines = lines_for(db, inquiry.id)
    payload = [{"part_id": line.part_id, "pn_raw": line.pn_raw, "description": line.description,
        "qty": line.qty, "require_exact_pn": line.require_exact_pn, "notes": line.notes,
        "expected_delivery": line.expected_delivery, "return_defective": line.return_defective}
        for line in source_lines]
    copied = create_draft(db, user.id, kind=inquiry.kind, customer_id=inquiry.customer_id,
        notes=inquiry.notes, lines=payload, assignee_ids=list(inquiry.assignee_ids), priority=inquiry.priority)
    copied.offer_audience_rule_id = inquiry.offer_audience_rule_id
    copied.notes_audience_rule_id = inquiry.notes_audience_rule_id
    for source_line, copied_line in zip(source_lines, lines_for(db, copied.id), strict=True):
        copied_line.notes_audience_rule_id = source_line.notes_audience_rule_id
    _record(db, copied, user.id, "inquiry.copied", {"source_inquiry_id": inquiry.id})
    return copied


def urge(db, inquiry_id, actor):
    """D-0047：发起人催未回复的询价，30 分钟限一次；只记时间线与次数，不改待办。"""
    inquiry = common.lock_inquiry(db, inquiry_id)
    user, ctx = common.resolve_actor(db, actor)
    _permission(ctx, inquiry.kind)
    if inquiry.created_by != user.id:
        raise Forbidden("只有询价发起人可以催办")
    if inquiry.kind != "sales_inquiry" or inquiry.status != "submitted":
        raise InvalidTransition("只能催办已提交的销售询价")
    open_items = [item for item in work_items.subject_items(db, subject(inquiry), user.id)
                  if item.status in work_items.ACTIVE and not item.paused]
    if not open_items:
        raise InvalidTransition("采购已全部回复，无需催办")
    current = common.now()
    if inquiry.last_urged_at is not None and current - inquiry.last_urged_at < URGE_INTERVAL:
        wait = URGE_INTERVAL - (current - inquiry.last_urged_at)
        minutes = max(1, -(-int(wait.total_seconds()) // 60))
        raise Conflict(f"30 分钟内只能催办一次，请 {minutes} 分钟后再催", code="urge_too_soon")
    # 不递增单据版本：采购正在填写的报价以询价版本做并发校验，催办不能让其保存冲突。
    inquiry.urge_count += 1
    inquiry.last_urged_at = current
    return _record(db, inquiry, user.id, "inquiry.urged", {"urge_count": inquiry.urge_count})

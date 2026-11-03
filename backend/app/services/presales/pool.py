"""询价报价共享池（D-0052）：按行读写，只锁单行，谁更新记录谁。"""

from sqlalchemy import and_, case, delete, func, null, or_, select, union_all, update
from sqlalchemy.orm import aliased

from app.models.dimensions import DimCustomer, DimSupplier
from app.models.presales import (
    PresalesInquiry, PresalesInquiryLine, PresalesLineResponse, PresalesOffer, PresalesOfferLineLink,
    PresalesOfferRevision,
)
from app.models.system import SysUser
from app.models.workflow import SubjectEvent
from app.services import field_pool
from app.services.presales import answers, common, inquiries, offers, visibility
from app.services.workflow import work_items
from app.services.workflow.registry import Conflict, InvalidTransition, NotFound

TABS = {"pending", "quoted", "skip", "mine", "all"}
OPEN_STATUSES = ("submitted", "revising")
# 这些条件决定价格；只改备注和交期不影响已有答复。
PRICE_CONDITIONS = {"qty": "数量", "return_defective": "坏件返回", "require_exact_pn": "PN 一致"}

_Requester = aliased(SysUser)
_Answerer = aliased(SysUser)


def _actor(db, actor):
    user, ctx = common.resolve_actor(db, actor)
    common.require(ctx, "page_presales")
    return user, ctx


def _name(user):
    return (user.display_name or user.username) if user is not None else None


def _base_filter():
    return (PresalesInquiryLine.status == "active", PresalesInquiry.kind == "sales_inquiry",
            PresalesInquiry.status != "draft", PresalesInquiry.submitted_at.is_not(None))


def _rows_query():
    customer = func.coalesce(func.nullif(DimCustomer.name_normalized, ""), DimCustomer.name_raw)
    return select(PresalesInquiryLine, PresalesInquiry, PresalesOffer, _Requester, _Answerer, customer,
                  DimSupplier.name_raw).join(
        PresalesInquiry, PresalesInquiry.id == PresalesInquiryLine.inquiry_id,
    ).join(_Requester, _Requester.id == PresalesInquiry.created_by).outerjoin(
        PresalesOffer, PresalesOffer.id == PresalesInquiryLine.current_offer_id,
    ).outerjoin(_Answerer, _Answerer.id == PresalesInquiryLine.answered_by).outerjoin(
        DimCustomer, DimCustomer.id == PresalesInquiry.customer_id,
    ).outerjoin(DimSupplier, DimSupplier.id == PresalesOffer.supplier_id).where(*_base_filter())


def _tab_filter(tab, user_id):
    is_open = PresalesInquiry.status.in_(OPEN_STATUSES)
    return {
        "pending": (PresalesInquiryLine.answer_state == "pending", is_open),
        "quoted": (PresalesInquiryLine.answer_state == "quoted", is_open),
        "skip": (PresalesInquiryLine.answer_state == "skip", is_open),
        "mine": (PresalesInquiry.created_by == user_id,),
        "all": (),
    }[tab]


def _search_filter(q, ctx):
    pattern = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    matches = [PresalesInquiryLine.pn_raw.ilike(pattern, escape="\\"),
               PresalesInquiryLine.description.ilike(pattern, escape="\\"),
               PresalesInquiry.inquiry_no.ilike(pattern, escape="\\"),
               _Requester.display_name.ilike(pattern, escape="\\")]
    if work_items.has_permission(ctx, "data_customer"):
        matches += [DimCustomer.name_raw.ilike(pattern, escape="\\"),
                    DimCustomer.name_normalized.ilike(pattern, escape="\\")]
    return or_(*matches)


def _money(value):
    return None if value is None else format(value, ".2f")


def _quantity(value):
    return None if value is None else format(value, ".3f")


def _latest_by_part(db, part_ids):
    """同 PN 的最新有效价，一次读完当前页，只用于提示和沿用。"""
    if not part_ids:
        return {}
    rows = db.execute(select(PresalesOffer, SysUser).distinct(PresalesOffer.part_id).join(
        SysUser, SysUser.id == func.coalesce(PresalesOffer.updated_by, PresalesOffer.created_by),
    ).where(PresalesOffer.part_id.in_(part_ids), PresalesOffer.status == "published",
            PresalesOffer.no_stock.is_(False), PresalesOffer.unit_price.is_not(None),
            PresalesOffer.valid_from <= common.now(),
    ).order_by(PresalesOffer.part_id, PresalesOffer.valid_from.desc(), PresalesOffer.id.desc()))
    return {offer.part_id: {"offer_id": offer.id, "unit_price": _money(offer.unit_price),
                            "is_tax_inclusive": offer.is_tax_inclusive, "price_basis": offer.price_basis,
                            "reporter": _name(user), "at": offer.valid_from} for offer, user in rows}


def _view(row, user_id, ctx, latest, fields):
    line, inquiry, offer, requester, answerer, customer, supplier = row
    result = {
        "line_id": line.id, "line_version": line.version, "inquiry_id": inquiry.id,
        "inquiry_no": inquiry.inquiry_no, "inquiry_version": inquiry.version, "inquiry_status": inquiry.status,
        "closed_outcome": inquiry.closed_outcome, "priority": inquiry.priority, "submitted_at": inquiry.submitted_at,
        "requester": _name(requester), "is_mine": inquiry.created_by == user_id, "inquiry_notes": inquiry.notes,
        "part_id": line.part_id, "pn": line.pn_raw, "description": line.description, "qty": _quantity(line.qty),
        "require_exact_pn": line.require_exact_pn, "return_defective": line.return_defective, "notes": line.notes,
        "expected_delivery": inquiries.delivery_value(line, fields), "answer_state": line.answer_state,
        "answer_reason": line.answer_reason, "answered_by": _name(answerer), "answered_at": line.answered_at,
        "price": None if offer is None else {
            "offer_id": offer.id, "unit_price": _money(offer.unit_price), "is_tax_inclusive": offer.is_tax_inclusive,
            "price_basis": offer.price_basis, "lead_time": offer.lead_time, "stock_location": offer.stock_location,
            "qty_available": _quantity(offer.qty_available),
            "notes": offer.notes, "supplier_id": offer.supplier_id, "supplier_name": supplier,
        },
        "latest_same_pn": None,
    }
    if latest and line.answer_state != "quoted" and line.part_id in latest:
        result["latest_same_pn"] = latest[line.part_id]
    # 客户名独立于共享池开放，只看系统数据权限。
    if work_items.has_permission(ctx, "data_customer"):
        result["customer_id"], result["customer_name"] = inquiry.customer_id, customer
    return result


def _views(db, rows, user_id, ctx):
    lines = [row[0] for row in rows]
    latest = _latest_by_part(db, {line.part_id for line in lines if line.part_id and line.answer_state != "quoted"})
    subjects = [field_pool.Subject("presales_inquiry", f"line:{line.id}", "line")
                for line in lines if line.expected_delivery is None]
    legacy = field_pool.read_values(db, subjects, ctx, visibility._public)
    return [_view(row, user_id, ctx, latest, legacy.get(f"line:{row[0].id}", [])) for row in rows]


def counts(db, actor):
    user, _ = _actor(db, actor)
    is_open = PresalesInquiry.status.in_(OPEN_STATUSES)

    def state(value):
        return func.count().filter(PresalesInquiryLine.answer_state == value, is_open)
    row = db.execute(select(state("pending"), state("quoted"), state("skip"),
        func.count().filter(PresalesInquiry.created_by == user.id), func.count(),
    ).select_from(PresalesInquiryLine).join(
        PresalesInquiry, PresalesInquiry.id == PresalesInquiryLine.inquiry_id,
    ).where(*_base_filter())).one()
    # 草稿不进池子，只给本人一个入口数。
    drafts = db.scalar(select(func.count()).select_from(PresalesInquiry).where(
        PresalesInquiry.kind == "sales_inquiry", PresalesInquiry.status == "draft",
        PresalesInquiry.created_by == user.id))
    return {**dict(zip(("pending", "quoted", "skip", "mine", "all"), row)), "drafts": drafts}


def list_lines(db, actor, *, tab="pending", q="", page=1, page_size=50):
    user, ctx = _actor(db, actor)
    if tab not in TABS:
        raise InvalidTransition("筛选条件无效")
    query = _rows_query().where(*_tab_filter(tab, user.id))
    if q.strip():
        query = query.where(_search_filter(q, ctx))
    total = db.scalar(select(func.count()).select_from(query.with_only_columns(PresalesInquiryLine.id).subquery()))
    urgent_first = case((PresalesInquiry.priority == "urgent", 0), else_=1)
    # 待报价先到先报；其他筛选看最近变化。
    order = (urgent_first, PresalesInquiry.submitted_at.asc(), PresalesInquiryLine.seq, PresalesInquiryLine.id) \
        if tab == "pending" else (PresalesInquiryLine.updated_at.desc(), PresalesInquiryLine.id.desc())
    rows = db.execute(query.order_by(*order).offset((page - 1) * page_size).limit(page_size)).all()
    return {"items": _views(db, rows, user.id, ctx), "total": total,
            "page": page, "page_size": page_size, "counts": counts(db, actor)}


def line_view(db, line_id, actor):
    user, ctx = _actor(db, actor)
    row = db.execute(_rows_query().where(PresalesInquiryLine.id == line_id)
                     .execution_options(populate_existing=True)).one_or_none()
    if row is None:
        raise NotFound("询价行不存在")
    return _views(db, [row], user.id, ctx)[0]


def _lock(db, line_id, actor, expected_version):
    user, ctx = _actor(db, actor)
    # 和关单保持父单→行的顺序。父单 SHARE 锁允许同单不同行并发写，但与关单互斥。
    inquiry = db.scalar(select(PresalesInquiry).join(
        PresalesInquiryLine, PresalesInquiryLine.inquiry_id == PresalesInquiry.id
    ).where(PresalesInquiryLine.id == line_id).with_for_update(read=True, of=PresalesInquiry)
                        .execution_options(populate_existing=True))
    if inquiry is None or inquiry.kind != "sales_inquiry" or not common.was_submitted(inquiry):
        raise NotFound("询价行不存在或已删除")
    line = db.scalar(select(PresalesInquiryLine).where(
        PresalesInquiryLine.id == line_id, PresalesInquiryLine.inquiry_id == inquiry.id)
                     .with_for_update().execution_options(populate_existing=True))
    if line is None or line.status != "active":
        raise NotFound("询价行不存在或已删除")
    if inquiry.status == "closed":
        raise InvalidTransition("询价已关单，不能修改询价行或重新答复")
    if line.version != expected_version:
        raise Conflict("这一行刚被其他人更新，请核对最新内容", code="version_conflict",
                       version=line.version, latest=line_view(db, line_id, actor))
    return line, inquiry, user, ctx


def _touch(line):
    line.version += 1
    line.updated_at = common.now()


def _linked_revision_ranges(line):
    """本行有效的引用、沿用与跨单迁移关联，从钉住版本到本次读到的源版本。"""
    line_id = line.id
    pins = union_all(
        select(PresalesInquiryLine.ref_offer_id.label("offer_id"),
               PresalesInquiryLine.ref_offer_revision_no.label("revision_no")).where(
            PresalesInquiryLine.id == line_id, PresalesInquiryLine.ref_offer_revision_no.is_not(None)),
        select(PresalesLineResponse.ref_offer_id, PresalesLineResponse.ref_offer_revision_no).join(
            PresalesInquiryLine, PresalesInquiryLine.id == PresalesLineResponse.line_id).where(
            PresalesLineResponse.line_id == line_id, PresalesLineResponse.mode == "reuse",
            PresalesLineResponse.is_draft.is_(False),
            PresalesLineResponse.line_version == PresalesInquiryLine.version),
        select(PresalesOfferLineLink.offer_id, PresalesOfferLineLink.offer_revision_no).where(
            PresalesOfferLineLink.line_id == line_id),
    ).subquery()
    sources = select(pins.c.offer_id, func.min(pins.c.revision_no).label("pin")).group_by(pins.c.offer_id).subquery()
    # 缺钉号不能猜历史版本。只确认这一刻本行实际采用的有效价，或快照明确归属本行的修订。
    adopted = and_(PresalesOffer.id == line.current_offer_id, line.answer_state == "quoted",
                   PresalesOffer.status == "published", PresalesOffer.no_stock.is_(False),
                   PresalesOffer.unit_price.is_not(None), PresalesOffer.valid_from <= func.clock_timestamp())
    first = func.coalesce(sources.c.pin, case((adopted, PresalesOffer.revision_no)))
    own_snapshot = select(PresalesOfferRevision.id).where(
        PresalesOfferRevision.offer_id == PresalesOffer.id, PresalesOfferRevision.snapshot["line_id"].as_integer() == line_id,
        PresalesOfferRevision.snapshot["valid_from"].astext.is_not(None)).exists()
    return select(sources.c.offer_id, first.label("first_revision"),
                  case((first.is_not(None), PresalesOffer.revision_no)).label("last_revision")).join(
        PresalesOffer, PresalesOffer.id == sources.c.offer_id).where(
        PresalesOffer.revision_no > 0, or_(first.is_not(None), own_snapshot))


def _clear_answers(db, line, user, reason, *, keep_offer_id=None, linked_ranges=None):
    """让本行旧答复失效；历史修订和其他行对原报价的引用不变。"""
    if linked_ranges is None:
        linked_ranges = db.execute(_linked_revision_ranges(line))
    for offer_id, first, last in linked_ranges:
        common.event(db, db.get(PresalesInquiry, line.inquiry_id), user.id, "line.price_unlinked",
                     {"line_id": line.id, "offer_id": offer_id, "first_revision": first, "last_revision": last})
    rows = db.scalars(select(PresalesOffer).where(
        PresalesOffer.line_id == line.id, PresalesOffer.status == "published",
    ).order_by(PresalesOffer.id).with_for_update()).all()
    for offer in rows:
        if offer.id != keep_offer_id:
            offers.mark_stale(db, offer, user.id, reason, already_versioned_line=line.id)
    db.execute(delete(PresalesLineResponse).where(
        PresalesLineResponse.line_id == line.id, PresalesLineResponse.is_draft.is_(False)))
    db.execute(delete(PresalesOfferLineLink).where(PresalesOfferLineLink.line_id == line.id))
    line.ref_offer_id, line.ref_offer_revision_no = None, None


def set_price(db, line_id, actor, data, *, expected_version):
    line, inquiry, user, ctx = _lock(db, line_id, actor, expected_version)
    if line.part_id is None:
        raise InvalidTransition("请先绑定型号，再填写报价")
    data = {**data, "no_stock": False}
    current = db.scalar(select(PresalesOffer).where(
        PresalesOffer.line_id == line.id, PresalesOffer.status == "published",
    ).order_by(PresalesOffer.valid_from.desc(), PresalesOffer.id.desc()).limit(1).with_for_update())
    # 发布修订会刷新本行 current_offer_id，必须在此之前确定即将解除的旧关联。
    linked_ranges = db.execute(_linked_revision_ranges(line)).all()
    moment = common.now()
    if current is None:
        current = PresalesOffer(inquiry_id=inquiry.id, line_id=line.id, part_id=line.part_id, created_by=user.id,
                                updated_by=user.id, source_key="", status="draft", no_stock=False, version=1,
                                revision_no=0, is_tax_inclusive=True)
        db.add(current)
        offers._apply(db, current, data, ctx)
        offers._publish(db, current, user.id, moment, "共享池报价", already_versioned_line=line.id)
        kind = "offer.published"
    else:
        changed = offers._apply(db, current, data, ctx)
        offers._publishable(db, current)
        current.part_id, current.updated_by = line.part_id, user.id
        current.valid_from, current.valid_until, current.updated_at = moment, None, moment
        current.version += 1
        offers.append_revision(db, current, user.id, [*changed, "updated_by", "valid_from"], "共享池更新报价",
                               already_versioned_line=line.id)
        kind = "offer.updated"
    offer_id = current.id
    _clear_answers(db, line, user, "同一询价行已有新价", keep_offer_id=offer_id, linked_ranges=linked_ranges)
    _touch(line)
    answers.refresh(db, [line_id])
    common.event(db, inquiry, user.id, kind, {"offer_id": offer_id, "line_id": line_id})
    common.audit(db, user.id, f"presales.pool.{kind}", offer_id)
    return line_view(db, line_id, actor)


def skip(db, line_id, actor, reason, *, expected_version):
    line, inquiry, user, _ = _lock(db, line_id, actor, expected_version)
    reason = reason.strip() if isinstance(reason, str) else ""
    if not reason:
        raise InvalidTransition("不报必须填写原因")
    _clear_answers(db, line, user, f"改为不报：{reason}")
    _touch(line)
    response = offers._response(db, line, user.id, None, "skip", reason=reason)
    response.created_at = common.now()
    answers.refresh(db, [line_id])
    common.event(db, inquiry, user.id, "line.skipped", {"line_id": line_id, "reason": reason})
    common.audit(db, user.id, "presales.pool.line.skipped", line_id)
    return line_view(db, line_id, actor)


def edit_line(db, line_id, actor, data, *, expected_version, confirm_reset=False):
    line, inquiry, user, ctx = _lock(db, line_id, actor, expected_version)
    if "expected_delivery" in data:
        inquiries._migrate_delivery(db, line, ctx)
    changed = {key for key, value in data.items() if getattr(line, key) != value}
    if not changed:
        return line_view(db, line_id, actor)
    conditions = sorted(PRICE_CONDITIONS[key] for key in changed & PRICE_CONDITIONS.keys())
    resets = conditions and line.answer_state != "pending"
    if resets and not confirm_reset:
        raise Conflict("修改后原答复会作废，该行回到待报价，请确认", code="price_reset_required",
                       version=line.version, latest=line_view(db, line_id, actor))
    for key in changed:
        setattr(line, key, data[key])
    if resets:
        _clear_answers(db, line, user, "询价条件已修改：" + "、".join(conditions))
    elif not conditions:
        # 只改备注或交期不改变答复条件；过期答复和私有草稿保留各自的原版本。
        db.execute(update(PresalesLineResponse).where(
            PresalesLineResponse.line_id == line.id, PresalesLineResponse.line_version == line.version,
            PresalesLineResponse.is_draft.is_(False)).values(line_version=line.version + 1))
    _touch(line)
    answers.refresh(db, [line_id])
    common.event(db, inquiry, user.id, "line.edited", {"line_id": line_id, "changed": sorted(changed)})
    common.audit(db, user.id, "presales.pool.line.edited", line_id)
    return line_view(db, line_id, actor)


def create_inquiry(db, actor, *, lines, customer_id=None, priority="normal", notes=None):
    """询价一步提交：不产生草稿和待办。"""
    user, ctx = _actor(db, actor)
    if not lines:
        raise InvalidTransition("至少填写一行询价")
    inquiry = PresalesInquiry(kind="sales_inquiry", inquiry_no=inquiries._number(db, "sales_inquiry"),
        customer_id=inquiries._customer(db, customer_id), status="submitted", revision=1, version=1,
        created_by=user.id, assignee_ids=[], notes=inquiries._text(notes, "备注"),
        priority=inquiries._priority(priority), submitted_at=common.now())
    db.add(inquiry)
    db.flush()
    for seq, entry in enumerate(lines, 1):
        inquiries._write_line(db, inquiry, entry, seq, ctx)
    inquiry.submitted_snapshot = inquiries._snapshot(db, inquiry)
    result = {"inquiry_id": inquiry.id, "inquiry_no": inquiry.inquiry_no, "line_count": len(lines)}
    common.event(db, inquiry, user.id, "inquiry.submitted")
    common.audit(db, user.id, "presales.inquiry.submitted", inquiry.id)
    return result


def history(db, line_id, actor):
    """这一行的价格修订和条件变更，最新在前；任何人可查。"""
    _actor(db, actor)
    line = db.get(PresalesInquiryLine, line_id)
    inquiry = db.get(PresalesInquiry, line.inquiry_id) if line is not None else None
    if line is None or line.status != "active" or inquiry.kind != "sales_inquiry" or not common.was_submitted(inquiry):
        raise NotFound("询价行不存在")
    payload = SubjectEvent.payload
    line_events = (SubjectEvent.subject_type == "presales_inquiry", SubjectEvent.subject_id == inquiry.id,
                   payload["line_id"].as_integer() == line.id)
    # 直接报价仅认修订快照中的原行。外部报价另取实际关联期间的修订，
    # 解除关联后只用当时冻结的版本区间，源单后续改价不会串入本行。
    sources = union_all(
        select(PresalesOffer.id.label("offer_id"), null().label("first_revision"),
               null().label("last_revision")).where(PresalesOffer.line_id == line.id),
        _linked_revision_ranges(line),
        select(payload["offer_id"].as_integer(), payload["first_revision"].as_integer(),
               payload["last_revision"].as_integer()).where(*line_events, SubjectEvent.kind == "line.price_unlinked"),
    ).subquery()
    revision_ids = select(PresalesOfferRevision.id).join(
        sources, sources.c.offer_id == PresalesOfferRevision.offer_id,
    ).where(or_(PresalesOfferRevision.snapshot["line_id"].as_integer() == line.id,
                PresalesOfferRevision.revision_no.between(sources.c.first_revision, sources.c.last_revision)))
    revisions = db.execute(select(PresalesOfferRevision, SysUser).join(
        SysUser, SysUser.id == PresalesOfferRevision.actor,
    ).where(
        PresalesOfferRevision.id.in_(revision_ids),
        PresalesOfferRevision.snapshot["valid_from"].astext.is_not(None),
        PresalesOfferRevision.snapshot["status"].astext != "draft",
    ).order_by(PresalesOfferRevision.id.desc()).limit(100))
    items = [{"kind": "price", "offer_id": revision.offer_id, "revision_no": revision.revision_no,
              "at": revision.created_at, "actor": _name(user), "reason": revision.reason,
              "unit_price": revision.snapshot.get("unit_price"), "status": revision.snapshot.get("status"),
              "is_tax_inclusive": revision.snapshot.get("is_tax_inclusive"),
              "price_basis": revision.snapshot.get("price_basis", "sale")} for revision, user in revisions]
    events = db.execute(select(SubjectEvent, SysUser).join(SysUser, SysUser.id == SubjectEvent.actor).where(
        *line_events, SubjectEvent.kind.in_(("line.edited", "line.skipped")),
    ).order_by(SubjectEvent.id.desc()).limit(100))
    items += [{"kind": event.kind, "at": event.created_at, "actor": _name(user),
               "changed": event.payload.get("changed", []), "reason": event.payload.get("reason")}
              for event, user in events]
    return sorted(items, key=lambda item: item["at"], reverse=True)[:100]

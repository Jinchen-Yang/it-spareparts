"""售前唯一的服务端输出边界；先记录准入，再合并受众与字段组限制。"""

from sqlalchemy import select

from app.models.presales import PresalesInquiry, PresalesInquiryLine, PresalesOffer, PresalesOfferRevision
from app.models.system import SysUser
from app.services import audience, field_pool
from app.services.presales.common import author_name, bypass, resolve_actor
from app.services.workflow import work_items
from app.services.workflow.registry import Forbidden, Subject


def _allowed(db, rule, ctx, author):
    # 记录准入也走与字段相同的 M8 过滤出口。
    return bool(audience.filter_fields(db, [{"record": True}], ctx,
                field_rules={"record": (rule, author_name(db, author))}, bypass=bypass)[0])


def can_view_inquiry(db, inquiry, ctx):
    if isinstance(inquiry, int):
        inquiry = db.get(PresalesInquiry, inquiry)
    if inquiry is None or not work_items.has_permission(ctx, "page_presales"):
        return False
    user, ctx = resolve_actor(db, ctx)
    if bypass(ctx) or inquiry.created_by == user.id:
        return True
    if inquiry.status == "draft":
        return False
    participation = work_items.subject_participation(db, Subject("presales_inquiry", inquiry.id), ctx)
    if participation["assigned"] or participation["unclaimed"]:
        return True
    if inquiry.submitted_at is None:
        return False
    # 指派询价只向参与的采购开放；其他销售可复用已共享的报价。
    shared_role = work_items.has_permission(ctx, "action_presales_inquire") or (
        inquiry.kind == "proactive_offer" and work_items.has_permission(ctx, "action_presales_quote")
    )
    return shared_role and _allowed(db, inquiry.offer_audience_rule_id, ctx, inquiry.created_by)


def _offer_batch(db, offers, ctx, *, fields):
    """一次准入与字段过滤；所有中间索引仅在本次调用内存在。"""
    offers = list(offers)
    if not offers:
        return []
    user, ctx = resolve_actor(db, ctx)
    if not work_items.has_permission(ctx, "page_presales"):
        return []
    inquiries = {row.id: row for row in db.scalars(select(PresalesInquiry).where(
        PresalesInquiry.id.in_({offer.inquiry_id for offer in offers})))}
    owner_ids = {offer.created_by for offer in offers} | {row.created_by for row in inquiries.values()}
    authors = {row.id: row for row in db.scalars(select(SysUser).where(SysUser.id.in_(owner_ids)))}
    bypassed = bypass(ctx)
    shared_sales = work_items.has_permission(ctx, "action_presales_inquire")
    shared_buyer = work_items.has_permission(ctx, "action_presales_quote")
    def shared(inquiry):
        return inquiry.submitted_at is not None and (
            shared_sales or (inquiry.kind == "proactive_offer" and shared_buyer))
    needed = {offer.inquiry_id for offer in offers if offer.inquiry_id in inquiries
        and not bypassed and offer.created_by != user.id and offer.status != "draft"
        and inquiries[offer.inquiry_id].created_by != user.id
        and inquiries[offer.inquiry_id].status != "draft" and not shared(inquiries[offer.inquiry_id])}
    participation = work_items.participation_batch(db, "presales_inquiry", needed, ctx) if needed else {}
    flat, rules, candidates = {}, {}, []
    omitted = {"created_by", "work_item_id", "supplier_audience_rule_id",
               "reporter_audience_rule_id", "notes_audience_rule_id"}
    for index, offer in enumerate(offers):
        inquiry = inquiries.get(offer.inquiry_id)
        if inquiry is None:
            continue
        direct = bypassed or offer.created_by == user.id
        if not direct:
            if offer.status == "draft":
                continue
            direct = inquiry.created_by == user.id
            flags = participation.get(inquiry.id, {})
            if not direct and (inquiry.status == "draft" or not (
                shared(inquiry) or flags.get("assigned") or flags.get("unclaimed")
            )):
                continue
        record_key = (index, "record")
        flat[record_key] = True
        rules[record_key] = (None, None) if direct else (
            inquiry.offer_audience_rule_id, authors[inquiry.created_by].username)
        values = {}
        if fields:
            author = authors[offer.created_by]
            values = {column.name: getattr(offer, column.name) for column in offer.__table__.columns
                      if column.name not in omitted}
            values["reporter"] = {"id": offer.created_by, "name": author.display_name or author.username}
            flat.update({(index, key): value for key, value in values.items()})
            for key, rule_id in (("supplier_id", offer.supplier_audience_rule_id),
                ("source_key", offer.supplier_audience_rule_id),
                ("reporter", offer.reporter_audience_rule_id), ("notes", offer.notes_audience_rule_id)):
                rules[(index, key)] = (rule_id, author.username)
        candidates.append((index, offer, values))
    # tuple 键隔离每条记录的规则，同名字段不会套用另一条报价的受众。
    filtered = audience.filter_fields(db, [flat], ctx, field_rules=rules, bypass=bypass)[0]
    rule_views = audience.read_rules(db, {
        (index, key): (getattr(offer, key + "_rule_id"), authors[offer.created_by].username)
        for index, offer, _ in candidates if (index, "record") in filtered
        for key in ("supplier_audience", "reporter_audience", "notes_audience")
    }, ctx, bypass=bypass) if fields else {}
    result = []
    for index, offer, values in candidates:
        if (index, "record") not in filtered:
            continue
        view = {key: filtered[(index, key)] for key in values if (index, key) in filtered}
        if not work_items.has_permission(ctx, "data_supplier"):
            view.pop("supplier_id", None)
            view.pop("source_key", None)
        for key in ("supplier_audience", "reporter_audience", "notes_audience"):
            if (index, key) in rule_views:
                view[key] = rule_views[(index, key)]
        result.append((offer, view))
    return result


def visible_offers(db, offers, ctx):
    return [offer for offer, _ in _offer_batch(db, offers, ctx, fields=False)]


def offer_views(db, offers, ctx):
    return [view for _, view in _offer_batch(db, offers, ctx, fields=True)]


def can_view_offer(db, offer, ctx):
    return bool(visible_offers(db, [offer], ctx))


def offer_view(db, offer, ctx):
    views = offer_views(db, [offer], ctx)
    return views[0] if views else None


def inquiry_view(db, inquiry, ctx, *, detail=True):
    if not can_view_inquiry(db, inquiry, ctx):
        raise Forbidden("无权查看该询价单")
    user, ctx = resolve_actor(db, ctx)
    owner = inquiry.created_by == user.id
    author = author_name(db, inquiry.created_by)
    omitted = {"submitted_snapshot", "offer_audience_rule_id", "notes_audience_rule_id"}
    result = {col.name: getattr(inquiry, col.name) for col in inquiry.__table__.columns
              if col.name not in omitted}
    result = audience.filter_fields(db, [result], ctx,
                field_rules={"notes": (inquiry.notes_audience_rule_id, author)}, bypass=bypass)[0]
    if not work_items.has_permission(ctx, "data_customer") or (not owner and not bypass(ctx)):
        result.pop("customer_id", None)
    if not owner and not bypass(ctx):
        result.pop("assignee_ids", None)
        result.pop("created_by", None)
    lines = list(db.scalars(select(PresalesInquiryLine).where(
        PresalesInquiryLine.inquiry_id == inquiry.id, PresalesInquiryLine.status == "active",
    ).order_by(PresalesInquiryLine.seq, PresalesInquiryLine.id)))
    offer_models = list(db.scalars(select(PresalesOffer).where(
        PresalesOffer.inquiry_id == inquiry.id,
    ).order_by(PresalesOffer.id)))
    offers = offer_views(db, offer_models, ctx)
    views_by_id = {view["id"]: view for view in offers}
    hidden_reporters = {offer.created_by: offer for offer in offer_models
        if offer.id in views_by_id and "reporter" not in views_by_id[offer.id]}
    if hidden_reporters:
        result.pop("assignee_ids", None)
    result["line_count"] = len(lines)
    result["progress"] = {"quoted_lines": len({offer["line_id"] for offer in offers
        if offer["status"] == "published"}), "line_count": len(lines)}
    if detail:
        result.update(audience.read_rules(db, {
            "offer_audience": (inquiry.offer_audience_rule_id, author),
            "notes_audience": (inquiry.notes_audience_rule_id, author),
        }, ctx, bypass=bypass))
        line_rules = audience.read_rules(db, {
            line.id: (line.notes_audience_rule_id, author) for line in lines
        }, ctx, bypass=bypass)
        header_subject = field_pool.Subject("presales_inquiry", f"inquiry:{inquiry.id}", "header")
        result["field_values"] = field_pool.read_values(db, [header_subject], ctx, bypass, include_rules=True).get(
            header_subject.subject_id, [])
        line_subjects = [field_pool.Subject("presales_inquiry", f"line:{line.id}", "line") for line in lines]
        values = field_pool.read_values(db, line_subjects, ctx, bypass, include_rules=True) if lines else {}
        result["lines"] = []
        for line in lines:
            row = {col.name: getattr(line, col.name) for col in line.__table__.columns
                   if col.name != "notes_audience_rule_id"}
            row = audience.filter_fields(db, [row], ctx,
                field_rules={"notes": (line.notes_audience_rule_id, author)}, bypass=bypass)[0]
            if line.id in line_rules:
                row["notes_audience"] = line_rules[line.id]
            row["field_values"] = values.get(f"line:{line.id}", [])
            row["field_template"] = [{"id": f.id, "name": f.name, "value_type": f.value_type,
                                       "choices": f.choices} for f in field_pool.template_for(
                db, "presales_inquiry", "line", ctx,
                anchor=f"part:{line.part_id}" if line.part_id else None)]
            result["lines"].append(row)
        result["offers"] = offers
        result["work_items"] = []
        for item in work_items.list_by_subject(db, Subject("presales_inquiry", inquiry.id), ctx):
            row = {"id": item.id, "assignee_id": item.assignee_id, "claimed_by": item.claimed_by,
                   "status": item.status, "paused": item.paused, "version": item.version}
            hidden = hidden_reporters.get(item.assignee_id)
            if hidden:
                field_rules = {field: (hidden.reporter_audience_rule_id, author_name(db, hidden.created_by))
                               for field in ("assignee_id", "claimed_by")}
                row = audience.filter_fields(db, [row], ctx, field_rules=field_rules, bypass=bypass)[0]
            result["work_items"].append(row)
    return result


def revision_views(db, offer_id, ctx):
    from types import SimpleNamespace
    from app.services.workflow.registry import NotFound
    offer = db.get(PresalesOffer, offer_id)
    if offer is None:
        raise NotFound("报价不存在")
    user, ctx = resolve_actor(db, ctx)
    inquiry = db.get(PresalesInquiry, offer.inquiry_id)
    if not can_view_offer(db, offer, ctx) or not (
        bypass(ctx) or user.id in {offer.created_by, inquiry.created_by}
    ):
        raise Forbidden("无权查看报价历史")
    current = offer_view(db, offer, ctx)
    result = []
    for revision in db.scalars(select(PresalesOfferRevision).where(
        PresalesOfferRevision.offer_id == offer_id).order_by(PresalesOfferRevision.revision_no)):
        snapshot = SimpleNamespace(**revision.snapshot, __table__=PresalesOffer.__table__)
        view = offer_view(db, snapshot, ctx)
        if view is None:
            continue
        # 当前已改为私密的字段不会经旧的公开快照再次泄漏。
        view = {key: value for key, value in view.items() if key in current}
        visible_fields = set(view)
        changed = [key for key in revision.changed_fields if key in visible_fields]
        row = {"revision_no": revision.revision_no, "snapshot": view,
               "changed_fields": changed, "created_at": revision.created_at}
        # 原因由操作者填写，采用备注的可见性，防止撤销原因成为私密备注旁路。
        reason = audience.filter_fields(db, [{"reason": revision.reason}], ctx,
            field_rules={"reason": (offer.notes_audience_rule_id, author_name(db, revision.actor))}, bypass=bypass)[0]
        row.update(reason)
        result.append(row)
    return result

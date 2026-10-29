"""售前唯一的服务端输出边界；先记录准入，再合并受众与字段组限制。"""

from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import or_, select

from app.models.presales import (
    PresalesInquiry, PresalesInquiryLine, PresalesLineResponse, PresalesOffer, PresalesOfferLineLink,
    PresalesOfferRevision,
)
from app.models.system import SysUser
from app.services import audience, field_pool
from app.services.presales.common import author_name, bypass, offer_effective, resolve_actor
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
    def inquiry_for(offer):
        snapshot = getattr(offer, "_inquiry_snapshot", None)
        if snapshot is not None:
            return snapshot
        if offer.inquiry_id is None:
            # 仅用于复用既有记录/字段权限判断，不创建任何来源业务单据。
            return SimpleNamespace(id=None, kind="proactive_offer", created_by=offer.created_by,
                offer_audience_rule_id=getattr(offer, "offer_audience_rule_id", None),
                status="submitted", submitted_at=offer.valid_from)
        return inquiries.get(offer.inquiry_id)
    owner_ids = {offer.created_by for offer in offers} | {row.created_by for row in inquiries.values()}
    owner_ids.update(inquiry_for(offer).created_by for offer in offers if inquiry_for(offer) is not None)
    authors = {row.id: row for row in db.scalars(select(SysUser).where(SysUser.id.in_(owner_ids)))}
    bypassed = bypass(ctx)
    shared_sales = work_items.has_permission(ctx, "action_presales_inquire")
    shared_buyer = work_items.has_permission(ctx, "action_presales_quote")
    def shared(inquiry):
        return inquiry.submitted_at is not None and (
            shared_sales or (inquiry.kind == "proactive_offer" and shared_buyer))
    needed = {offer.inquiry_id for offer in offers if offer.inquiry_id is not None and inquiry_for(offer) is not None
        and not bypassed and offer.created_by != user.id and offer.status != "draft"
        and inquiry_for(offer).created_by != user.id
        and inquiry_for(offer).status != "draft" and not shared(inquiry_for(offer))}
    participation = work_items.participation_batch(db, "presales_inquiry", needed, ctx) if needed else {}
    flat, rules, candidates = {}, {}, []
    # valid_until / validity_days 已停用（D-0046），不再对外输出。
    omitted = {"created_by", "work_item_id", "supplier_audience_rule_id",
               "reporter_audience_rule_id", "notes_audience_rule_id", "workbook_choice",
               "valid_until", "validity_days", "offer_audience_rule_id"}
    for index, offer in enumerate(offers):
        inquiry = inquiry_for(offer)
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



def _snapshot_offer(revision):
    values = {column.name: revision.snapshot.get(column.name) for column in PresalesOffer.__table__.columns}
    # D-0047 上线前的不可变历史没有价格性质；存量按售价解释，不改写快照。
    values["price_basis"] = revision.snapshot.get("price_basis", "sale")
    for key in ("valid_from", "valid_until", "created_at", "updated_at"):
        if isinstance(values[key], str):
            values[key] = datetime.fromisoformat(values[key])
    for key in ("unit_price", "qty_available"):
        if values[key] is not None:
            values[key] = Decimal(str(values[key]))
    inquiry = revision.snapshot.get("_inquiry")
    if inquiry is not None:
        values["_inquiry_snapshot"] = SimpleNamespace(**inquiry)
    return SimpleNamespace(**values, __table__=PresalesOffer.__table__)


def pinned_offer_view(db, offer_id, revision_no, ctx):
    """旧单只读明确钉住的历史，不把未知版本猜成当前版本。"""
    if offer_id is None or revision_no is None:
        return None
    revision = db.scalar(select(PresalesOfferRevision).where(
        PresalesOfferRevision.offer_id == offer_id, PresalesOfferRevision.revision_no == revision_no))
    current = db.get(PresalesOffer, offer_id)
    if revision is None or current is None or "_inquiry" not in revision.snapshot:
        return None
    snapshot = _snapshot_offer(revision)
    view = offer_view(db, snapshot, ctx)
    if view is None:
        return None
    from app.services.presales.common import offer_effective
    # D-0046：报价直到下一次更新前一直有效。钉住的版本在原报价改价、改税口径、
    # 标无货或失效后即被取代；只改备注或可见范围不算取代。撤回仍由 original_withdrawn 单独表达。
    superseded = current.status != "withdrawn" and (
        not offer_effective(current) or current.unit_price != snapshot.unit_price
        or current.is_tax_inclusive != snapshot.is_tax_inclusive)
    if not superseded and current.revision_no > revision_no:
        later = db.scalars(select(PresalesOfferRevision).where(
            PresalesOfferRevision.offer_id == offer_id,
            PresalesOfferRevision.revision_no > revision_no,
            PresalesOfferRevision.revision_no <= current.revision_no))
        # 曾经改价/改税/无货/失效即已取代；之后恢复旧值不能让旧引用重新生效。
        superseded = any(
            item.status == "stale" or item.no_stock or item.unit_price is None
            or item.unit_price != snapshot.unit_price
            or item.is_tax_inclusive != snapshot.is_tax_inclusive
            for item in (_snapshot_offer(row) for row in later))
    view["expired"] = not offer_effective(snapshot) or superseded
    view["original_withdrawn"] = current.status == "withdrawn"
    if current.revision_no > revision_no:
        latest = offer_view(db, current, ctx)
        if latest is not None:
            view["latest"] = {key: latest[key] for key in ("unit_price", "is_tax_inclusive", "revision_no")}
    return view

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
    responses = list(db.scalars(select(PresalesLineResponse).where(
        PresalesLineResponse.line_id.in_([line.id for line in lines]))))
    responses = [row for row in responses
                 if not row.is_draft or row.created_by == user.id or bypass(ctx)]
    links = list(db.scalars(select(PresalesOfferLineLink).where(
        PresalesOfferLineLink.line_id.in_([line.id for line in lines]))))
    references = {line.ref_offer_id for line in lines if line.ref_offer_id is not None} | {
        row.ref_offer_id for row in responses if row.ref_offer_id is not None} | {link.offer_id for link in links}
    if inquiry.won_offer_id is not None:
        references.add(inquiry.won_offer_id)
    offer_models = list(db.scalars(select(PresalesOffer).where(
        or_(PresalesOffer.inquiry_id == inquiry.id, PresalesOffer.id.in_(references)),
    ).order_by(PresalesOffer.id)))
    offers = offer_views(db, [offer for offer in offer_models if offer.inquiry_id == inquiry.id], ctx)
    pinned = {}
    def read_pin(offer_id, revision_no):
        key = (offer_id, revision_no)
        if key not in pinned:
            pinned[key] = pinned_offer_view(db, offer_id, revision_no, ctx)
        return pinned[key]
    line_references = {line.id: read_pin(line.ref_offer_id, line.ref_offer_revision_no)
                       for line in lines if line.ref_offer_id is not None}
    response_references = {row.id: read_pin(row.ref_offer_id, row.ref_offer_revision_no)
                           for row in responses if row.ref_offer_id is not None}
    link_references = {(link.line_id, link.offer_id): read_pin(link.offer_id, link.offer_revision_no)
                       for link in links}
    views_by_id = {view["id"]: view for view in offers}
    for view in [*line_references.values(),
                 *(response_references[row.id] for row in responses if not row.is_draft and row.id in response_references),
                 *link_references.values()]:
        if view is not None and view["id"] not in views_by_id:
            offers.append(view)
            views_by_id[view["id"]] = view
    hidden_reporters = {offer.created_by: offer for offer in offer_models
        if offer.id in views_by_id and "reporter" not in views_by_id[offer.id]}
    if hidden_reporters and not owner:
        result.pop("assignee_ids", None)
    # 销售询价的发起人是采购处理需求所需的标识；主动报价仍遵守报价人的受众规则。
    if inquiry.kind == "sales_inquiry" or owner or bypass(ctx) or inquiry.created_by not in hidden_reporters:
        creator = db.get(SysUser, inquiry.created_by)
        result["created_by_name"] = creator.display_name or creator.username if creator else None
    result["line_count"] = len(lines)
    line_versions = {line.id: line.version for line in lines}
    handled = {offer["line_id"] for offer in offers
               if offer["status"] == "published" and offer["line_id"] in line_versions}
    handled.update(line_id for line_id, view in line_references.items() if view is not None)
    handled.update(row.line_id for row in responses
                   if not row.is_draft and row.line_version == line_versions[row.line_id]
                   and (owner or bypass(ctx) or row.created_by == user.id))
    handled.update(line_id for (line_id, _), view in link_references.items() if view is not None)
    result["progress"] = {"quoted_lines": len(handled), "line_count": len(lines)}
    # D-0048：历史引用仍是不可变快照；“当前答复”独立读取同一报价的当前权限和状态。
    current_views = {view["id"]: view for view in offer_views(db, offer_models, ctx)}
    current_models = {offer.id: offer for offer in offer_models}
    line_answers = []
    for line in lines:
        visible_responses = [row for row in responses if row.line_id == line.id
            and not row.is_draft and row.line_version == line.version
            and (owner or bypass(ctx) or row.created_by == user.id)]
        ids = {offer.id for offer in offer_models if offer.line_id == line.id}
        if line_references.get(line.id) is not None:
            ids.add(line.ref_offer_id)
        ids.update(row.ref_offer_id for row in visible_responses
                   if row.ref_offer_id is not None and response_references.get(row.id) is not None)
        ids.update(link.offer_id for link in links if link.line_id == line.id
                   and link_references.get((line.id, link.offer_id)) is not None)
        candidates = [current_views[key] for key in ids if key in current_views
                      and offer_effective(current_models[key])]
        answered = line.id in handled
        answer = {"line_id": line.id, "responded": answered}
        if candidates:
            latest = max(candidates, key=lambda row: (row["valid_from"], row["id"]))
            answer.update(state="quoted", offer=latest,
                          reporter=latest.get("reporter", {}).get("name", "采购"))
        else:
            skipped = next((row for row in visible_responses if row.mode == "skip"), None)
            no_stock = next((current_views[key] for key in ids if key in current_views
                and current_models[key].status == "published" and current_models[key].no_stock), None)
            if skipped is not None:
                reporter = db.get(SysUser, skipped.created_by)
                answer.update(state="skip", reason=skipped.reason,
                    reporter=(reporter.display_name or reporter.username) if reporter else "采购")
            elif no_stock is not None:
                answer.update(state="skip", reason=no_stock.get("notes") or "无货",
                    reporter=no_stock.get("reporter", {}).get("name", "采购"))
            else:
                answer["state"] = "unavailable" if answered else "pending"
        line_answers.append(answer)
    result["line_answers"] = line_answers
    result["progress"]["effective_quoted_lines"] = sum(row["state"] == "quoted" for row in line_answers)
    subject_tasks = work_items.list_by_subject(db, Subject("presales_inquiry", inquiry.id), ctx)
    result["can_urge"] = (owner and inquiry.kind == "sales_inquiry" and inquiry.status == "submitted"
                          and work_items.has_permission(ctx, "action_presales_inquire")
                          and any(item.status in work_items.ACTIVE and not item.paused for item in subject_tasks))
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
            from app.services.presales.inquiries import delivery_value
            visible_fields = values.get(f"line:{line.id}", [])
            row["expected_delivery"] = delivery_value(line, visible_fields)
            row["field_values"] = [field for field in visible_fields if field["name"] != "期望交期"]
            if line.id in line_references:
                ref = line_references[line.id]
                row["ref_offer"] = ref
                row["expired"] = ref is None or ref["expired"]
                row["original_withdrawn"] = bool(ref and ref["original_withdrawn"])
            row["linked_offer_ids"] = [link.offer_id for link in links
                if link.line_id == line.id and link_references[(link.line_id, link.offer_id)] is not None]
            row["linked_offers"] = [link_references[(link.line_id, link.offer_id)] for link in links
                if link.line_id == line.id and link_references[(link.line_id, link.offer_id)] is not None]
            row["field_template"] = [{"id": f.id, "name": f.name, "value_type": f.value_type,
                                       "choices": f.choices} for f in field_pool.template_for(
                db, "presales_inquiry", "line", ctx,
                anchor=f"part:{line.part_id}" if line.part_id else None) if f.name != "期望交期"]
            result["lines"].append(row)
        result["offers"] = offers
        if inquiry.won_offer_id is not None:
            result["won_offer"] = read_pin(inquiry.won_offer_id, inquiry.won_offer_revision_no)
        result["line_responses"] = []
        for response in responses:
            if not (owner or bypass(ctx) or response.created_by == user.id):
                continue
            row = {key: getattr(response, key) for key in (
                "line_id", "mode", "created_by", "work_item_id", "reason", "ref_offer_id", "ref_offer_revision_no")}
            if response.is_draft:
                row["is_draft"] = True
            offer = response_references.get(response.id)
            row["expired"] = response.mode == "reuse" and (offer is None or offer["expired"])
            row["original_withdrawn"] = bool(offer and offer["original_withdrawn"])
            if offer is not None:
                row["offer"] = offer
                if response.ref_offer_id in current_views:
                    row["current_offer"] = current_views[response.ref_offer_id]
                owner_view = pinned_offer_view(db, response.ref_offer_id, response.ref_offer_revision_no,
                                              resolve_actor(db, inquiry.created_by)[1])
                row["reporter_label"] = (owner_view or {}).get("reporter", {}).get("name", "其他采购")
            result["line_responses"].append(row)
        result["work_items"] = []
        for item in subject_tasks:
            row = {"id": item.id, "assignee_id": item.assignee_id, "claimed_by": item.claimed_by,
                   "status": item.status, "paused": item.paused, "version": item.version}
            hidden = hidden_reporters.get(item.assignee_id)
            if hidden and not owner:
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
    inquiry = db.get(PresalesInquiry, offer.inquiry_id) if offer.inquiry_id is not None else None
    if not can_view_offer(db, offer, ctx) or not (
        bypass(ctx) or user.id == offer.created_by or (inquiry is not None and user.id == inquiry.created_by)
    ):
        raise Forbidden("无权查看报价历史")
    current = offer_view(db, offer, ctx)
    result = []
    for revision in db.scalars(select(PresalesOfferRevision).where(
        PresalesOfferRevision.offer_id == offer_id).order_by(PresalesOfferRevision.revision_no)):
        snapshot = SimpleNamespace(**{"price_basis": "sale", **revision.snapshot},
                                   __table__=PresalesOffer.__table__)
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

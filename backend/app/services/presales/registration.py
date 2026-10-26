"""将两个售前主体注册到待办引擎；所有业务准入和摘要都在此侧完成。"""

from types import SimpleNamespace

from sqlalchemy.orm import object_session

from sqlalchemy import or_, select

from app.models.presales import PresalesInquiry, PresalesInquiryLine, PresalesOffer
from app.models.system import SysUser
from app.services import audience
from app.services.presales.common import bypass, resolve_actor
from app.services.workflow.registry import InvalidTransition, SubjectSpec, register, registered


def _can_view(db, inquiry_id, ctx):
    from app.services.presales.visibility import can_view_inquiry
    return can_view_inquiry(db, inquiry_id, ctx)


def _summarize(db, inquiry_ids, ctx):
    from app.services.presales.visibility import inquiry_view
    result = {}
    for inquiry_id in inquiry_ids:
        inquiry = db.get(PresalesInquiry, inquiry_id)
        if inquiry and _can_view(db, inquiry_id, ctx):
            view = inquiry_view(db, inquiry, ctx, detail=False)
            result[inquiry_id] = {key: view[key] for key in (
                "inquiry_no", "kind", "status", "customer_id", "line_count", "progress"
            ) if key in view}
    return result


def _filter_events(rows, ctx):
    from app.services.presales.visibility import can_view_offer, offer_view
    result = []
    for row in rows:
        db = object_session(row)
        inquiry = db.get(PresalesInquiry, row.subject_id)
        user, ctx = resolve_actor(db, ctx)
        own = inquiry.created_by == user.id
        if not own and not bypass(ctx):
            # 非发起人只看本人参与事件；共享销售通过报价读取受控内容。
            if row.actor != user.id:
                continue
        payload = row.payload or {}
        actor = row.actor if own or bypass(ctx) else None
        offer_id = payload.get("offer_id")
        if offer_id is not None:
            offer = db.get(PresalesOffer, offer_id)
            if not offer or not can_view_offer(db, offer, ctx):
                continue
            if "reporter" not in offer_view(db, offer, ctx):
                actor = None
        elif actor is not None and actor != inquiry.created_by and not bypass(ctx):
            # 领单/完成事件也可能透露被隐藏的报价人。
            actor = None
        clean = {key: value for key, value in payload.items() if key in {
            "offer_id", "line_id", "revision", "version", "outcome", "added", "changed", "deleted",
        }}
        result.append(SimpleNamespace(**{col.name: getattr(row, col.name) for col in row.__table__.columns
                                        if col.name not in {"actor", "payload", "work_item_id"}},
                                      actor=actor, payload=clean,
                                      work_item_id=row.work_item_id if actor is not None else None,
                                      __table__=row.__table__))
    return result


def _notice_view(db, inquiry_id, ctx):
    from app.services.presales.visibility import can_view_inquiry
    from app.services.workflow import work_items
    from app.services.workflow.registry import Subject
    inquiry = db.get(PresalesInquiry, inquiry_id)
    if not inquiry:
        return False
    user, ctx = resolve_actor(db, ctx)
    if not work_items.has_permission(ctx, "page_presales"):
        return False
    if not can_view_inquiry(db, inquiry, ctx):
        return False
    return bypass(ctx) or inquiry.created_by == user.id or work_items.subject_participation(
        db, Subject("presales_offer_notice", inquiry_id), ctx)["assigned"]


def _filter_notice_items(db, rows, ctx):
    if not rows or bypass(ctx):
        return rows
    inquiry_ids = {row["subject_id"] for row in rows}
    references = list(db.execute(select(
        PresalesInquiryLine.inquiry_id, PresalesInquiryLine.ref_offer_id,
    ).where(PresalesInquiryLine.inquiry_id.in_(inquiry_ids),
            PresalesInquiryLine.ref_offer_id.is_not(None))))
    offers = list(db.scalars(select(PresalesOffer).where(or_(
        PresalesOffer.inquiry_id.in_(inquiry_ids),
        PresalesOffer.id.in_({offer_id for _, offer_id in references}),
    ))))
    linked = {inquiry_id: [] for inquiry_id in inquiry_ids}
    by_id = {offer.id: offer for offer in offers}
    for offer in offers:
        if offer.inquiry_id in linked:
            linked[offer.inquiry_id].append(offer)
    for inquiry_id, offer_id in references:
        if offer_id in by_id:
            linked[inquiry_id].append(by_id[offer_id])
    # 创建通知的人通常就是报价人；顶层 created_by 也必须遵守各条报价的受众规则。
    markers = {inquiry_id: {offer.id: True for offer in linked[inquiry_id]}
               for inquiry_id in inquiry_ids}
    authors = dict(db.execute(select(SysUser.id, SysUser.username).where(
        SysUser.id.in_({offer.created_by for offer in offers}))).all()) if offers else {}
    rules = {offer.id: (offer.reporter_audience_rule_id, authors.get(offer.created_by, ""))
             for offer in offers}
    visible = audience.filter_fields(db, list(markers.values()), ctx, field_rules=rules, bypass=bypass)
    hidden = {inquiry_id for inquiry_id, filtered in zip(markers, visible, strict=True)
              if len(filtered) < len(markers[inquiry_id])}
    return [{key: value for key, value in row.items()
             if row["subject_id"] not in hidden or key not in {"created_by", "created_by_name"}}
            for row in rows]


def _validate_complete(db, item, ctx):
    inquiry = db.get(PresalesInquiry, item.subject_id)
    if inquiry is None or inquiry.status != "submitted":
        raise InvalidTransition("只能回复已提交的询价")
    needed = set(db.scalars(select(PresalesInquiryLine.id).where(
        PresalesInquiryLine.inquiry_id == inquiry.id, PresalesInquiryLine.status == "active",
        PresalesInquiryLine.ref_offer_id.is_(None))))
    quoted = set(db.scalars(select(PresalesOffer.line_id).where(
        PresalesOffer.inquiry_id == inquiry.id, PresalesOffer.created_by == item.assignee_id,
        PresalesOffer.status == "published")))
    if needed - quoted:
        raise InvalidTransition("每行必须先回复报价或标记无货，请使用询价回复接口")


def _can_manage(db, inquiry_id, ctx):
    inquiry = db.get(PresalesInquiry, inquiry_id)
    user, ctx = resolve_actor(db, ctx)
    return inquiry is not None and (inquiry.created_by == user.id or bypass(ctx))


def _notice_badge_ids(db, ctx):
    """批量 M8 准入，不读取待办表，让角标仍只有一条待办聚合 SQL。"""
    from app.services.workflow import work_items
    user, ctx = resolve_actor(db, ctx)
    rows = list(db.scalars(select(PresalesInquiry).where(PresalesInquiry.kind == "proactive_offer")))
    if bypass(ctx):
        return [row.id for row in rows]
    owned = [row.id for row in rows if row.created_by == user.id]
    if not any(work_items.has_permission(ctx, key) for key in (
        "action_presales_inquire", "action_presales_quote"
    )):
        return owned
    shared = [row for row in rows if row.created_by != user.id and row.submitted_at is not None
              and row.status != "draft"]
    authors = dict(db.execute(select(SysUser.id, SysUser.username).where(
        SysUser.id.in_({row.created_by for row in shared}))).all()) if shared else {}
    filtered = audience.filter_fields(db, [{row.id: row.id} for row in shared], ctx,
        field_rules={row.id: (row.offer_audience_rule_id, authors.get(row.created_by, "")) for row in shared},
        bypass=bypass)
    return owned + [inquiry_id for record in filtered for inquiry_id in record]


def register_subjects():
    specs = registered()
    if "presales_inquiry" not in specs:
        register("presales_inquiry", SubjectSpec(
            pool_permission="action_presales_quote", supervisor_permission="action_presales_supervise",
            can_view_subject=_can_view, summarize=_summarize, filter_events=_filter_events,
            validate_complete=_validate_complete,
            can_manage_subject=_can_manage,
            access_permission="page_presales",
        ))
    if "presales_offer_notice" not in specs:
        register("presales_offer_notice", SubjectSpec(
            pool_permission="page_presales", supervisor_permission="action_presales_supervise",
            can_view_subject=_notice_view, summarize=_summarize, filter_events=_filter_events,
            can_manage_subject=_can_manage,
            access_permission="page_presales",
            badge_subject_ids=_notice_badge_ids,
            filter_items=_filter_notice_items,
        ))

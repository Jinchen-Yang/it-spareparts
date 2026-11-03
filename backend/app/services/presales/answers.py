"""询价行当前答复的唯一计算入口；共享池列表只读这里写入的结果。"""

from sqlalchemy import and_, bindparam, case, delete, func, inspect, or_, select, text, union_all, update
from sqlalchemy.exc import OperationalError

from app.models.presales import (
    PresalesInquiry, PresalesInquiryLine, PresalesLineResponse, PresalesOffer, PresalesOfferLineLink, PresalesOfferRevision,
)
from app.models.workflow import SubjectEvent
from app.services.presales import common
from app.services.workflow.registry import Conflict

# 迁移保留同一口径的副本：迁移不能依赖之后可能变化的业务代码。
# 用 clock_timestamp：now() 是事务开始时间，会排除本事务刚写入的报价。
_REFRESH = text("""
UPDATE presales_inquiry_line AS l
SET current_offer_id = answer.offer_id, answer_state = answer.state, answer_reason = answer.reason,
    answered_by = answer.actor, answered_at = answer.at
FROM (
    SELECT target.id,
        best.id AS offer_id,
        CASE WHEN best.id IS NOT NULL THEN 'quoted'
             WHEN skipped.id IS NOT NULL OR no_stock.id IS NOT NULL THEN 'skip'
             ELSE 'pending' END AS state,
        CASE WHEN best.id IS NOT NULL THEN NULL
             WHEN skipped.id IS NOT NULL THEN skipped.reason
             WHEN no_stock.id IS NOT NULL THEN COALESCE(no_stock.notes, '无货') END AS reason,
        CASE WHEN best.id IS NOT NULL THEN COALESCE(best.updated_by, best.created_by)
             WHEN skipped.id IS NOT NULL THEN skipped.created_by
             WHEN no_stock.id IS NOT NULL THEN COALESCE(no_stock.updated_by, no_stock.created_by) END AS actor,
        CASE WHEN best.id IS NOT NULL THEN best.valid_from
             WHEN skipped.id IS NOT NULL THEN skipped.created_at
             WHEN no_stock.id IS NOT NULL THEN no_stock.valid_from END AS at
    FROM presales_inquiry_line AS target
    LEFT JOIN LATERAL (
        SELECT offer.* FROM presales_offer AS offer
        WHERE offer.status = 'published' AND NOT offer.no_stock AND offer.unit_price IS NOT NULL
          AND offer.valid_from IS NOT NULL AND offer.valid_from <= clock_timestamp()
          AND (offer.line_id = target.id
               OR (offer.id = target.ref_offer_id AND target.ref_offer_revision_no IS NOT NULL)
               OR EXISTS (SELECT 1 FROM presales_line_response AS response
                          WHERE response.line_id = target.id AND response.ref_offer_id = offer.id
                            AND NOT response.is_draft AND response.line_version = target.version)
               OR EXISTS (SELECT 1 FROM presales_offer_line_link AS link
                          WHERE link.line_id = target.id AND link.offer_id = offer.id))
        ORDER BY offer.valid_from DESC, offer.id DESC LIMIT 1
    ) AS best ON TRUE
    LEFT JOIN LATERAL (
        SELECT response.* FROM presales_line_response AS response
        WHERE response.line_id = target.id AND response.mode = 'skip'
          AND NOT response.is_draft AND response.line_version = target.version
        ORDER BY response.id DESC LIMIT 1
    ) AS skipped ON TRUE
    LEFT JOIN LATERAL (
        SELECT offer.* FROM presales_offer AS offer
        WHERE offer.line_id = target.id AND offer.status = 'published' AND offer.no_stock
        ORDER BY offer.id DESC LIMIT 1
    ) AS no_stock ON TRUE
    WHERE target.id IN :line_ids
) AS answer
WHERE l.id = answer.id
  AND ((l.current_offer_id, l.answer_state, l.answer_reason, l.answered_by, l.answered_at)
       IS DISTINCT FROM (answer.offer_id, answer.state, answer.reason, answer.actor, answer.at)
       OR l.current_offer_id = :revised_offer_id)
RETURNING l.id
""").bindparams(bindparam("line_ids", expanding=True))


def _lock_affected_lines(db, ids):
    line = PresalesInquiryLine
    in_pool = ((line.status == "active") & (PresalesInquiry.kind == "sales_inquiry") &
               (PresalesInquiry.status != "draft") & PresalesInquiry.submitted_at.is_not(None))
    query = select(line.id, in_pool).join(PresalesInquiry, PresalesInquiry.id == line.inquiry_id).where(
        line.id.in_(ids)).order_by(line.id).with_for_update(of=line, key_share=True, nowait=True)
    # 旧接口可能已经持有报价锁，共享池先锁行再锁报价。不能在这里反向等行锁。
    try:
        with db.connection().begin_nested():
            return {line_id for line_id, shared in db.execute(query) if shared}
    except OperationalError as exc:
        if getattr(exc.orig, "sqlstate", None) == "55P03":
            raise Conflict("关联询价行正在修改，请核对最新内容后重试", code="concurrent_write") from exc
        raise


def refresh(db, line_ids, *, bump_versions=False, revised_offer_id=None, already_versioned_line=None):
    ids = {line_id for line_id in line_ids if line_id is not None}
    if not ids:
        return
    db.flush()
    versioned = _lock_affected_lines(db, ids) if bump_versions else set()
    versioned.discard(already_versioned_line)
    changed = db.execute(_REFRESH, {"line_ids": sorted(ids), "revised_offer_id": revised_offer_id}).scalars()
    versioned.intersection_update(changed)
    moved_responses = set()
    if versioned:
        # 只前移当前已发布的答复。过期版本和个人草稿仍归属原条件版本。
        carry_response = update(PresalesLineResponse).where(
            PresalesLineResponse.line_id.in_(versioned), PresalesLineResponse.is_draft.is_(False),
            PresalesLineResponse.line_id == PresalesInquiryLine.id,
            PresalesLineResponse.line_version == PresalesInquiryLine.version,
        ).values(line_version=PresalesInquiryLine.version + 1).returning(PresalesLineResponse.id)
        moved_responses = set(db.scalars(carry_response.execution_options(synchronize_session=False)))
        db.execute(update(PresalesInquiryLine).where(PresalesInquiryLine.id.in_(versioned)).values(
            version=PresalesInquiryLine.version + 1, updated_at=func.clock_timestamp()))
    for row in list(db.identity_map.values()):
        if isinstance(row, PresalesInquiryLine) and inspect(row).identity[0] in ids:
            db.expire(row)
        elif isinstance(row, PresalesLineResponse) and inspect(row).identity[0] in moved_responses:
            db.expire(row, ["line_version"])


def refresh_for_offer(db, offer, *, already_versioned_line=None):
    """沿用该报价的其他行也要跟随其当前状态。"""
    if offer.id is None:
        return
    db.flush()
    # 即使以前不是当前价，更新后也可能取代另一来源；只考虑真实有效的历史关联。
    line = PresalesInquiryLine
    ids = db.scalars(union_all(
        select(line.id).where(line.current_offer_id == offer.id),
        select(line.id).where(line.ref_offer_id == offer.id, line.ref_offer_revision_no.is_not(None)),
        select(PresalesOfferLineLink.line_id).where(PresalesOfferLineLink.offer_id == offer.id),
        select(PresalesLineResponse.line_id).join(line, line.id == PresalesLineResponse.line_id).where(
            PresalesLineResponse.ref_offer_id == offer.id, PresalesLineResponse.is_draft.is_(False),
            PresalesLineResponse.line_version == line.version),
    )).all()
    refresh(db, [offer.line_id, *ids], bump_versions=True, revised_offer_id=offer.id,
            already_versioned_line=already_versioned_line)


def refresh_inquiry(db, inquiry_id, *, bump_versions=False):
    db.flush()
    refresh(db, db.scalars(select(PresalesInquiryLine.id).where(
        PresalesInquiryLine.inquiry_id == inquiry_id)).all(), bump_versions=bump_versions)


def linked_revision_ranges(line):
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


def clear_line_answers(db, line, actor, reason, *, keep_offer_id=None, linked_ranges=None, keep_response_version=None):
    """让本行旧答复失效；历史修订和其他行对原报价的引用不变。"""
    from app.services.presales.offers import mark_stale

    if linked_ranges is None:
        linked_ranges = db.execute(linked_revision_ranges(line))
    for offer_id, first, last in linked_ranges:
        common.event(db, db.get(PresalesInquiry, line.inquiry_id), actor, "line.price_unlinked",
                     {"line_id": line.id, "offer_id": offer_id, "first_revision": first, "last_revision": last})
    rows = db.scalars(select(PresalesOffer).where(
        PresalesOffer.line_id == line.id, PresalesOffer.status == "published",
    ).order_by(PresalesOffer.id).with_for_update()).all()
    for offer in rows:
        if offer.id != keep_offer_id:
            mark_stale(db, offer, actor, reason, already_versioned_line=line.id)
    archive_published_skips(db, [line.id])
    remove = delete(PresalesLineResponse).where(PresalesLineResponse.line_id == line.id, PresalesLineResponse.is_draft.is_(False))
    if keep_response_version is not None:
        remove = remove.where(PresalesLineResponse.line_version != keep_response_version)
    db.execute(remove)
    db.execute(delete(PresalesOfferLineLink).where(PresalesOfferLineLink.line_id == line.id))
    line.ref_offer_id, line.ref_offer_revision_no = None, None


def carry_current_responses(db, line_ids):
    """非条件改动已使行版本递增，仅前移此前仍然生效的答复。"""
    if not line_ids:
        return
    db.flush()
    prior_version = select(PresalesInquiryLine.version - 1).where(
        PresalesInquiryLine.id == PresalesLineResponse.line_id).scalar_subquery()
    db.execute(update(PresalesLineResponse).where(
        PresalesLineResponse.line_id.in_(line_ids), PresalesLineResponse.is_draft.is_(False),
        PresalesLineResponse.line_version == prior_version).values(line_version=PresalesLineResponse.line_version + 1))


def unrecorded_skips(line_ids):
    """已提交询价上公开过、且尚无对应行历史事件的不报答复。"""
    r, line, event = PresalesLineResponse, PresalesInquiryLine, SubjectEvent
    source_id = event.payload["response_id"].as_integer()
    # 旧共享池事件未写 response_id。该入口不关联待办；老整单回复则携带真实待办 ID，
    # 即使同一人随后填写了相同原因，也不得与旧共享池事件误合并。
    recorded = select(event.id).where(
        event.subject_type == "presales_inquiry", event.subject_id == line.inquiry_id, event.kind == "line.skipped",
        event.actor == r.created_by, event.payload["line_id"].as_integer() == r.line_id,
        event.payload["reason"].astext == r.reason,
        or_(and_(source_id == r.id, event.created_at == r.created_at),
            and_(source_id.is_(None), r.work_item_id.is_(None))),
    ).correlate(line, r).exists()
    return select(r, line.inquiry_id).join(line, line.id == r.line_id).join(
        PresalesInquiry, PresalesInquiry.id == line.inquiry_id).where(
        r.line_id.in_(line_ids), r.mode == "skip", r.is_draft.is_(False),
        PresalesInquiry.kind == "sales_inquiry", PresalesInquiry.status != "draft",
        PresalesInquiry.submitted_at.is_not(None), ~recorded)


def record_published_skip(db, inquiry_id, response):
    db.add(SubjectEvent(subject_type="presales_inquiry", subject_id=inquiry_id, kind="line.skipped",
                        actor=response.created_by, created_at=response.created_at,
                        payload={"line_id": response.line_id, "response_id": response.id, "reason": response.reason}))


def archive_published_skips(db, line_ids):
    db.flush()
    for response, inquiry_id in db.execute(unrecorded_skips(line_ids)):
        record_published_skip(db, inquiry_id, response)
    db.flush()

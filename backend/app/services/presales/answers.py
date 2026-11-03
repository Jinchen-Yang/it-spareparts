"""询价行当前答复的唯一计算入口；共享池列表只读这里写入的结果。"""

from sqlalchemy import bindparam, func, inspect, select, text, union_all, update
from sqlalchemy.exc import OperationalError

from app.models.presales import PresalesInquiry, PresalesInquiryLine, PresalesLineResponse, PresalesOfferLineLink
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

"""询价行当前答复的唯一计算入口；共享池列表只读这里写入的结果。"""

from sqlalchemy import bindparam, inspect, select, text

from app.models.presales import PresalesInquiryLine

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
""").bindparams(bindparam("line_ids", expanding=True))


def refresh(db, line_ids):
    ids = sorted({line_id for line_id in line_ids if line_id is not None})
    if ids:
        db.flush()
        db.execute(_REFRESH, {"line_ids": ids})
        for row in list(db.identity_map.values()):
            if isinstance(row, PresalesInquiryLine) and inspect(row).identity[0] in ids:
                db.expire(row)


def refresh_for_offer(db, offer):
    """沿用该报价的其他行也要跟随其当前状态。"""
    if offer.id is None:
        return
    db.flush()
    refresh(db, [offer.line_id, *db.scalars(select(PresalesInquiryLine.id).where(
        PresalesInquiryLine.current_offer_id == offer.id))])


def refresh_inquiry(db, inquiry_id):
    db.flush()
    refresh(db, db.scalars(select(PresalesInquiryLine.id).where(
        PresalesInquiryLine.inquiry_id == inquiry_id)).all())

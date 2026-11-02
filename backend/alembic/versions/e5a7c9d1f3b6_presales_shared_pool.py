"""D-0052: 询价报价共享池。

询价行增加当前答复（当前报价、状态、更新人），报价增加最后更新人；按现有报价回填答复，
并取消进行中的售前待办。被取消的待办在 ``last_reason`` 留下标记，downgrade 据此恢复。

Revision ID: e5a7c9d1f3b6
Revises: a3c5e7f9b2d4
"""

from alembic import op
import sqlalchemy as sa

revision = "e5a7c9d1f3b6"
down_revision = "a3c5e7f9b2d4"
branch_labels = None
depends_on = None

MARKER = "D-0052|"
MARKED_CANCELLATIONS = """
    subject_type IN ('presales_inquiry', 'presales_offer_notice') AND status = 'cancelled'
    AND (starts_with(last_reason, :marker || 'pending|') OR starts_with(last_reason, :marker || 'in_progress|'))
"""

# 生产冻结时在 a3 上只读核验同一组 ID；不能依赖 D-0052 新列或修改旧报价/引用。
REVIEW_LINE_IDS = """
WITH revising AS (
    SELECT l.id, l.version, l.part_id, l.qty, l.ref_offer_id,
           i.submitted_snapshot->'lines'->l.line_key AS submitted,
           jsonb_build_object('part_id', l.part_id, 'pn_raw', l.pn_raw,
                              'require_exact_pn', l.require_exact_pn, 'return_defective', l.return_defective) AS fields
    FROM presales_inquiry_line l JOIN presales_inquiry i ON i.id = l.inquiry_id
    WHERE l.status = 'active' AND i.kind = 'sales_inquiry' AND i.status = 'revising' AND i.submitted_at IS NOT NULL
), signatures AS (
    SELECT l.id AS line_id, NULL::integer AS offer_id, jsonb_build_object('return_defective', true) || l.submitted AS sig
    FROM revising l
    UNION ALL
    SELECT l.id, o.id,
           CASE WHEN o.part_id IS NOT DISTINCT FROM l.part_id AND r.snapshot->'line_id' = to_jsonb(l.id)
                     AND r.snapshot->>'status' = 'published'
                THEN r.snapshot #> '{_inquiry,line_conditions}' END
    FROM revising l JOIN presales_offer o ON o.line_id = l.id AND o.status = 'published'
    LEFT JOIN presales_offer_revision r ON r.offer_id = o.id AND r.revision_no = o.revision_no
), checked AS (
    SELECT l.id, s.offer_id, COALESCE(jsonb_typeof(s.sig) = 'object' AND s.sig @> l.fields AND
        CASE WHEN (s.sig->>'qty') ~ '^[+]?[0-9]{1,32}([.][0-9]{1,32})?([Ee][+-]?[0-9]{1,3})?$'
             THEN (s.sig->>'qty')::numeric = l.qty ELSE false END, false) AS matches
    FROM revising l JOIN signatures s ON s.line_id = l.id
)
SELECT l.id
FROM revising l JOIN checked baseline ON baseline.id = l.id AND baseline.offer_id IS NULL
WHERE NOT baseline.matches AND (
    l.ref_offer_id IS NOT NULL
    OR EXISTS (SELECT 1 FROM presales_offer_line_link link WHERE link.line_id = l.id)
    OR EXISTS (SELECT 1 FROM presales_line_response r WHERE r.line_id = l.id AND NOT r.is_draft AND r.line_version = l.version)
    OR EXISTS (SELECT 1 FROM checked quote WHERE quote.id = l.id AND quote.offer_id IS NOT NULL AND NOT quote.matches)
)
"""

# 与 app.services.presales.answers 同一口径；迁移不依赖之后可能变化的业务代码。
BACKFILL = """
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
        CASE WHEN best.id IS NOT NULL THEN best.created_by
             WHEN skipped.id IS NOT NULL THEN skipped.created_by
             WHEN no_stock.id IS NOT NULL THEN no_stock.created_by END AS actor,
        CASE WHEN best.id IS NOT NULL THEN best.valid_from
             WHEN skipped.id IS NOT NULL THEN skipped.created_at
             WHEN no_stock.id IS NOT NULL THEN no_stock.valid_from END AS at
    FROM presales_inquiry_line AS target
    LEFT JOIN LATERAL (
        SELECT offer.* FROM presales_offer AS offer
        WHERE offer.status = 'published' AND NOT offer.no_stock AND offer.unit_price IS NOT NULL
          AND offer.valid_from IS NOT NULL AND offer.valid_from <= now()
          AND NOT EXISTS (SELECT 1 FROM presales_inquiry_line source
                          WHERE source.id = offer.line_id AND source.answer_requires_review)
          AND (offer.line_id = target.id
               OR (offer.id = target.ref_offer_id AND target.ref_offer_revision_no IS NOT NULL)
               OR EXISTS (SELECT 1 FROM presales_line_response AS response
                          WHERE response.line_id = target.id AND response.ref_offer_id = offer.id
                            AND NOT response.is_draft AND response.line_version = target.version)
               OR EXISTS (SELECT 1 FROM presales_offer_line_link AS link
                          WHERE link.line_id = target.id AND link.offer_id = offer.id))
        ORDER BY offer.valid_from DESC, offer.id DESC LIMIT 1
    ) AS best ON NOT target.answer_requires_review
    LEFT JOIN LATERAL (
        SELECT response.* FROM presales_line_response AS response
        WHERE response.line_id = target.id AND response.mode = 'skip'
          AND NOT response.is_draft AND response.line_version = target.version
        ORDER BY response.id DESC LIMIT 1
    ) AS skipped ON NOT target.answer_requires_review
    LEFT JOIN LATERAL (
        SELECT offer.* FROM presales_offer AS offer
        WHERE offer.line_id = target.id AND offer.status = 'published' AND offer.no_stock
        ORDER BY offer.id DESC LIMIT 1
    ) AS no_stock ON NOT target.answer_requires_review
) AS answer
WHERE l.id = answer.id
"""


def upgrade() -> None:
    bind = op.get_bind()
    # 检查历史原因与写入标记共用事务，阻止旧工作流在两者之间写入相同的原因。
    bind.execute(sa.text("LOCK TABLE work_item IN SHARE ROW EXCLUSIVE MODE"))
    has_reserved_reason = bind.scalar(
        sa.text(f"SELECT EXISTS(SELECT 1 FROM work_item WHERE {MARKED_CANCELLATIONS})"), {"marker": MARKER})
    if has_reserved_reason:
        raise RuntimeError("已有取消的售前待办使用 D-0052 回滚标记，无法区分原记录与迁移取消的待办")
    op.add_column("presales_offer", sa.Column("updated_by", sa.Integer(), nullable=True))
    op.create_foreign_key("fk_presales_offer_updated_by", "presales_offer", "sys_user", ["updated_by"], ["id"])
    op.add_column("presales_inquiry_line", sa.Column(
        "answer_state", sa.String(8), nullable=False, server_default="pending"))
    op.add_column("presales_inquiry_line", sa.Column(
        "answer_requires_review", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("presales_inquiry_line", sa.Column("current_offer_id", sa.Integer(), nullable=True))
    op.add_column("presales_inquiry_line", sa.Column("answer_reason", sa.Text(), nullable=True))
    op.add_column("presales_inquiry_line", sa.Column("answered_by", sa.Integer(), nullable=True))
    op.add_column("presales_inquiry_line", sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key("fk_presales_line_current_offer", "presales_inquiry_line", "presales_offer",
                          ["current_offer_id"], ["id"])
    op.create_foreign_key("fk_presales_line_answered_by", "presales_inquiry_line", "sys_user", ["answered_by"], ["id"])
    op.create_check_constraint("ck_presales_line_answer_state", "presales_inquiry_line",
                               "answer_state IN ('pending', 'quoted', 'skip')")
    op.execute(f"UPDATE presales_inquiry_line SET answer_requires_review = true WHERE id IN ({REVIEW_LINE_IDS})")
    op.execute(BACKFILL)
    op.create_index("ix_presales_line_pool", "presales_inquiry_line", ["answer_state", "inquiry_id"],
                    postgresql_where=sa.text("status = 'active'"))
    op.create_index("ix_presales_line_current_offer", "presales_inquiry_line", ["current_offer_id"],
                    postgresql_where=sa.text("current_offer_id IS NOT NULL"))
    bind.execute(sa.text("""
        UPDATE work_item
        SET last_reason = :marker || status || '|' || COALESCE(last_reason, ''),
            status = 'cancelled', closed_at = now(), updated_at = now(), version = version + 1
        WHERE subject_type IN ('presales_inquiry', 'presales_offer_notice')
          AND status IN ('pending', 'in_progress')
    """), {"marker": MARKER})


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text(f"""
        UPDATE work_item
        SET status = split_part(last_reason, '|', 2),
            last_reason = NULLIF(substr(last_reason, length(:marker) + length(split_part(last_reason, '|', 2)) + 2), ''),
            closed_at = NULL, updated_at = now(), version = version + 1
        WHERE {MARKED_CANCELLATIONS}
    """), {"marker": MARKER})
    op.drop_index("ix_presales_line_current_offer", table_name="presales_inquiry_line")
    op.drop_index("ix_presales_line_pool", table_name="presales_inquiry_line")
    op.drop_constraint("ck_presales_line_answer_state", "presales_inquiry_line", type_="check")
    op.drop_constraint("fk_presales_line_answered_by", "presales_inquiry_line", type_="foreignkey")
    op.drop_constraint("fk_presales_line_current_offer", "presales_inquiry_line", type_="foreignkey")
    for column in ("answered_at", "answered_by", "answer_reason", "current_offer_id", "answer_state", "answer_requires_review"):
        op.drop_column("presales_inquiry_line", column)
    op.drop_constraint("fk_presales_offer_updated_by", "presales_offer", type_="foreignkey")
    op.drop_column("presales_offer", "updated_by")

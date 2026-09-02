"""回填 47 个期限缺失维保项目的 period_from/period_to

三类策略（2026-08-26 业务拍板方案 1 自动补齐）：
1. 名称含 YYYYMMDD-YYYYMMDD 日期段：解析名称填期限（3 个）；
2. XSDD 命名的自动项目（AUTO-*）：用挂靠订单的 min/max order_date 填（35 个）；
3. 真正无期限的（顺丰单次服务/搬迁/canary）：保留为空，待业务人工补。

lifecycle_status 按 _lifecycle_status 函数口径重算。

Revision ID: a8e4f1c7d3b9
Revises: f7a3d2c8e6b1
"""

from collections.abc import Sequence

from alembic import op


revision: str = "a8e4f1c7d3b9"
down_revision: str | None = "f7a3d2c8e6b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")

    # 1) 名称含日期段 YYYYMMDD-YYYYMMDD：解析填入
    op.execute(r"""
        UPDATE maintenance_project p
        SET period_from = TO_DATE(sub.d1, 'YYYYMMDD'),
            period_to   = TO_DATE(sub.d2, 'YYYYMMDD'),
            updated_at  = now()
        FROM (
            SELECT project_id,
                   (regexp_matches(display_name, '(20\d{6})[-~至](20\d{6})'))[1] AS d1,
                   (regexp_matches(display_name, '(20\d{6})[-~至](20\d{6})'))[2] AS d2
            FROM maintenance_project
            WHERE is_active AND period_from IS NULL AND period_to IS NULL
              AND display_name ~ '20\d{6}[-~至]20\d{6}'
        ) sub
        WHERE p.project_id = sub.project_id
    """)

    # 2) 名称含 YYYY-YYYY（粗粒度如"2023-2024年"）：取当年 1/1 ~ 次年 12/31
    op.execute(r"""
        UPDATE maintenance_project p
        SET period_from = TO_DATE(sub.y1 || '0101', 'YYYYMMDD'),
            period_to   = TO_DATE(sub.y2 || '1231', 'YYYYMMDD'),
            updated_at  = now()
        FROM (
            SELECT project_id,
                   (regexp_matches(display_name, '(20\d{2})[-~至年](20\d{2})'))[1] AS y1,
                   (regexp_matches(display_name, '(20\d{2})[-~至年](20\d{2})'))[2] AS y2
            FROM maintenance_project
            WHERE is_active AND period_from IS NULL AND period_to IS NULL
              AND display_name ~ '20\d{2}[-~至年]20\d{2}'
              AND display_name !~ '20\d{6}'  -- 确保不是精确日期段（已被规则1捕获）
        ) sub
        WHERE p.project_id = sub.project_id
    """)

    # 3) 其余期限仍为空的活跃项目：用挂靠订单 min/max order_date 近似
    op.execute("""
        UPDATE maintenance_project p
        SET period_from = sub.min_date,
            period_to   = sub.max_date,
            updated_at  = now()
        FROM (
            SELECT a.project_id,
                   min(o.order_date) AS min_date,
                   max(o.order_date) AS max_date
            FROM maintenance_source_order_assignment a
            JOIN f_maintenance_order o ON o.raw_order_id = a.source_order_id
            WHERE a.is_active
              AND o.order_date IS NOT NULL
            GROUP BY a.project_id
            HAVING min(o.order_date) IS NOT NULL AND max(o.order_date) IS NOT NULL
        ) sub
        WHERE p.project_id = sub.project_id
          AND p.is_active
          AND p.period_from IS NULL
          AND p.period_to IS NULL
    """)

    # lifecycle 重算（与 maintenance_ledger._lifecycle_status 同口径）
    op.execute("""
        UPDATE maintenance_project
        SET lifecycle_status = CASE
            WHEN period_from IS NULL OR period_to IS NULL THEN 'missing'
            WHEN CURRENT_DATE < period_from THEN 'ongoing'
            WHEN CURRENT_DATE > period_to THEN 'ended'
            ELSE 'ongoing'
        END,
        updated_at = now()
        WHERE is_active AND (period_from IS NOT NULL OR period_to IS NOT NULL)
    """)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # 回退无法精确区分原始空值与回填值；不自动还原。
    pass

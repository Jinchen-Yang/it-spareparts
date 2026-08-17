"""维保期限进项目主数据（REQUIREMENTS #51）。

甲方核心需求 §3.3「能修改补充项目的名称、维保期限」与 #39「编辑基本信息（起止
时间/负责人）」要求期限是可显示、可编辑的主数据；#50 只回填了 lifecycle 三态
标签，期限日期本身没有落点——本迁移补上 `period_from/period_to` 两列并回填。

回填三层（业务指示 2026-08-17：现有数据即可确定期限，不必等台账）：
1. **WBDD 挂靠聚合**（权威度最高的现有事实）：项目挂靠单据的
   min(maint_start)/max(maint_end)——WBDD 单头自带维保起止日期，生产非空率
   99.97%，可覆盖 411/415 个项目；这正是 v1.17 老版项目成本页的口径
   （maintenance_cost.projects_aggregate，以最晚终止日判期限）。
2. **名称解析兜底**：#50 的同款解析（8 位日起止 + 6 位年月段），此次把日期本身
   落列，不只回填状态。
3. 都解析不出保持 NULL。

lifecycle_status 随后按最终 period 全量重算（单据事实优先于 #50 的名称回填值）；
period 仍空的行保持原状态。台账导入仍是权威源：ledger 导入会以台账期限覆盖
本回填（services/maintenance_ledger.py 同步改造）。

纯加法：2 个 nullable Date 列、无 default、无索引、无约束变更；回填只写新列与
既有枚举值。旧应用不引用新列，向前兼容；回滚口径仍是关 flag，downgrade 仅供
迁移测试往返。自包含：名称解析内嵌本文件（CI 空库重放整链，不得 import app）。

Revision ID: f3b5d7c9e2a4
Revises: e8b2c6f4d1a7
"""
import re
from calendar import monthrange
from datetime import date, datetime

import sqlalchemy as sa
from alembic import op

revision = "f3b5d7c9e2a4"
down_revision = "e8b2c6f4d1a7"
branch_labels = None
depends_on = None

# 与 services/maintenance_ledger.py 的 _period_from_display_name 同款（改了要同步）。
_NAME_PERIOD_RE = re.compile(r"(?<!\d)(\d{8})\s*[-—~至]\s*(\d{8})(?!\d)")
_NAME_PERIOD_YM_RE = re.compile(r"(?<!\d)(\d{6})\s*[-—~至]\s*(\d{6})(?!\d)")


def _period_from_name(name: str | None) -> tuple[date | None, date | None]:
    if not name:
        return (None, None)
    match = _NAME_PERIOD_RE.search(name)
    if match is not None:
        try:
            start = datetime.strptime(match.group(1), "%Y%m%d").date()
            end = datetime.strptime(match.group(2), "%Y%m%d").date()
        except ValueError:
            return (None, None)
    else:
        match = _NAME_PERIOD_YM_RE.search(name)
        if match is None:
            return (None, None)
        try:
            start = datetime.strptime(match.group(1), "%Y%m").date()
            end_month = datetime.strptime(match.group(2), "%Y%m").date()
        except ValueError:
            return (None, None)
        end = end_month.replace(day=monthrange(end_month.year, end_month.month)[1])
    if start > end:
        return (None, None)
    return (start, end)


def _status(start: date | None, end: date | None, today: date) -> str | None:
    if start is None and end is None:
        return None
    if end is not None and end < today:
        return "ended"
    if start is not None and start <= today and (end is None or today <= end):
        return "ongoing"
    return "missing"


def upgrade() -> None:
    op.add_column("maintenance_project", sa.Column("period_from", sa.Date(), nullable=True))
    op.add_column("maintenance_project", sa.Column("period_to", sa.Date(), nullable=True))

    bind = op.get_bind()

    # 1) WBDD 挂靠聚合（老版口径：min 起始 / max 终止）
    bind.execute(
        sa.text(
            """
            UPDATE maintenance_project p
               SET period_from = agg.ps, period_to = agg.pe, updated_at = now()
              FROM (
                    SELECT a.project_id,
                           min(mo.maint_start) AS ps,
                           max(mo.maint_end)   AS pe
                      FROM maintenance_source_order_assignment a
                      JOIN f_maintenance_order mo
                        ON mo.raw_order_id = a.source_order_id
                     WHERE a.is_active
                     GROUP BY a.project_id
                   ) agg
             WHERE agg.project_id = p.project_id
               AND (agg.ps IS NOT NULL OR agg.pe IS NOT NULL)
            """
        )
    )

    # 2) 名称解析兜底（仅 period 仍全空的行）
    rows = bind.execute(
        sa.text(
            "SELECT project_id, display_name FROM maintenance_project "
            "WHERE period_from IS NULL AND period_to IS NULL"
        )
    ).all()
    update_period = sa.text(
        "UPDATE maintenance_project SET period_from = :pf, period_to = :pt, "
        "updated_at = now() WHERE project_id = :pid"
    )
    for pid, name in rows:
        start, end = _period_from_name(name)
        if start is not None or end is not None:
            bind.execute(update_period, {"pf": start, "pt": end, "pid": pid})

    # 3) lifecycle 按最终 period 全量重算（有 period 才动；单据事实优先于名称回填值）
    today = date.today()
    rows = bind.execute(
        sa.text(
            "SELECT project_id, period_from, period_to, lifecycle_status "
            "FROM maintenance_project "
            "WHERE period_from IS NOT NULL OR period_to IS NOT NULL"
        )
    ).all()
    update_lc = sa.text(
        "UPDATE maintenance_project SET lifecycle_status = :lc, updated_at = now() "
        "WHERE project_id = :pid"
    )
    for pid, pf, pt, current in rows:
        status = _status(pf, pt, today)
        if status is not None and status != current:
            bind.execute(update_lc, {"lc": status, "pid": pid})


def downgrade() -> None:
    # 仅供迁移测试 upgrade↔downgrade 往返。
    op.drop_column("maintenance_project", "period_to")
    op.drop_column("maintenance_project", "period_from")

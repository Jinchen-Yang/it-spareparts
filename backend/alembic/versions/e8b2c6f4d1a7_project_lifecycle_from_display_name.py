"""项目 lifecycle 按名称周期回填（REQUIREMENTS #50）。

业务 2026-08-17 指示：维保项目名称里嵌着服务周期（`客户名YYYYMMDD-YYYYMMDD服务商
业务类型`），周期从名称提取。生产 415 个项目因台账从未导入而全部 lifecycle=missing，
卡墙「进行中」恒空——本迁移把名称可解析的存量项目一次性回填为 ongoing/ended。

口径（与 services/maintenance_ledger.py 的 _resolve_lifecycle 一致）：
- 台账周期仍是权威源：只回填当前 **missing** 的行，不碰台账已判定的 ongoing/ended；
- 解析不出（无日期段/非法日期/起止倒置）保持 missing，宁缺毋错；
- 快照语义：按执行日判定，之后的 ongoing→ended 翻转靠台账导入刷新（导入侧已带
  同款名称兜底，不会把这里的结果打回 missing）。

纯数据 UPDATE：零 DDL、无新列、无索引/约束变更，旧应用只读到合法枚举值之一，
向前兼容；回滚口径仍是「关 flag」，downgrade 仅供迁移测试往返（不清数据——
回填结果本就是名称里明摆着的事实，反向清除没有业务意义）。

自包含：解析逻辑内嵌本文件（CI 每轮在空库重放整链，迁移不得 import app.services，
否则历史迁移与 app 包的当前形状绑死）。改 services 侧解析时记得同步这里。

Revision ID: e8b2c6f4d1a7
Revises: d6e1f4a8c3b5
"""
import re
from calendar import monthrange
from datetime import date, datetime

import sqlalchemy as sa
from alembic import op

revision = "e8b2c6f4d1a7"
down_revision = "d6e1f4a8c3b5"
branch_labels = None
depends_on = None

# 与 services/maintenance_ledger.py 的 _period_from_display_name 同款（那边改了要同步）：
# 8 位日起止为主，连接符 `-`/`~`/空格混排；6 位年月段取起月首日、止月末日。
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


def _status(start: date | None, end: date | None, today: date) -> str:
    if start is None and end is None:
        return "missing"
    if end is not None and end < today:
        return "ended"
    if start is not None and start <= today and (end is None or today <= end):
        return "ongoing"
    return "missing"


def upgrade() -> None:
    bind = op.get_bind()
    today = date.today()
    rows = bind.execute(
        sa.text(
            "SELECT project_id, display_name FROM maintenance_project "
            "WHERE lifecycle_status = 'missing'"
        )
    ).all()
    update = sa.text(
        "UPDATE maintenance_project SET lifecycle_status = :status, updated_at = now() "
        "WHERE project_id = :pid AND lifecycle_status = 'missing'"
    )
    for pid, name in rows:
        status = _status(*_period_from_name(name), today)
        if status != "missing":
            bind.execute(update, {"status": status, "pid": pid})


def downgrade() -> None:
    # 仅供迁移测试 upgrade↔downgrade 往返：不清回填结果（见文件头说明）。
    pass

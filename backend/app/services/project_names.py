"""预交付项目名前缀的单一事实源（plan v1.3 M2-3 技术债统一）。

口径（业务确认，需求定义 §3.3 / 铁律 4）：
- 「预交付-X」是单据级前缀，X 才是真实合同项目身份（transform 的 project_std 剥前缀）；
- 横线**必需**（半角-/长横—/全角－/短横–容差）：只剥「预交付-X」，
  不动恰好以「预交付」开头的正常项目名；
- 展示层「预交付」徽标 = project_raw 命中本前缀（不落库、不合并项目档案——方案 B）。

引用方：etl/transform.py（project_std 生成）、services/maintenance_roundtrip.py
（工作簿回填的 project_std 口径）、services/maintenance_source_assignments.py
（归属候选与徽标）。services/date_loose.parse_project_name 保留其更宽的
台账手写名容差（另含「预付/预」变体、横线可选），属台账身份解析特化，
其「预交付」分支语义以本模块为准。
"""
import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

PRE_DELIVERY_PREFIX = re.compile(r"^预交付[-—－–]")


def display_name_identity(name: str) -> str:
    """并发身份键；比业务精确匹配更保守，大小写/空白变体共用一把锁。"""
    return " ".join(str(name).split()).casefold()


def lock_display_name_identities(db: Session, names) -> None:
    """项目 create/rename/auto-create 共用的名称 advisory，必须先于 state 锁。"""
    identities = sorted({display_name_identity(name) for name in names if str(name).strip()})
    for identity in identities:
        db.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(
            f"maintenance-project-display-name:{identity}", 0,
        ))))


def strip_pre_delivery(name: str) -> str:
    """剥「预交付-」前缀；剥空则原样返回（防止「预交付-」单独成名被清空）。"""
    stripped = PRE_DELIVERY_PREFIX.sub("", name).strip()
    return stripped or name


def is_pre_delivery(name: str | None) -> bool:
    return bool(name) and PRE_DELIVERY_PREFIX.match(name) is not None

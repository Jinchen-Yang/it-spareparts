"""商城目录互通共用的 PN 候选查找与合并解析。

`POST /integrations/resale/parts/resolve` 与新的参考价 GET 必须使用同一套
标准化、别名和合并规则；放在此处避免两个入口各自演化出不同身份判断。
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.etl.cleaner import standardize_pn
from app.models.dimensions import DimPart, PartAlias

MAX_MERGE_HOPS = 10


def canonical_part(
    db: Session, requested: DimPart | None
) -> tuple[DimPart | None, bool]:
    """沿合并链取最终身份；循环、断链或超过 10 跳返回 (None, True)。

    返回的第二项表示是否发生过合并重定向。无法唯一确定的链绝不任选一个节点。
    """
    part = requested
    seen: set[int] = set()
    merged = False
    while part is not None and part.status == "merged":
        if (
            part.id in seen
            or len(seen) >= MAX_MERGE_HOPS
            or part.merged_into_id is None
        ):
            return None, True
        seen.add(part.id)
        part = db.get(DimPart, part.merged_into_id)
        merged = True
    return part, merged


def find_pn_candidates(
    db: Session, raw: str
) -> tuple[dict[int, tuple[DimPart, str]], bool]:
    """返回直接候选（id → (part, "exact"/"alias")）及是否存在 pending 别名。

    ``raw`` 由调用方先做 strip；此处沿用既有 `/resolve` 的查找顺序：规范名精确
    命中在前，active 别名在后，pending 别名只报告歧义、不进入候选。
    """
    pn_std, _, _ = standardize_pn(raw)
    matches: dict[int, tuple[DimPart, str]] = {}
    if pn_std:
        exact = db.scalar(select(DimPart).where(DimPart.pn_std == pn_std))
        if exact is not None:
            matches[exact.id] = (exact, "exact")
    aliases = db.scalars(
        select(PartAlias).where(
            func.upper(PartAlias.pn_raw) == raw.upper(),
            PartAlias.status.in_(("active", "pending")),
        )
    )
    pending = False
    for alias in aliases:
        if alias.status == "pending":
            pending = True
            continue
        part = db.get(DimPart, alias.part_id) if alias.part_id else None
        if part is not None:
            matches.setdefault(part.id, (part, "alias"))
    return matches, pending

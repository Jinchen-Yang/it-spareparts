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


def find_pn_candidates_batch(
    db: Session, raws: list[str]
) -> dict[str, tuple[dict[int, tuple[DimPart, str]], bool]]:
    """同批解析候选，保留单项查找的规范名优先和全部别名歧义。

    调用方已 strip 且限制至多 100 项。规范名和别名各查一次，未在规范名
    结果中的 active 别名目标再批量载入；合并链仍由调用方沿原路径解析。
    别名分组必须用 PostgreSQL upper 的实际值，不能对返回的 pn_raw 再用
    Python upper：例如 ß 的转换可能随数据库 locale 与 Python 而不同。
    """
    unique_raws = list(dict.fromkeys(raws))
    if not unique_raws:
        return {}
    normalized = {raw: standardize_pn(raw)[0] for raw in unique_raws}
    pn_stds = {pn for pn in normalized.values() if pn}
    exact_by_pn = {
        part.pn_std: part
        for part in db.scalars(select(DimPart).where(DimPart.pn_std.in_(pn_stds)))
    } if pn_stds else {}
    parts_by_id = {part.id: part for part in exact_by_pn.values()}
    upper_raws = {raw.upper() for raw in unique_raws}
    aliases = db.execute(
        select(PartAlias, func.upper(PartAlias.pn_raw)).where(
            func.upper(PartAlias.pn_raw).in_(upper_raws),
            PartAlias.status.in_(("active", "pending")),
        )
    ).all()
    missing_ids = {
        alias.part_id for alias, _upper_raw in aliases
        if alias.status == "active" and alias.part_id and alias.part_id not in parts_by_id
    }
    if missing_ids:
        parts_by_id.update({
            part.id: part
            for part in db.scalars(select(DimPart).where(DimPart.id.in_(missing_ids)))
        })
    aliases_by_upper: dict[str, list[PartAlias]] = {}
    for alias, upper_raw in aliases:
        aliases_by_upper.setdefault(upper_raw, []).append(alias)

    result = {}
    for raw in unique_raws:
        matches: dict[int, tuple[DimPart, str]] = {}
        exact = exact_by_pn.get(normalized[raw])
        if exact is not None:
            matches[exact.id] = (exact, "exact")
        pending = False
        # Preserve exact-first and database alias iteration order, as in the
        # single-item helper; do not sort candidates or choose an arbitrary one.
        for alias in aliases_by_upper.get(raw.upper(), []):
            if alias.status == "pending":
                pending = True
                continue
            part = parts_by_id.get(alias.part_id) if alias.part_id else None
            if part is not None:
                matches.setdefault(part.id, (part, "alias"))
        result[raw] = matches, pending
    return result

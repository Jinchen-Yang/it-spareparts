"""机型目录与原始 PN 匹配；读模型只投影经核定的机型归属。"""

import hashlib
import unicodedata
from collections import defaultdict

from fastapi import HTTPException
from sqlalchemy import case, func, literal_column, or_, select
from sqlalchemy.orm import Session

from app.models.dimensions import DimPart, PartAlias
from app.models.master_data import ProductSpec
from app.models.model_parts import (
    MachineModel,
    MachineModelCommand,
    MachineModelMember,
    MachineModelName,
    MachineModelPending,
)
from app.schemas.model_parts import PN_EDGE_SPACE
from app.services.resale_catalog_matching import like_pattern, search_condition, search_terms
from app.services.resale_pn_lookup import MAX_MERGE_HOPS


def key(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip(PN_EDGE_SPACE).lower()


def _sql_key(column):
    return func.lower(func.btrim(func.normalize(column, literal_column("NFKC")), PN_EDGE_SPACE))


def revision(db: Session) -> int:
    return db.scalar(select(func.max(MachineModelCommand.revision))) or 0


def canonical_ids(db: Session, ids) -> dict[int, int]:
    """按层批读现存合并链，关系保留原 ID；撤销合并时可还原。"""
    ids = set(ids)
    pending = ids
    nodes = {}
    for _ in range(MAX_MERGE_HOPS + 1):
        if not pending:
            break
        rows = db.execute(
            select(DimPart.id, DimPart.status, DimPart.merged_into_id).where(DimPart.id.in_(pending))
        )
        for pid, status, parent in rows:
            nodes[pid] = parent if status == "merged" else pid
        pending = {nodes[pid] for pid in pending if pid in nodes} - nodes.keys()
    result = {}
    for original in ids:
        pid = original
        for _ in range(MAX_MERGE_HOPS + 1):
            if pid not in nodes:
                break
            if nodes[pid] == pid:
                result[original] = pid
                break
            pid = nodes[pid]
        if original not in result:
            raise HTTPException(409, "备件身份已变更且无法解析，请核对主数据库")
    return result


def lookup(db: Session, raws: list[str]) -> dict[str, set[int]]:
    """原号整值比对；保持固件、后缀与内部分隔符，规范 PN 优先于已有别名。"""
    keys = {key(raw) for raw in raws} - {""}
    if not keys:
        return {}
    exact = defaultdict(set)
    aliases = defaultdict(set)
    # PostgreSQL 的 lower() 受 locale 影响；例如 İ 和词尾 Σ 与浏览器可能不同。
    # 纯 ASCII 用固定排序规则粗筛，有 Unicode 查询时同时覆盖非 ASCII 原号索引。
    ascii_keys = {k for k in keys if k.isascii()}

    def prefilter(column):
        ascii_match = _sql_key(column.collate("C")).in_(ascii_keys)
        if ascii_keys == keys:
            return ascii_match
        return or_(ascii_match, column.op("~")(literal_column("'[^ -~]'")))

    for raw, pid in db.execute(select(DimPart.pn_std, DimPart.id).where(prefilter(DimPart.pn_std))):
        name = key(raw)
        if name in keys:
            exact[name].add(pid)
    for raw, pid in db.execute(
        select(PartAlias.pn_raw, PartAlias.part_id).where(
            prefilter(PartAlias.pn_raw),
            PartAlias.status == "active",
            PartAlias.part_id.is_not(None),
        )
    ):
        name = key(raw)
        if name in keys:
            aliases[name].add(pid)
    source_ids = {pid for name in keys for pid in (exact[name] or aliases[name])}
    current = canonical_ids(db, source_ids)
    return {name: {current[pid] for pid in (exact[name] or aliases[name])} for name in keys}


def part_views(db: Session, ids) -> list[dict]:
    ids = set(ids)
    if not ids:
        return []
    aliases = defaultdict(list)
    for pid, value in db.execute(
        select(PartAlias.part_id, PartAlias.pn_raw)
        .where(PartAlias.part_id.in_(ids), PartAlias.status == "active")
        .order_by(PartAlias.id)
    ):
        aliases[pid].append(value)
    specs = defaultdict(list)
    for pid, value, unit in db.execute(
        select(ProductSpec.part_id, ProductSpec.spec_value, ProductSpec.spec_unit)
        .where(ProductSpec.part_id.in_(ids))
        .order_by(ProductSpec.part_id, ProductSpec.spec_key)
    ):
        specs[pid].append(
            value if not unit or value.casefold().endswith(unit.casefold()) else f"{value} {unit}"
        )
    return [
        {
            "id": str(p.id),
            "pn": p.pn_std,
            "name": p.summary or p.description or p.pn_std,
            "category": p.category_minor or p.category_major or "未分类",
            "aliases": [a for a in aliases[p.id] if key(a) != key(p.pn_std)],
            "specs": list(dict.fromkeys(specs[p.id])),
            "good": None,
            "bad": None,
        }
        for p in db.scalars(select(DimPart).where(DimPart.id.in_(ids)).order_by(DimPart.pn_std, DimPart.id))
    ]


def candidates(db, body):
    if body.pns is not None:
        # 以原号为键返回主库实际解析结果。旧规范名已经合并时，一个活动档案
        # 的全局别名可以碰巧等于它；前端不能从档案集合反推每行的身份。
        matches = lookup(db, body.pns)
        normalized = {key(raw) for raw in body.pns}
        return {
            "parts": part_views(db, {pid for ids in matches.values() for pid in ids}),
            "matches": {
                name: [str(pid) for pid in sorted(matches.get(name, ()))] for name in sorted(normalized)
            },
        }
    q = body.q
    columns = [
        DimPart.pn_std,
        DimPart.description,
        DimPart.summary,
        DimPart.category_minor,
        DimPart.category_major,
    ]
    statement = select(DimPart.id).where(DimPart.status == "active")
    for term in search_terms(q):
        statement = statement.where(
            or_(
                search_condition(columns, term),
                DimPart.id.in_(
                    select(PartAlias.part_id).where(
                        PartAlias.status == "active", PartAlias.pn_raw.ilike(like_pattern(term), escape="\\")
                    )
                ),
            )
        )
    rank = case(
        (_sql_key(DimPart.pn_std) == key(q), 0),
        (
            DimPart.id.in_(
                select(PartAlias.part_id).where(
                    PartAlias.status == "active", _sql_key(PartAlias.pn_raw) == key(q)
                )
            ),
            1,
        ),
        else_=2,
    )
    ids = list(db.scalars(statement.order_by(rank, DimPart.pn_std, DimPart.id).limit(50)))
    rows = {int(p["id"]): p for p in part_views(db, ids)}
    return {"parts": [rows[pid] for pid in ids]}


def membership_version(model_id: int, canonical_id: int, originals) -> str:
    # 主库合并/撤销可改变一条界面归属背后的原始行，而无需写本模块修订。
    material = f"{model_id}:{canonical_id}:" + ",".join(str(pid) for pid in sorted(originals))
    return hashlib.sha256(material.encode("ascii")).hexdigest()


def member_view(member, canonical_id: int) -> dict:
    return {
        "modelId": str(member.model_id),
        "partId": str(canonical_id),
        "position": member.position,
        "condition": member.condition,
        "source": member.source,
        "rawPns": list(member.raw_pns),
    }


def pending_view(row, candidate_ids, *, current=False) -> dict:
    return {
        "id": str(row.id),
        "modelId": str(row.model_id),
        "rawPn": row.raw_pn,
        "position": row.position,
        "condition": row.condition,
        "source": row.source,
        "reason": ("ambiguous" if candidate_ids else "unknown") if current else row.reason,
        "candidateIds": [str(pid) for pid in sorted(set(candidate_ids))],
    }


def joined(*values: str) -> str:
    return " / ".join(dict.fromkeys(value for value in values if value))


def catalog(db: Session) -> dict:
    models = {
        m.id: {"id": str(m.id), "brand": m.brand, "name": m.name, "aliases": []}
        for m in db.scalars(select(MachineModel).order_by(MachineModel.id))
    }
    for row in db.scalars(
        select(MachineModelName).where(MachineModelName.is_primary.is_(False)).order_by(MachineModelName.id)
    ):
        models[row.model_id]["aliases"].append(row.name)
    links = list(
        db.scalars(
            select(MachineModelMember).order_by(MachineModelMember.model_id, MachineModelMember.part_id)
        )
    )
    pending = list(db.scalars(select(MachineModelPending).order_by(MachineModelPending.id)))
    current = canonical_ids(db, {row.part_id for row in links})
    current_choices = lookup(db, [row.raw_pn for row in pending])
    pending_rows = []
    visible_ids = {current[row.part_id] for row in links}
    for row in pending:
        choices = current_choices[key(row.raw_pn)]
        visible_ids.update(choices)
        pending_rows.append(pending_view(row, choices, current=True))
    # 主库合并时，两个原始身份在清单里折叠成一个成员，各自的源证据仍可查。
    grouped = {}
    originals = defaultdict(list)
    for row in links:
        target = current[row.part_id]
        item = member_view(row, target)
        pair = (row.model_id, target)
        originals[pair].append(row.part_id)
        if pair in grouped:
            item["rawPns"] = list(dict.fromkeys([*grouped[pair]["rawPns"], *item["rawPns"]]))
            for field in ("position", "condition", "source"):
                item[field] = joined(grouped[pair][field], item[field])
        grouped[pair] = item
    for (mid, pid), item in grouped.items():
        item["membershipVersion"] = membership_version(mid, pid, originals[(mid, pid)])
    return {
        "version": 1,
        "revision": revision(db),
        "models": list(models.values()),
        "parts": part_views(db, visible_ids),
        "members": list(grouped.values()),
        "pending": pending_rows,
    }

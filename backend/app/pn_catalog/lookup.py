"""Bounded exact identity discovery for both user interfaces; never a fuzzy binding."""
from sqlalchemy import func, select

from app.etl.cleaner import standardize_pn
from app.models.dimensions import DimPart, PartAlias
from . import api
from .errors import CatalogError
from .models import PartState
from .persistence import acquire_identity_lock


def lookup_parts(db, raw_values, *, context, authorize):
    decision = api._authorize(db, context, authorize, "list", None)
    if decision.metadata.get("scope") != "all":
        raise CatalogError("auth.forbidden", "当前资料范围不支持此查询", status_code=403)
    if not 1 <= len(raw_values) <= 100 or any(type(v) is not str or not v.strip() or len(v) > 128 for v in raw_values):
        raise CatalogError("request.validation_failed", "需要 1–100 个非空 PN，每个不超过128字符", status_code=422)
    manual = [api.CreatePart.canonical_pn(v) for v in raw_values]
    normalized = [standardize_pn(v)[0] for v in raw_values]
    keys = set(manual) | {v for v in normalized if v}
    acquire_identity_lock(db, shared=True)
    # The graph projection excludes descriptions, source JSON and other field-granted data.
    def rows(statement):
        return db.execute(statement).mappings().all()
    columns = (DimPart.id, DimPart.pn_std, DimPart.status, DimPart.merged_into_id,
        func.coalesce(PartState.identity_revision, 0).label("identity_revision"),
        func.coalesce(PartState.availability, "active").label("availability"))
    base = select(*columns).outerjoin(PartState, PartState.part_id == DimPart.id)
    direct = rows(base.where(DimPart.pn_std.in_(keys)))
    parts = {row["id"]: row for row in direct}
    by_name = {row["pn_std"]: row["id"] for row in direct}
    aliases = rows(select(PartAlias.pn_raw, PartAlias.part_id, PartAlias.status).where(
        func.upper(func.trim(PartAlias.pn_raw)).in_(manual), PartAlias.status.in_(("active", "pending"))).limit(1001))
    if len(aliases) > 1000:
        raise CatalogError("catalog.lookup_limit", "精确别名候选过多，请缩小查询批次")
    targets = {row["part_id"] for row in aliases if row["status"] == "active" and row["part_id"] is not None}
    targets.update(row["merged_into_id"] for row in direct if row["status"] == "merged" and row["merged_into_id"])
    for _ in range(64):
        pending = targets - parts.keys()
        if not pending:
            break
        found = rows(base.where(DimPart.id.in_(pending)))
        parts.update({row["id"]: row for row in found})
        if pending - parts.keys():
            raise CatalogError("identity.invalid_state", "精确来源关联缺失，需核对")
        targets.update(row["merged_into_id"] for row in found if row["status"] == "merged" and row["merged_into_id"])
    def canonical(pid):
        seen = set()
        while pid in parts and parts[pid]["status"] == "merged" and pid not in seen:
            seen.add(pid)
            pid = parts[pid]["merged_into_id"]
        if pid not in parts or pid in seen:
            raise CatalogError("identity.invalid_state", "精确来源身份关联无效")
        row = parts[pid]
        return row if row["status"] == "active" and row["availability"] == "active" else None
    result = []
    for raw, key, normalized_pn in zip(raw_values, manual, normalized):
        candidate_ids = {by_name[v] for v in {key, normalized_pn} if v in by_name}
        related = [a for a in aliases if a["pn_raw"].strip().upper() == key]
        candidate_ids.update(a["part_id"] for a in related if a["status"] == "active" and a["part_id"] is not None)
        pending_alias = any(a["status"] == "pending" or a["part_id"] is None for a in related)
        matches = {p["id"]: p for pid in candidate_ids if (p := canonical(pid)) is not None}
        status = "ambiguous" if pending_alias or len(matches) > 1 else "matched" if matches else "unresolved"
        values = [{"part_id": p["id"], "pn_std": p["pn_std"], "identity_revision": p["identity_revision"]}
                  for p in sorted(matches.values(), key=lambda row: row["id"])[:10]]
        result.append({"raw": raw, "normalized_pn": key, "import_normalized_pn": normalized_pn,
            "status": status, "pending_alias": pending_alias, "matches": values, "has_more": len(matches) > 10})
    return {"data": result, "meta": {"request_id": str(context.request_id), "replayed": False, "next_cursor": None}}

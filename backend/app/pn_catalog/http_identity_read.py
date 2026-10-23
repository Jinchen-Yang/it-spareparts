"""Bounded identity selectors shared by both hosts, under current identity grants."""
import hashlib
import json

from fastapi import Query, Request
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import aliased

from app.models.dimensions import DimPart, PartAlias
from app.models.master_data import ProductMergeLog

from . import api
from .errors import CatalogError
from .http import AccessDep, DbDep, _context, router
from .wire_identity_read import AliasListResponse, MergeLogListResponse


def _read(db, request, access, field, parameters):
    context = _context(request, access)
    decision = api._authorize(db, context, access.authorize, "manage_identity", None)
    if decision.metadata.get("scope") != "all" or not api._allowed(decision.readable_fields, field):
        raise CatalogError("auth.forbidden", "没有当前身份资料的读取权限", status_code=403)
    # Reuse the catalog cursor format, bound to current actor, grants and query.
    fingerprint = hashlib.sha256(json.dumps([
        context.actor.principal_id, context.actor.client_id, field, parameters,
        sorted(decision.readable_fields), decision.metadata,
    ], sort_keys=True, default=str).encode()).hexdigest()
    return context, fingerprint


def _page(context, rows, limit, fingerprint, id_key):
    visible = [dict(row) for row in rows[:limit]]
    return {"data": visible, "meta": {"request_id": str(context.request_id),
        "replayed": False, "next_cursor": api._encode_cursor(visible[-1][id_key], fingerprint)
        if len(rows) > limit else None}}


@router.get("/identity/merge-logs", response_model=MergeLogListResponse)
def merge_logs(request: Request, db: DbDep, access: AccessDep,
               part_id: int | None = Query(default=None, gt=0),
               cursor: str | None = Query(default=None, max_length=512),
               limit: int = Query(default=20, ge=1, le=100)):
    context, fingerprint = _read(db, request, access, "identity", [part_id, "merge-logs"])
    before = api._decode_cursor(cursor, fingerprint)
    source, target = aliased(DimPart), aliased(DimPart)
    other_log = aliased(ProductMergeLog)
    latest = select(func.max(other_log.id)).where(
        other_log.source_part_id == ProductMergeLog.source_part_id).correlate(ProductMergeLog).scalar_subquery()
    statement = select(
        ProductMergeLog.id.label("merge_log_id"), ProductMergeLog.source_part_id,
        ProductMergeLog.target_part_id, source.pn_std.label("source_pn"),
        target.pn_std.label("target_pn"), ProductMergeLog.created_at,
        func.coalesce(and_(source.status == "merged", source.merged_into_id == target.id,
             target.status == "active", ProductMergeLog.id == latest), False).label("relationship_active"),
    ).join(source, source.id == ProductMergeLog.source_part_id).join(target, target.id == ProductMergeLog.target_part_id)
    if part_id is not None:
        statement = statement.where(or_(ProductMergeLog.source_part_id == part_id,
                                         ProductMergeLog.target_part_id == part_id))
    if before:
        statement = statement.where(ProductMergeLog.id < before)
    rows = db.execute(statement.order_by(ProductMergeLog.id.desc()).limit(limit + 1)).mappings().all()
    return _page(context, rows, limit, fingerprint, "merge_log_id")


@router.get("/identity/aliases", response_model=AliasListResponse)
def aliases(request: Request, db: DbDep, access: AccessDep,
            q: str = Query(default="", max_length=256),
            cursor: str | None = Query(default=None, max_length=512),
            limit: int = Query(default=20, ge=1, le=100)):
    q = q.strip()
    context, fingerprint = _read(db, request, access, "aliases", [q, "aliases"])
    after = api._decode_cursor(cursor, fingerprint)
    statement = select(PartAlias.id.label("alias_id"), PartAlias.pn_raw, PartAlias.part_id,
                       PartAlias.pn_std, PartAlias.status, PartAlias.created_at).where(PartAlias.id > after)
    if q:
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        statement = statement.where(or_(PartAlias.pn_raw.ilike(f"%{escaped}%", escape="\\"),
                                         PartAlias.pn_std.ilike(f"%{escaped}%", escape="\\")))
    rows = db.execute(statement.order_by(PartAlias.id).limit(limit + 1)).mappings().all()
    return _page(context, rows, limit, fingerprint, "alias_id")

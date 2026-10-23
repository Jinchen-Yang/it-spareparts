"""Exact source UUID resolution; no public arbitrary rebinding endpoint."""
from uuid import UUID

from fastapi import Path, Query, Request

from .errors import CatalogError
from .http import AccessDep, DbDep, _context, router
from .reference_dto import ReferenceListResponse, ReferenceResponse
from .references import resolve_references


@router.get("/references", response_model=ReferenceListResponse)
def reference_list(request: Request, db: DbDep, access: AccessDep,
                   namespace: str = Query(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:-]*$"),
                   source_ids: list[UUID] = Query(min_length=1, max_length=100)):
    return resolve_references(db, namespace=namespace, source_ids=source_ids,
                              context=_context(request, access), authorize=access.authorize)


@router.get("/references/{namespace}/{source_id}", response_model=ReferenceResponse)
def reference_get(request: Request, db: DbDep, access: AccessDep, source_id: UUID,
                  namespace: str = Path(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:-]*$")):
    result = resolve_references(db, namespace=namespace, source_ids=[source_id],
                                context=_context(request, access), authorize=access.authorize)
    if result["data"][0]["state"] == "unresolved":
        raise CatalogError("catalog.reference_not_found", "来源编号尚未绑定", status_code=404)
    return {"data": result["data"][0], "meta": result["meta"]}

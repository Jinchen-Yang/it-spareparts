"""Exact source UUID resolution; no public arbitrary rebinding endpoint."""
from uuid import UUID

from fastapi import Path, Query, Request

from .errors import CatalogError
from .http import AccessDep, DbDep, PartId, _commit, _context, _WRITE_HEADERS, router
from .reference_dto import BindReference, ReferenceListResponse, ReferenceMutationResponse, ReferenceResponse
from .references import bind_existing_reference, resolve_references


@router.post("/parts/{part_id}/references", response_model=ReferenceMutationResponse, openapi_extra=_WRITE_HEADERS)
def reference_bind(part_id: PartId, command: BindReference, request: Request, db: DbDep, access: AccessDep):
    return _commit(db, lambda: bind_existing_reference(db, part_id, command,
        context=_context(request, access, write=True), authorize=access.authorize))


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

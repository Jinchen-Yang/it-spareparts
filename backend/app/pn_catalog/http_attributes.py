"""HTTP commit boundary for independent JSON attributes."""
from fastapi import Request

from . import api
from .attribute_dto import PatchAttributes
from .http import AccessDep, DbDep, PartId, _WRITE_HEADERS, _commit, _context, router
from .wire_attributes import AttributesResponse


@router.get("/parts/{part_id}/attributes", response_model=AttributesResponse)
def get_attributes(part_id: PartId, request: Request, db: DbDep, access: AccessDep):
    return api.get_attributes(db, part_id, context=_context(request, access), authorize=access.authorize)


@router.patch("/parts/{part_id}/attributes", response_model=AttributesResponse, openapi_extra=_WRITE_HEADERS)
def patch_attributes(part_id: PartId, command: PatchAttributes, request: Request, db: DbDep, access: AccessDep):
    context = _context(request, access, write=True)
    return _commit(db, lambda: api.patch_attributes(db, part_id, command, context=context, authorize=access.authorize))

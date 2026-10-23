"""Authentication exchange is separate from the business OpenAPI/client surface."""

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import Field

from app.services.pn_catalog_access import exchange_delegation, get_part_access
from .dto import StrictDTO
from .http import (
    AccessDep,
    CatalogRoute,
    DbDep,
    PartId,
    _commit,
    _context,
    router as business_router,
)
from .wire import ErrorResponse, PageMeta, WireModel


class DelegationRequest(StrictDTO):
    proof: str = Field(min_length=1, max_length=16384)


class DelegationResponse(WireModel):
    token: str = Field(min_length=1, max_length=16384)
    expires_at: int = Field(ge=0)
    token_type: Literal["PN-Delegation"]


router = APIRouter(
    prefix="/pn-catalog/v1/auth",
    tags=["pn-catalog-authority"],
    route_class=CatalogRoute,
    responses={
        code: {"model": ErrorResponse}
        for code in (400, 401, 403, 409, 413, 422, 500, 503)
    },
)


@router.post(
    "/delegations",
    response_model=DelegationResponse,
    openapi_extra={"x-server-only": True},
)
def exchange(command: DelegationRequest, db: DbDep):
    # The signed server-session proof is this endpoint's credential. Never take
    # identity/grants from an ordinary request body or echo the proof in errors.
    return _commit(db, lambda: exchange_delegation(db, command.proof))


class PartAccessSnapshot(WireModel):
    part_id: int = Field(gt=0)
    allowed_actions: list[str]
    readable_fields: list[str]
    writable_fields: list[str]


class PartAccessResponse(WireModel):
    data: PartAccessSnapshot
    meta: PageMeta


class CatalogAccessSnapshot(WireModel):
    allowed_actions: list[str]
    readable_fields: list[str]
    writable_fields: list[str]


class CatalogAccessResponse(WireModel):
    data: CatalogAccessSnapshot
    meta: PageMeta


@business_router.get("/access", response_model=CatalogAccessResponse)
def catalog_access(request: Request, db: DbDep, access: AccessDep):
    context = _context(request, access)
    value = get_part_access(db, access.actor, None)
    value.pop("part_id")
    return {"data": value, "meta": {"request_id": str(context.request_id),
        "replayed": False, "next_cursor": None}}


@business_router.get("/parts/{part_id}/access", response_model=PartAccessResponse)
def part_access(part_id: PartId, request: Request, db: DbDep, access: AccessDep):
    context = _context(request, access)
    return {
        "data": get_part_access(db, access.actor, part_id),
        "meta": {
            "request_id": str(context.request_id),
            "replayed": False,
            "next_cursor": None,
        },
    }

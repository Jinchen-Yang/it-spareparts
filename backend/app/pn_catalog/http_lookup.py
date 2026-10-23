"""Read-only, body-bound exact lookup; no miss logging or implicit identity writes."""
from typing import Annotated, Literal

from fastapi import Request
from pydantic import Field

from .dto import StrictDTO
from .http import AccessDep, DbDep, _context, router
from .lookup import lookup_parts
from .wire import PageMeta, WireModel


class LookupParts(StrictDTO):
    raw_values: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(min_length=1, max_length=100)


class LookupMatch(WireModel):
    part_id: int = Field(gt=0)
    pn_std: str
    identity_revision: int = Field(ge=0)


class LookupResult(WireModel):
    raw: str
    normalized_pn: str
    import_normalized_pn: str | None
    status: Literal["matched", "unresolved", "ambiguous"]
    pending_alias: bool
    matches: list[LookupMatch]
    has_more: bool


class LookupResponse(WireModel):
    data: list[LookupResult]
    meta: PageMeta


@router.post("/parts/lookup", response_model=LookupResponse, openapi_extra={"x-read-only": True})
def lookup(command: LookupParts, request: Request, db: DbDep, access: AccessDep):
    return lookup_parts(db, command.raw_values, context=_context(request, access), authorize=access.authorize)

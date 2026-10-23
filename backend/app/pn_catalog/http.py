"""Versioned HTTP adapter. The facade owns domain rules; this owns commit."""
import logging
import re
from functools import partial
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import Field
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

from app.db import get_db
from app.pn_catalog.authz import CatalogAccess, current_access
from app.pn_catalog.dto import (
    CommandContext,
    CreatePart,
    PatchProfile,
    SetAvailability,
    StrictDTO,
)
from app.pn_catalog.errors import CatalogError
from app.pn_catalog.http_controls import CatalogHttpControls, error_body
from app.pn_catalog.wire import (
    AuditListResponse,
    ErrorResponse,
    PageMeta,
    PartListResponse,
    PartResponse,
    WireModel,
)
from app.pn_catalog.wire_attributes import AttributesResponse
from app.pn_catalog.wire_content import CandidateResponse, ContentResponse
from app.pn_catalog.reference_dto import ReferenceMutationResponse
from app.pn_catalog.wire_jobs import IdentityResponse, JobResponse
from app.pn_catalog.wire_media import MediaReceiptResponse

_log = logging.getLogger(__name__)
PartId = Annotated[int, Path(gt=0)]
DbDep = Annotated[Session, Depends(get_db)]
AccessDep = Annotated[CatalogAccess, Depends(current_access)]


class CatalogRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def wrapped(request):
            request_id = request.state.pn_catalog_request_id
            try:
                return await handler(request)
            except CatalogError as exc:
                return JSONResponse(error_body(exc.code, exc.message, request_id,
                    details=exc.details, retryable=exc.retryable), status_code=exc.status_code)
            except RequestValidationError as exc:
                errors = exc.errors()
                missing_base = any(e["type"] == "missing" and str(e["loc"][-1]).startswith("expected_")
                                   for e in errors)
                invalid_json = any(e["type"] == "json_invalid" for e in errors)
                code = ("request.precondition_required" if missing_base else
                        "request.invalid_json" if invalid_json else "request.validation_failed")
                # Do not echo arbitrary input, token-like values or exception objects.
                fields = [{"path": "/" + "/".join(map(str, e["loc"])), "type": e["type"]}
                          for e in errors]
                return JSONResponse(error_body(code, "请求参数或编辑基线无效", request_id,
                    details={"fields": fields}), status_code=428 if missing_base else 400 if invalid_json else 422)
            except HTTPException as exc:
                code = {401: "auth.unauthenticated", 403: "auth.forbidden", 404: "catalog.part_not_found",
                        503: "auth.unavailable"}.get(exc.status_code, "request.validation_failed")
                return JSONResponse(error_body(code, "请求未通过验证", request_id),
                                    status_code=exc.status_code)
            except (DBAPIError, PoolTimeoutError):
                _log.exception("catalog database failure request_id=%s", request_id)
                return JSONResponse(error_body("write.temporarily_busy", "服务暂时繁忙，请沿用原请求号重试",
                                    request_id, retryable=True), status_code=503)
            except Exception:  # noqa: BLE001 - the HTTP boundary returns a stable, sanitized 500
                _log.exception("catalog unexpected failure request_id=%s", request_id)
                return JSONResponse(error_body("internal.unexpected", "处理结果暂时无法确认，请查询原请求回执",
                                               request_id), status_code=500)
        return wrapped


router = APIRouter(prefix="/pn-catalog/v1", tags=["pn-catalog-v1"], route_class=CatalogRoute,
                   responses={code: {"model": ErrorResponse} for code in
                              (400, 401, 403, 404, 409, 410, 412, 413, 422, 428, 429, 500, 503)})
_WRITE_HEADERS = {"parameters": [{"name": "Idempotency-Key", "in": "header", "required": True,
                                "schema": {"type": "string", "format": "uuid"}}]}
_PROFILE_HEADERS = {"parameters": [*_WRITE_HEADERS["parameters"],
    {"name": "If-Match", "in": "header", "required": False,
     "schema": {"type": "string", "pattern": '^"profile-[0-9]+"$'}}]}


def _context(request: Request, access: CatalogAccess, *, write=False, allow_if_match=False):
    if write:
        key = request.headers.get("Idempotency-Key")
        if not key:
            raise CatalogError("request.precondition_required", "缺少 Idempotency-Key", status_code=428)
        try:
            UUID(key)
        except ValueError:
            raise CatalogError("request.validation_failed", "Idempotency-Key 必须是 UUID", status_code=422) from None
    version = None
    if value := request.headers.get("If-Match"):
        match = re.fullmatch(r'"profile-([0-9]+)"', value)
        if not allow_if_match or match is None:
            raise CatalogError("request.validation_failed", "If-Match 与本操作的资源版本不匹配", status_code=422)
        version = int(match[1])
    return CommandContext(actor=access.actor, request_id=request.state.pn_catalog_request_id,
                          if_match_revision=version)


def _commit(db, operation):
    try:
        result = operation()
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


class CapabilitySnapshot(WireModel):
    media: bool
    bundle: bool
    research: bool
    recognition: bool


class CapabilityResponse(WireModel):
    data: CapabilitySnapshot
    meta: PageMeta


@router.get("/capabilities", response_model=CapabilityResponse)
def capabilities(request: Request, access: AccessDep):
    """Enabled host features; actual operations still verify current grants and configuration."""
    configured = getattr(request.app.state, "pn_catalog_capabilities", {})
    context = _context(request, access)
    return {"data": {key: bool(configured.get(key, False)) for key in
                     ("media", "bundle", "research", "recognition")},
            "meta": {"request_id": str(context.request_id), "replayed": False, "next_cursor": None}}


@router.get("/parts", response_model=PartListResponse)
def list_parts(request: Request, db: DbDep, access: AccessDep, q: str | None = Query(default=None, max_length=256),
               cursor: str | None = Query(default=None, max_length=2048),
               limit: int = Query(default=50, ge=1, le=200), include_inactive: bool = False,
               category_major: str | None = Query(default=None, max_length=128),
               part_ids: list[int] | None = Query(default=None, max_length=200),
               include_gallery: bool = False, missing_image: bool | None = None):
    from app.pn_catalog import api
    return api.list_parts(db, context=_context(request, access), authorize=access.authorize,
                          query=q or "", cursor=cursor, limit=limit, include_inactive=include_inactive,
                          category_major=category_major, part_ids=part_ids, include_gallery=include_gallery,
                          missing_image=missing_image)


@router.post("/parts", status_code=201, response_model=PartResponse, openapi_extra=_WRITE_HEADERS)
def create_part(command: CreatePart, request: Request, db: DbDep,
                access: AccessDep):
    from app.pn_catalog import api
    context = _context(request, access, write=True)
    return _commit(db, lambda: api.create_part(db, command, context=context,
                    authorize=access.authorize, confirmation_secret=access.confirmation_secret))


@router.get("/parts/{part_id}", response_model=PartResponse)
def get_part(part_id: PartId, request: Request, response: Response, db: DbDep,
             access: AccessDep):
    from app.pn_catalog import api
    result = api.get_part(db, part_id, context=_context(request, access), authorize=access.authorize)
    response.headers["ETag"] = f'"profile-{result["data"]["profile_revision"]}"'
    return result


@router.patch("/parts/{part_id}/profile", response_model=PartResponse, openapi_extra=_PROFILE_HEADERS)
def patch_profile(part_id: PartId, command: PatchProfile, request: Request,
                  db: DbDep, access: AccessDep):
    from app.pn_catalog import api
    context = _context(request, access, write=True, allow_if_match=True)
    return _commit(db, lambda: api.patch_profile(db, part_id, command, context=context,
                                                authorize=access.authorize))


class AvailabilityCommand(StrictDTO):
    expected_identity_revision: int = Field(ge=0)
    expected_revision: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=2000)


def _availability(part_id, command, request, db, access, state):
    from app.pn_catalog import api
    context = _context(request, access, write=True)
    dto = SetAvailability(**command.model_dump(), availability=state)
    return _commit(db, lambda: api.set_availability(db, part_id, dto, context=context,
                                                   authorize=access.authorize))


@router.post("/parts/{part_id}/deactivation", response_model=PartResponse, openapi_extra=_WRITE_HEADERS)
def deactivate(part_id: PartId, command: AvailabilityCommand, request: Request,
               db: DbDep, access: AccessDep):
    return _availability(part_id, command, request, db, access, "inactive")


@router.post("/parts/{part_id}/reactivation", response_model=PartResponse, openapi_extra=_WRITE_HEADERS)
def reactivate(part_id: PartId, command: AvailabilityCommand, request: Request,
               db: DbDep, access: AccessDep):
    return _availability(part_id, command, request, db, access, "active")


@router.get("/requests/{request_id}", response_model=AttributesResponse | PartResponse | ContentResponse | CandidateResponse | MediaReceiptResponse | JobResponse | IdentityResponse | ReferenceMutationResponse,
            response_model_exclude_unset=True)
def get_receipt(request_id: UUID, request: Request, db: DbDep,
                access: AccessDep):
    from app.pn_catalog import api

    from .http_jobs import project_host_receipt
    return api.get_receipt(db, request_id, context=_context(request, access), authorize=access.authorize,
                           projector=partial(project_host_receipt, access=access))


@router.get("/parts/{part_id}/audit-events", response_model=AuditListResponse)
def audit_events(part_id: PartId, request: Request, db: DbDep, access: AccessDep,
                 cursor: str | None = Query(default=None, max_length=2048),
                 limit: int = Query(default=50, ge=1, le=200)):
    from app.pn_catalog import api
    return api.list_audit_events(db, part_id, context=_context(request, access),
                          authorize=access.authorize, cursor=cursor, limit=limit)


def install_http(app, *, api_prefix="/api"):
    """Keep host HTTP errors; catalog domain errors also cover migrated legacy routes."""
    from fastapi.exception_handlers import http_exception_handler
    prefix = api_prefix.rstrip("/") + router.prefix
    previous = app.exception_handlers.get(StarletteHTTPException, http_exception_handler)

    async def host_http_error(request, exc):
        if request.url.path == prefix or request.url.path.startswith(prefix + "/"):
            code = "request.method_not_allowed" if exc.status_code == 405 else "request.route_not_found"
            return JSONResponse(error_body(code, "接口路径或请求方法无效",
                    request.state.pn_catalog_request_id), status_code=exc.status_code,
                    headers=exc.headers)
        return await previous(request, exc)

    async def catalog_domain_error(request, exc):
        request_id = getattr(request.state, "pn_catalog_request_id", None) or uuid4()
        return JSONResponse(error_body(exc.code, exc.message, request_id,
            details=exc.details, retryable=exc.retryable), status_code=exc.status_code,
            headers={"Cache-Control": "no-store"})

    app.add_exception_handler(CatalogError, catalog_domain_error)
    app.add_exception_handler(StarletteHTTPException, host_http_error)
    app.add_middleware(CatalogHttpControls, path_prefix=prefix)
    app.include_router(router, prefix=api_prefix)
    app.include_router(_authority_routes.router, prefix=api_prefix)


# Resource adapters attach to this single versioned router after boundary helpers exist.
from . import http_content as _content_routes  # noqa: E402,F401
from . import http_enrichment as _enrichment_routes  # noqa: E402,F401
from . import http_jobs as _job_routes  # noqa: E402,F401
from . import http_media as _media_routes  # noqa: E402,F401

from . import http_authority as _authority_routes  # noqa: E402,F401

from . import http_references as _reference_routes  # noqa: E402,F401
from . import http_attributes as _attribute_routes  # noqa: E402,F401
from . import http_lookup as _lookup_routes  # noqa: E402,F401

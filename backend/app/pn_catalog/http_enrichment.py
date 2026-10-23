"""Explicit manual actions. GET endpoints do not create or poll provider work."""
from uuid import UUID
from fastapi import Request
from app.services import pn_catalog_enrichment as commands
from .enrichment_dto import CreateBundle, RecognizeInbox, ResearchPN
from .errors import CatalogError
from .http import AccessDep, DbDep, PartId, _WRITE_HEADERS, _commit, _context, router
from .wire_enrichment import BundleDownloadResponse
from .wire_jobs import JobResponse


def _enabled(request, capability):
    if not getattr(request.app.state, "pn_catalog_capabilities", {}).get(capability, False):
        raise CatalogError("enrichment.not_configured", "此辅助功能尚未配置", status_code=503)


@router.post("/inbox/{inbox_id}/recognition", response_model=JobResponse, openapi_extra=_WRITE_HEADERS)
def recognize(inbox_id: UUID, command: RecognizeInbox, request: Request, db: DbDep, access: AccessDep):
    _enabled(request, "recognition")
    context = _context(request, access, write=True)
    return _commit(db, lambda: commands.recognize(db, inbox_id, command, context=context, authorize=access.authorize))


@router.post("/parts/{part_id}/research", response_model=JobResponse, openapi_extra=_WRITE_HEADERS)
def research(part_id: PartId, command: ResearchPN, request: Request, db: DbDep, access: AccessDep):
    _enabled(request, "research")
    context = _context(request, access, write=True)
    return _commit(db, lambda: commands.research(db, part_id, command, context=context, authorize=access.authorize))


@router.post("/parts/{part_id}/bundles", response_model=JobResponse, openapi_extra=_WRITE_HEADERS)
def bundle(part_id: PartId, command: CreateBundle, request: Request, db: DbDep, access: AccessDep):
    _enabled(request, "bundle")
    context = _context(request, access, write=True)
    return _commit(db, lambda: commands.bundle(db, part_id, command, context=context, authorize=access.authorize))


@router.get("/jobs/{job_id}/bundle-downloads", response_model=BundleDownloadResponse)
def downloads(job_id: UUID, request: Request, db: DbDep, access: AccessDep):
    storage = getattr(request.app.state, "pn_media_storage", None)
    if storage is None:
        raise CatalogError("media.not_configured", "图片存储尚未配置", status_code=503)
    context = _context(request, access)
    result = commands.bundle_downloads(db, job_id, context=context, authorize=access.authorize, storage=storage)
    result["meta"]["request_id"] = str(context.request_id)
    return result

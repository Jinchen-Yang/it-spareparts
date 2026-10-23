"""Host identity orchestration and opaque worker-job operations."""
from typing import Annotated
from uuid import UUID

from fastapi import Body, Query, Request

from app.config import get_settings
from app.services import pn_catalog_identity

from . import jobs
from .errors import CatalogError
from .http import _WRITE_HEADERS, AccessDep, DbDep, _commit, _context, router
from .job_dto import CancelJob, JobStatus, RetryJob
from .wire_jobs import IdentityPreviewResponse, IdentityResponse, JobResponse, JobListResponse


def require_job_authorizer(access):
    if access.authorize_job is None:
        raise CatalogError("auth.unavailable", "任务资源授权尚未接入", status_code=503)
    return access.authorize_job


def project_host_receipt(db, receipt, *, context, authorize, replayed, access):
    if receipt.result["resource"] == "identity":
        return pn_catalog_identity.project_receipt(db, receipt, context=context, authorize=authorize, replayed=replayed)
    if receipt.result["resource"] == "job":
        return jobs.project_receipt(db, receipt, context=context,
                                    authorize_job=require_job_authorizer(access), replayed=replayed)
    raise CatalogError("request.not_found", "此入口无法读取该资源回执", status_code=404)


@router.get("/jobs", response_model=JobListResponse)
def list_jobs(request: Request, db: DbDep, access: AccessDep,
              part_id: Annotated[int | None, Query(gt=0)] = None,
              inbox_id: UUID | None = None,
              status: Annotated[list[JobStatus] | None, Query(max_length=6)] = None,
              cursor: Annotated[str | None, Query(max_length=512)] = None,
              limit: Annotated[int, Query(ge=1, le=50)] = 20):
    return jobs.list_jobs_authorized(db, context=_context(request, access),
        authorize_job=require_job_authorizer(access), cursor_secret=get_settings().secret_key.encode(),
        part_id=part_id, inbox_id=inbox_id, statuses=status or (), cursor=cursor, limit=limit)


@router.get("/jobs/{job_id}", response_model=JobResponse, response_model_exclude_unset=True)
def get_job(job_id: UUID, request: Request, db: DbDep, access: AccessDep):
    context = _context(request, access)
    value = jobs.get_job_authorized(db, job_id, context=context, authorize_job=require_job_authorizer(access))
    value["meta"].update(request_id=str(context.request_id), replayed=False)
    return value


@router.post("/jobs/{job_id}/cancellation", response_model=JobResponse, openapi_extra=_WRITE_HEADERS)
def cancel_job(job_id: UUID, command: CancelJob, request: Request, db: DbDep, access: AccessDep):
    context = _context(request, access, write=True)
    return _commit(db, lambda: jobs.cancel_job(db, job_id, command, context=context,
                   authorize_job=require_job_authorizer(access)))


@router.post("/jobs/{job_id}/retry", response_model=JobResponse, openapi_extra=_WRITE_HEADERS)
def retry_job(job_id: UUID, command: RetryJob, request: Request, db: DbDep, access: AccessDep):
    context = _context(request, access, write=True)
    return _commit(db, lambda: jobs.retry_job(db, job_id, command, context=context,
                   authorize_job=require_job_authorizer(access)))


@router.post("/identity/preview", response_model=IdentityPreviewResponse, response_model_exclude_unset=True)
def identity_preview(command: pn_catalog_identity.IdentityPreview, request: Request, db: DbDep, access: AccessDep):
    context = _context(request, access)
    return {"data": pn_catalog_identity.preview(db, command, context=context, authorize=access.authorize),
            "meta": {"request_id": str(context.request_id), "replayed": False, "next_cursor": None}}


@router.post("/identity/commands", response_model=IdentityResponse, openapi_extra=_WRITE_HEADERS,
             response_model_exclude_unset=True)
def identity_command(command: Annotated[pn_catalog_identity.IdentityCommand, Body(discriminator="operation")],
                     request: Request, db: DbDep, access: AccessDep):
    context = _context(request, access, write=True)
    return _commit(db, lambda: pn_catalog_identity.execute(db, command, context=context, authorize=access.authorize))

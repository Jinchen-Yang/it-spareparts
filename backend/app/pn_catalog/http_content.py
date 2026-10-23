"""Content and proposal HTTP adapter. Permission and transaction rules remain in the facade."""
from uuid import UUID

from fastapi import Query, Request

from . import api
from .content_dto import (
    AdoptCandidate,
    CandidateStatus,
    CreateCandidate,
    Grade,
    PatchContent,
    RejectCandidate,
)
from .http import _WRITE_HEADERS, AccessDep, DbDep, PartId, _commit, _context, router
from .wire_content import CandidateListResponse, CandidateResponse, ContentResponse


@router.get("/parts/{part_id}/content/{grade}", response_model=ContentResponse, response_model_exclude_unset=True)
def get_content(part_id: PartId, grade: Grade, request: Request, db: DbDep, access: AccessDep):
    return api.get_content(db, part_id, grade, context=_context(request, access), authorize=access.authorize)


@router.patch("/parts/{part_id}/content/{grade}", response_model=ContentResponse,
              response_model_exclude_unset=True, openapi_extra=_WRITE_HEADERS)
def patch_content(part_id: PartId, grade: Grade, command: PatchContent,
                  request: Request, db: DbDep, access: AccessDep):
    context = _context(request, access, write=True)
    return _commit(db, lambda: api.patch_content(db, part_id, grade, command,
                   context=context, authorize=access.authorize))


@router.get("/parts/{part_id}/candidates", response_model=CandidateListResponse)
def list_candidates(part_id: PartId, request: Request, db: DbDep, access: AccessDep,
                    status: CandidateStatus | None = None,
                    cursor: str | None = Query(default=None, max_length=2048),
                    limit: int = Query(default=50, ge=1, le=200)):
    return api.list_candidates(db, part_id, status=status, cursor=cursor, limit=limit,
                               context=_context(request, access), authorize=access.authorize)


@router.post("/candidates", response_model=CandidateResponse, status_code=201, openapi_extra=_WRITE_HEADERS)
def create_candidate(command: CreateCandidate, request: Request, db: DbDep, access: AccessDep):
    context = _context(request, access, write=True)
    return _commit(db, lambda: api.create_candidate(db, command, context=context, authorize=access.authorize))


@router.get("/candidates/{candidate_id}", response_model=CandidateResponse)
def get_candidate(candidate_id: UUID, request: Request, db: DbDep, access: AccessDep):
    return api.get_candidate(db, candidate_id, context=_context(request, access), authorize=access.authorize)


@router.post("/candidates/{candidate_id}/adoption", response_model=CandidateResponse, openapi_extra=_WRITE_HEADERS)
def adopt_candidate(candidate_id: UUID, command: AdoptCandidate, request: Request, db: DbDep, access: AccessDep):
    context = _context(request, access, write=True)
    return _commit(db, lambda: api.adopt_candidate(db, candidate_id, command, context=context, authorize=access.authorize))


@router.post("/candidates/{candidate_id}/rejection", response_model=CandidateResponse, openapi_extra=_WRITE_HEADERS)
def reject_candidate(candidate_id: UUID, command: RejectCandidate, request: Request, db: DbDep, access: AccessDep):
    context = _context(request, access, write=True)
    return _commit(db, lambda: api.reject_candidate(db, candidate_id, command, context=context, authorize=access.authorize))

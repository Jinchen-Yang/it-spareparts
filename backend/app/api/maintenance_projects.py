"""Read-only stable maintenance project APIs (#195)."""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy.orm import Session

from app.auth import current_role
from app.business_time import business_today
from app.db import get_db
from app.security import UserContext, get_current_user_context, record_access_log, require_page
from app.services import maintenance_project

router = APIRouter(prefix="/maintenance/projects/stable", tags=["maintenance"])


@router.get("")
def stable_project_directory(
    q: str | None = Query(None, max_length=128),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    include_inactive: bool = Query(False),
    as_of: date | None = Query(None),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    effective_as_of = as_of or business_today()
    record_access_log(
        ctx,
        "stable_project_directory",
        "maintenance",
        {"q": q, "include_inactive": include_inactive, "as_of": str(effective_as_of)},
    )
    return maintenance_project.project_directory(
        db,
        q_text=q,
        page=page,
        page_size=page_size,
        include_inactive=include_inactive,
        as_of=effective_as_of,
    )


@router.get("/{project_id}")
def stable_project_overview(
    project_id: str = Path(..., min_length=1, max_length=36),
    as_of: date | None = Query(None),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    effective_as_of = as_of or business_today()
    record_access_log(
        ctx,
        "stable_project_overview",
        "maintenance_project",
        {"project_id": project_id, "as_of": str(effective_as_of)},
    )
    payload = maintenance_project.project_overview(
        db,
        project_id,
        as_of=effective_as_of,
        user_ctx=ctx,
    )
    if payload is None:
        raise HTTPException(status_code=404, detail="维保项目不存在")
    return payload

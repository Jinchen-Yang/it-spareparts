"""Shared server-side row-scope guard for stable maintenance projects."""

from fastapi import Depends, HTTPException, Path, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.security import UserContext, get_current_user_context
from app.services import maintenance_project_assignments as assignments


def enforce_maintenance_project_access(
    db: Session,
    *,
    project_id: str,
    ctx: UserContext,
) -> None:
    """Fail closed for every direct project read/write, not only directories."""

    if not assignments.can_access_project(
        db,
        project_id=project_id,
        user_ctx=ctx,
    ):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "无权访问该维保项目",
        )


def require_maintenance_project_access(
    project_id: str = Path(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
) -> None:
    enforce_maintenance_project_access(
        db,
        project_id=project_id,
        ctx=ctx,
    )

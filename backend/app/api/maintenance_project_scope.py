"""FastAPI dependency adapter; public scope names remain compatible."""
from fastapi import Depends, Path
from sqlalchemy.orm import Session

from app.db import get_db
from app.security import UserContext, get_current_user_context
from app.services.maintenance_project_access import (
    enforce_maintenance_project_access,
    resolve_visible_project_ids,
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

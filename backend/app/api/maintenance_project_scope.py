"""Project-scope access control for maintenance APIs.

Resolves which projects the current user is allowed to read or write.
Admin sees everything; non-admin users only see projects where their
username matches ``maintenance_project.project_manager_id``.
"""

from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.maintenance_project import MaintenanceProject
from app.security import UserContext, get_current_user_context


# Sentinel returned for admin — the caller must treat None as "unrestricted".
_ADMIN_SCOPE_SENTINEL: object = object()


def resolve_project_ids_for_user(
    db: Session,
    ctx: UserContext,
) -> set[str] | None:
    """Return the set of project_ids visible to *ctx*, or None for admin.

    None means "all projects + unassigned demand lines".
    An empty set means "no projects at all".
    """
    if ctx.role == "admin":
        return None
    rows = db.scalars(
        select(MaintenanceProject.project_id).where(
            MaintenanceProject.project_manager_id == ctx.sub,
            MaintenanceProject.is_active.is_(True),
        )
    ).all()
    return set(rows)


def require_project_scope(
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
) -> set[str] | None:
    """FastAPI dependency: inject allowed project_ids or None (admin)."""
    return resolve_project_ids_for_user(db, ctx)


def require_project_access(
    allowed: set[str] | None,
    target_project_id: str,
) -> None:
    """Raise 403 if *allowed* is non-None and *target_project_id* is absent."""
    if allowed is not None and target_project_id not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无权访问该项目的数据",
        )

"""Shared server-side row-scope guard for stable maintenance projects."""

from fastapi import Depends, HTTPException, Path, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.maintenance_project import MaintenanceProject
from app.security import FULL_SCOPE_ROLES, UserContext, get_current_user_context
from app.services import maintenance_project_assignments as assignments
from app.authz import runtime
from app.authz.principal import current_verified_identity

_bearer = HTTPBearer(auto_error=False)


def identity_for(*resources):
    """Typed identity is obtained only from the verified transport adapter."""
    def dependency(db: Session = Depends(get_db), credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)):
        if any(runtime.is_authoritative(db, resource) for resource in resources):
            return current_verified_identity(credentials, db)
        return None
    return dependency


def legacy_gate(resources, gate):
    resources = (resources,) if isinstance(resources, str) else tuple(resources)
    def dependency(db: Session = Depends(get_db), ctx=Depends(get_current_user_context)):
        if not any(runtime.is_authoritative(db, resource) for resource in resources):
            return gate(ctx)
    return dependency


def legacy_page(resource, key):
    from app.security import require_page
    return legacy_gate(resource, require_page(key))


def legacy_project_scope(resource):
    resources = (resource,) if isinstance(resource, str) else tuple(resource)
    def dependency(project_id: str = Path(..., min_length=1, max_length=36),
                   db: Session = Depends(get_db), ctx=Depends(get_current_user_context)):
        if not any(runtime.is_authoritative(db, item) for item in resources):
            enforce_maintenance_project_access(db, project_id=project_id, ctx=ctx)
    return dependency


def enforce_maintenance_project_access(
    db: Session,
    *,
    project_id: str,
    ctx: UserContext,
) -> None:
    """Require an existing parent and row access for direct project reads/writes."""

    if db.scalar(select(MaintenanceProject.project_id).where(
        MaintenanceProject.project_id == project_id,
    )) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "维保项目不存在")

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


def resolve_visible_project_ids(
    db: Session,
    ctx: UserContext,
) -> set[str] | None:
    """Directory-level scope: None = full scope (admin), otherwise owned ids.

    Callers that need to filter a listing by project must treat None as
    "no filter" and an empty set as "no visible rows".

    2026-08-21：own_maintenance_projects_only 行键开的账号返回
    「维保负责人 ∪ 项目销售」并集（maintenance_scope_project_ids），
    未开键的账号维持 #205 挂靠负责人口径。
    """
    if ctx.role in FULL_SCOPE_ROLES:
        return None
    scoped = assignments.maintenance_scope_project_ids(db, ctx)
    if scoped is not None:
        return scoped
    return set(
        db.scalars(assignments.owned_project_ids(ctx)).all()
    )


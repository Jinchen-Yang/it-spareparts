"""Shared maintenance project scope policy for HTTP and read-model adapters.

Preserves the existing HTTPException contract during incremental migration.
None means unrestricted directory scope; an empty set must remain empty.
Direct project access and directory visibility intentionally differ.
"""
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.maintenance_project import MaintenanceProject
from app.security import FULL_SCOPE_ROLES, UserContext
from app.services import maintenance_project_assignments as assignments


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


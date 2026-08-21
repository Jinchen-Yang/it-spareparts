"""Explicit, auditable maintenance-project manager account assignments (#205)."""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import false, func, or_, select
from sqlalchemy.orm import Session

from app.models.maintenance_project import (
    MaintenanceProject,
    MaintenanceProjectAuditLog,
    MaintenanceProjectUserAssignment,
)
from app.models.system import SysUser
from app.security import FULL_SCOPE_ROLES, UserContext


class MaintenanceProjectAssignmentError(Exception):
    """The requested assignment is invalid."""


class MaintenanceProjectAssignmentConflict(Exception):
    """The assignment changed after the caller loaded it."""


class MaintenanceProjectAssignmentPermissionError(Exception):
    """The caller tried to expand their project row scope."""


def _required_text(value: str, label: str, limit: int) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise MaintenanceProjectAssignmentError(f"{label}不能为空")
    if len(cleaned) > limit:
        raise MaintenanceProjectAssignmentError(f"{label}不能超过 {limit} 个字符")
    return cleaned


def assignment_dict(
    assignment: MaintenanceProjectUserAssignment,
    user: SysUser,
) -> dict:
    return {
        "assignment_id": assignment.assignment_id,
        "project_id": assignment.project_id,
        "responsibility_type": assignment.responsibility_type,
        "user_id": assignment.user_id,
        "username": user.username,
        "display_name": user.display_name,
        "account_status": "active" if user.is_active else "inactive",
        "source_manager_text": assignment.source_manager_text,
        "version": assignment.version,
        "assigned_at": assignment.assigned_at.isoformat(),
        "archived_at": (
            assignment.archived_at.isoformat() if assignment.archived_at else None
        ),
    }


def active_assignment_views(
    db: Session,
    *,
    project_ids: list[str],
) -> dict[str, dict]:
    if not project_ids:
        return {}
    return {
        assignment.project_id: assignment_dict(assignment, user)
        for assignment, user in db.execute(
            select(MaintenanceProjectUserAssignment, SysUser)
            .join(SysUser, SysUser.id == MaintenanceProjectUserAssignment.user_id)
            .where(
                MaintenanceProjectUserAssignment.project_id.in_(project_ids),
                MaintenanceProjectUserAssignment.responsibility_type
                == "primary_manager",
                MaintenanceProjectUserAssignment.archived_at.is_(None),
            )
        )
    }


def resolve_owner_scope(
    user_ctx: UserContext,
    requested_scope: str | None,
) -> str:
    scope = requested_scope or (
        "all" if user_ctx.role in FULL_SCOPE_ROLES else "me"
    )
    if scope not in {"me", "all"}:
        raise MaintenanceProjectAssignmentError("项目负责人范围无效")
    if scope == "all" and user_ctx.role not in FULL_SCOPE_ROLES:
        raise MaintenanceProjectAssignmentPermissionError
    return scope


def owned_project_ids(user_ctx: UserContext):
    """Stable-project ids currently owned by the authenticated primary manager."""

    if not user_ctx.is_authenticated or not user_ctx.user_id:
        return select(MaintenanceProjectUserAssignment.project_id).where(false())
    return (
        select(MaintenanceProjectUserAssignment.project_id)
        .join(SysUser, SysUser.id == MaintenanceProjectUserAssignment.user_id)
        .where(
            MaintenanceProjectUserAssignment.responsibility_type == "primary_manager",
            MaintenanceProjectUserAssignment.archived_at.is_(None),
            SysUser.username == user_ctx.user_id,
            SysUser.is_active.is_(True),
        )
    )


def owned_project_condition(user_ctx: UserContext):
    return MaintenanceProject.project_id.in_(owned_project_ids(user_ctx))


def can_access_project(
    db: Session,
    *,
    project_id: str,
    user_ctx: UserContext,
) -> bool:
    if user_ctx.role in FULL_SCOPE_ROLES:
        return True
    # 2026-08-21 行键收敛：own_maintenance_projects_only 开 → 负责人∪销售并集判定
    from app.security import is_scoped_maintenance

    if is_scoped_maintenance(user_ctx):
        return project_id in (maintenance_scope_project_ids(db, user_ctx) or set())
    return bool(
        db.scalar(
            select(MaintenanceProjectUserAssignment.assignment_id)
            .join(SysUser, SysUser.id == MaintenanceProjectUserAssignment.user_id)
            .where(
                MaintenanceProjectUserAssignment.project_id == project_id,
                MaintenanceProjectUserAssignment.responsibility_type
                == "primary_manager",
                MaintenanceProjectUserAssignment.archived_at.is_(None),
                SysUser.username == user_ctx.user_id,
                SysUser.is_active.is_(True),
            )
        )
    )


def maintenance_scope_project_ids(
    db: Session, user_ctx: UserContext
) -> set[str] | None:
    """行键 own_maintenance_projects_only 的可见项目集（None = 全范围）。

    并集规则（2026-08-21 客户反馈拍板）：
    「我是项目维保负责人（primary_manager 挂靠本人账号）」∪
    「项目销售 = 我的销售名（台账 salesperson 事实源）」。
    两条件皆无匹配 → 空集，**绝不误放全量**。
    """
    from app.security import is_scoped_maintenance

    if not is_scoped_maintenance(user_ctx):
        return None
    ids: set[str] = set(db.scalars(owned_project_ids(user_ctx)).all())
    if user_ctx.salesperson_name:
        ids.update(
            db.scalars(
                select(MaintenanceProject.project_id).where(
                    MaintenanceProject.salesperson == user_ctx.salesperson_name
                )
            ).all()
        )
    return ids


def search_active_users(
    db: Session,
    *,
    q_text: str | None,
    page: int,
    page_size: int,
) -> dict:
    filters = [SysUser.is_active.is_(True)]
    if q_text and (search := q_text.strip()):
        filters.append(
            or_(
                SysUser.username.icontains(search, autoescape=True),
                SysUser.display_name.icontains(search, autoescape=True),
            )
        )
    total = int(
        db.scalar(select(func.count()).select_from(SysUser).where(*filters)) or 0
    )
    users = list(
        db.scalars(
            select(SysUser)
            .where(*filters)
            .order_by(
                SysUser.display_name.asc().nulls_last(),
                SysUser.username,
                SysUser.id,
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    return {
        "rows": [
            {
                "user_id": user.id,
                "username": user.username,
                "display_name": user.display_name,
                "is_active": user.is_active,
            }
            for user in users
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def assign_primary_manager(
    db: Session,
    *,
    project_id: str,
    user_id: int,
    expected_assignment_id: str | None,
    expected_assignment_version: int | None,
    reason: str,
    operated_by: str,
) -> dict | None:
    reason = _required_text(reason, "改派原因", 1000)
    operated_by = _required_text(operated_by, "操作人", 64)
    project = db.scalar(
        select(MaintenanceProject)
        .where(MaintenanceProject.project_id == project_id)
        .with_for_update()
    )
    if project is None:
        return None
    if not project.is_active:
        raise MaintenanceProjectAssignmentError("项目主档已归档")
    target_user = db.scalar(
        select(SysUser).where(SysUser.id == user_id).with_for_update()
    )
    if target_user is None:
        raise MaintenanceProjectAssignmentError("负责人账号不存在")
    if not target_user.is_active:
        raise MaintenanceProjectAssignmentError("负责人账号已停用")

    current = db.scalar(
        select(MaintenanceProjectUserAssignment)
        .where(
            MaintenanceProjectUserAssignment.project_id == project_id,
            MaintenanceProjectUserAssignment.responsibility_type == "primary_manager",
            MaintenanceProjectUserAssignment.archived_at.is_(None),
        )
        .with_for_update()
    )
    if current is None:
        if expected_assignment_id is not None or expected_assignment_version is not None:
            raise MaintenanceProjectAssignmentConflict("项目负责人已变化，请刷新后重试")
        before = None
    else:
        if (
            expected_assignment_id != current.assignment_id
            or expected_assignment_version != current.version
        ):
            raise MaintenanceProjectAssignmentConflict("项目负责人已变化，请刷新后重试")
        if current.user_id == target_user.id:
            raise MaintenanceProjectAssignmentError("所选账号已是当前主负责人")
        current_user = db.get(SysUser, current.user_id)
        if current_user is None:
            raise MaintenanceProjectAssignmentError("当前负责人账号记录缺失")
        before = assignment_dict(current, current_user)
        archived_at = datetime.now(UTC)
        current.archived_at = archived_at
        current.archived_by = operated_by
        current.archive_reason = reason
        current.version += 1

    assigned_at = datetime.now(UTC)
    assignment = MaintenanceProjectUserAssignment(
        assignment_id=str(uuid4()),
        project_id=project.project_id,
        responsibility_type="primary_manager",
        user_id=target_user.id,
        source_manager_text=project.project_manager_id,
        version=1,
        assigned_at=assigned_at,
        assigned_by=operated_by,
        assignment_reason=reason,
    )
    db.add(assignment)
    db.flush()
    after = assignment_dict(assignment, target_user)
    db.add(
        MaintenanceProjectAuditLog(
            project_id=project.project_id,
            entity_type="manager_assignment",
            entity_id=assignment.assignment_id,
            action="reassign" if before is not None else "assign",
            before_json=before,
            after_json=after,
            reason=reason,
            operated_by=operated_by,
        )
    )
    db.flush()
    return after


def archive_primary_manager(
    db: Session,
    *,
    assignment_id: str,
    version: int,
    reason: str,
    operated_by: str,
) -> dict | None:
    reason = _required_text(reason, "归档原因", 1000)
    operated_by = _required_text(operated_by, "操作人", 64)
    # Resolve the project first, then take locks in the same project ->
    # assignment order as ``assign_primary_manager``.  This avoids a lock-order
    # inversion when an archive races with a reassignment.
    assignment = db.scalar(
        select(MaintenanceProjectUserAssignment)
        .where(MaintenanceProjectUserAssignment.assignment_id == assignment_id)
    )
    if assignment is None:
        return None
    project_id = assignment.project_id
    project = db.scalar(
        select(MaintenanceProject)
        .where(MaintenanceProject.project_id == project_id)
        .with_for_update()
    )
    if project is None:
        return None
    assignment = db.scalar(
        select(MaintenanceProjectUserAssignment)
        .where(
            MaintenanceProjectUserAssignment.assignment_id == assignment_id,
            MaintenanceProjectUserAssignment.project_id == project_id,
        )
        .with_for_update()
    )
    if assignment is None:
        return None
    if assignment.version != version:
        raise MaintenanceProjectAssignmentConflict("项目负责人已变化，请刷新后重试")
    if assignment.archived_at is not None:
        raise MaintenanceProjectAssignmentConflict("项目负责人关系已归档")
    user = db.get(SysUser, assignment.user_id)
    if user is None:
        raise MaintenanceProjectAssignmentError("负责人账号记录缺失")
    before = assignment_dict(assignment, user)
    assignment.archived_at = datetime.now(UTC)
    assignment.archived_by = operated_by
    assignment.archive_reason = reason
    assignment.version += 1
    db.flush()
    after = assignment_dict(assignment, user)
    db.add(
        MaintenanceProjectAuditLog(
            project_id=assignment.project_id,
            entity_type="manager_assignment",
            entity_id=assignment.assignment_id,
            action="archive",
            before_json=before,
            after_json=after,
            reason=reason,
            operated_by=operated_by,
        )
    )
    db.flush()
    return after

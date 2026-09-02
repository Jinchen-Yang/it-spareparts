"""Explicit, auditable maintenance-project manager account assignments (#205)."""

from datetime import UTC, datetime
from typing import TYPE_CHECKING
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

if TYPE_CHECKING:
    from app.models.maintenance_project_operations import (
        MaintenanceProjectWorkbookState,
    )


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
    """Stable-project ids linked to the authenticated account.

    2026-08-25：primary_manager（负责人）∪ viewer（项目级可见账号，
    「基础信息编辑」多选配置）都计入本人可见集。
    """

    if not user_ctx.is_authenticated or not user_ctx.user_id:
        return select(MaintenanceProjectUserAssignment.project_id).where(false())
    return (
        select(MaintenanceProjectUserAssignment.project_id)
        .join(SysUser, SysUser.id == MaintenanceProjectUserAssignment.user_id)
        .where(
            MaintenanceProjectUserAssignment.responsibility_type.in_(
                ("primary_manager", "viewer")),
            MaintenanceProjectUserAssignment.archived_at.is_(None),
            SysUser.username == user_ctx.user_id,
            SysUser.is_active.is_(True),
        )
    )


def owned_project_condition(user_ctx: UserContext):
    return MaintenanceProject.project_id.in_(owned_project_ids(user_ctx))


def accessible_project_condition(user_ctx: UserContext):
    """行级可见范围 SQL 谓词——与 can_access_project 同一口径（2026-08-25 抽取）。

    开 own_maintenance_projects_only 行键的账号：负责人 ∪ 台账 salesperson
    并集；未开键的受限账号：仅挂靠负责人（#205）。FULL_SCOPE 角色按
    owned_project_condition 的使用约定由调用方不加过滤。search 列表与
    直达路由共用此谓词为唯一事实源，避免两条路径口径漂移。
    """
    from app.security import is_scoped_maintenance

    condition = owned_project_condition(user_ctx)
    if is_scoped_maintenance(user_ctx) and user_ctx.salesperson_name:
        condition = or_(
            condition,
            MaintenanceProject.salesperson == user_ctx.salesperson_name,
        )
    return condition


def is_project_workbook_editor(
    db: Session,
    *,
    project_id: str,
    user_ctx: UserContext,
) -> bool:
    """项目负责人/销售对本人项目拥有工作簿编辑权（2026-09-02 拍板）。

    FULL_SCOPE 账号不在此判定（由 API 层按既有 action 键放行）；这里只回答
    「该账号是不是这个项目的 primary_manager 或 canonical 销售」。
    primary_manager 以活跃挂靠为准；销售以
    ``project.salesperson == user_ctx.salesperson_name`` 为准（含 override
    语义：override 后 canonical 值即权威值）。
    """
    if not user_ctx.is_authenticated or not user_ctx.user_id:
        return False
    managed = db.scalar(
        select(MaintenanceProjectUserAssignment.assignment_id).where(
            MaintenanceProjectUserAssignment.project_id == project_id,
            MaintenanceProjectUserAssignment.responsibility_type == "primary_manager",
            MaintenanceProjectUserAssignment.archived_at.is_(None),
            MaintenanceProjectUserAssignment.user_id.in_(
                select(SysUser.id).where(
                    SysUser.username == user_ctx.user_id,
                    SysUser.is_active.is_(True),
                )
            ),
        ).limit(1)
    )
    if managed is not None:
        return True
    if user_ctx.salesperson_name:
        return db.scalar(
            select(MaintenanceProject.project_id).where(
                MaintenanceProject.project_id == project_id,
                MaintenanceProject.salesperson == user_ctx.salesperson_name,
            ).limit(1)
        ) is not None
    return False


def is_project_workbook_editor_locked(
    db: Session,
    *,
    project: MaintenanceProject,
    user_ctx: UserContext,
) -> bool:
    """apply 事务内（项目行已 FOR UPDATE）复检，防挂靠/销售被并发吊销。"""
    if not user_ctx.is_authenticated or not user_ctx.user_id:
        return False
    managed = db.scalar(
        select(MaintenanceProjectUserAssignment.assignment_id).where(
            MaintenanceProjectUserAssignment.project_id == project.project_id,
            MaintenanceProjectUserAssignment.responsibility_type == "primary_manager",
            MaintenanceProjectUserAssignment.archived_at.is_(None),
            MaintenanceProjectUserAssignment.user_id.in_(
                select(SysUser.id).where(
                    SysUser.username == user_ctx.user_id,
                    SysUser.is_active.is_(True),
                )
            ),
        ).limit(1)
    )
    if managed is not None:
        return True
    if user_ctx.salesperson_name:
        return project.salesperson == user_ctx.salesperson_name
    return False


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
                MaintenanceProjectUserAssignment.responsibility_type.in_(
                    ("primary_manager", "viewer")),
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
    2026-08-25：并集唯一事实源收敛到 accessible_project_condition
    （search 列表同用），本函数只是其物化。
    """
    from app.security import is_scoped_maintenance

    if not is_scoped_maintenance(user_ctx):
        return None
    return set(
        db.scalars(
            select(MaintenanceProject.project_id).where(
                accessible_project_condition(user_ctx)
            )
        ).all()
    )


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
    sync_salesperson: bool = False,
    reason: str,
    operated_by: str,
    _prelocked_state: "MaintenanceProjectWorkbookState | None" = None,
    _skip_workbook_bump: bool = False,
) -> dict | None:
    reason = _required_text(reason, "改派原因", 1000)
    operated_by = _required_text(operated_by, "操作人", 64)
    # Probe before taking any lock so a 404 never leaves an orphan workbook
    # state row behind, then lock state -> project -> assignment in the
    # canonical writer order.  Internal callers (owner backfill) pass an
    # already-locked state via ``_prelocked_state``; this function must never
    # take a late state lock after the project row.
    if db.scalar(
        select(MaintenanceProject.project_id).where(
            MaintenanceProject.project_id == project_id
        )
    ) is None:
        return None
    from app.services import maintenance_project_operations as operations

    state = _prelocked_state
    if state is None and not _skip_workbook_bump:
        state = operations.lock_workbook_states(
            db, project_ids=[project_id]
        )[project_id]
    project = db.scalar(
        select(MaintenanceProject)
        .where(MaintenanceProject.project_id == project_id)
        .with_for_update()
    )
    if project is None:
        return None
    if not project.is_active:
        raise MaintenanceProjectAssignmentError("项目主档已归档")
    # 先锁/校验当前 assignment，再决定是否需要锁 target user。
    # manager workbook 已收口为相同的 state → project → assignment →
    # owner user；same-user/陈旧请求仍必须在 target-user 锁之前结束，
    # 保持 no-op/OCC 零额外等待、零写入。
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
        if current.user_id == user_id:
            raise MaintenanceProjectAssignmentError("所选账号已是当前主负责人")
        current_user = db.get(SysUser, current.user_id)
        if current_user is None:
            raise MaintenanceProjectAssignmentError("当前负责人账号记录缺失")
        before = assignment_dict(current, current_user)

    target_user = db.scalar(
        select(SysUser).where(SysUser.id == user_id).with_for_update()
    )
    if target_user is None:
        raise MaintenanceProjectAssignmentError("负责人账号不存在")
    if not target_user.is_active:
        raise MaintenanceProjectAssignmentError("负责人账号已停用")
    if sync_salesperson:
        from app.services import maintenance_project_catalog as catalog

        synced_salesperson = next(
            (
                cleaned
                for value in (
                    target_user.salesperson_name,
                    target_user.display_name,
                    target_user.username,
                )
                if (cleaned := str(value or "").strip())
            ),
            target_user.username,
        )
        if project.salesperson != synced_salesperson:
            project_before = catalog.project_dict(project)
            project.salesperson = synced_salesperson
            project.salesperson_override_active = True
            project.version += 1
            db.flush()
            project_after = catalog.project_dict(project)
            db.add(
                MaintenanceProjectAuditLog(
                    project_id=project.project_id,
                    entity_type="project",
                    entity_id=project.project_id,
                    action="update",
                    before_json=project_before,
                    after_json=project_after,
                    reason=reason,
                    operated_by=operated_by,
                )
            )
    if current is not None:
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
    if state is not None:
        # primary_manager 是导出投影（旧 V2 task owner）的输入：真实变更抬高
        # 同一根事务 revision 恰好一次（bump 内部按事务去重）。
        operations.bump_locked_workbook_revision(db, state=state)
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
    # Resolve the project first (unlocked probe), then take locks in the
    # canonical state -> project -> assignment order shared with
    # ``assign_primary_manager``.  This avoids a lock-order inversion when an
    # archive races with a reassignment.
    assignment = db.scalar(
        select(MaintenanceProjectUserAssignment)
        .where(MaintenanceProjectUserAssignment.assignment_id == assignment_id)
    )
    if assignment is None:
        return None
    project_id = assignment.project_id
    from app.services import maintenance_project_operations as operations

    state = operations.lock_workbook_states(db, project_ids=[project_id])[
        project_id
    ]
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
    operations.bump_locked_workbook_revision(db, state=state)
    return after


def project_viewers(db: Session, *, project_id: str) -> list[dict]:
    """项目级可见账号（viewer）当前名单，展示与表单回显用。"""
    rows = db.execute(
        select(MaintenanceProjectUserAssignment, SysUser)
        .join(SysUser, SysUser.id == MaintenanceProjectUserAssignment.user_id)
        .where(
            MaintenanceProjectUserAssignment.project_id == project_id,
            MaintenanceProjectUserAssignment.responsibility_type == "viewer",
            MaintenanceProjectUserAssignment.archived_at.is_(None),
        )
        .order_by(SysUser.username)
    ).all()
    return [
        {"username": user.username, "display_name": user.display_name}
        for _assignment, user in rows
    ]


def sync_project_viewers(
    db: Session,
    *,
    project_id: str,
    usernames: list[str],
    operated_by: str,
    reason: str,
) -> tuple[list[dict], bool]:
    """把项目级可见账号整组同步为 ``usernames``（2026-08-25 客户需求：
    「基础信息编辑」里多选已有账号控制项目可见性）。

    差量写：新增缺失 viewer、归档多余 viewer（软删留审计，同负责人口径）。
    账号不存在/停用 → 整组拒绝，零半截写入。
    """
    clean_reason = _required_text(reason, "操作原因", 500)
    wanted = sorted({name.strip() for name in usernames if name.strip()})
    users = {
        user.username: user
        for user in db.execute(
            select(SysUser).where(
                SysUser.username.in_(wanted), SysUser.is_active.is_(True)
            )
        ).scalars()
    }
    missing = [name for name in wanted if name not in users]
    if missing:
        raise MaintenanceProjectAssignmentError(
            f"账号不存在或已停用：{', '.join(missing)}")

    current = list(
        db.execute(
            select(MaintenanceProjectUserAssignment, SysUser)
            .join(SysUser, SysUser.id == MaintenanceProjectUserAssignment.user_id)
            .where(
                MaintenanceProjectUserAssignment.project_id == project_id,
                MaintenanceProjectUserAssignment.responsibility_type == "viewer",
                MaintenanceProjectUserAssignment.archived_at.is_(None),
            )
        ).all()
    )
    current_names = {user.username for _assignment, user in current}
    changed = current_names != set(wanted)
    if not changed:
        return project_viewers(db, project_id=project_id), False
    now = datetime.now(UTC)
    for assignment, user in current:
        if user.username in users:
            continue
        before = assignment_dict(assignment, user)
        assignment.archived_at = now
        assignment.archived_by = operated_by
        assignment.archive_reason = clean_reason
        assignment.version += 1
        db.add(
            MaintenanceProjectAuditLog(
                project_id=project_id,
                entity_type="viewer_assignment",
                entity_id=assignment.assignment_id,
                action="archive",
                before_json=before,
                after_json=assignment_dict(assignment, user),
                reason=clean_reason,
                operated_by=operated_by,
            )
        )
    for name in wanted:
        if name in current_names:
            continue
        assignment = MaintenanceProjectUserAssignment(
            assignment_id=str(uuid4()),
            project_id=project_id,
            responsibility_type="viewer",
            user_id=users[name].id,
            version=1,
            assigned_by=operated_by,
            assignment_reason=clean_reason,
        )
        db.add(assignment)
        db.flush()
        db.add(
            MaintenanceProjectAuditLog(
                project_id=project_id,
                entity_type="viewer_assignment",
                entity_id=assignment.assignment_id,
                action="create",
                before_json=None,
                after_json=assignment_dict(assignment, users[name]),
                reason=clean_reason,
                operated_by=operated_by,
            )
        )
    db.flush()
    # project.version 与 workbook revision 由 API 与同次主档 PATCH 聚合：
    # 混合修改最多 +1，viewer-only 真实变化 +1，全 no-op +0。
    return project_viewers(db, project_id=project_id), True

"""Only write path for the stable maintenance project master."""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.maintenance_project import (
    MaintenanceProject,
    MaintenanceProjectAuditLog,
)
from app.models.maintenance_project_operations import MaintenanceProjectWorkbookState
from app.business_time import business_today
from app.services import (
    maintenance_periods,
    maintenance_project_identity,
    maintenance_project_operations as operations,
    project_names,
)


class MaintenanceProjectCatalogError(Exception):
    """Invalid project-master request."""


class MaintenanceProjectCatalogConflict(Exception):
    """Concurrent or uniqueness conflict."""


def project_dict(project: MaintenanceProject) -> dict:
    return {
        "project_id": project.project_id,
        "project_code": project.project_code,
        "display_name": project.display_name,
        "salesperson": project.salesperson,
        "salesperson_override_active": project.salesperson_override_active,
        "project_manager_id": project.project_manager_id,
        # 维保期限主数据（#51）：面板可显示、可编辑（#39）
        "period_from": project.period_from.isoformat() if project.period_from else None,
        "period_to": project.period_to.isoformat() if project.period_to else None,
        # 动态计算：存库列只是写入时兼容快照，期限改动后不会自动刷新
        "lifecycle_status": maintenance_periods.lifecycle_status(
            project.period_from, project.period_to, business_today()),
        "is_active": project.is_active,
        "version": project.version,
    }


def _clean_required(value: str | None, *, label: str, max_length: int) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        raise MaintenanceProjectCatalogError(f"{label}不能为空")
    if len(cleaned) > max_length:
        raise MaintenanceProjectCatalogError(f"{label}过长（最多 {max_length} 个字符）")
    return cleaned


def _clean_optional(value: str | None, *, label: str, max_length: int) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if len(cleaned) > max_length:
        raise MaintenanceProjectCatalogError(f"{label}过长（最多 {max_length} 个字符）")
    return cleaned


def _audit(
    db: Session,
    project: MaintenanceProject,
    *,
    action: str,
    before: dict | None,
    after: dict | None,
    reason: str,
    operated_by: str,
) -> None:
    db.add(
        MaintenanceProjectAuditLog(
            project_id=project.project_id,
            entity_type="project",
            entity_id=project.project_id,
            action=action,
            before_json=before,
            after_json=after,
            reason=reason,
            operated_by=operated_by,
        )
    )


def _lock_project_for_master_write(
    db: Session,
    *,
    project_id: str,
) -> tuple[MaintenanceProject | None, MaintenanceProjectWorkbookState | None]:
    """Lock the workbook state before the project master row.

    Workbook apply and operating-fact writes use the same state -> project/fact
    order.  The unlocked existence probe avoids creating an orphan state for an
    unknown project while preserving that global lock order.
    """

    exists = db.scalar(
        select(MaintenanceProject.project_id).where(
            MaintenanceProject.project_id == project_id
        )
    )
    if exists is None:
        return None, None
    state = operations.get_or_create_workbook_state(
        db,
        project_id=project_id,
        lock=True,
    )
    project = db.scalar(
        select(MaintenanceProject)
        .where(MaintenanceProject.project_id == project_id)
        .with_for_update()
    )
    return project, state


def create_project(
    db: Session,
    *,
    project_code: str,
    display_name: str,
    project_manager_id: str | None,
    reason: str,
    operated_by: str,
) -> dict:
    clean_code = _clean_required(project_code, label="稳定项目编号", max_length=64)
    clean_name = _clean_required(display_name, label="项目名称", max_length=256)
    clean_manager = _clean_optional(
        project_manager_id,
        label="项目经理标识",
        max_length=64,
    )
    clean_reason = _clean_required(reason, label="操作原因", max_length=1000)
    project_names.lock_display_name_identities(db, [clean_name])

    project = MaintenanceProject(
        project_id=str(uuid4()),
        project_code=clean_code,
        display_name=clean_name,
        project_manager_id=clean_manager,
        # 业务期限的权威来源尚未锁定；新主档必须显式暴露为待确认，不能猜。
        lifecycle_status="missing",
        is_active=True,
        version=1,
    )
    db.add(project)
    try:
        db.flush()
    except IntegrityError as exc:
        raise MaintenanceProjectCatalogConflict(
            f"稳定项目编号「{clean_code}」已存在，请勿重复建档"
        ) from exc
    maintenance_project_identity.record_alias(
        db,
        project_id=project.project_id,
        alias_name=clean_name,
        source="project_create",
    )
    payload = project_dict(project)
    _audit(
        db,
        project,
        action="create",
        before=None,
        after=payload,
        reason=clean_reason,
        operated_by=operated_by,
    )
    db.flush()
    return payload


def update_project(
    db: Session,
    *,
    project_id: str,
    version: int,
    updates: dict,
    reason: str,
    operated_by: str,
) -> dict | None:
    allowed = {key: value for key, value in updates.items() if key in {
        "display_name", "salesperson", "project_manager_id", "period_from", "period_to"
    }}
    if not allowed:
        raise MaintenanceProjectCatalogError("没有可修改的项目字段")
    clean_reason = _clean_required(reason, label="操作原因", max_length=1000)
    if "display_name" in allowed:
        allowed["display_name"] = _clean_required(
            allowed["display_name"],
            label="项目名称",
            max_length=256,
        )
        # 新名称 identity 必须在 state/project 之前锁，避免与 auto-create
        # 形成 name→state / state→name 反序。
        project_names.lock_display_name_identities(db, [allowed["display_name"]])
    project, state = _lock_project_for_master_write(db, project_id=project_id)
    if project is None or state is None:
        return None
    if not project.is_active:
        raise MaintenanceProjectCatalogError("项目主档已归档，请先恢复后再编辑")
    if project.version != version:
        raise MaintenanceProjectCatalogConflict(
            f"项目主档已被他人修改（当前版本 {project.version}），请刷新后重试"
        )
    previous_display_name = project.display_name
    before = project_dict(project)
    if "display_name" in allowed:
        project.display_name = allowed["display_name"]
    if "salesperson" in allowed:
        project.salesperson = _clean_optional(
            allowed["salesperson"],
            label="销售人员",
            max_length=64,
        )
        project.salesperson_override_active = True
    if "project_manager_id" in allowed:
        project.project_manager_id = _clean_optional(
            allowed["project_manager_id"],
            label="项目经理标识",
            max_length=64,
        )
    # 维保期限编辑（#39/#51）：起止都传才生效（表单整组提交）。期限双源 P1 修复后
    # project.period_* 是唯一事实源：统一走 canonical helper 同步 projection 投影，
    # lifecycle 按新期限快照口径重算；起>止由 helper 拒绝。
    if "period_from" in allowed or "period_to" in allowed:
        new_from = allowed.get("period_from", project.period_from)
        new_to = allowed.get("period_to", project.period_to)
        from app.business_time import business_today

        try:
            maintenance_periods.apply_canonical_period_locked(
                db,
                project=project,
                period_from=new_from,
                period_to=new_to,
                source=maintenance_periods.SOURCE_DIRECT_API,
                as_of=business_today(),
                operated_by=operated_by,
                reason=clean_reason,
            )
        except maintenance_periods.MaintenancePeriodError as exc:
            raise MaintenanceProjectCatalogError(str(exc)) from exc
    changed = project_dict(project) != before
    if not changed:
        return before
    if project.display_name != previous_display_name:
        maintenance_project_identity.record_alias(
            db,
            project_id=project.project_id,
            alias_name=previous_display_name,
            source="project_rename_previous",
        )
        maintenance_project_identity.record_alias(
            db,
            project_id=project.project_id,
            alias_name=project.display_name,
            source="project_rename_current",
        )
    project.version += 1
    db.flush()
    after = project_dict(project)
    _audit(
        db,
        project,
        action="update",
        before=before,
        after=after,
        reason=clean_reason,
        operated_by=operated_by,
    )
    operations.bump_locked_workbook_revision(db, state=state)
    db.flush()
    return after


def set_project_active(
    db: Session,
    *,
    project_id: str,
    version: int,
    active: bool,
    reason: str,
    operated_by: str,
) -> dict | None:
    project, state = _lock_project_for_master_write(db, project_id=project_id)
    if project is None or state is None:
        return None
    if project.version != version:
        raise MaintenanceProjectCatalogConflict(
            f"项目主档已被他人修改（当前版本 {project.version}），请刷新后重试"
        )
    if project.is_active == active:
        state_label = "有效" if active else "归档"
        raise MaintenanceProjectCatalogError(f"项目主档已是{state_label}状态")
    clean_reason = _clean_required(reason, label="操作原因", max_length=1000)
    before = project_dict(project)
    project.is_active = active
    project.version += 1
    db.flush()
    after = project_dict(project)
    _audit(
        db,
        project,
        action="restore" if active else "archive",
        before=before,
        after=after,
        reason=clean_reason,
        operated_by=operated_by,
    )
    operations.bump_locked_workbook_revision(db, state=state)
    db.flush()
    return after

"""Explicit project-manager account assignment APIs (#205)."""

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import current_identity, current_role
from app.db import get_db
from app.models.system import SysUser
from app.security import (
    UserContext,
    get_current_user_context,
    record_access_log,
    require_action,
    require_page,
)
from app.services import maintenance_project_assignments as assignments


router = APIRouter(prefix="/maintenance", tags=["maintenance"])


class ManagerAssignmentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: int = Field(ge=1)
    expected_assignment_id: str | None = Field(default=None, max_length=36)
    expected_assignment_version: int | None = Field(default=None, ge=1)
    reason: str = Field(min_length=1, max_length=1000)


class ManagerAccountSearch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    q: str = ""
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)


class ManagerAssignmentArchive(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=1000)


def _real_operator(db: Session, ident: dict) -> str:
    if ident.get("authn") != "sys_user" or ident.get("fb"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "负责人改派必须使用实名系统账号")
    username = str(ident.get("sub") or "").strip()
    user = db.scalar(
        select(SysUser).where(
            SysUser.username == username,
            SysUser.is_active.is_(True),
        )
    )
    if not username or user is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "负责人改派必须使用实名系统账号")
    return username


def _require_assignment_admin(
    ctx: UserContext = Depends(get_current_user_context),
) -> None:
    # This responsibility mapping changes row-level authorization.  It is not
    # delegable through a customizable action bit.
    if ctx.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "仅管理员可管理项目负责人")


@router.post("/project-manager-assignments/search")
def search_manager_accounts(
    body: ManagerAccountSearch,
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(
        require_action(
            "action_maintenance_project_manage",
            require_data="data_profit",
        )
    ),
    _admin: None = Depends(_require_assignment_admin),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    q = body.q.strip()
    if len(q) > 128:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "负责人账号搜索条件无效",
        )
    payload = assignments.search_active_users(
        db,
        q_text=q,
        page=body.page,
        page_size=body.page_size,
    )
    record_access_log(
        ctx,
        "maintenance_project_manager_account_search",
        "sys_user",
        {
            "searched": bool(q),
            "page": body.page,
            "page_size": body.page_size,
            "result_count": len(payload["rows"]),
        },
    )
    return payload


@router.post(
    "/projects/stable/{project_id}/manager-assignment",
    status_code=status.HTTP_201_CREATED,
)
def assign_project_manager(
    body: ManagerAssignmentCreate,
    project_id: str = Path(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(
        require_action(
            "action_maintenance_project_manage",
            require_data="data_profit",
        )
    ),
    _admin: None = Depends(_require_assignment_admin),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    operator = _real_operator(db, ident)
    try:
        payload = assignments.assign_primary_manager(
            db,
            project_id=project_id,
            user_id=body.user_id,
            expected_assignment_id=body.expected_assignment_id,
            expected_assignment_version=body.expected_assignment_version,
            reason=body.reason,
            operated_by=operator,
        )
        if payload is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "维保项目不存在")
        db.commit()
    except assignments.MaintenanceProjectAssignmentConflict as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except assignments.MaintenanceProjectAssignmentError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "项目负责人已变化，请刷新后重试",
        ) from exc
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    record_access_log(
        ctx,
        "maintenance_project_manager_assign",
        "maintenance_project",
        {
            "project_id": project_id,
            "assignment_id": payload["assignment_id"],
            "target_user_id": body.user_id,
        },
    )
    return payload


@router.post("/project-manager-assignments/{assignment_id}/archive")
def archive_project_manager_assignment(
    body: ManagerAssignmentArchive,
    assignment_id: str = Path(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(
        require_action(
            "action_maintenance_project_manage",
            require_data="data_profit",
        )
    ),
    _admin: None = Depends(_require_assignment_admin),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    operator = _real_operator(db, ident)
    try:
        payload = assignments.archive_primary_manager(
            db,
            assignment_id=assignment_id,
            version=body.version,
            reason=body.reason,
            operated_by=operator,
        )
        if payload is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "负责人关系不存在")
        db.commit()
    except assignments.MaintenanceProjectAssignmentConflict as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except assignments.MaintenanceProjectAssignmentError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "项目负责人已变化，请刷新后重试",
        ) from exc
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    record_access_log(
        ctx,
        "maintenance_project_manager_archive",
        "maintenance_project",
        {
            "project_id": payload["project_id"],
            "assignment_id": assignment_id,
        },
    )
    return payload

"""受众分组与在职人员目录；权限键只在接口层解释。"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import permissions
from app.api.work_items import workflow_context
from app.auth import current_identity
from app.db import get_db
from app.models.fields_audience import AudienceGroup, AudienceGroupMember
from app.models.system import SysUser
from app.security import UserContext, record_access_log
from app.services import audience

router = APIRouter(prefix="/audience", tags=["audience"])


def audience_user(
    ident: dict = Depends(current_identity),
    db: Session = Depends(get_db),
) -> UserContext:
    """分组归属必须绑定实名账号，RBAC 总开关不改变登录要求。"""
    try:
        viewer = workflow_context(ident, db)
    except HTTPException as exc:
        # 复用待办的实名门禁，同时保留受众接口既有的身份失败响应。
        raise HTTPException(401, exc.detail) from exc
    viewer.permissions = permissions.runtime_safe(viewer.permissions)
    return viewer


def manages_public_groups(viewer: UserContext) -> bool:
    graph = viewer.permissions or {}
    return viewer.role in {"admin", "boss"} or bool(
        graph.get("action_account_manage") or graph.get("action_presales_supervise")
    )


class CreateGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    scope: Literal["personal", "public"] = "personal"
    user_ids: list[str] = Field(default_factory=list)


class UpdateGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=64)
    user_ids: list[str] | None = None


def _group_views(db: Session, groups: list[AudienceGroup]) -> list[dict]:
    members: dict[int, list[str]] = {group.id: [] for group in groups}
    if members:
        for group_id, user_id in db.execute(
            select(AudienceGroupMember.group_id, AudienceGroupMember.user_id)
            .where(AudienceGroupMember.group_id.in_(members))
            .order_by(AudienceGroupMember.user_id)
        ):
            members[group_id].append(user_id)
    return [
        {
            "id": group.id,
            "name": group.name,
            "scope": group.scope,
            "owner_id": group.owner_id,
            "user_ids": members[group.id],
            "archived": group.archived,
            "created_at": group.created_at,
            "updated_at": group.updated_at,
        }
        for group in groups
    ]


def service_error(db: Session, exc: ValueError | PermissionError) -> None:
    db.rollback()
    code = getattr(exc, "status_code", 403 if isinstance(exc, PermissionError) else 422)
    raise HTTPException(code, str(exc)) from exc


@router.get("/groups")
def list_groups(
    db: Session = Depends(get_db), viewer: UserContext = Depends(audience_user)
) -> list[dict]:
    return _group_views(db, audience.list_groups(db, viewer))


@router.post("/groups", status_code=201)
def create_group(
    body: CreateGroup,
    db: Session = Depends(get_db),
    viewer: UserContext = Depends(audience_user),
) -> dict:
    try:
        group = audience.create_group(
            db, body.name, body.scope, body.user_ids, viewer,
            bypass=manages_public_groups,
        )
        db.commit()
    except (ValueError, PermissionError) as exc:
        service_error(db, exc)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "同名分组已存在") from exc
    record_access_log(viewer, "audience_group_create", "audience_group", {"id": group.id})
    return _group_views(db, [group])[0]


@router.patch("/groups/{group_id}")
def update_group(
    group_id: int,
    body: UpdateGroup,
    db: Session = Depends(get_db),
    viewer: UserContext = Depends(audience_user),
) -> dict:
    if any(value is None for value in body.model_dump(exclude_unset=True).values()):
        raise HTTPException(422, "分组名称和成员不能为 null")
    try:
        group = audience.update_group(
            db, group_id, viewer, name=body.name, user_ids=body.user_ids,
            bypass=manages_public_groups,
        )
        db.commit()
    except (ValueError, PermissionError) as exc:
        service_error(db, exc)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "同名分组已存在") from exc
    record_access_log(viewer, "audience_group_update", "audience_group", {"id": group.id})
    return _group_views(db, [group])[0]


@router.delete("/groups/{group_id}")
def archive_group(
    group_id: int,
    db: Session = Depends(get_db),
    viewer: UserContext = Depends(audience_user),
) -> dict:
    try:
        group = audience.archive_group(db, group_id, viewer, bypass=manages_public_groups)
        db.commit()
    except (ValueError, PermissionError) as exc:
        service_error(db, exc)
    record_access_log(viewer, "audience_group_archive", "audience_group", {"id": group.id})
    return _group_views(db, [group])[0]


@router.get("/people")
def list_people(
    q: str = Query(default="", max_length=128),
    db: Session = Depends(get_db),
    _viewer: UserContext = Depends(audience_user),
) -> list[dict]:
    stmt = select(SysUser.username, SysUser.display_name).where(
        SysUser.is_active.is_(True), SysUser.deleted_at.is_(None)
    )
    if q.strip():
        pattern = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        stmt = stmt.where(or_(
            SysUser.username.ilike(pattern, escape="\\"),
            SysUser.display_name.ilike(pattern, escape="\\"),
        ))
    return [
        {"id": username, "display_name": display_name or username}
        for username, display_name in db.execute(stmt.order_by(SysUser.username))
    ]

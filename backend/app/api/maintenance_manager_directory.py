"""负责人账号目录搜索（稳定版，page_maintenance 门）。

2026-08-21 客户反馈：项目面板「编辑项目基本信息」的维保负责人下拉数据源。
原在 maintenance_project_assignments（Beta 总闸下）——那套闸看守的是改派/归档
（行级授权变更，仅 admin）；搜索是稳定版面板的只读目录，放这里脱离 Beta，
模块级 beta_gate 看守测试的「模块内路由一律带闸」不变量不受影响。
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session, sessionmaker

from app.auth import current_role
from app.db import get_db
from app.security import (
    UserContext,
    get_current_user_context,
    record_access_log,
    require_page,
)
from app.services import maintenance_project_assignments as assignments
from app.api import maintenance_project_scope as auth_transport
_RESOURCE = 'maintenance_manager_directory'
from app.api.maintenance_project_assignments import ManagerAccountSearch

router = APIRouter(prefix="/maintenance", tags=["maintenance"])


@router.post("/project-manager-assignments/search")
def search_manager_accounts(
    body: ManagerAccountSearch,
    response: Response,
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(auth_transport.legacy_page(_RESOURCE, "page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
    identity=Depends(auth_transport.identity_for(_RESOURCE)),
    view: str | None = Query(None),
) -> dict:
    if view not in (None, "authorization"):
        from app.authz.errors import authz_error
        raise authz_error(422, "AUTHZ_INVALID_REQUEST", "不支持的授权视图")
    q = body.q.strip()
    if len(q) > 128:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "负责人账号搜索条件无效",
        )
    if identity is not None:
        response.headers["Cache-Control"] = "no-store"
        return assignments.read_manager_directory(sessionmaker(bind=db.get_bind()), identity,
            body.model_dump(exclude={'authorization_precondition'}),
            authorization_view=view == 'authorization',
            authorization_precondition=body.authorization_precondition)
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

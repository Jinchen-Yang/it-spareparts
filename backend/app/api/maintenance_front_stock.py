"""维保前置库只读 API（B1）：项目结存 + 流水。写账由导入 adapter / 变卖模块调用服务完成。"""

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import permissions as _perm
from app.api.maintenance_project_scope import enforce_maintenance_project_access, identity_for, legacy_page
from app.auth import current_role
from app.db import get_db
from app.authz import runtime
from app.services import maintenance_project_read_access as protected_reads
from app.models.maintenance_project import MaintenanceProject
from app.models.system import SysUser
from app.security import (
    UserContext,
    get_current_user_context,
    require_page,
)
from app.services import maintenance_front_stock as front_stock

router = APIRouter(prefix="/maintenance", tags=["maintenance"])


def _can_view_cost(db: Session, ctx: UserContext) -> bool:
    if not ctx.is_authenticated:
        return False
    user = db.scalar(
        select(SysUser).where(
            SysUser.username == ctx.user_id,
            SysUser.is_active.is_(True),
        )
    )
    if user is None:
        return False
    graph = _perm.effective_for_user(user)
    return bool(_perm.runtime_safe(graph).get("data_purchase_cost", False))


@router.get("/projects/stable/{project_id}/front-stock")
def get_front_stock(
    project_id: str = Path(...),
    include_ledger: bool = Query(False, description="同时返回最近流水"),
    response: Response = None,
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(legacy_page("maintenance_front_stock", "page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
    typed_identity=Depends(identity_for("maintenance_front_stock")),
    view: str | None = Query(None),
    authorization_precondition: str | None = Query(None),
) -> dict:
    """项目前置库结存（含库龄与金额估值）。

    访问门：登录 + page_maintenance + 项目范围（IDOR 失败关闭）；
    成本门：无 data_purchase_cost 的账号只看到数量与库龄，成本/估值字段脱敏。
    """
    response.headers["Cache-Control"] = "no-store"
    if runtime.is_authoritative(db, "maintenance_front_stock"):
        return protected_reads.read_front_stock(db, typed_identity, project_id,
            include_ledger=include_ledger, view=view, precondition=authorization_precondition)
    project = db.get(MaintenanceProject, project_id)
    if project is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            {"code": "not_found", "message": "项目不存在"},
        )
    enforce_maintenance_project_access(db, project_id=project_id, ctx=ctx)
    can_view_cost = _can_view_cost(db, ctx)
    return protected_reads.front_stock_payload(db, project_id, include_ledger=include_ledger, can_view_cost=can_view_cost)

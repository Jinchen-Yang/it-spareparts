"""项目工作簿 v3 导出 API（D1）：新模板六 sheet + 隐藏技术 sheet。

访问门（round-5 Blocker 2）：登录 + page_maintenance + 项目范围，
且工作簿含合同额/回款/成本/前置库估值 → 要求 data_profit 与
data_purchase_cost 两个独立数据组同时可见；否则 403。
"""

from fastapi import APIRouter, Depends, HTTPException, Path, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import permissions as _perm
from app.api.maintenance_project_scope import enforce_maintenance_project_access
from app.auth import current_role
from app.db import get_db
from app.models.maintenance_project import MaintenanceProject
from app.models.system import SysUser
from app.security import (
    UserContext,
    get_current_user_context,
    require_page,
)
from app.services import maintenance_project_workbook_v3 as workbook_v3

router = APIRouter(prefix="/maintenance", tags=["maintenance"])


def _require_workbook_data(db: Session, ctx: UserContext) -> None:
    if not ctx.is_authenticated:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "请先登录")
    user = db.scalar(
        select(SysUser).where(
            SysUser.username == ctx.user_id,
            SysUser.is_active.is_(True),
        )
    )
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "请先登录")
    graph = _perm.runtime_safe(_perm.effective_for_user(user))
    missing = [
        key
        for key in ("data_profit", "data_purchase_cost")
        if not graph.get(key, False)
    ]
    if missing:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "项目工作簿含合同额/回款/成本/前置库估值，"
            "要求同时具备利润与采购成本数据权限",
        )


@router.get("/projects/stable/{project_id}/workbook-v3.xlsx")
def export_project_workbook_v3(
    project_id: str = Path(...),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> Response:
    """按新模板 v1 导出项目工作簿（00-06 + 隐藏 98/99）。"""
    project = db.get(MaintenanceProject, project_id)
    if project is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            {"code": "not_found", "message": "项目不存在"},
        )
    enforce_maintenance_project_access(db, project_id=project_id, ctx=ctx)
    _require_workbook_data(db, ctx)
    data = workbook_v3.build_project_workbook(db, project_id)
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            # HTTP 头仅限 latin-1；文件名用项目编号的 ASCII 化回退
            "Content-Disposition": (
                f"attachment; filename=project_workbook_v3_{project_id}.xlsx"
            ),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )

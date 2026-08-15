"""项目工作簿 v3 导出 API（D1）：新模板六 sheet + 隐藏技术 sheet。"""

from fastapi import APIRouter, Depends, HTTPException, Path, Response, status
from sqlalchemy.orm import Session

from app.api.maintenance_project_scope import enforce_maintenance_project_access
from app.auth import current_role
from app.db import get_db
from app.models.maintenance_project import MaintenanceProject
from app.security import (
    UserContext,
    get_current_user_context,
    require_page,
)
from app.services import maintenance_project_workbook_v3 as workbook_v3

router = APIRouter(prefix="/maintenance", tags=["maintenance"])


@router.get("/projects/stable/{project_id}/workbook-v3.xlsx")
def export_project_workbook_v3(
    project_id: str = Path(...),
    response: Response = None,
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
    data = workbook_v3.build_project_workbook(db, project_id)
    response.headers["Cache-Control"] = "no-store"
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            # HTTP 头仅限 latin-1；文件名用项目编号的 ASCII 化回退
            "Content-Disposition": (
                f"attachment; filename=project_workbook_v3_{project_id}.xlsx"
            )
        },
    )

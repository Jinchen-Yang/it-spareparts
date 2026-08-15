"""项目结束收回清单 API（F4）：好件/坏件分离 + 未收回结存。"""

from fastapi import APIRouter, Depends, HTTPException, Path, Response, status
from sqlalchemy.orm import Session

from app.api.maintenance_project_scope import enforce_maintenance_project_access
from app.auth import current_role
from app.db import get_db
from app.models.maintenance_project import MaintenanceProject
from app.security import UserContext, get_current_user_context, require_page
from app.services import maintenance_recovery as recovery

router = APIRouter(prefix="/maintenance", tags=["maintenance"])


@router.get("/projects/stable/{project_id}/recovery-summary")
def get_recovery_summary(
    project_id: str = Path(...),
    response: Response = None,
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """项目结束收回清单：好件收回（返库单）/ 坏件返还（入库单）/ 未收回结存。

    访问门：登录 + page_maintenance + 项目范围（IDOR 失败关闭）。
    """
    response.headers["Cache-Control"] = "no-store"
    project = db.get(MaintenanceProject, project_id)
    if project is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            {"code": "not_found", "message": "项目不存在"},
        )
    enforce_maintenance_project_access(db, project_id=project_id, ctx=ctx)
    return recovery.recovery_summary(db, project_id)

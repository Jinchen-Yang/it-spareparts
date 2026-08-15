"""维保前置库只读 API（B1）：项目结存 + 流水。写账由导入 adapter / 变卖模块调用服务完成。"""

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy.orm import Session

from app.auth import current_role
from app.db import get_db
from app.models.maintenance_project import MaintenanceProject
from app.security import require_page
from app.services import maintenance_front_stock as front_stock

router = APIRouter(prefix="/maintenance", tags=["maintenance"])


@router.get("/projects/stable/{project_id}/front-stock")
def get_front_stock(
    project_id: str = Path(...),
    include_ledger: bool = Query(False, description="同时返回最近流水"),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
) -> dict:
    project = db.get(MaintenanceProject, project_id)
    if project is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            {"code": "not_found", "message": "项目不存在"},
        )
    rows = front_stock.balance_rows(db, project_id)
    result: dict = {
        "project_id": project_id,
        "rows": rows,
        "total_qty": round(sum(row["qty"] for row in rows), 3),
        "total_value_ex_tax": round(
            sum(
                value
                for row in rows
                if (value := row["value_ex_tax"]) is not None
            ),
            2,
        ),
    }
    if include_ledger:
        result["ledger"] = front_stock.ledger_entries(db, project_id)
    return result

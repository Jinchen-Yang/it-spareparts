"""维保前置库只读 API（B1）：项目结存 + 流水。写账由导入 adapter / 变卖模块调用服务完成。"""

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status
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
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """项目前置库结存（含库龄与金额估值）。

    访问门：登录 + page_maintenance + 项目范围（IDOR 失败关闭）；
    成本门：无 data_purchase_cost 的账号只看到数量与库龄，成本/估值字段脱敏。
    """
    response.headers["Cache-Control"] = "no-store"
    project = db.get(MaintenanceProject, project_id)
    if project is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            {"code": "not_found", "message": "项目不存在"},
        )
    enforce_maintenance_project_access(db, project_id=project_id, ctx=ctx)
    can_view_cost = _can_view_cost(db, ctx)
    rows = front_stock.balance_rows(db, project_id)
    if not can_view_cost:
        for row in rows:
            for key in (
                "unit_cost_ex_tax",
                "unit_cost_inc_tax",
                "value_ex_tax",
                "value_inc_tax",
            ):
                row[key] = None
    values_ex = [row["value_ex_tax"] for row in rows]
    values_inc = [row["value_inc_tax"] for row in rows]
    complete_ex = all(value is not None for value in values_ex) if rows else True
    complete_inc = all(value is not None for value in values_inc) if rows else True
    if not can_view_cost:
        total_ex = None
        total_inc = None
        completeness = "not_visible"
    else:
        total_ex = (
            round(sum(v for v in values_ex if v is not None), 2)
            if complete_ex
            else None
        )
        total_inc = (
            round(sum(v for v in values_inc if v is not None), 2)
            if complete_inc
            else None
        )
        completeness = (
            "complete" if (complete_ex and complete_inc) else "incomplete"
        )
    result: dict = {
        "project_id": project_id,
        "rows": rows,
        "total_qty": round(sum(row["qty"] for row in rows), 3),
        # 缺失成本不按 0：不完整/无权限时返回 null + completeness 标记
        "total_value_ex_tax": total_ex,
        "total_value_inc_tax": total_inc,
        "value_completeness": completeness,
        "cost_visible": can_view_cost,
        "stale_90d_count": sum(1 for row in rows if row["stale_90d"]),
    }
    if include_ledger:
        result["ledger"] = front_stock.ledger_entries(db, project_id)
    return result

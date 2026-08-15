"""报销对账只读 API（C4）。"""

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import permissions as _perm
from app.auth import current_role
from app.db import get_db
from app.models.system import SysUser
from app.security import (
    UserContext,
    get_current_user_context,
    require_page,
)
from app.services import maintenance_expense_reconcile as reconcile

router = APIRouter(prefix="/maintenance", tags=["maintenance"])


@router.get("/reconcile/expenses")
def get_expense_reconcile(
    response: Response = None,
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """台账报销归集 vs 氚云 BXD raw vs 正式费用事实逐单对账（只读）。"""
    response.headers["Cache-Control"] = "no-store"
    if not ctx.is_authenticated:
        raise HTTPException(status_code=401, detail="请先登录")
    if ctx.role not in ("admin", "boss"):
        raise HTTPException(
            status_code=403,
            detail="全库报销对账仅限管理员（跨项目财务数据）",
        )
    user = db.scalar(
        select(SysUser).where(
            SysUser.username == ctx.user_id, SysUser.is_active.is_(True)
        )
    )
    graph = _perm.effective_for_user(user) if user is not None else {}
    if not _perm.runtime_safe(graph).get("data_profit", False):
        raise HTTPException(
            status_code=403,
            detail="报销对账要求同时具备利润数据可见权限",
        )
    rows = reconcile.expense_reconcile_rows(db)
    return {
        "rows": rows,
        "matched": sum(1 for row in rows if row["status"] == "matched"),
        "mismatch": sum(1 for row in rows if row["status"] in ("mismatch", "partial_match")),
        "ledger_only": sum(1 for row in rows if row["status"] == "ledger_only"),
        "bxd_only": sum(1 for row in rows if row["status"] == "bxd_only"),
    }

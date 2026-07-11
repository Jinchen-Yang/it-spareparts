"""老板经营看板 API（P1 第二阶段第一刀）。仅管理员/老板（page_boss_board）。

只读分析。金额未税口径（与利润引擎同源）。字段仍过 apply_field_visibility 统一脱敏
（boss/admin 全可见；将来若给其它角色开需靠脱敏兜底）。
"""
from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.auth import current_role
from app.db import get_db
from app.security import (UserContext, apply_field_visibility, get_current_user_context,
                          record_access_log, require_page)
from app.services import dashboard

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/kpi")
def kpi(
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    db: Session = Depends(get_db),
    _: str = Depends(current_role),
    _page: None = Depends(require_page("page_boss_board")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    record_access_log(ctx, "kpi", "dashboard", {"date_from": str(date_from), "date_to": str(date_to)})
    data = dashboard.kpi(db, date_from, date_to, user_ctx=ctx)
    return apply_field_visibility(data, ctx)

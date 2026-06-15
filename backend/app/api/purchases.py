"""采购记录 API（合同重点：销售和采购都能直接看最近采购）。

页面级准入 require_page("page_purchases")：销售/采购/老板/只读/管理员均 page_purchases=True
可达（甲方 2026-06-15 确认销售也要能查采购记录）。字段级仍过 apply_field_visibility：
未授权角色的供应商等仍被遮（销售 data_supplier=False → 供应商列为空，但进价可见）。
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.auth import current_role
from app.db import get_db
from app.security import (UserContext, apply_field_visibility, get_current_user_context,
                          record_access_log, require_page)
from app.services import purchase_query

router = APIRouter(prefix="/purchases", tags=["purchases"])


@router.get("/recent")
def recent(
    q: str | None = Query(None, description="型号/描述/品牌关键词"),
    days: int = Query(30, ge=1, le=3660),
    supplier: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _: str = Depends(current_role),
    _page: None = Depends(require_page("page_purchases")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    record_access_log(ctx, "recent", "purchases",
                      {"q": q, "days": days, "supplier": supplier})
    data = purchase_query.recent_purchases(db, ctx, q=q, days=days,
                                           supplier=supplier, page=page,
                                           page_size=page_size)
    return apply_field_visibility(data, ctx)

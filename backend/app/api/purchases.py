"""采购记录 API（合同重点：销售/采购直接看最近采购）。任意登录角色可用——
三期口径：销售可以看采购价（整机拆解加点直卖）；行级敏感的是销售客户数据，
采购记录无销售归属，apply_data_scope/apply_field_visibility 钩子照过以备将来收紧。
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.auth import current_role
from app.db import get_db
from app.security import UserContext, apply_field_visibility, get_current_user_context, record_access_log
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
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    record_access_log(ctx, "recent", "purchases",
                      {"q": q, "days": days, "supplier": supplier})
    data = purchase_query.recent_purchases(db, ctx, q=q, days=days,
                                           supplier=supplier, page=page,
                                           page_size=page_size)
    return apply_field_visibility(data, ctx)

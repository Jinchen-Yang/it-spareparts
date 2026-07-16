"""DEV-05B1 采购价规则校准预览 API（只读）。"""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.auth import current_role
from app.db import get_db
from app.security import UserContext, get_current_user_context, is_field_hidden, require_page
from app.services import data_quality_calibration as svc


router = APIRouter(
    prefix="/data-quality/calibration",
    tags=["data-quality-calibration"],
    dependencies=[Depends(current_role), Depends(require_page("page_governance"))],
)


def _require_purchase_cost(
    ctx: UserContext = Depends(get_current_user_context),
) -> UserContext:
    # 不能只隐藏金额。倍率、排序和候选数本身就能反推采购价差异。
    if is_field_hidden(ctx, "unit_price"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "无权查看采购成本校准预览",
        )
    return ctx


@router.get("/purchase-price")
def purchase_price_preview(
    date_from: date | None = None,
    date_to: date | None = None,
    purchase_type: str | None = Query(None, max_length=64),
    sample_limit: int = Query(5, ge=1, le=20),
    db: Session = Depends(get_db),
    _ctx: UserContext = Depends(_require_purchase_cost),
) -> dict:
    today = date.today()
    effective_to = min(date_to or today, today)
    if date_from is not None and date_from > effective_to:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "开始日期不能晚于截止日期，且校准预览不包含未来记录",
        )
    normalized_type = purchase_type.strip() if purchase_type else None
    return svc.purchase_price_preview(
        db,
        date_from=date_from,
        date_to=effective_to,
        purchase_type=normalized_type or None,
        sample_limit=sample_limit,
    )

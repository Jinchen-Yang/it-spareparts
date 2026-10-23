"""采购价校准预览 transport：独立 preview 动作及成本字段保护。

仅资源尚未切换时，protected service 的 legacy adapter 保留旧权限语义。
"""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.authz.principal import VerifiedIdentity, current_verified_identity
from app.services import governance_access
from app.security import UserContext, get_current_user_context
from app.services import data_quality_calibration as svc


router = APIRouter(
    prefix="/data-quality/calibration",
    tags=["data-quality-calibration"],
)


@router.get("/purchase-price")
def purchase_price_preview(
    date_from: date | None = None,
    date_to: date | None = None,
    purchase_type: str | None = Query(None, max_length=64),
    sample_limit: int = Query(5, ge=1, le=20),
    db: Session = Depends(get_db),
    _ctx: UserContext = Depends(get_current_user_context),
    identity: VerifiedIdentity = Depends(current_verified_identity),
) -> dict:
    today = date.today()
    effective_to = min(date_to or today, today)
    if date_from is not None and date_from > effective_to:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "开始日期不能晚于截止日期，且校准预览不包含未来记录",
        )
    normalized_type = purchase_type.strip() if purchase_type else None
    try:
        return governance_access.preview_quality_calibration(
            db, identity, legacy_context=_ctx,
            date_from=date_from,
            date_to=effective_to,
            purchase_type=normalized_type or None,
            sample_limit=sample_limit,
        )
    except svc.CalibrationPreviewTimeout as exc:
        # statement_timeout 会使当前事务进入失败态；先回滚再交还连接池。
        db.rollback()
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "规则校准预览查询超时，请缩小日期范围后重试",
        ) from exc

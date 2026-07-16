"""维保需求号匹配归因 API（DEV-13A，只读）。"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.auth import current_role
from app.db import get_db
from app.security import (
    UserContext,
    get_current_user_context,
    record_access_log,
    require_page,
)
from app.services import maintenance_match_audit

router = APIRouter(prefix="/maintenance", tags=["maintenance"])


@router.get("/match-audit")
def match_audit(
    sample_limit: int = Query(5, ge=0, le=10),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    record_access_log(ctx, "match_audit", "maintenance", {"sample_limit": sample_limit})
    return maintenance_match_audit.build_report(db, sample_limit=sample_limit)

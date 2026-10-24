"""维保需求号匹配归因 API（DEV-13A，只读）。"""
from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from app.auth import current_role
from app.authz import runtime
from app.api.maintenance_project_scope import identity_for, legacy_page
from app.services import maintenance_diagnostics_access as protected_diagnostics
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
    _page: None = Depends(legacy_page("maintenance_audit", "page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
    typed_identity=Depends(identity_for("maintenance_audit")),
    view: str | None = Query(None),
    authorization_precondition: str | None = Query(None),
    response: Response = None,
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    if runtime.is_authoritative(db,"maintenance_audit"):
        return protected_diagnostics.read_match_audit(db,typed_identity,sample_limit=sample_limit,view=view,precondition=authorization_precondition)
    record_access_log(ctx, "match_audit", "maintenance", {"sample_limit": sample_limit})
    return maintenance_match_audit.build_report(db, sample_limit=sample_limit)

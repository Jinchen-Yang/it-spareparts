"""跨项目流转搜索（只读 POST）。"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth import current_role
from app.db import get_db
from app.security import UserContext, get_current_user_context, record_access_log, require_page
from app.services.maintenance_movement_search import MovementSearchQuery, search_movements

router = APIRouter(prefix="/maintenance/movements", tags=["maintenance"])


@router.post("/search")
def search_maintenance_movements(
    body: MovementSearchQuery,
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    payload = search_movements(db, body, ctx)
    record_access_log(ctx, "maintenance_movement_search", "maintenance_project", {
        "searched": True, "entity": body.entity, "field": body.field,
        "page": body.page, "page_size": body.page_size,
    })
    return payload

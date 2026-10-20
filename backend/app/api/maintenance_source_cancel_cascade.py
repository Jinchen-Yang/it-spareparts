"""Project-scoped source cancellation review and explicit human commands."""
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import permissions
from app.api.maintenance_demands import _real_operator
from app.api.maintenance_project_scope import (
    enforce_maintenance_project_access,
    require_maintenance_project_access,
)
from app.auth import current_identity, current_role
from app.db import get_db
from app.maintenance_boss import require_maintenance_boss
from app.models.system import SysUser
from app.security import (
    get_current_user_context,
    record_access_log,
    require_action,
    require_page,
)
from app.services import maintenance_source_cancel_cascade as service

router = APIRouter(
    prefix="/maintenance/projects/{project_id}/source-cancellations", tags=["maintenance"],
    dependencies=[Depends(require_maintenance_boss), Depends(current_role),
                  Depends(require_page("page_maintenance")), Depends(require_maintenance_project_access)],
)


@router.get("")
def list_impacts(project_id: str, response: Response,
                 page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
                 db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    return service.list_candidates(db, project_id=project_id, page=page, page_size=page_size)


@router.get("/{batch_id}/{source_order_id}")
def detail(project_id: str, batch_id: int, source_order_id: str, response: Response,
           db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    try:
        return service.candidate_detail(db, project_id=project_id, batch_id=batch_id,
                                        source_order_id=source_order_id)
    except service.CancellationConflict as exc:
        raise HTTPException(409, {"code": "source_cancellation_changed", "message": str(exc)}) from exc


_bearer = HTTPBearer(auto_error=False)


class ConfirmCancellationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    reason: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")


@router.get("/{batch_id}/{source_order_id}/confirmation-status")
def confirmation_status(project_id: str, batch_id: int, source_order_id: str,
                        response: Response,
                        idempotency_key: str = Query(
                            min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"),
                        db: Session = Depends(get_db),
                        identity: dict = Depends(current_identity),
                        _action: None = Depends(require_action("action_maintenance_demand_delete"))):
    response.headers["Cache-Control"] = "no-store"
    operator = _real_operator(db, identity)
    try:
        return service.confirmation_status(
            db, project_id=project_id, batch_id=batch_id, source_order_id=source_order_id,
            operated_by=operator, idempotency_key=idempotency_key,
        )
    except service.CancellationConflict as exc:
        raise HTTPException(409, {"code": "source_cancellation_changed", "message": str(exc)}) from exc


@router.post("/{batch_id}/{source_order_id}/confirm")
def confirm(project_id: str, batch_id: int, source_order_id: str,
            body: ConfirmCancellationRequest, response: Response,
            db: Session = Depends(get_db), identity: dict = Depends(current_identity),
            creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
            _action: None = Depends(require_action("action_maintenance_demand_delete"))):
    response.headers["Cache-Control"] = "no-store"
    operator = _real_operator(db, identity)
    audit_ctx = None

    def reauthorize():
        nonlocal audit_ctx
        fresh_identity = current_identity(creds, db)
        if _real_operator(db, fresh_identity) != operator:
            raise HTTPException(403, "实名身份已变化，请重新登录")
        user = db.scalar(select(SysUser).where(SysUser.username == operator)
                         .execution_options(populate_existing=True))
        ctx = get_current_user_context(creds, db)
        # Re-read account permissions too: an earlier dependency's token/context
        # must not authorize a command after waiting on a project/data lock.
        ctx.role, ctx.salesperson_name = user.role, user.salesperson_name
        ctx.permissions = permissions.runtime_safe(permissions.effective_for_user(user))
        require_page("page_maintenance")(ctx)
        require_action("action_maintenance_demand_delete")(ctx)
        enforce_maintenance_project_access(db, project_id=project_id, ctx=ctx)
        audit_ctx = ctx
        return ctx

    try:
        result = service.confirm_cancellation(db, project_id=project_id, batch_id=batch_id,
            source_order_id=source_order_id, expected_digest=body.expected_digest, reason=body.reason,
            operated_by=operator, idempotency_key=body.idempotency_key, reauthorize=reauthorize)
        db.commit()
        record_access_log(audit_ctx, "confirm", "maintenance_source_cancel",
                          {"batch_id": batch_id, "source_order_id": source_order_id, "replayed": result["replayed"]})
        return result
    except service.CancellationConflict as exc:
        db.rollback()
        raise HTTPException(409, {"code": "source_cancellation_changed", "message": str(exc)}) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(422, str(exc)) from exc
    except Exception:
        db.rollback()
        raise

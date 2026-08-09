"""WBDD header search and server-enforced safe logical deletion API."""

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import current_identity, current_role, require_admin
from app.db import get_db
from app.models.system import SysUser
from app.security import (
    UserContext,
    get_current_user_context,
    record_access_log,
    require_action,
    require_page,
)
from app.services import maintenance_demands


router = APIRouter(prefix="/maintenance/demands", tags=["maintenance"])


class DemandSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Length is validated explicitly in the endpoint.  Pydantic's default
    # max_length error includes the rejected input, which would reflect a
    # potentially sensitive order/project/PN search term in the 422 response.
    q: str | None = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=200)


class DeleteIntentCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Keep item/count validation in the service.  Pydantic's collection bound
    # error serializes the complete rejected list into ``input``, which can
    # reflect up to 1,001 private stable business identifiers to the caller.
    source_order_ids: list
    # The service enforces the upper bound with a generic no-reflection error.
    reason: str = Field(min_length=1)
    idempotency_key: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )


class DeleteIntentActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    digest: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")


class DemandRestoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Do not use Pydantic max_length here: its default 422 reflects the entire
    # rejected reason.  The service applies the same 1,000-character bound.
    reason: str = Field(min_length=1)


def _real_operator(db: Session, ident: dict) -> str:
    """High-risk writes reject shared/fallback/legacy token provenance."""

    if ident.get("authn") != "sys_user" or ident.get("fb"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "维保需求单删除必须使用实名系统账号，请重新登录",
        )
    username = str(ident.get("sub") or "").strip()
    user = db.scalar(
        select(SysUser).where(
            SysUser.username == username,
            SysUser.is_active.is_(True),
        )
    )
    if not username or user is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "维保需求单删除必须使用实名系统账号，请重新登录",
        )
    return username


def _raise_service_error(exc: Exception) -> None:
    if isinstance(exc, maintenance_demands.DeleteIntentTooEarly):
        raise HTTPException(
            425,
            {
                "message": str(exc),
                "not_before": exc.not_before.isoformat(),
                "server_now": exc.server_now.isoformat(),
            },
            headers={"Retry-After": "1"},
        ) from exc
    if isinstance(exc, maintenance_demands.MaintenanceDemandNotFound):
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    if isinstance(exc, maintenance_demands.DeleteIntentConflict):
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    if isinstance(exc, maintenance_demands.MaintenanceDemandError):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    raise exc


@router.post("/search")
def search_demands(
    body: DemandSearchRequest,
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    if body.q is not None and len(body.q) > 128:
        # Keep the rejection generic and perform it before access auditing so
        # neither the raw term nor any derivative is persisted.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "维保需求单搜索条件无效",
        )
    # Deliberately audit only the presence of a search, never its user-entered text.
    record_access_log(
        ctx,
        "maintenance_demand_search",
        "maintenance_demands",
        {"searched": bool(body.q and body.q.strip()), "page": body.page},
    )
    return maintenance_demands.search_demands(
        db,
        q=body.q,
        page=body.page,
        page_size=body.page_size,
    )


@router.post("/delete-intents", status_code=status.HTTP_201_CREATED)
def create_delete_intent(
    body: DeleteIntentCreateRequest,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(require_action("action_maintenance_demand_delete")),
) -> dict:
    operated_by = _real_operator(db, ident)
    try:
        result = maintenance_demands.create_delete_intent(
            db,
            source_order_ids=body.source_order_ids,
            reason=body.reason,
            idempotency_key=body.idempotency_key,
            operated_by=operated_by,
        )
        db.commit()
        return result
    except maintenance_demands.MaintenanceDemandError as exc:
        db.rollback()
        _raise_service_error(exc)
    except Exception:
        db.rollback()
        raise


@router.get("/delete-intents/{intent_id}")
def get_delete_intent(
    intent_id: str = Path(..., min_length=36, max_length=36),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(require_action("action_maintenance_demand_delete")),
) -> dict:
    operated_by = _real_operator(db, ident)
    try:
        return maintenance_demands.get_delete_intent(
            db,
            intent_id=intent_id,
            operated_by=operated_by,
        )
    except maintenance_demands.MaintenanceDemandError as exc:
        _raise_service_error(exc)


def _intent_action(
    *,
    action: str,
    intent_id: str,
    body: DeleteIntentActionRequest,
    db: Session,
    ident: dict,
) -> dict:
    operated_by = _real_operator(db, ident)
    operation = {
        "arm": maintenance_demands.arm_delete_intent,
        "execute": maintenance_demands.execute_delete_intent,
        "cancel": maintenance_demands.cancel_delete_intent,
    }[action]
    try:
        result = operation(
            db,
            intent_id=intent_id,
            digest=body.digest,
            operated_by=operated_by,
        )
        db.commit()
        return result
    except maintenance_demands.DeleteIntentTooEarly as exc:
        db.rollback()
        _raise_service_error(exc)
    except maintenance_demands.DeleteIntentConflict as exc:
        # A version conflict is itself durable business evidence.  The service
        # changes no tombstones before validation, so committing here preserves
        # the terminal conflict/event while still guaranteeing zero deletion.
        db.commit()
        _raise_service_error(exc)
    except maintenance_demands.MaintenanceDemandError as exc:
        db.rollback()
        _raise_service_error(exc)
    except Exception:
        db.rollback()
        raise


@router.post("/delete-intents/{intent_id}/arm")
def arm_delete_intent(
    body: DeleteIntentActionRequest,
    intent_id: str = Path(..., min_length=36, max_length=36),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(require_action("action_maintenance_demand_delete")),
) -> dict:
    return _intent_action(
        action="arm", intent_id=intent_id, body=body, db=db, ident=ident
    )


@router.post("/delete-intents/{intent_id}/execute")
def execute_delete_intent(
    body: DeleteIntentActionRequest,
    intent_id: str = Path(..., min_length=36, max_length=36),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(require_action("action_maintenance_demand_delete")),
) -> dict:
    return _intent_action(
        action="execute", intent_id=intent_id, body=body, db=db, ident=ident
    )


@router.post("/delete-intents/{intent_id}/cancel")
def cancel_delete_intent(
    body: DeleteIntentActionRequest,
    intent_id: str = Path(..., min_length=36, max_length=36),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(require_action("action_maintenance_demand_delete")),
) -> dict:
    return _intent_action(
        action="cancel", intent_id=intent_id, body=body, db=db, ident=ident
    )


@router.post("/{source_order_id}/restore")
def restore_demand(
    body: DemandRestoreRequest,
    # The service applies a generic no-reflection bound.  A constrained Path
    # would include the rejected identifier in FastAPI's default 422 payload.
    source_order_id: str = Path(...),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _admin: str = Depends(require_admin),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(require_action("action_maintenance_demand_delete")),
) -> dict:
    operated_by = _real_operator(db, ident)
    try:
        result = maintenance_demands.restore_demand(
            db,
            source_order_id=source_order_id,
            reason=body.reason,
            operated_by=operated_by,
        )
        db.commit()
        return result
    except maintenance_demands.MaintenanceDemandError as exc:
        db.rollback()
        _raise_service_error(exc)
    except Exception:
        db.rollback()
        raise

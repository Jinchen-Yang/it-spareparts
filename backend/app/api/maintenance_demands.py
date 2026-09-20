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
from app.api.maintenance_project_scope import (
    resolve_visible_project_ids as scope_resolve,
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
    # #268 场景一「含已作废」视图：默认 false 只看有效单。
    include_voided: bool = False


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
    if isinstance(exc, maintenance_demands.MaintenanceDemandForbidden):
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
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
    allowed_project_ids = scope_resolve(db, ctx)
    # Deliberately audit only the presence of a search, never its user-entered text.
    record_access_log(
        ctx,
        "maintenance_demand_search",
        "maintenance_demands",
        {
            "searched": bool(body.q and body.q.strip()),
            "page": body.page,
            "scope": "full" if allowed_project_ids is None else "owned",
        },
    )
    return maintenance_demands.search_demands(
        db,
        q=body.q,
        page=body.page,
        page_size=body.page_size,
        allowed_project_ids=allowed_project_ids,
        include_voided=body.include_voided,
        user_ctx=ctx,
    )


class DemandVoidFastRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Bound validation stays in the service（同 DeleteIntentCreateRequest 的
    # 防反射约定：Pydantic 集合边界错误会把整份被拒列表回显给调用方）。
    source_order_ids: list
    reason: str = Field(min_length=1)
    idempotency_key: str | None = Field(
        default=None,
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )


@router.post("/void-fast")
def void_fast(
    body: DemandVoidFastRequest,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(require_action("action_maintenance_demand_delete")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """一键批量作废（#264/#267）：单事务墓碑 + 挂靠停用，无两阶段等待。"""
    operated_by = _real_operator(db, ident)
    allowed_project_ids = scope_resolve(db, ctx)
    record_access_log(
        ctx,
        "maintenance_demand_void_fast",
        "maintenance_demands",
        {
            "headers": len(body.source_order_ids),
            "scope": "full" if allowed_project_ids is None else "owned",
        },
    )
    try:
        result = maintenance_demands.void_fast(
            db,
            source_order_ids=body.source_order_ids,
            reason=body.reason,
            operated_by=operated_by,
            allowed_project_ids=allowed_project_ids,
            idempotency_key=body.idempotency_key,
        )
        db.commit()
        return result
    except maintenance_demands.MaintenanceDemandError as exc:
        db.rollback()
        _raise_service_error(exc)
    except Exception:
        db.rollback()
        raise


@router.post("/delete-intents", status_code=status.HTTP_201_CREATED)
def create_delete_intent(
    body: DeleteIntentCreateRequest,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(require_action("action_maintenance_demand_delete")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    operated_by = _real_operator(db, ident)
    allowed_project_ids = scope_resolve(db, ctx)
    try:
        result = maintenance_demands.create_delete_intent(
            db,
            source_order_ids=body.source_order_ids,
            reason=body.reason,
            idempotency_key=body.idempotency_key,
            operated_by=operated_by,
            allowed_project_ids=allowed_project_ids,
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
    allowed_project_ids: set[str] | None,
) -> dict:
    operated_by = _real_operator(db, ident)
    operation = {
        "arm": maintenance_demands.arm_delete_intent,
        "execute": maintenance_demands.execute_delete_intent,
        "cancel": maintenance_demands.cancel_delete_intent,
    }[action]
    kwargs = {
        "intent_id": intent_id,
        "digest": body.digest,
        "operated_by": operated_by,
    }
    if action == "execute":
        kwargs["allowed_project_ids"] = allowed_project_ids
    try:
        result = operation(db, **kwargs)
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
        action="arm",
        intent_id=intent_id,
        body=body,
        db=db,
        ident=ident,
        allowed_project_ids=None,
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
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    # 执行时重新鉴权（TOCTOU）：创建意图时的范围可能已失效（改派/撤权）。
    allowed_project_ids = scope_resolve(db, ctx)
    return _intent_action(
        action="execute",
        intent_id=intent_id,
        body=body,
        db=db,
        ident=ident,
        allowed_project_ids=allowed_project_ids,
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
        action="cancel",
        intent_id=intent_id,
        body=body,
        db=db,
        ident=ident,
        allowed_project_ids=None,
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


# ---------- v1.36 Phase E：页面直改/直建需求行 ----------


class DemandLinePatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    updates: dict  # 字段白名单在 service 层失败关闭校验
    reason: str = Field(min_length=1)


class DemandLineCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_date: str  # YYYY-MM-DD（服务层解析）
    project_id: str = Field(min_length=1, max_length=36)
    pn_std: str = Field(min_length=1, max_length=128)
    qty: float = Field(gt=0)
    return_qty: float = Field(default=0, ge=0)
    serial_numbers: str | None = Field(default=None, max_length=32767)
    description: str | None = Field(default=None, max_length=32767)
    reason: str = Field(min_length=1)


class DemandLineOverrideClearRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_name: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1)


def _raise_manual_error(exc: Exception) -> None:
    from app.services import maintenance_demand_manual

    if isinstance(exc, maintenance_demand_manual.DemandManualConflict):
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.patch("/lines/{raw_line_id}")
def patch_demand_line(
    body: DemandLinePatchRequest,
    raw_line_id: str = Path(min_length=1, max_length=64),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(require_action("action_maintenance_demand_manage")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """页面直改一条需求明细行（override 账本 + 审计 + recompute 联动）。"""
    from app.services import maintenance_demand_manual

    operated_by = _real_operator(db, ident)
    record_access_log(ctx, "demand_line_patch", "maintenance_demands",
                      {"raw_line_id": raw_line_id,
                       "fields": sorted(set(body.updates))})
    try:
        result = maintenance_demand_manual.patch_demand_line(
            db, raw_line_id=raw_line_id, updates=body.updates,
            reason=body.reason, operated_by=operated_by,
        )
        db.commit()
        return result
    except maintenance_demand_manual.DemandManualError as exc:
        db.rollback()
        _raise_manual_error(exc)
    except maintenance_demand_manual.DemandManualConflict as exc:
        db.rollback()
        _raise_manual_error(exc)
    except Exception:
        db.rollback()
        raise


@router.post("/lines", status_code=status.HTTP_201_CREATED)
def create_demand_line(
    body: DemandLineCreateRequest,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(require_action("action_maintenance_demand_manage")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """页面直建手工需求行（page_manual 来源，不参与氚云删单比对）。"""
    from datetime import date as _date

    from app.services import maintenance_demand_manual

    operated_by = _real_operator(db, ident)
    try:
        parsed_date = _date.fromisoformat(body.order_date)
    except ValueError:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "order_date 必须是 YYYY-MM-DD"
        ) from None
    record_access_log(ctx, "demand_line_create", "maintenance_demands",
                      {"project_id": body.project_id, "pn_std": body.pn_std})
    try:
        result = maintenance_demand_manual.create_manual_demand_line(
            db, order_date=parsed_date, project_id=body.project_id,
            pn_std=body.pn_std, qty=body.qty, return_qty=body.return_qty,
            serial_numbers=body.serial_numbers, description=body.description,
            reason=body.reason, operated_by=operated_by,
        )
        db.commit()
        return result
    except maintenance_demand_manual.DemandManualError as exc:
        db.rollback()
        _raise_manual_error(exc)
    except maintenance_demand_manual.DemandManualConflict as exc:
        db.rollback()
        _raise_manual_error(exc)
    except Exception:
        db.rollback()
        raise


@router.post("/lines/{raw_line_id}/clear-override")
def clear_override(
    body: DemandLineOverrideClearRequest,
    raw_line_id: str = Path(min_length=1, max_length=64),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(require_action("action_maintenance_demand_manage")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """撤销一个字段的 override，恢复氚云原始值（source_value 快照）。"""
    from app.services import maintenance_demand_manual

    operated_by = _real_operator(db, ident)
    record_access_log(ctx, "demand_line_override_clear", "maintenance_demands",
                      {"raw_line_id": raw_line_id, "field": body.field_name})
    try:
        result = maintenance_demand_manual.clear_override(
            db, raw_line_id=raw_line_id, field=body.field_name,
            reason=body.reason, operated_by=operated_by,
        )
        db.commit()
        return result
    except maintenance_demand_manual.DemandManualError as exc:
        db.rollback()
        _raise_manual_error(exc)
    except maintenance_demand_manual.DemandManualConflict as exc:
        db.rollback()
        _raise_manual_error(exc)
    except Exception:
        db.rollback()
        raise


class DemandLineOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    raw_line_id: str
    order_raw_id: str
    order_no: str | None
    line_no: int | None
    part_id: int | None
    pn_std: str | None
    pn_raw: str | None
    description: str | None
    qty: str | None
    return_qty: str | None
    serial_numbers: str | None
    edited_source: str
    manual_override: dict
    is_active: bool


@router.get("/orders/{source_order_id}/lines")
def list_demand_lines(
    source_order_id: str = Path(min_length=1, max_length=64),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """列出一个需求单头下的明细行（页面行编辑的数据源）。

    可见性：scope 受限账号只能看自己项目内的单（失败关闭）；
    include_inactive=false 默认只回活行（作废行不可编辑也无须展示）。
    """
    from app.models.maintenance import FMaintenanceLine

    visible = scope_resolve(db, ctx)
    if visible is not None:
        assigned = db.scalars(
            select(maintenance_demands.MaintenanceSourceOrderAssignment.project_id).where(
                maintenance_demands.MaintenanceSourceOrderAssignment.source_order_id
                == source_order_id,
                maintenance_demands.MaintenanceSourceOrderAssignment.is_active.is_(True),
            )
        ).all()
        if not assigned or not set(assigned) <= set(visible):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "该项目不在你的可见范围")
    order = db.scalar(
        select(maintenance_demands.FMaintenanceOrder).where(
            maintenance_demands.FMaintenanceOrder.raw_order_id == source_order_id
        )
    )
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "需求单不存在")
    rows = db.execute(
        select(FMaintenanceLine)
        .where(FMaintenanceLine.order_id == order.id,
               FMaintenanceLine.is_active.is_(True))
        .order_by(FMaintenanceLine.line_no)
    ).scalars().all()
    return {"items": [
        {
            "raw_line_id": r.raw_line_id,
            "order_raw_id": source_order_id,
            "order_no": order.order_no,
            "line_no": r.line_no,
            "part_id": r.part_id,
            "pn_std": r.pn_std,
            "pn_raw": r.pn_raw,
            "description": r.description,
            "qty": str(r.qty) if r.qty is not None else None,
            "return_qty": str(r.return_qty) if r.return_qty is not None else None,
            "serial_numbers": r.serial_numbers,
            "edited_source": r.edited_source,
            "manual_override": dict(r.manual_override or {}),
            "is_active": r.is_active,
        }
        for r in rows
    ]}

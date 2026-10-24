"""Independent replenishment-cart Beta API.

The server-side feature gate hides every business read/write/export endpoint
without deleting data.  Capabilities remains available so the Beta page can
show a clear closed-state and a link back to the stable inventory page.
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import permissions
from app.api.maintenance_project_scope import resolve_visible_project_ids
from app.auth import current_identity
from app.authz import runtime
from app.authz.errors import authz_error
from app.api import maintenance_project_scope as auth_transport
from app.services import replenishment_authorization as canonical
from app.services import maintenance_authorization as foundation
from app.beta_access import replenishment_beta_whitelisted
from app.config import get_settings
from app.db import get_db
from app.models.system import SysUser
from app.security import (
    UserContext,
    get_current_user_context,
    require_action,
)
from app.services import replenishment
from app.services import replenishment_cart

router = APIRouter(prefix="/replenishment-beta", tags=["replenishment-beta"])


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"


def _beta_enabled() -> None:
    if not get_settings().replenishment_beta_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "补库申请当前未开放")


def _beta_page_whitelist(ident: dict = Depends(current_identity)) -> None:
    """Account allowlist gate independent of the legacy RBAC admin bypass."""
    real_identity = (
        ident.get("authn") == "sys_user"
        and not ident.get("fb")
        and bool(ident.get("sub"))
    )
    if not real_identity:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "补库申请必须使用实名系统账号",
        )
    if not replenishment_beta_whitelisted(
        role=str(ident.get("role") or "guest"),
        permission_map=ident.get("perms"),
        real_identity=True,
    ):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "未获得补库申请页面权限",
        )


def _page_for(*resources):
    def gate(db: Session = Depends(get_db), ident: dict = Depends(current_identity)):
        if not any(runtime.is_authoritative(db, resource) for resource in resources):
            _beta_page_whitelist(ident)
    return gate


def _view(value):
    if value not in (None, 'authorization'):
        raise authz_error(422, 'AUTHZ_INVALID_REQUEST', '未知的授权视图')
    return value == 'authorization'


def _allowed(ctx: UserContext, key: str) -> bool:
    graph = ctx.permissions if isinstance(ctx.permissions, dict) else permissions.effective(ctx.role, None)
    return bool(graph.get(key, False))


def _require_price_data(ctx: UserContext) -> None:
    if not _allowed(ctx, "data_pool_price_governance"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "无权查看补库申请中的半年价格事实",
        )


def _identity(db: Session, ident: dict) -> tuple[str, str]:
    """Return a current, active named account; shared/fallback identities fail closed."""
    if ident.get("authn") != "sys_user" or ident.get("fb"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "补库申请必须使用实名系统账号",
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
            "补库申请必须使用实名系统账号",
        )
    return username, user.role


def _raise_domain(exc: replenishment.ReplenishmentError) -> None:
    raise HTTPException(exc.status_code, {"code": exc.code, "message": str(exc)}) from exc


def _retired() -> None:
    raise HTTPException(
        status.HTTP_410_GONE,
        {
            "code": "retired",
            "message": "旧补库草稿与审核流程已停用，请使用按项目一次性提交和人工复核包。",
        },
    )


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AtomicLineWrite(StrictModel):
    part_id: int = Field(ge=1)
    quantity: int = Field(ge=1, le=999999)
    special_note: str | None = Field(None, max_length=4000)


class ApplicationCreate(StrictModel):
    authorization_precondition: dict | None = None
    client_request_id: str = Field(min_length=8, max_length=128)
    project_id: str = Field(min_length=1, max_length=36)
    # 2026-08-18：不在 pydantic 层限长——超长文本反射进 422 detail 泄露敏感内容；
    # 长度校验交给业务层 _clean_optional（400，不反射原文）
    request_note: str | None = None
    lines: list[AtomicLineWrite] = Field(min_length=1, max_length=200)


class CartLineWrite(StrictModel):
    part_id: int = Field(ge=1)
    quantity: int = Field(ge=1, le=999999)
    special_note: str | None = Field(None, max_length=4000)


class CartReplace(StrictModel):
    authorization_precondition: dict | None = None
    expected_version: int | None = Field(None, ge=1)
    request_note: str | None = Field(None, max_length=4000)
    lines: list[CartLineWrite] = Field(min_length=1, max_length=200)


class CartSubmit(StrictModel):
    authorization_precondition: dict | None = None
    expected_version: int = Field(ge=1)
    # Stable cloud-draft key lets a response-loss retry recover the application
    # after the first successful transaction has deleted the submitted cart.
    client_request_id: str = Field(min_length=8, max_length=128)


class RevisionResolution(StrictModel):
    request_line_id: str = Field(min_length=1, max_length=36)
    action: str = Field(pattern=r"^(replace|remove)$")
    part_id: int | None = Field(None, ge=1)
    quantity: int | None = Field(None, ge=1, le=999999)
    special_note: str | None = Field(None, max_length=4000)


class RevisionCreate(StrictModel):
    authorization_precondition: dict | None = None
    expected_application_version: int = Field(ge=1)
    client_request_id: str = Field(min_length=8, max_length=128)
    # 二选一（2026-08-18）：
    # - lines：完整期望行集合——打回后「退回编辑」全量重编辑（可添加/删减/换PN/改数量/填备注）
    # - resolutions：仅逐条处理打回行（旧交互，兼容）
    lines: list[AtomicLineWrite] | None = Field(None, min_length=1, max_length=200)
    resolutions: list[RevisionResolution] | None = Field(None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def exactly_one_revision_mode(self) -> "RevisionCreate":
        if (self.lines is None) == (self.resolutions is None):
            raise ValueError(
                "lines（完整重编辑）与 resolutions（逐条处理）必须且只能提供一种"
            )
        return self


@router.get("/capabilities")
def capabilities(
    response: Response,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    ctx: UserContext = Depends(get_current_user_context),
    _page: None = Depends(_page_for('replenishment_capabilities')),
    identity=Depends(auth_transport.identity_for('replenishment_capabilities')),
    view: str | None = Query(None),
    authorization_precondition: str | None = Query(None),
) -> dict:
    if identity is not None:
        try:
            result = canonical.read(db, identity, entry_id=canonical.CAPABILITIES, params={},
                authorization_view=_view(view), precondition=foundation.decode_precondition(authorization_precondition))
        except replenishment.ReplenishmentError as exc:
            _raise_domain(exc)
        _no_store(response)
        return result
    _no_store(response)
    _identity(db, ident)
    return {
        "enabled": get_settings().replenishment_beta_enabled,
        "beta": True,
        "can_view_price": _allowed(ctx, "data_pool_price_governance"),
        "can_create": (
            _allowed(ctx, "action_replenishment_create")
            and _allowed(ctx, "data_pool_price_governance")
        ),
        "can_review": False,
        "workflow_mode": (
            "system_auto_review"
            if get_settings().replenishment_auto_review_enabled
            else "system_screening"
        ),
        "stage": "screening_complete",
        "stable_path": "/inventory",
        "data_contract": (
            "仅记录维保项目补库申请与提交时冻结的三查事实；"
            "不修改库存、不自动定价；自动审核开启时只对 PN 做系统裁决。"
        ),
    }


@router.get("/catalog")
def catalog(
    response: Response,
    q: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=50),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    ctx: UserContext = Depends(get_current_user_context),
    _gate: None = Depends(_beta_enabled),
    _page: None = Depends(_page_for('replenishment_catalog')),
    identity=Depends(auth_transport.identity_for('replenishment_catalog')),
    view: str | None = Query(None),
    authorization_precondition: str | None = Query(None),
) -> dict:
    if identity is not None:
        try:
            result = canonical.read(db, identity, entry_id=canonical.CATALOG, params={'q':q,'page':page,'page_size':page_size},
                authorization_view=_view(view), precondition=foundation.decode_precondition(authorization_precondition))
        except replenishment.ReplenishmentError as exc:
            _raise_domain(exc)
        _no_store(response)
        return result
    _no_store(response)
    _identity(db, ident)
    _require_price_data(ctx)
    if q is not None and len(q) > 128:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "搜索内容不能超过 128 个字符")
    return replenishment.catalog_search(db, q, page=page, page_size=page_size)


@router.get("/projects")
def projects(
    response: Response,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    ctx: UserContext = Depends(get_current_user_context),
    _gate: None = Depends(_beta_enabled),
    _page: None = Depends(_page_for('replenishment_project_directory')),
    identity=Depends(auth_transport.identity_for('replenishment_project_directory')),
    view: str | None = Query(None),
    authorization_precondition: str | None = Query(None),
) -> dict:
    if identity is not None:
        try:
            result = canonical.read(db, identity, entry_id=canonical.PROJECTS, params={},
                authorization_view=_view(view), precondition=foundation.decode_precondition(authorization_precondition))
        except replenishment.ReplenishmentError as exc:
            _raise_domain(exc)
        _no_store(response)
        return result
    _no_store(response)
    _identity(db, ident)
    _require_price_data(ctx)
    return {
        "items": replenishment.available_projects(
            db,
            visible_project_ids=resolve_visible_project_ids(db, ctx),
        )
    }


@router.get("/cart-drafts/{project_id}")
def get_cart_draft(
    project_id: str,
    response: Response,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    ctx: UserContext = Depends(get_current_user_context),
    _gate: None = Depends(_beta_enabled),
    _page: None = Depends(_page_for('replenishment_project_commands')),
    identity=Depends(auth_transport.identity_for('replenishment_project_commands')),
    view: str | None = Query(None),
    authorization_precondition: str | None = Query(None),
) -> dict:
    if identity is not None:
        try:
            result = canonical.read(db, identity, entry_id=canonical.CART_READ, params={'project_id':project_id},
                authorization_view=_view(view), precondition=foundation.decode_precondition(authorization_precondition))
        except replenishment.ReplenishmentError as exc:
            _raise_domain(exc)
        _no_store(response)
        return result
    _no_store(response)
    username, _role = _identity(db, ident)
    _require_price_data(ctx)
    try:
        return {"draft": replenishment_cart.get_cart_draft(
            db,
            username=username,
            user_ctx=ctx,
            project_id=project_id,
        )}
    except replenishment.ReplenishmentError as exc:
        _raise_domain(exc)


@router.put("/cart-drafts/{project_id}")
def put_cart_draft(
    project_id: str,
    body: CartReplace,
    response: Response,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    ctx: UserContext = Depends(get_current_user_context),
    _gate: None = Depends(_beta_enabled),
    _page: None = Depends(_page_for('replenishment_project_commands')),
    _action: None = Depends(auth_transport.legacy_gate(('replenishment_project_commands',), require_action("action_replenishment_create", require_data="data_pool_price_governance"))),
    identity=Depends(auth_transport.identity_for('replenishment_project_commands')),
    view: str | None = Query(None),
) -> dict:
    if identity is not None:
        _no_store(response)
        try:
            return canonical.command(db, identity, entry_id=canonical.CART_UPDATE, params={'project_id':project_id, **body.model_dump(exclude={'authorization_precondition'})},
                legacy_context=ctx, precondition=body.authorization_precondition, authorization_view=_view(view))
        except replenishment.ReplenishmentError as exc:
            _raise_domain(exc)
    _no_store(response)
    username, _role = _identity(db, ident)
    try:
        return {"draft": replenishment_cart.replace_cart_draft(
            db,
            username=username,
            user_ctx=ctx,
            project_id=project_id,
            expected_version=body.expected_version, request_note=body.request_note,
            lines=[line.model_dump() for line in body.lines],
        )}
    except replenishment.ReplenishmentError as exc:
        _raise_domain(exc)


@router.delete("/cart-drafts/{project_id}")
def remove_cart_draft(
    project_id: str,
    response: Response,
    expected_version: int | None = Query(None, ge=1),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    ctx: UserContext = Depends(get_current_user_context),
    _gate: None = Depends(_beta_enabled),
    _page: None = Depends(_page_for('replenishment_project_commands')),
    _action: None = Depends(auth_transport.legacy_gate(('replenishment_project_commands',), require_action("action_replenishment_create", require_data="data_pool_price_governance"))),
    identity=Depends(auth_transport.identity_for('replenishment_project_commands')),
    view: str | None = Query(None),
    authorization_precondition: dict | None = Body(None, embed=True),
) -> dict:
    if identity is not None:
        _no_store(response)
        try:
            return canonical.command(db, identity, entry_id=canonical.CART_DELETE, params={'project_id':project_id,'expected_version':expected_version},
                legacy_context=ctx, precondition=authorization_precondition, authorization_view=_view(view))
        except replenishment.ReplenishmentError as exc:
            _raise_domain(exc)
    _no_store(response)
    username, _role = _identity(db, ident)
    try:
        return {"deleted": replenishment_cart.delete_cart_draft(
            db,
            username=username,
            user_ctx=ctx,
            project_id=project_id,
            expected_version=expected_version,
        )}
    except replenishment.ReplenishmentError as exc:
        _raise_domain(exc)


@router.post("/cart-drafts/{project_id}/submit", status_code=status.HTTP_201_CREATED)
def submit_cart_draft(
    project_id: str,
    body: CartSubmit,
    response: Response,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    ctx: UserContext = Depends(get_current_user_context),
    _gate: None = Depends(_beta_enabled),
    _page: None = Depends(_page_for('replenishment_project_commands')),
    _action: None = Depends(auth_transport.legacy_gate(('replenishment_project_commands',), require_action("action_replenishment_create", require_data="data_pool_price_governance"))),
    identity=Depends(auth_transport.identity_for('replenishment_project_commands')),
    view: str | None = Query(None),
) -> dict:
    if identity is not None:
        _no_store(response)
        try:
            return canonical.command(db, identity, entry_id=canonical.CART_SUBMIT, params={'project_id':project_id, **body.model_dump(exclude={'authorization_precondition'})},
                legacy_context=ctx, precondition=body.authorization_precondition, authorization_view=_view(view))
        except replenishment.ReplenishmentError as exc:
            _raise_domain(exc)
    _no_store(response)
    username, _role = _identity(db, ident)
    try:
        return replenishment_cart.submit_cart_draft_atomic(
            db,
            username=username,
            user_ctx=ctx,
            project_id=project_id,
            expected_version=body.expected_version,
            client_request_id=body.client_request_id,
        )
    except replenishment.ReplenishmentError as exc:
        _raise_domain(exc)


@router.get("/applications")
def applications(
    response: Response,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    ctx: UserContext = Depends(get_current_user_context),
    _gate: None = Depends(_beta_enabled),
    _page: None = Depends(_page_for('replenishment_request')),
    identity=Depends(auth_transport.identity_for('replenishment_request')),
    view: str | None = Query(None),
    authorization_precondition: str | None = Query(None),
) -> dict:
    if identity is not None:
        try:
            result = canonical.read(db, identity, entry_id=canonical.LIST, params={'page':page,'page_size':page_size},
                authorization_view=_view(view), precondition=foundation.decode_precondition(authorization_precondition))
        except replenishment.ReplenishmentError as exc:
            _raise_domain(exc)
        _no_store(response)
        return result
    _no_store(response)
    username, role = _identity(db, ident)
    _require_price_data(ctx)
    return replenishment.list_applications(db, username=username, role=role, page=page, page_size=page_size)


@router.post("/applications", status_code=status.HTTP_201_CREATED)
def create_application(
    body: ApplicationCreate,
    response: Response,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    ctx: UserContext = Depends(get_current_user_context),
    _gate: None = Depends(_beta_enabled),
    _page: None = Depends(_page_for('replenishment_project_commands')),
    _action: None = Depends(auth_transport.legacy_gate(('replenishment_project_commands',), require_action("action_replenishment_create", require_data="data_pool_price_governance"))),
    identity=Depends(auth_transport.identity_for('replenishment_project_commands')),
    view: str | None = Query(None),
) -> dict:
    if identity is not None:
        _no_store(response)
        try:
            return canonical.command(db, identity, entry_id=canonical.CREATE, params=body.model_dump(exclude={'authorization_precondition'}),
                legacy_context=ctx, precondition=body.authorization_precondition, authorization_view=_view(view))
        except replenishment.ReplenishmentError as exc:
            _raise_domain(exc)
    _no_store(response)
    username, _role = _identity(db, ident)
    try:
        return replenishment.submit_application_atomic(
            db,
            username=username,
            user_ctx=ctx,
            client_request_id=body.client_request_id,
            project_id=body.project_id,
            request_note=body.request_note,
            lines=[line.model_dump() for line in body.lines],
        )
    except replenishment.ReplenishmentError as exc:
        _raise_domain(exc)


@router.get("/applications/{application_id}")
def application_detail(
    application_id: str,
    response: Response,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    ctx: UserContext = Depends(get_current_user_context),
    _gate: None = Depends(_beta_enabled),
    _page: None = Depends(_page_for('replenishment_request')),
    identity=Depends(auth_transport.identity_for('replenishment_request')),
    view: str | None = Query(None),
    authorization_precondition: str | None = Query(None),
) -> dict:
    if identity is not None:
        try:
            result = canonical.read(db, identity, entry_id=canonical.READ, params={'application_id':application_id},
                authorization_view=_view(view), precondition=foundation.decode_precondition(authorization_precondition))
        except replenishment.ReplenishmentError as exc:
            _raise_domain(exc)
        _no_store(response)
        return result
    _no_store(response)
    username, role = _identity(db, ident)
    _require_price_data(ctx)
    try:
        return replenishment.get_application(db, application_id, username=username, role=role)
    except replenishment.ReplenishmentError as exc:
        _raise_domain(exc)


@router.patch("/applications/{application_id}")
def patch_application(
    application_id: str,
    _gate: None = Depends(_beta_enabled),
    _page: None = Depends(_beta_page_whitelist),
    _action: None = Depends(require_action("action_replenishment_create", require_data="data_pool_price_governance")),
) -> None:
    _retired()


@router.post("/applications/{application_id}/lines")
def add_line(
    application_id: str,
    _gate: None = Depends(_beta_enabled),
    _page: None = Depends(_beta_page_whitelist),
    _action: None = Depends(require_action("action_replenishment_create", require_data="data_pool_price_governance")),
) -> None:
    _retired()


@router.patch("/applications/{application_id}/lines/{line_id}")
def patch_line(
    application_id: str,
    line_id: str,
    _gate: None = Depends(_beta_enabled),
    _page: None = Depends(_beta_page_whitelist),
    _action: None = Depends(require_action("action_replenishment_create", require_data="data_pool_price_governance")),
) -> None:
    _retired()


@router.delete("/applications/{application_id}/lines/{line_id}")
def delete_line(
    application_id: str,
    line_id: str,
    _gate: None = Depends(_beta_enabled),
    _page: None = Depends(_beta_page_whitelist),
    _action: None = Depends(require_action("action_replenishment_create", require_data="data_pool_price_governance")),
) -> None:
    _retired()


@router.post("/applications/{application_id}/submit")
def submit_application(
    application_id: str,
    _gate: None = Depends(_beta_enabled),
    _page: None = Depends(_beta_page_whitelist),
    _action: None = Depends(require_action("action_replenishment_create", require_data="data_pool_price_governance")),
) -> None:
    _retired()


@router.post("/applications/{application_id}/revision")
def create_revision(
    application_id: str,
    _gate: None = Depends(_beta_enabled),
    _page: None = Depends(_beta_page_whitelist),
    _action: None = Depends(require_action("action_replenishment_create", require_data="data_pool_price_governance")),
) -> None:
    _retired()


@router.post("/applications/{application_id}/revisions")
def apply_application_revision(
    application_id: str,
    body: RevisionCreate,
    response: Response,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    ctx: UserContext = Depends(get_current_user_context),
    _gate: None = Depends(_beta_enabled),
    _page: None = Depends(_page_for('replenishment_project_commands', 'replenishment_request')),
    _action: None = Depends(auth_transport.legacy_gate(('replenishment_project_commands', 'replenishment_request'), require_action("action_replenishment_create", require_data="data_pool_price_governance"))),
    identity=Depends(auth_transport.identity_for('replenishment_project_commands', 'replenishment_request')),
    view: str | None = Query(None),
) -> dict:
    if identity is not None:
        _no_store(response)
        try:
            return canonical.command(db, identity, entry_id=canonical.REVISE, params={'application_id':application_id, **body.model_dump(exclude={'authorization_precondition'})},
                legacy_context=ctx, precondition=body.authorization_precondition, authorization_view=_view(view))
        except replenishment.ReplenishmentError as exc:
            _raise_domain(exc)
    _no_store(response)
    username, _role = _identity(db, ident)
    for item in body.resolutions or []:
        if item.action == "replace" and item.part_id is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, {
                "code": "replacement_part_required", "message": "replace 必须提供 part_id"
            })
    try:
        return replenishment.apply_revision_atomic(
            db,
            application_id,
            username=username,
            user_ctx=ctx,
            expected_application_version=body.expected_application_version,
            client_request_id=body.client_request_id,
            lines=[item.model_dump() for item in body.lines]
            if body.lines is not None else None,
            resolutions=[item.model_dump() for item in body.resolutions]
            if body.resolutions is not None else None,
        )
    except replenishment.ReplenishmentError as exc:
        _raise_domain(exc)


@router.post("/applications/{application_id}/review-results")
def review_result(
    application_id: str,
    _gate: None = Depends(_beta_enabled),
    _page: None = Depends(_beta_page_whitelist),
    _action: None = Depends(require_action("action_replenishment_review")),
) -> None:
    _retired()


def _excel_response(data: bytes, filename: str) -> StreamingResponse:
    encoded = quote(filename, safe="!#$&+-.^_`|~")
    response = StreamingResponse(
        iter([data]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="replenishment.xlsx"; filename*=UTF-8\'\'{encoded}',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
    return response


@router.get("/applications/{application_id}/exports/manual-review.xlsx")
def export_manual_review(
    application_id: str,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    ctx: UserContext = Depends(get_current_user_context),
    _gate: None = Depends(_beta_enabled),
    _page: None = Depends(_beta_page_whitelist),
    _action: None = Depends(
        require_action(
            "action_replenishment_create",
            require_data="data_pool_price_governance",
        )
    ),
) -> StreamingResponse:
    _retired()
    username, role = _identity(db, ident)
    _require_price_data(ctx)
    try:
        data, filename = replenishment.manual_review_workbook(db, application_id, username=username, role=role)
    except replenishment.ReplenishmentError as exc:
        _raise_domain(exc)
    return _excel_response(data, filename)


@router.get("/applications/{application_id}/exports/system-screening.xlsx")
def export_system_screening(
    application_id: str,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    ctx: UserContext = Depends(get_current_user_context),
    _gate: None = Depends(_beta_enabled),
    _page: None = Depends(_page_for('replenishment_request')),
    _action: None = Depends(auth_transport.legacy_gate(('replenishment_request',), require_action("action_replenishment_create", require_data="data_pool_price_governance"))),
    identity=Depends(auth_transport.identity_for('replenishment_request')),
    view: str | None = Query(None),
    authorization_precondition: str | None = Query(None),
):
    if identity is not None:
        try:
            result = canonical.read(db, identity, entry_id=canonical.EXPORT, params={'application_id':application_id},
                authorization_view=_view(view), precondition=foundation.decode_precondition(authorization_precondition))
        except replenishment.ReplenishmentError as exc:
            _raise_domain(exc)
        if _view(view):
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        return _excel_response(*result)
    """AB-4：系统三查结果导出，交人工复核。系统不记录人工审核结论。"""
    username, role = _identity(db, ident)
    _require_price_data(ctx)
    try:
        data, filename = replenishment.system_screening_workbook(
            db, application_id, username=username, role=role
        )
    except replenishment.ReplenishmentError as exc:
        _raise_domain(exc)
    return _excel_response(data, filename)


@router.get("/applications/{application_id}/exports/wbdd-subset.xlsx")
def export_wbdd_subset(
    application_id: str,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    ctx: UserContext = Depends(get_current_user_context),
    _gate: None = Depends(_beta_enabled),
    _page: None = Depends(_beta_page_whitelist),
    _action: None = Depends(
        require_action(
            "action_replenishment_create",
            require_data="data_pool_price_governance",
        )
    ),
) -> StreamingResponse:
    _retired()
    username, role = _identity(db, ident)
    _require_price_data(ctx)
    try:
        data, filename = replenishment.wbdd_subset_workbook(db, application_id, username=username, role=role)
    except replenishment.ReplenishmentError as exc:
        _raise_domain(exc)
    return _excel_response(data, filename)


@router.get("/applications/{application_id}/evidence")
def application_evidence(
    application_id: str,
    response: Response,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    ctx: UserContext = Depends(get_current_user_context),
    _gate: None = Depends(_beta_enabled),
    _page: None = Depends(_beta_page_whitelist),
    _action: None = Depends(
        require_action(
            "action_replenishment_create",
            require_data="data_pool_price_governance",
        )
    ),
) -> dict:
    """补库行增强证据：365 天无记录提醒 / 通用池替代 / 高频件 / 成本区间。

    仅 owner/admin 可见（非 owner 与不存在同 404）；仅 approved 状态（否则 409）。
    """
    _retired()
    _no_store(response)
    _require_price_data(ctx)
    from app.services import maintenance_replenishment_evidence as evidence

    username, role = _identity(db, ident)
    try:
        result = evidence.application_evidence(
            db, application_id, username=username, role=role
        )
    except replenishment.ReplenishmentError as exc:
        _raise_domain(exc)
    return result


@router.get("/applications/{application_id}/exports/purchase-list.xlsx")
def export_purchase_list(
    application_id: str,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    ctx: UserContext = Depends(get_current_user_context),
    _gate: None = Depends(_beta_enabled),
    _page: None = Depends(_beta_page_whitelist),
    _action: None = Depends(
        require_action(
            "action_replenishment_create",
            require_data="data_pool_price_governance",
        )
    ),
) -> StreamingResponse:
    """审核通过后的四列导出：PN / 数量 / 采购金额(参考) / 销售金额(参考)。

    仅 owner/admin 可见（非 owner 与不存在同 404）；仅 approved 状态（否则 409）。
    """
    _retired()
    from app.services import maintenance_replenishment_evidence as evidence

    _require_price_data(ctx)
    username, role = _identity(db, ident)
    try:
        data = evidence.export_purchase_list(
            db, application_id, username=username, role=role
        )
    except replenishment.ReplenishmentError as exc:
        _raise_domain(exc)
    return _excel_response(data, f"补库采购清单_{application_id}.xlsx")

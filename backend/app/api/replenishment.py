"""Independent replenishment-cart Beta API.

The server-side feature gate hides every business read/write/export endpoint
without deleting data.  Capabilities remains available so the Beta page can
show a clear closed-state and a link back to the stable inventory page.
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import permissions
from app.auth import current_identity
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
    client_request_id: str = Field(min_length=8, max_length=128)
    project_id: str = Field(min_length=1, max_length=36)
    request_note: str | None = Field(None, max_length=4000)
    lines: list[AtomicLineWrite] = Field(min_length=1, max_length=200)


class CartLineWrite(StrictModel):
    part_id: int = Field(ge=1)
    quantity: int = Field(ge=1, le=999999)
    special_note: str | None = Field(None, max_length=4000)


class CartReplace(StrictModel):
    expected_version: int | None = Field(None, ge=1)
    request_note: str | None = Field(None, max_length=4000)
    lines: list[CartLineWrite] = Field(min_length=1, max_length=200)


class CartSubmit(StrictModel):
    expected_version: int = Field(ge=1)


class RevisionResolution(StrictModel):
    request_line_id: str = Field(min_length=1, max_length=36)
    action: str = Field(pattern=r"^(replace|remove)$")
    part_id: int | None = Field(None, ge=1)
    quantity: int | None = Field(None, ge=1, le=999999)
    special_note: str | None = Field(None, max_length=4000)


class RevisionCreate(StrictModel):
    expected_application_version: int = Field(ge=1)
    client_request_id: str = Field(min_length=8, max_length=128)
    # 二选一（2026-08-18）：
    # - lines：完整期望行集合——打回后「退回编辑」全量重编辑（可添加/删减/换PN/改数量/填备注）
    # - resolutions：仅逐条处理打回行（旧交互，兼容）
    lines: list[AtomicLineWrite] | None = Field(None, min_length=1, max_length=200)
    resolutions: list[RevisionResolution] | None = Field(None, min_length=1, max_length=200)


@router.get("/capabilities")
def capabilities(
    response: Response,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    ctx: UserContext = Depends(get_current_user_context),
    _page: None = Depends(_beta_page_whitelist),
) -> dict:
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
    _page: None = Depends(_beta_page_whitelist),
) -> dict:
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
    _gate: None = Depends(_beta_enabled),
    _page: None = Depends(_beta_page_whitelist),
) -> dict:
    _no_store(response)
    username, role = _identity(db, ident)
    return {"items": replenishment.available_projects(db, username=username, role=role)}


@router.get("/cart-drafts/{project_id}")
def get_cart_draft(
    project_id: str,
    response: Response,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _gate: None = Depends(_beta_enabled),
    _page: None = Depends(_beta_page_whitelist),
) -> dict:
    _no_store(response)
    username, _role = _identity(db, ident)
    try:
        return {"draft": replenishment_cart.get_cart_draft(db, username=username, project_id=project_id)}
    except replenishment.ReplenishmentError as exc:
        _raise_domain(exc)


@router.put("/cart-drafts/{project_id}")
def put_cart_draft(
    project_id: str,
    body: CartReplace,
    response: Response,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _gate: None = Depends(_beta_enabled),
    _page: None = Depends(_beta_page_whitelist),
    _action: None = Depends(require_action("action_replenishment_create", require_data="data_pool_price_governance")),
) -> dict:
    _no_store(response)
    username, role = _identity(db, ident)
    try:
        return {"draft": replenishment_cart.replace_cart_draft(
            db, username=username, role=role, project_id=project_id,
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
    _gate: None = Depends(_beta_enabled),
    _page: None = Depends(_beta_page_whitelist),
    _action: None = Depends(require_action("action_replenishment_create", require_data="data_pool_price_governance")),
) -> dict:
    _no_store(response)
    username, _role = _identity(db, ident)
    try:
        return {"deleted": replenishment_cart.delete_cart_draft(
            db, username=username, project_id=project_id, expected_version=expected_version
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
    _gate: None = Depends(_beta_enabled),
    _page: None = Depends(_beta_page_whitelist),
    _action: None = Depends(require_action("action_replenishment_create", require_data="data_pool_price_governance")),
) -> dict:
    _no_store(response)
    username, role = _identity(db, ident)
    try:
        return replenishment_cart.submit_cart_draft_atomic(
            db, username=username, role=role, project_id=project_id,
            expected_version=body.expected_version,
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
    _page: None = Depends(_beta_page_whitelist),
) -> dict:
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
    _gate: None = Depends(_beta_enabled),
    _page: None = Depends(_beta_page_whitelist),
    _action: None = Depends(require_action("action_replenishment_create", require_data="data_pool_price_governance")),
) -> dict:
    _no_store(response)
    username, role = _identity(db, ident)
    try:
        return replenishment.submit_application_atomic(
            db,
            username=username,
            role=role,
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
    _page: None = Depends(_beta_page_whitelist),
) -> dict:
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
    _gate: None = Depends(_beta_enabled),
    _page: None = Depends(_beta_page_whitelist),
    _action: None = Depends(require_action("action_replenishment_create", require_data="data_pool_price_governance")),
) -> dict:
    _no_store(response)
    username, role = _identity(db, ident)
    for item in body.resolutions or []:
        if item.action == "replace" and item.part_id is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, {
                "code": "replacement_part_required", "message": "replace 必须提供 part_id"
            })
    if not body.lines and not body.resolutions:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, {
            "code": "revision_content_required",
            "message": "必须提供 lines（完整重编辑）或 resolutions（逐条处理打回行）",
        })
    try:
        return replenishment.apply_revision_atomic(
            db, application_id, username=username, role=role,
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
    _page: None = Depends(_beta_page_whitelist),
    _action: None = Depends(
        require_action(
            "action_replenishment_create",
            require_data="data_pool_price_governance",
        )
    ),
) -> StreamingResponse:
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

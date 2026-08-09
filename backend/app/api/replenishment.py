"""Independent replenishment-cart Beta API.

The server-side feature gate hides every business read/write/export endpoint
without deleting data.  Capabilities remains available so the Beta page can
show a clear closed-state and a link back to the stable inventory page.
"""

from __future__ import annotations

from decimal import Decimal
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

router = APIRouter(prefix="/replenishment-beta", tags=["replenishment-beta"])


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"


def _beta_enabled() -> None:
    if not get_settings().replenishment_beta_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "补库申请 Beta 当前未开放")


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
            "补库申请 Beta 必须使用实名系统账号",
        )
    if not replenishment_beta_whitelisted(
        role=str(ident.get("role") or "guest"),
        permission_map=ident.get("perms"),
        real_identity=True,
    ):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "未加入补库申请 Beta 试用名单",
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
            "补库申请 Beta 必须使用实名系统账号",
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
            "补库申请 Beta 必须使用实名系统账号",
        )
    return username, user.role


def _raise_domain(exc: replenishment.ReplenishmentError) -> None:
    raise HTTPException(exc.status_code, {"code": exc.code, "message": str(exc)}) from exc


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ApplicationCreate(StrictModel):
    # Free text bounds are enforced in the service with generic errors. Pydantic's
    # max_length error reflects the rejected business text in the 422 response.
    warehouse: str | None = None
    request_note: str | None = None


class DraftUpdate(StrictModel):
    expected_version: int = Field(ge=1)
    warehouse: str | None = None
    request_note: str | None = None


class LineWrite(StrictModel):
    expected_version: int = Field(ge=1)
    part_id: int = Field(ge=1)
    quantity: Decimal = Field(gt=0, le=Decimal("999999.999"), max_digits=14, decimal_places=3)
    special_note: str | None = None


class VersionCommand(StrictModel):
    expected_version: int = Field(ge=1)


class ReviewDecision(StrictModel):
    line_id: str = Field(min_length=36, max_length=36)
    decision: str = Field(pattern="^(approved|rejected)$")
    reason: str | None = None


class ReviewWrite(StrictModel):
    version_id: str = Field(min_length=36, max_length=36)
    content_digest: str = Field(pattern="^[a-f0-9]{64}$")
    idempotency_key: str = Field(min_length=8, max_length=128)
    external_reference: str | None = None
    summary_note: str | None = None
    # The service validates exact submitted-line coverage without reflecting a
    # rejected oversized decision list in FastAPI's default 422 payload.
    decisions: list[ReviewDecision]


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
        "can_review": _allowed(ctx, "action_replenishment_review"),
        "stable_path": "/inventory",
        "data_contract": "仅记录补库申请，不修改库存；历史价格为未税聚合事实，不是自动定价。",
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


@router.post("/applications")
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
    username, _role = _identity(db, ident)
    try:
        return replenishment.create_application(db, username=username, warehouse=body.warehouse, request_note=body.request_note)
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
    body: DraftUpdate,
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
        return replenishment.update_draft(
            db,
            application_id,
            username=username,
            role=role,
            expected_version=body.expected_version,
            warehouse=body.warehouse,
            request_note=body.request_note,
        )
    except replenishment.ReplenishmentError as exc:
        _raise_domain(exc)


@router.post("/applications/{application_id}/lines")
def add_line(
    application_id: str,
    body: LineWrite,
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
        return replenishment.add_line(
            db,
            application_id,
            username=username,
            role=role,
            expected_version=body.expected_version,
            part_id=body.part_id,
            quantity=body.quantity,
            special_note=body.special_note,
        )
    except replenishment.ReplenishmentError as exc:
        _raise_domain(exc)


@router.patch("/applications/{application_id}/lines/{line_id}")
def patch_line(
    application_id: str,
    line_id: str,
    body: LineWrite,
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
        return replenishment.update_line(
            db,
            application_id,
            line_id,
            username=username,
            role=role,
            expected_version=body.expected_version,
            part_id=body.part_id,
            quantity=body.quantity,
            special_note=body.special_note,
        )
    except replenishment.ReplenishmentError as exc:
        _raise_domain(exc)


@router.delete("/applications/{application_id}/lines/{line_id}")
def delete_line(
    application_id: str,
    line_id: str,
    response: Response,
    expected_version: int = Query(..., ge=1),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _gate: None = Depends(_beta_enabled),
    _page: None = Depends(_beta_page_whitelist),
    _action: None = Depends(require_action("action_replenishment_create", require_data="data_pool_price_governance")),
) -> dict:
    _no_store(response)
    username, role = _identity(db, ident)
    try:
        return replenishment.remove_line(
            db,
            application_id,
            line_id,
            username=username,
            role=role,
            expected_version=expected_version,
        )
    except replenishment.ReplenishmentError as exc:
        _raise_domain(exc)


@router.post("/applications/{application_id}/submit")
def submit_application(
    application_id: str,
    body: VersionCommand,
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
        return replenishment.submit(db, application_id, username=username, role=role, expected_version=body.expected_version)
    except replenishment.ReplenishmentError as exc:
        _raise_domain(exc)


@router.post("/applications/{application_id}/revision")
def create_revision(
    application_id: str,
    body: VersionCommand,
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
        return replenishment.start_revision(db, application_id, username=username, role=role, expected_version=body.expected_version)
    except replenishment.ReplenishmentError as exc:
        _raise_domain(exc)


@router.post("/applications/{application_id}/review-results")
def review_result(
    application_id: str,
    body: ReviewWrite,
    response: Response,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _gate: None = Depends(_beta_enabled),
    # Deliberate sole exception to the account Beta-page whitelist: this is a write-only
    # machine/integration callback guarded by a named identity, the global kill switch and
    # action_replenishment_review.  It cannot list applications, read price facts or export.
    # Named admins retain this action through the ordinary admin invariant; all human Beta
    # page/read/create endpoints above and below still require page_replenishment_beta.
    _action: None = Depends(require_action("action_replenishment_review")),
) -> dict:
    _no_store(response)
    username, _role = _identity(db, ident)
    if not 1 <= len(body.decisions) <= replenishment.MAX_LINES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"审核结论条数必须为 1-{replenishment.MAX_LINES}",
        )
    try:
        return replenishment.record_review(
            db,
            application_id,
            reviewer=username,
            version_id=body.version_id,
            content_digest=body.content_digest,
            idempotency_key=body.idempotency_key,
            external_reference=body.external_reference,
            summary_note=body.summary_note,
            decisions=[item.model_dump() for item in body.decisions],
        )
    except replenishment.ReplenishmentError as exc:
        _raise_domain(exc)


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
    username, role = _identity(db, ident)
    _require_price_data(ctx)
    try:
        data, filename = replenishment.manual_review_workbook(db, application_id, username=username, role=role)
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
    username, role = _identity(db, ident)
    _require_price_data(ctx)
    try:
        data, filename = replenishment.wbdd_subset_workbook(db, application_id, username=username, role=role)
    except replenishment.ReplenishmentError as exc:
        _raise_domain(exc)
    return _excel_response(data, filename)

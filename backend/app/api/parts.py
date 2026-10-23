"""型号查询 API（§9）。用 query 传 pn_std，避开 PN 中的 / # 路由问题。需登录。

/parts/master*（WP1）：采购可新建/编辑备件主数据，require_page('page_master_data') 准入。
"""
from datetime import date
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.security.utils import get_authorization_scheme_param
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse

from app.auth import current_identity, current_role
from app.authz.principal import current_verified_identity
from app.authz.runtime import is_authoritative, runtime_states
from app.api.query_bounds import PG_INT_MAX
from app.db import get_db
from app.models.dimensions import DimPart
from app.models.system import SysUser
from app.security import (
    UserContext, apply_field_visibility, get_current_user_context,
    record_access_log, require_page,
)
from app.services import batch_normalize, master_edit, part_overview, part_resolver, pool_price_analysis, standardize, taxonomy
from app.pn_catalog import api as catalog
from app.pn_catalog.authz import CatalogAccess
from app.pn_catalog.dto import CommandContext, CreatePart, PatchProfile
from app.pn_catalog.errors import CatalogError
from app.pn_catalog.http import _commit, _context
from app.pn_catalog.http_controls import error_body
from app.services.pn_catalog_access import resolve_native_access
from app.services.pn_catalog_projection import project_business_master

class LegacyCatalogRoute(APIRoute):
    """Retain legacy responses, but transport shared facade errors without leaking internals."""
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def wrapped(request):
            try:
                response = await handler(request)
            except CatalogError as exc:
                response = JSONResponse(error_body(exc.code, exc.message,
                    getattr(request.state, "pn_catalog_request_id", uuid4()),
                    details=exc.details, retryable=exc.retryable), status_code=exc.status_code)
            if getattr(request.state, "pn_catalog_shared", False):
                response.headers["Cache-Control"] = "no-store"
                response.headers["X-Request-ID"] = str(request.state.pn_catalog_request_id)
            return response
        return wrapped


router = APIRouter(prefix="/parts", tags=["parts"], route_class=LegacyCatalogRoute)

# 批量应用 kill-switch：确定性 standardize 引擎已接入并上线(v1.2.3)，已解冻(v1.2.4)。
# 写回门槛在 apply_batch（仅 review_status=AUTO_OK 才写回，§17）。如线上发现问题，改回 True 即刻冻结。
BATCH_APPLY_FROZEN = False


@router.get("/search")
def search(
    request: Request,
    q: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    part_type: str | None = Query(None, description="HDD | SSD | RAM"),
    interface: str | None = Query(None, description="SAS | SATA | NVME | FC | SCSI"),
    capacity_min: float | None = Query(None, description="容量下限(GB)"),
    capacity_max: float | None = Query(None, description="容量上限(GB)"),
    category_major: str | None = Query(None, description="一级品类名，如 硬盘/内存/卡/CPU"),
    category_minor: str | None = Query(None, description="二级品类名，如 显卡GPU/SAS-HDD-3.5"),
    browse: bool = Query(False, description="全量浏览分页(主数据管理页)：文字查询也走 search_parts，"
                         "跨PN+描述分词模糊、真实 total、可翻到底；关=智能解析取Top(型号查询)"),
    db: Session = Depends(get_db),
    role: str = Depends(current_role),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    # whitespace-only q 视同空：避免滑进 structured 分支触发 '%   %' 全表 LIKE
    q_norm = (q or "").strip() or None
    has_spec_filter = any(
        x is not None for x in (part_type, interface, capacity_min, capacity_max,
                                category_major, category_minor))
    # browse=True：主数据管理需要"浏览所有匹配、可翻页看全"，走 search_parts(全量分页)；
    # 否则纯文本走 resolver(相似度取 Top 单页，型号查询"这是哪个料号"语义)。见宋总 2026-07-06 反馈。
    branch = "resolver" if (q_norm and not has_spec_filter and not browse) else "structured"
    # 审计带 branch：纯文本走近似解析(可能含 merged 墓碑标签)，结构化走 part_id 浏览
    # (排除墓碑)——同一 URL 两种语义，事后溯源必须能区分
    record_access_log(ctx, "search", "parts", {"q": q_norm, "branch": branch})
    if branch == "resolver":
        # 纯文本查询：近似解析（pg_trgm 召回 + 精排），结果单页带 part_id/score/match_reason。
        # exact=True（统一搜索：查询与 PN/别名完全一致）→ items 只有唯一标准型号，前端据此
        # 直开型号全景；相似候选降级到 similar_items 独立区域，不与精确结果混排。
        data = part_resolver.resolve(db, q_norm, limit=page_size, operated_by=role,
                                     include_similar=True)
        data = {"total": len(data["items"]), "page": 1, "page_size": page_size,
                "exact": data["exact"], "items": data["items"],
                "similar_items": data["similar_items"],
                "low_confidence": data["low_confidence"], "ambiguous": data["ambiguous"]}
    else:
        # 空查询浏览，或带结构化规格过滤：走 part_id 主口径的 search_parts（含 merged 墓碑排除）
        data = part_overview.search_parts(db, q_norm, page, page_size, ctx,
                                          part_type=part_type, interface=interface,
                                          capacity_min=capacity_min, capacity_max=capacity_max,
                                          category_major=category_major, category_minor=category_minor)
    return apply_field_visibility(_business_master_projection(request, db, data), ctx)


@router.get("/categories")
def part_categories(db: Session = Depends(get_db), _: str = Depends(current_role)) -> dict:
    """型号查询「按品类筛选」的下拉数据：实际存在于型号上的（一级品类 → 二级品类列表）。
    任意登录用户可用（搜索页对所有角色开放，与 master/categories 的编辑权门分开）。"""
    from app.models.dimensions import DimPart as _DP
    rows = db.execute(
        select(_DP.category_major, _DP.category_minor)
        .where(_DP.status == "active", _DP.category_major.is_not(None)).distinct()
    ).all()
    tree: dict[str, list[str]] = {}
    for ma, mi in rows:
        tree.setdefault(ma, [])
        if mi and mi not in tree[ma]:
            tree[ma].append(mi)
    return {"categories": [{"major": ma, "minors": sorted(mis)}
                           for ma, mis in sorted(tree.items())]}


@router.get("/overview")
def overview(
    request: Request,
    pn_std: str | None = Query(None, min_length=1),
    part_id: int | None = Query(None, ge=1, description="稳定深链主键（/parts?part_id=）；与 pn_std 二选一，同传时以 part_id 为准"),
    db: Session = Depends(get_db),
    _: str = Depends(current_role),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    if pn_std is None and part_id is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "pn_std 与 part_id 至少传一个")
    record_access_log(ctx, "overview", "part", {"pn_std": pn_std, "part_id": part_id})
    data = part_overview.get_overview(db, pn_std, ctx, part_id=part_id)
    if data is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"型号不存在: {pn_std if part_id is None else f'part_id={part_id}'}")
    return apply_field_visibility(_business_master_projection(request, db, data), ctx)


@router.get("/purchases")
def purchases(
    pn_std: str = Query(..., min_length=1),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    _: str = Depends(current_role),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    return apply_field_visibility(part_overview.list_purchases(db, pn_std, page, page_size, ctx), ctx)


@router.get("/sales")
def sales(
    pn_std: str = Query(..., min_length=1),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    _: str = Depends(current_role),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    return apply_field_visibility(part_overview.list_sales(db, pn_std, page, page_size, ctx), ctx)


class PoolReferencesRequest(BaseModel):
    part_ids: list[int] = Field(min_length=1, max_length=50)
    range: str | None = Field("90d", pattern="^(30d|90d|365d|all|custom)$")
    date_from: date | None = None
    date_to: date | None = None
    purchase_type: str | None = None


@router.get("/{part_id}/pool-reference")
def pool_reference(
    part_id: int,
    range_: str | None = Query("90d", alias="range", pattern="^(30d|90d|365d|all|custom)$"),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    purchase_type: str | None = Query(None, description="仅查看指定采购类型"),
    db: Session = Depends(get_db),
    _role: str = Depends(current_role),
    _page: None = Depends(require_page("page_pool_analysis")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    try:
        data = pool_price_analysis.reference(
            db, part_id, range_=range_, date_from=date_from, date_to=date_to,
            purchase_type=purchase_type)
    except pool_price_analysis.WindowValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    if data is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "型号不存在")
    record_access_log(ctx, "pool_reference", "part", {
        "part_id": part_id, "purchase_type": purchase_type})
    # 池内历史价属于价格纪律事实，不是利润成本；专用净化器只按池治理、供应商、
    # 客户权限处理，不能再套全局 data_purchase_cost 脱敏。
    return pool_price_analysis.apply_visibility(data, ctx)


@router.post("/pool-references")
def pool_references(
    body: PoolReferencesRequest,
    db: Session = Depends(get_db),
    _role: str = Depends(current_role),
    _page: None = Depends(require_page("page_pool_analysis")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    try:
        items = pool_price_analysis.references(
            db, body.part_ids, range_=body.range,
            date_from=body.date_from, date_to=body.date_to,
            purchase_type=body.purchase_type)
    except pool_price_analysis.WindowValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    record_access_log(ctx, "pool_references", "part", {
        "count": len(body.part_ids), "purchase_type": body.purchase_type})
    return {"items": pool_price_analysis.apply_visibility(items, ctx)}


def _verified_catalog_access(request: Request, db: Session) -> CatalogAccess:
    scheme, token = get_authorization_scheme_param(request.headers.get("Authorization"))
    credentials = HTTPAuthorizationCredentials(scheme=scheme, credentials=token) if scheme.lower() == "bearer" and token else None
    return resolve_native_access(db, current_verified_identity(credentials, db))


def _business_master_projection(request: Request, db: Session, data: dict) -> dict:
    """Business facts retain their own access rules; live master fields use current grants."""
    state = runtime_states(db)["part_master"].runtime_state
    if state == "legacy":
        return data
    request.state.pn_catalog_shared = True
    request.state.pn_catalog_request_id = uuid4()
    readable = frozenset()
    availability = "unavailable" if state == "suspended" else "restricted"
    if state == "enabled":
        try:
            access = _verified_catalog_access(request, db)
            decision = access.authorize(db, CommandContext(actor=access.actor,
                request_id=request.state.pn_catalog_request_id), "read", None)
            readable = decision.readable_fields
            availability = "available" if readable else "restricted"
        except (CatalogError, HTTPException) as exc:
            # A missing master grant or unavailable PDP must not expose fallback
            # master values, nor remove unrelated, already-authorized order facts.
            if exc.status_code not in (403, 503):
                raise
            availability = "unavailable" if exc.status_code == 503 else "restricted"
    return project_business_master(data, readable, runtime_state=state, availability=availability)


# ---------------- 备件主数据自治（WP1，采购可新建/编辑） ----------------

def _master_access(
    request: Request,
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
) -> CatalogAccess | None:
    """Once cut over, neither the old page grant nor role can reopen a suspended module."""
    if not is_authoritative(db, "part_master"):
        require_page("page_master_data")(ctx)
        return None
    request.state.pn_catalog_shared = True
    request.state.pn_catalog_request_id = uuid4()
    for name in ("Idempotency-Key", "X-Request-ID", "If-Match"):
        if len(request.headers.getlist(name)) > 1:
            raise CatalogError("request.validation_failed", "请求前置条件不能重复", status_code=422)
    identifiers = {}
    for name in ("Idempotency-Key", "X-Request-ID"):
        value = request.headers.get(name)
        if value is not None:
            try:
                identifiers[name] = UUID(value)
            except ValueError:
                raise CatalogError("request.validation_failed", f"{name} 必须是 UUID", status_code=422) from None
    if len(set(identifiers.values())) > 1:
        raise CatalogError("request.validation_failed", "请求编号与幂等编号不一致", status_code=422)
    if identifiers:
        request.state.pn_catalog_request_id = next(iter(identifiers.values()))
    return _verified_catalog_access(request, db)


def _shared_state(value: dict) -> dict:
    """Project the current facade snapshot into the old editor's baseline contract."""
    return {
        "version_mode": "field_snapshot", "part_id": value["part_id"], "pn_std": value["pn_std"],
        "identity": {"part_id": value["part_id"], **value["identity"]},
        "identity_revision": value["identity_revision"], "profile_revision": value["profile_revision"],
        "field_versions": value["field_versions"], "fields": value["profile"],
        "deleted_specs": [key for key, spec in value["specs"].items() if spec["deleted"]],
        "specs": {key: {k: spec[k] for k in ("value", "unit", "numeric_value", "source")}
                  for key, spec in value["specs"].items() if not spec["deleted"]},
    }


def _retired_master_entry() -> None:
    raise CatalogError("catalog.legacy_writer_retired", "此入口已迁移，请使用共享 PN 维护页面", status_code=410,
                       details={"replacement": "/api/pn-catalog/v1"})


def _catalog_command(model, **values):
    try:
        return model(**values)
    except ValidationError as exc:
        raise CatalogError("request.validation_failed", "提交内容格式不正确", status_code=422,
                           details={"fields": [{"path": "/" + "/".join(map(str, error["loc"])), "type": error["type"]}
                                               for error in exc.errors()]}) from exc

class PartCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pn_std: str
    description: str | None = None
    brand: str | None = None
    category_major: str | None = None
    category_minor: str | None = None
    machine_or_part: str | None = "备件"
    unit: str | None = None
    force: bool = False   # 确认无重复后强制新建（跳过近似提示）
    confirmation_token: str | None = Field(default=None, max_length=4096)
    confirmation_reason: str | None = Field(default=None, max_length=2000)


class PartEditIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    part_id: int = Field(gt=0)
    pn_std: str
    status: Literal["active", "merged"]
    merged_into_id: int | None


class PartSpecBaseline(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str
    unit: str | None
    numeric_value: str | None
    source: str


class PartEditBaseline(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version_mode: Literal["field_snapshot"]
    identity_revision: int | None = Field(default=None, ge=0)
    profile_revision: int | None = Field(default=None, ge=0)
    field_versions: dict[str, int] | None = None
    deleted_specs: list[str] = Field(default_factory=list)
    part_id: int = Field(gt=0)
    pn_std: str
    identity: PartEditIdentity
    fields: dict[str, str | None]
    specs: dict[str, PartSpecBaseline]


class PartEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    part_id: int = Field(gt=0)
    pn_std: str           # 定位被编辑的型号
    base_snapshot: PartEditBaseline
    description: str | None = None
    brand: str | None = None
    category_major: str | None = None
    category_minor: str | None = None
    machine_or_part: str | None = None
    unit: str | None = None
    spec_updates: dict | None = None


def _real_operator(db: Session, ident: dict) -> tuple[str, str]:
    """人工主数据库写入拒绝共享回退/旧凭证和不存在的实名账号。"""
    username = str(ident.get("sub") or "").strip()
    if ident.get("authn") != "sys_user" or ident.get("fb") or not username:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "主数据库编辑必须使用实名系统账号，请重新登录")
    user = db.scalar(select(SysUser).where(SysUser.username == username, SysUser.is_active.is_(True)))
    if user is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "主数据库编辑必须使用实名系统账号，请重新登录")
    return username, f"account:{user.id}"


@router.get("/master/check")
def master_check_duplicates(
    request: Request,
    pn_std: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),   # 硬鉴权：缺/失效凭证 → 401，不依赖全局 RBAC 开关
    ctx: UserContext = Depends(get_current_user_context),
    access: CatalogAccess | None = Depends(_master_access),
) -> dict:
    """新建前查近似重复（前端实时提示）。"""
    rows = master_edit.find_near_duplicates(db, pn_std) if access is None else None
    if access is not None:
        decision = access.authorize(db, _context(request, access), "read", None)
        rows = master_edit.find_near_duplicates(db, pn_std)
        if "*" not in decision.readable_fields and "description" not in decision.readable_fields:
            rows = [{k: v for k, v in row.items() if k != "description"} for row in rows]
    return {"near_duplicates": rows}


@router.post("/master")
def master_create(
    body: PartCreate,
    request: Request,
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),   # 硬鉴权：缺/失效凭证 → 401，不依赖全局 RBAC 开关
    ctx: UserContext = Depends(get_current_user_context),
    access: CatalogAccess | None = Depends(_master_access),
    ident: dict = Depends(current_identity),
) -> dict:
    """采购手工新建型号。near_duplicates 非空且未 force → 不建、返回候选待确认。"""
    if access is not None:
        context = _context(request, access, write=True)
        if body.force and not body.confirmation_token:
            raise CatalogError("request.precondition_required", "独立 PN 确认需要服务端签名，请从共享维护页面重新核对", status_code=428)
        command = _catalog_command(CreatePart, pn_std=body.pn_std,
            changes={**body.model_dump(exclude={"pn_std", "force", "confirmation_token", "confirmation_reason"}, exclude_unset=True),
                     "machine_or_part": body.machine_or_part},
            confirmation_token=body.confirmation_token, confirmation_reason=body.confirmation_reason)
        result = _commit(db, lambda: catalog.create_part(db, command, context=context,
                          authorize=access.authorize, confirmation_secret=access.confirmation_secret))
        value = result["data"]
        return {"id": value["part_id"], "pn_std": value["pn_std"], "created": True,
                "near_duplicates": [], "locked_fields": value["locked_fields"], "request_id": str(context.request_id)}
    operated_by, principal_id = _real_operator(db, ident)
    try:
        res = master_edit.create_part(
            db, pn_std=body.pn_std, description=body.description, brand=body.brand,
            category_major=body.category_major, category_minor=body.category_minor,
            machine_or_part=body.machine_or_part, unit=body.unit,
            force=body.force, operated_by=operated_by, principal_id=principal_id)
    except master_edit.MasterEditConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, exc.detail) from exc
    except master_edit.MasterEditError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return res


@router.patch("/master")
def master_edit_part(
    body: PartEdit,
    request: Request,
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),   # 硬鉴权：缺/失效凭证 → 401，不依赖全局 RBAC 开关
    ctx: UserContext = Depends(get_current_user_context),
    access: CatalogAccess | None = Depends(_master_access),
    ident: dict = Depends(current_identity),
) -> dict:
    """编辑任意型号的人工字段（描述/品类/品牌/单位/类型）；改过的字段重导不覆盖。"""
    if access is not None:
        context = _context(request, access, write=True)
        baseline = body.base_snapshot
        if baseline.part_id != body.part_id or baseline.pn_std != body.pn_std or baseline.identity.part_id != body.part_id:
            raise CatalogError("catalog.identity_changed", "提交目标与编辑基线不一致")
        if baseline.identity_revision is None or baseline.field_versions is None:
            raise CatalogError("request.precondition_required", "需要共享资料的字段版本基线，请重新打开编辑器", status_code=428)
        updates = body.model_dump(exclude={"pn_std", "part_id", "base_snapshot", "spec_updates"}, exclude_unset=True)
        command = _catalog_command(PatchProfile, expected_identity_revision=baseline.identity_revision,
            field_versions=baseline.field_versions, changes=updates, spec_updates=body.spec_updates or {})
        result = _commit(db, lambda: catalog.patch_profile(db, body.part_id, command, context=context, authorize=access.authorize))
        value = result["data"]
        return {"id": value["part_id"], "pn_std": value["pn_std"], "updated": [*updates, *(f"specs.{key}" for key in command.spec_updates)],
                "locked_fields": value["locked_fields"], "request_id": str(context.request_id)}
    operated_by, principal_id = _real_operator(db, ident)
    updates = body.model_dump(exclude={"pn_std", "part_id", "base_snapshot", "spec_updates"},
                              exclude_unset=True)
    try:
        res = master_edit.edit_part(db, pn_std=body.pn_std, part_id=body.part_id,
                                    base_snapshot=body.base_snapshot.model_dump(), updates=updates,
                                    spec_updates=body.spec_updates,
                                    operated_by=operated_by, principal_id=principal_id)
    except master_edit.MasterEditConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, exc.detail) from exc
    except master_edit.MasterEditError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if res is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"型号不存在: {body.pn_std}")
    return res


@router.get("/master/{part_id}/edit-state")
def master_edit_state(
    part_id: int,
    request: Request,
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    access: CatalogAccess | None = Depends(_master_access),
) -> dict:
    if access is not None:
        return _shared_state(catalog.get_part(db, part_id, context=_context(request, access), authorize=access.authorize)["data"])
    result = master_edit.get_part_edit_state(db, part_id)
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "型号不存在")
    return result


@router.get("/master/{part_id}/edit-history")
def master_edit_history(
    part_id: str,
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    before_id: int | None = Query(None, gt=0),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    access: CatalogAccess | None = Depends(_master_access),
) -> dict:
    if access is not None:
        access.authorize(db, _context(request, access), "read_audit", None)
        _retired_master_entry()
    # 历史是型号的子资源：非法/不存在的身份不能冒充「尚无人工历史」。
    try:
        resolved_id = int(part_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "型号不存在") from exc
    if not 1 <= resolved_id <= PG_INT_MAX or db.scalar(
        select(DimPart.id).where(DimPart.id == resolved_id)
    ) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "型号不存在")
    return master_edit.list_part_edit_history(
        db, resolved_id, limit=limit, before_id=before_id,
    )


@router.get("/master/categories")
def master_categories(
    request: Request,
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    access: CatalogAccess | None = Depends(_master_access),
) -> dict:
    """品类字典（两级树）供编辑页下拉。轻量 C：字典作为代码常量，采购可维护表是 WP3。"""
    if access is not None:
        access.authorize(db, _context(request, access), "read", None)
    tree = [
        {"code": code, "name": name,
         "children": [{"code": c, "name": n} for c, n in taxonomy.CATEGORY_NAMES.items()
                      if len(c) == 4 and c.startswith(code)]}
        for code, name in taxonomy.CATEGORY_NAMES.items() if len(code) == 2
    ]
    return {"categories": tree, "battery_subtypes": taxonomy.BATTERY_SUBTYPES,
            "cooling_types": taxonomy.COOLING_TYPES}


@router.get("/master/suggest")
def master_suggest(
    request: Request,
    description: str = Query(""),
    pn: str = Query(""),
    brand: str = Query(""),
    _auth: str = Depends(current_role),
    db: Session = Depends(get_db),
    access: CatalogAccess | None = Depends(_master_access),
) -> dict:
    """据描述给确定性标准化建议：先识别类型 → 按字段证据渲染标准描述 + 一级/二级分类 +
    品牌归一 + 每字段证据(source/evidence) + 校验 + 审核状态。无可靠证据的字段不猜，
    缺关键字段时 canonical_description=null、分类为 null、review_status=REVIEW_REQUIRED，交人工。"""
    if access is not None:
        access.authorize(db, _context(request, access), "normalize", None)
    s = standardize.standardize(pn, description, brand)
    return {"suggestion": {k: v for k, v in s.items() if not k.startswith("_")}}


class BatchApply(BaseModel):
    part_ids: list[int]
    fields: list[str] | None = None   # description / category / brand，缺省全应用


@router.get("/master/batch-preview")
def master_batch_preview(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    only_changes: bool = Query(True),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    access: CatalogAccess | None = Depends(_master_access),
) -> dict:
    """批量规范化预览：按近期销售额降序列出备件 + 标准化建议（高价值先清）。"""
    if access is not None:
        access.authorize(db, _context(request, access), "normalize", None)
        _retired_master_entry()
    return batch_normalize.preview(db, page, page_size, only_changes)


@router.post("/master/batch-apply")
def master_batch_apply(
    body: BatchApply,
    request: Request,
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
    _auth: str = Depends(current_role),
    access: CatalogAccess | None = Depends(_master_access),
) -> dict:
    """批量应用标准化（服务端重算，锁定字段不动，应用后字段锁定防重导覆盖）。"""
    if access is not None:
        access.authorize(db, _context(request, access, write=True), "normalize", None)
        _retired_master_entry()
    if BATCH_APPLY_FROZEN:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "批量规范化「应用」已暂停：描述标准化引擎正在升级为确定性系统，避免批量写入低质量结果。"
            "升级完成后开放。期间可单条手工编辑。",
        )
    return batch_normalize.apply_batch(db, body.part_ids, body.fields, (ctx.user_id or ctx.role))

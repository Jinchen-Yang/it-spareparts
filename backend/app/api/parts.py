"""型号查询与主数据 transport。独立数据块和领域写由 part_access 保护；池引用调用 pool_access。

仅资源尚未切换时，protected service 的 legacy adapter 保留旧权限语义。
"""
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.authz import runtime
from app.authz.principal import VerifiedIdentity, current_verified_identity
from app.services import pool_access, part_access
from app.security import UserContext, get_current_user_context, record_access_log, require_page
from app.services import master_edit, pool_price_analysis

router = APIRouter(prefix="/parts", tags=["parts"])

# 批量应用 kill-switch：确定性 standardize 引擎已接入并上线(v1.2.3)，已解冻(v1.2.4)。
# 写回门槛在 apply_batch（仅 review_status=AUTO_OK 才写回，§17）。如线上发现问题，改回 True 即刻冻结。
BATCH_APPLY_FROZEN = False


@router.get("/search")
def search(
    q: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    part_type: str | None = Query(None, description='HDD | SSD | RAM'),
    interface: str | None = Query(None, description='SAS | SATA | NVME | FC | SCSI'),
    capacity_min: float | None = Query(None, description='容量下限(GB)'),
    capacity_max: float | None = Query(None, description='容量上限(GB)'),
    category_major: str | None = Query(None, description='一级品类名，如 硬盘/内存/卡/CPU'),
    category_minor: str | None = Query(None, description='二级品类名，如 显卡GPU/SAS-HDD-3.5'),
    browse: bool = Query(False, description='全量浏览分页(主数据管理页)：文字查询也走 search_parts，跨PN+描述分词模糊、真实 total、可翻到底；关=智能解析取Top(型号查询)'),
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
    identity: VerifiedIdentity = Depends(current_verified_identity),
) -> dict:
    return part_access.read_part_search(db, identity, legacy_context=ctx, q=q, page=page, page_size=page_size,
        part_type=part_type, interface=interface, capacity_min=capacity_min, capacity_max=capacity_max,
        category_major=category_major, category_minor=category_minor, browse=browse)


@router.get("/categories")
def part_categories(
    db: Session = Depends(get_db),
    identity: VerifiedIdentity = Depends(current_verified_identity),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    return part_access.read_part_categories(db, identity, legacy_context=ctx)


@router.get("/overview")
def overview(
    pn_std: str | None = Query(None, min_length=1),
    part_id: int | None = Query(None, ge=1, description='稳定深链主键（/parts?part_id=）；与 pn_std 二选一，同传时以 part_id 为准'),
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
    identity: VerifiedIdentity = Depends(current_verified_identity),
) -> dict:
    if pn_std is None and part_id is None:
        raise HTTPException(422, "pn_std 与 part_id 至少传一个")
    record_access_log(ctx, "overview", "part", {"pn_std": pn_std, "part_id": part_id})
    data = part_access.read_part_overview(db, identity, legacy_context=ctx, pn_std=pn_std, part_id=part_id)
    if data is None:
        raise HTTPException(404, f"型号不存在: {pn_std if part_id is None else f'part_id={part_id}'}")
    return data


@router.get("/purchases")
def purchases(
    pn_std: str = Query(..., min_length=1),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
    identity: VerifiedIdentity = Depends(current_verified_identity),
) -> dict:
    return part_access.read_part_purchases(db, identity, legacy_context=ctx, pn_std=pn_std, page=page, page_size=page_size)


@router.get("/sales")
def sales(
    pn_std: str = Query(..., min_length=1),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
    identity: VerifiedIdentity = Depends(current_verified_identity),
) -> dict:
    return part_access.read_part_sales(db, identity, legacy_context=ctx, pn_std=pn_std, page=page, page_size=page_size)


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
    identity: VerifiedIdentity = Depends(current_verified_identity),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    try:
        data = pool_access.read_pool_reference(
            db, identity, part_id, legacy_context=ctx, range_=range_, date_from=date_from, date_to=date_to,
            purchase_type=purchase_type)
    except pool_price_analysis.WindowValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    if data is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "型号不存在")
    record_access_log(ctx, "pool_reference", "part", {
        "part_id": part_id, "purchase_type": purchase_type})
    # 池内历史价属于价格纪律事实，不是利润成本；专用净化器只按池治理、供应商、
    # 客户权限处理，不能再套全局 data_purchase_cost 脱敏。
    return data


@router.post("/pool-references")
def pool_references(
    body: PoolReferencesRequest,
    db: Session = Depends(get_db),
    identity: VerifiedIdentity = Depends(current_verified_identity),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    try:
        items = pool_access.read_pool_references(
            db, identity, body.part_ids, legacy_context=ctx, range_=body.range,
            date_from=body.date_from, date_to=body.date_to,
            purchase_type=body.purchase_type)
    except pool_price_analysis.WindowValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    record_access_log(ctx, "pool_references", "part", {
        "count": len(body.part_ids), "purchase_type": body.purchase_type})
    return {"items": items}


# ---------------- 备件主数据自治（WP1，采购可新建/编辑） ----------------

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


def _legacy_master_gate(db: Session = Depends(get_db),
                        identity: VerifiedIdentity = Depends(current_verified_identity),
                        ctx: UserContext = Depends(get_current_user_context)):
    if not runtime.is_authoritative(db, 'part_master'):
        require_page('page_master_data')(ctx)


@router.get("/master/check", dependencies=[Depends(_legacy_master_gate)])
def master_check_duplicates(
    pn_std: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
    identity: VerifiedIdentity = Depends(current_verified_identity),
) -> dict:
    return part_access.check_master_duplicates(db, identity, legacy_context=ctx, pn_std=pn_std)


@router.post("/master", dependencies=[Depends(_legacy_master_gate)])
def master_create(
    body: PartCreate,
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
    identity: VerifiedIdentity = Depends(current_verified_identity),
) -> dict:
    """采购手工新建型号。near_duplicates 非空且未 force → 不建、返回候选待确认。"""
    try:
        res = part_access.create_master_part(
            db, identity, legacy_context=ctx, pn_std=body.pn_std, description=body.description, brand=body.brand,
            category_major=body.category_major, category_minor=body.category_minor,
            machine_or_part=body.machine_or_part, unit=body.unit,
            force=body.force)
    except master_edit.MasterEditConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, exc.detail) from exc
    except master_edit.MasterEditError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return res


@router.patch("/master", dependencies=[Depends(_legacy_master_gate)])
def master_edit_part(
    body: PartEdit,
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
    identity: VerifiedIdentity = Depends(current_verified_identity),
) -> dict:
    """编辑任意型号的人工字段（描述/品类/品牌/单位/类型）；改过的字段重导不覆盖。"""
    updates = body.model_dump(exclude={"pn_std", "part_id", "base_snapshot", "spec_updates"},
                              exclude_unset=True)
    try:
        res = part_access.edit_master_part(db, identity, legacy_context=ctx, pn_std=body.pn_std, part_id=body.part_id,
                                    base_snapshot=body.base_snapshot.model_dump(), updates=updates,
                                    spec_updates=body.spec_updates)
    except master_edit.MasterEditConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, exc.detail) from exc
    except master_edit.MasterEditError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if res is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"型号不存在: {body.pn_std}")
    return res


@router.get("/master/{part_id}/edit-state", dependencies=[Depends(_legacy_master_gate)])
def master_edit_state(
    part_id: int,
    db: Session = Depends(get_db),
    identity: VerifiedIdentity = Depends(current_verified_identity),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    result = part_access.read_master_edit_state(db, identity, part_id, legacy_context=ctx)
    if result is None:
        raise HTTPException(404, "型号不存在")
    return result


@router.get("/master/{part_id}/edit-history", dependencies=[Depends(_legacy_master_gate)])
def master_edit_history(
    part_id: str,
    limit: int = Query(20, ge=1, le=100),
    before_id: int | None = Query(None, gt=0),
    db: Session = Depends(get_db),
    identity: VerifiedIdentity = Depends(current_verified_identity),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    try:
        resolved_id = int(part_id)
    except ValueError as exc:
        raise HTTPException(404, "型号不存在") from exc
    result = part_access.read_master_edit_history(db, identity, resolved_id, legacy_context=ctx, limit=limit, before_id=before_id)
    if result is None:
        raise HTTPException(404, "型号不存在")
    return result


@router.get("/master/categories", dependencies=[Depends(_legacy_master_gate)])
def master_categories(
    db: Session = Depends(get_db),
    identity: VerifiedIdentity = Depends(current_verified_identity),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    return part_access.read_master_categories(db, identity, legacy_context=ctx)


@router.get("/master/suggest", dependencies=[Depends(_legacy_master_gate)])
def master_suggest(
    description: str = Query(''),
    pn: str = Query(''),
    brand: str = Query(''),
    db: Session = Depends(get_db),
    identity: VerifiedIdentity = Depends(current_verified_identity),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    return part_access.read_master_suggestion(db, identity, legacy_context=ctx, description=description, pn=pn, brand=brand)


class BatchApply(BaseModel):
    part_ids: list[int]
    fields: list[str] | None = None   # description / category / brand，缺省全应用


@router.get("/master/batch-preview", dependencies=[Depends(_legacy_master_gate)])
def master_batch_preview(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    only_changes: bool = Query(True),
    db: Session = Depends(get_db),
    identity: VerifiedIdentity = Depends(current_verified_identity),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    return part_access.preview_master_normalization(db, identity, legacy_context=ctx, page=page, page_size=page_size, only_changes=only_changes)


@router.post("/master/batch-apply", dependencies=[Depends(_legacy_master_gate)])
def master_batch_apply(
    body: BatchApply,
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
    identity: VerifiedIdentity = Depends(current_verified_identity),
) -> dict:
    """批量应用标准化（服务端重算，锁定字段不动，应用后字段锁定防重导覆盖）。"""
    if BATCH_APPLY_FROZEN:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "批量规范化「应用」已暂停：描述标准化引擎正在升级为确定性系统，避免批量写入低质量结果。"
            "升级完成后开放。期间可单条手工编辑。",
        )
    return part_access.apply_master_normalization(db, identity, legacy_context=ctx, part_ids=body.part_ids, fields=body.fields)


@router.get("/master/search", dependencies=[Depends(_legacy_master_gate)])
def master_search(
    q: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    part_type: str | None = Query(None, description='HDD | SSD | RAM'),
    interface: str | None = Query(None, description='SAS | SATA | NVME | FC | SCSI'),
    capacity_min: float | None = Query(None, description='容量下限(GB)'),
    capacity_max: float | None = Query(None, description='容量上限(GB)'),
    category_major: str | None = Query(None, description='一级品类名，如 硬盘/内存/卡/CPU'),
    category_minor: str | None = Query(None, description='二级品类名，如 显卡GPU/SAS-HDD-3.5'),
    browse: bool = Query(False, description='全量浏览分页(主数据管理页)：文字查询也走 search_parts，跨PN+描述分词模糊、真实 total、可翻到底；关=智能解析取Top(型号查询)'),
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
    identity: VerifiedIdentity = Depends(current_verified_identity),
) -> dict:
    return part_access.read_master_search(db, identity, legacy_context=ctx, q=q, page=page, page_size=page_size,
        part_type=part_type, interface=interface, capacity_min=capacity_min, capacity_max=capacity_max,
        category_major=category_major, category_minor=category_minor, browse=browse)

"""型号查询 API（§9）。用 query 传 pn_std，避开 PN 中的 / # 路由问题。需登录。

/parts/master*（WP1）：采购可新建/编辑备件主数据，require_page('page_master_data') 准入。
"""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import current_role
from app.db import get_db
from app.security import (
    UserContext, apply_field_visibility, get_current_user_context, record_access_log, require_page,
)
from app.services import batch_normalize, master_edit, part_overview, part_resolver, standardize, taxonomy

router = APIRouter(prefix="/parts", tags=["parts"])

# 批量应用 kill-switch：确定性 standardize 引擎已接入并上线(v1.2.3)，已解冻(v1.2.4)。
# 写回门槛在 apply_batch（仅 review_status=AUTO_OK 才写回，§17）。如线上发现问题，改回 True 即刻冻结。
BATCH_APPLY_FROZEN = False


@router.get("/search")
def search(
    q: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    part_type: str | None = Query(None, description="HDD | SSD | RAM"),
    interface: str | None = Query(None, description="SAS | SATA | NVME | FC | SCSI"),
    capacity_min: float | None = Query(None, description="容量下限(GB)"),
    capacity_max: float | None = Query(None, description="容量上限(GB)"),
    category_major: str | None = Query(None, description="一级品类名，如 硬盘/内存/卡/CPU"),
    category_minor: str | None = Query(None, description="二级品类名，如 显卡GPU/SAS-HDD-3.5"),
    db: Session = Depends(get_db),
    role: str = Depends(current_role),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    # whitespace-only q 视同空：避免滑进 structured 分支触发 '%   %' 全表 LIKE
    q_norm = (q or "").strip() or None
    has_spec_filter = any(
        x is not None for x in (part_type, interface, capacity_min, capacity_max,
                                category_major, category_minor))
    branch = "resolver" if (q_norm and not has_spec_filter) else "structured"
    # 审计带 branch：纯文本走近似解析(可能含 merged 墓碑标签)，结构化走 part_id 浏览
    # (排除墓碑)——同一 URL 两种语义，事后溯源必须能区分
    record_access_log(ctx, "search", "parts", {"q": q_norm, "branch": branch})
    if branch == "resolver":
        # 纯文本查询：近似解析（pg_trgm 召回 + 精排），结果单页带 score/match_reason
        data = part_resolver.resolve(db, q_norm, limit=page_size, operated_by=role)
        data = {"total": len(data["items"]), "page": 1, "page_size": page_size,
                "items": data["items"], "low_confidence": data["low_confidence"],
                "ambiguous": data["ambiguous"]}
    else:
        # 空查询浏览，或带结构化规格过滤：走 part_id 主口径的 search_parts（含 merged 墓碑排除）
        data = part_overview.search_parts(db, q_norm, page, page_size, ctx,
                                          part_type=part_type, interface=interface,
                                          capacity_min=capacity_min, capacity_max=capacity_max,
                                          category_major=category_major, category_minor=category_minor)
    return apply_field_visibility(data, ctx)


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
    pn_std: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
    _: str = Depends(current_role),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    record_access_log(ctx, "overview", "part", {"pn_std": pn_std})
    data = part_overview.get_overview(db, pn_std, ctx)
    if data is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"型号不存在: {pn_std}")
    return apply_field_visibility(data, ctx)


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


# ---------------- 备件主数据自治（WP1，采购可新建/编辑） ----------------

class PartCreate(BaseModel):
    pn_std: str
    description: str | None = None
    brand: str | None = None
    category_major: str | None = None
    category_minor: str | None = None
    machine_or_part: str | None = "备件"
    unit: str | None = None
    force: bool = False   # 确认无重复后强制新建（跳过近似提示）


class PartEdit(BaseModel):
    pn_std: str           # 定位被编辑的型号
    description: str | None = None
    brand: str | None = None
    category_major: str | None = None
    category_minor: str | None = None
    machine_or_part: str | None = None
    unit: str | None = None


@router.get("/master/check")
def master_check_duplicates(
    pn_std: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),   # 硬鉴权：缺/失效凭证 → 401，不依赖全局 RBAC 开关
    ctx: UserContext = Depends(get_current_user_context),
    _: None = Depends(require_page("page_master_data")),
) -> dict:
    """新建前查近似重复（前端实时提示）。"""
    return {"near_duplicates": master_edit.find_near_duplicates(db, pn_std)}


@router.post("/master")
def master_create(
    body: PartCreate,
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),   # 硬鉴权：缺/失效凭证 → 401，不依赖全局 RBAC 开关
    ctx: UserContext = Depends(get_current_user_context),
    _: None = Depends(require_page("page_master_data")),
) -> dict:
    """采购手工新建型号。near_duplicates 非空且未 force → 不建、返回候选待确认。"""
    try:
        res = master_edit.create_part(
            db, pn_std=body.pn_std, description=body.description, brand=body.brand,
            category_major=body.category_major, category_minor=body.category_minor,
            machine_or_part=body.machine_or_part, unit=body.unit,
            force=body.force, operated_by=(ctx.user_id or ctx.role))
    except master_edit.MasterEditError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return res


@router.patch("/master")
def master_edit_part(
    body: PartEdit,
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),   # 硬鉴权：缺/失效凭证 → 401，不依赖全局 RBAC 开关
    ctx: UserContext = Depends(get_current_user_context),
    _: None = Depends(require_page("page_master_data")),
) -> dict:
    """编辑任意型号的人工字段（描述/品类/品牌/单位/类型）；改过的字段重导不覆盖。"""
    updates = body.model_dump(exclude={"pn_std"}, exclude_unset=True)
    try:
        res = master_edit.edit_part(db, pn_std=body.pn_std, updates=updates,
                                    operated_by=(ctx.user_id or ctx.role))
    except master_edit.MasterEditError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if res is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"型号不存在: {body.pn_std}")
    return res


@router.get("/master/categories")
def master_categories(
    _auth: str = Depends(current_role),
    _: None = Depends(require_page("page_master_data")),
) -> dict:
    """品类字典（两级树）供编辑页下拉。轻量 C：字典作为代码常量，采购可维护表是 WP3。"""
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
    description: str = Query(""),
    pn: str = Query(""),
    brand: str = Query(""),
    _auth: str = Depends(current_role),
    _: None = Depends(require_page("page_master_data")),
) -> dict:
    """据描述给确定性标准化建议：先识别类型 → 按字段证据渲染标准描述 + 一级/二级分类 +
    品牌归一 + 每字段证据(source/evidence) + 校验 + 审核状态。无可靠证据的字段不猜，
    缺关键字段时 canonical_description=null、分类为 null、review_status=REVIEW_REQUIRED，交人工。"""
    s = standardize.standardize(pn, description, brand)
    return {"suggestion": {k: v for k, v in s.items() if not k.startswith("_")}}


class BatchApply(BaseModel):
    part_ids: list[int]
    fields: list[str] | None = None   # description / category / brand，缺省全应用


@router.get("/master/batch-preview")
def master_batch_preview(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    only_changes: bool = Query(True),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _: None = Depends(require_page("page_master_data")),
) -> dict:
    """批量规范化预览：按近期销售额降序列出备件 + 标准化建议（高价值先清）。"""
    return batch_normalize.preview(db, page, page_size, only_changes)


@router.post("/master/batch-apply")
def master_batch_apply(
    body: BatchApply,
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
    _auth: str = Depends(current_role),
    _: None = Depends(require_page("page_master_data")),
) -> dict:
    """批量应用标准化（服务端重算，锁定字段不动，应用后字段锁定防重导覆盖）。"""
    if BATCH_APPLY_FROZEN:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "批量规范化「应用」已暂停：描述标准化引擎正在升级为确定性系统，避免批量写入低质量结果。"
            "升级完成后开放。期间可单条手工编辑。",
        )
    return batch_normalize.apply_batch(db, body.part_ids, body.fields, (ctx.user_id or ctx.role))

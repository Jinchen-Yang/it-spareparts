"""互通 PN 池独立接口（/api/pools*，互通PN池价格分析 §17）——脱离老板看板权限。

权限口径（§12）：
- 读（清单/档案）：全员可读，但必须登录（require_login）；约束价字段随
  data_pool_price_governance 过 apply_field_visibility 脱敏。
- 池维护（建池/改名/成员/归档/恢复）：action_pool_manage（默认老板/管理员，可单独授权）。
- 约束价设置：action_pool_set_policy（默认老板/管理员）。
所有写操作携带 version（乐观锁）：他人先保存 → 409，前端提示重新加载，不静默覆盖。
保存即生效，无提交/审核/批准环节（§11）。
"""
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.security import (
    UserContext,
    apply_field_visibility,
    get_current_user_context,
    is_field_hidden,
    record_access_log,
    require_action,
    require_login,
)
from app.services import pool_catalog as svc

router = APIRouter(prefix="/pools", tags=["pools"])

_BASIS_PATTERN = "^(ex_tax|inc_tax)$"


class PoolCreate(BaseModel):
    name: str
    description: str | None = None
    member_part_ids: list[int] = Field(default_factory=list)
    note: str | None = None


class PoolPatch(BaseModel):
    version: int
    name: str | None = None
    description: str | None = None
    note: str | None = None


class PoolMembersPatch(BaseModel):
    version: int
    add_part_ids: list[int] = Field(default_factory=list)
    remove_part_ids: list[int] = Field(default_factory=list)
    note: str | None = None


class PoolPolicyPut(BaseModel):
    """约束价单侧更新语义（复审阻塞 4）：每侧独立三态。

    - 给了 *_value → set 该侧；
    - *_unset=True → 显式清空该侧（此时不得同时给值）；
    - 都没有 → keep，该侧保持原值。
    普通 null 永远不是"清空"——被脱敏成 null 的一侧原样保留。
    """
    version: int
    purchase_value: Decimal | None = None
    purchase_basis: str = Field("ex_tax", pattern=_BASIS_PATTERN)
    purchase_unset: bool = False
    sales_value: Decimal | None = None
    sales_basis: str = Field("ex_tax", pattern=_BASIS_PATTERN)
    sales_unset: bool = False
    note: str | None = None


class PoolLifecycle(BaseModel):
    version: int
    note: str | None = None


def _operated_by(ctx: UserContext) -> str | None:
    # 真实用户名优先（审计勿记角色串——S-2 教训，见 services/master_edit.py）
    return ctx.user_id or ctx.role


def _price_restricted(ctx: UserContext) -> bool:
    """约束价对该用户是否被权限隐藏（data_pool_price_governance=False）。

    作为明确旗标随清单/详情返回（复审非阻塞 1）："未设置"（null + 不受限）与
    "无权限"（受限）是两种状态，前端不允许都渲染成 "--" 让人猜。
    旗标只表达"看不看得见"，不携带任何金额信息，不属于脱敏字段组。
    """
    return is_field_hidden(ctx, "purchase_ceiling_ex_tax")


def _hide_price_policy_structure(data: dict, *, restricted: bool) -> None:
    """价格治理受限时收敛详情结构，避免元数据侧漏。

    通用字段脱敏只能把金额叶子置空；策略备注可能直接写出价格，changed_by、
    录入口径和生效区间也属于价格策略信息。这里在 /api/pools 详情边界整体移除
    当前策略与历史，同时保留稳定、前端可判定的响应形状。
    """
    if restricted:
        data["price_policy"] = None
        data["price_policy_history"] = []


def _run(fn, **kwargs):
    """service 领域异常 → HTTP 语义：业务非法 400、并发/唯一性冲突 409、不存在 404。"""
    try:
        res = fn(**kwargs)
    except svc.PoolCatalogError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except svc.PoolConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    if res is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "池不存在")
    return res


@router.get("")
def list_pools(
    q: str | None = Query(None, description="搜索：池名/描述/成员PN/品牌"),
    status_: str = Query("active", alias="status", pattern="^(active|archived|all)$"),
    policy_missing: str | None = Query(
        None,
        pattern="^(purchase|sales|either|both)$",
        description="筛选有效池中未设置采购上限/销售下限/任一侧/两侧约束的池",
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(require_login),
) -> dict:
    restricted = _price_restricted(ctx)
    if restricted and policy_missing is not None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "无池约束价查看权限，不能按约束价缺失状态筛选",
        )
    record_access_log(
        ctx,
        "pool_catalog_list",
        "pools",
        {"q": q, "status": status_, "policy_missing": policy_missing},
    )
    data = svc.list_pools(
        db,
        q=q,
        status=status_,
        policy_missing=policy_missing,
        page=page,
        page_size=page_size,
    )
    data["coverage_restricted"] = restricted
    if restricted:
        data["coverage"] = None
    data["price_restricted"] = restricted
    for item in data["items"]:
        item["price_restricted"] = restricted
    return apply_field_visibility(data, ctx)


@router.get("/{group_id}")
def get_pool(
    group_id: int,
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(require_login),
) -> dict:
    record_access_log(ctx, "pool_catalog_detail", "pools", {"group_id": group_id})
    data = svc.get_pool(db, group_id)
    if data is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "池不存在")
    restricted = _price_restricted(ctx)
    data["price_restricted"] = restricted
    _hide_price_policy_structure(data, restricted=restricted)
    return apply_field_visibility(data, ctx)


@router.post("")
def create_pool(
    body: PoolCreate,
    db: Session = Depends(get_db),
    _act: None = Depends(require_action("action_pool_manage")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    data = _run(svc.create_pool, db=db, name=body.name, description=body.description,
                member_part_ids=body.member_part_ids, note=body.note,
                operated_by=_operated_by(ctx))
    return apply_field_visibility(data, ctx)


@router.patch("/{group_id}")
def patch_pool(
    group_id: int,
    body: PoolPatch,
    db: Session = Depends(get_db),
    _act: None = Depends(require_action("action_pool_manage")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    updates = body.model_dump(exclude_unset=True, exclude={"version", "note"})
    data = _run(svc.update_pool, db=db, group_id=group_id, version=body.version,
                updates=updates, note=body.note, operated_by=_operated_by(ctx))
    return apply_field_visibility(data, ctx)


@router.patch("/{group_id}/members")
def patch_members(
    group_id: int,
    body: PoolMembersPatch,
    db: Session = Depends(get_db),
    _act: None = Depends(require_action("action_pool_manage")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    data = _run(svc.update_members, db=db, group_id=group_id, version=body.version,
                add_part_ids=body.add_part_ids, remove_part_ids=body.remove_part_ids,
                note=body.note, operated_by=_operated_by(ctx))
    return apply_field_visibility(data, ctx)


@router.put("/{group_id}/price-policy")
def put_price_policy(
    group_id: int,
    body: PoolPolicyPut,
    db: Session = Depends(get_db),
    # "能改必须能看"（复审阻塞 4）：设置约束价还必须持有约束价查看权限，
    # 否则会在看不见现值的情况下改写/清空另一侧
    _act: None = Depends(require_action("action_pool_set_policy",
                                        require_data="data_pool_price_governance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    data = _run(svc.set_price_policy, db=db, group_id=group_id, version=body.version,
                purchase_op=("unset" if body.purchase_unset else None),
                purchase_value=body.purchase_value, purchase_basis=body.purchase_basis,
                sales_op=("unset" if body.sales_unset else None),
                sales_value=body.sales_value, sales_basis=body.sales_basis,
                note=body.note, operated_by=_operated_by(ctx))
    return apply_field_visibility(data, ctx)


@router.post("/{group_id}/archive")
def archive_pool(
    group_id: int,
    body: PoolLifecycle,
    db: Session = Depends(get_db),
    _act: None = Depends(require_action("action_pool_manage")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    data = _run(svc.archive_pool, db=db, group_id=group_id, version=body.version,
                note=body.note, operated_by=_operated_by(ctx))
    return apply_field_visibility(data, ctx)


@router.post("/{group_id}/restore")
def restore_pool(
    group_id: int,
    body: PoolLifecycle,
    db: Session = Depends(get_db),
    _act: None = Depends(require_action("action_pool_manage")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    data = _run(svc.restore_pool, db=db, group_id=group_id, version=body.version,
                note=body.note, operated_by=_operated_by(ctx))
    return apply_field_visibility(data, ctx)

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
    version: int
    purchase_value: Decimal | None = None
    purchase_basis: str = Field("ex_tax", pattern=_BASIS_PATTERN)
    sales_value: Decimal | None = None
    sales_basis: str = Field("ex_tax", pattern=_BASIS_PATTERN)
    note: str | None = None


class PoolLifecycle(BaseModel):
    version: int
    note: str | None = None


def _operated_by(ctx: UserContext) -> str | None:
    # 真实用户名优先（审计勿记角色串——S-2 教训，见 services/master_edit.py）
    return ctx.user_id or ctx.role


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
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(require_login),
) -> dict:
    record_access_log(ctx, "pool_catalog_list", "pools", {"q": q, "status": status_})
    data = svc.list_pools(db, q=q, status=status_, page=page, page_size=page_size)
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
    _act: None = Depends(require_action("action_pool_set_policy")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    data = _run(svc.set_price_policy, db=db, group_id=group_id, version=body.version,
                purchase_value=body.purchase_value, purchase_basis=body.purchase_basis,
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

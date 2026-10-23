"""池目录与价格策略 transport。两个资源独立授权，策略字段按其自身 Grant 投影。

仅资源尚未切换时，protected service 的 legacy adapter 保留旧权限语义。
"""
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.authz.principal import VerifiedIdentity, current_verified_identity
from app.services import pool_access
from app.security import UserContext, get_current_user_context, record_access_log
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
    identity: VerifiedIdentity = Depends(current_verified_identity),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    record_access_log(ctx, 'pool_catalog_list', 'pools', {'q': q, 'status': status_, 'policy_missing': policy_missing})
    return pool_access.read_pool_catalog(db, identity, legacy_context=ctx, q=q,
        status=status_, policy_missing=policy_missing, page=page, page_size=page_size)


@router.get("/{group_id}")
def get_pool(
    group_id: int,
    db: Session = Depends(get_db),
    identity: VerifiedIdentity = Depends(current_verified_identity),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    record_access_log(ctx, 'pool_catalog_detail', 'pools', {'group_id': group_id})
    data = pool_access.read_pool_catalog_detail(db, identity, group_id, legacy_context=ctx)
    if data is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "池不存在")
    return data


@router.post("")
def create_pool(
    body: PoolCreate,
    db: Session = Depends(get_db),
    identity: VerifiedIdentity = Depends(current_verified_identity),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    return _run(pool_access.create_pool, db=db, identity=identity, legacy_context=ctx,
        name=body.name, description=body.description, member_part_ids=body.member_part_ids,
        note=body.note)


@router.patch("/{group_id}")
def patch_pool(
    group_id: int,
    body: PoolPatch,
    db: Session = Depends(get_db),
    identity: VerifiedIdentity = Depends(current_verified_identity),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    updates = body.model_dump(exclude_unset=True, exclude={"version", "note"})
    return _run(pool_access.edit_pool, db=db, identity=identity, legacy_context=ctx,
        group_id=group_id, version=body.version, updates=updates, note=body.note)


@router.patch("/{group_id}/members")
def patch_members(
    group_id: int,
    body: PoolMembersPatch,
    db: Session = Depends(get_db),
    identity: VerifiedIdentity = Depends(current_verified_identity),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    return _run(pool_access.edit_pool_members, db=db, identity=identity, legacy_context=ctx,
        group_id=group_id, version=body.version, add_part_ids=body.add_part_ids,
        remove_part_ids=body.remove_part_ids, note=body.note)


@router.put("/{group_id}/price-policy")
def put_price_policy(
    group_id: int,
    body: PoolPolicyPut,
    db: Session = Depends(get_db),
    # "能改必须能看"（复审阻塞 4）：设置约束价还必须持有约束价查看权限，
    # 否则会在看不见现值的情况下改写/清空另一侧
    identity: VerifiedIdentity = Depends(current_verified_identity),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    return _run(pool_access.set_pool_price_policy, db=db, identity=identity, legacy_context=ctx,
        group_id=group_id, version=body.version,
        purchase_op=("unset" if body.purchase_unset else None),
        purchase_value=body.purchase_value, purchase_basis=body.purchase_basis,
        sales_op=("unset" if body.sales_unset else None),
        sales_value=body.sales_value, sales_basis=body.sales_basis, note=body.note)


@router.post("/{group_id}/archive")
def archive_pool(
    group_id: int,
    body: PoolLifecycle,
    db: Session = Depends(get_db),
    identity: VerifiedIdentity = Depends(current_verified_identity),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    return _run(pool_access.archive_pool, db=db, identity=identity, legacy_context=ctx,
        group_id=group_id, version=body.version, note=body.note)


@router.post("/{group_id}/restore")
def restore_pool(
    group_id: int,
    body: PoolLifecycle,
    db: Session = Depends(get_db),
    identity: VerifiedIdentity = Depends(current_verified_identity),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    return _run(pool_access.restore_pool, db=db, identity=identity, legacy_context=ctx,
        group_id=group_id, version=body.version, note=body.note)

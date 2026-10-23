"""Load live account, one grant, and explicit customer assignments per request."""
from dataclasses import dataclass, replace

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.authz import engine
from app.authz.catalog import SALES_ACTIONS, SALES_FIELD_PERMISSIONS, SALES_RESOURCE
from app.config import get_settings
from app.models.authorization import SysAuthorizationGrant, SysCustomerAssignment
from app.models.sales import FSalesOrder
from app.models.system import SysUser
from app.security import UserContext


@dataclass(frozen=True)
class SalesAccess:
    predicate: object
    context: UserContext
    scope: str
    customer_count: int


@dataclass(frozen=True)
class _LoadedGrant:
    user: SysUser
    grant: engine.SalesGrant
    customer_ids: tuple[int, ...]


def enabled() -> bool:
    settings = get_settings()
    return settings.authz_sales_analysis_enabled or getattr(settings, "authz_sales_analysis_suspended", False)


def _suspended() -> bool:
    return getattr(get_settings(), "authz_sales_analysis_suspended", False)


def _load(db: Session, ctx: UserContext) -> _LoadedGrant:
    if (not ctx.is_authenticated or not ctx.user_id or ctx.authn != "sys_user"
            or ctx.is_fallback or type(ctx.token_version) is not int):
        raise HTTPException(403, "销售分析需要有效实名账号")
    # All grant/assignment writers lock this parent row FOR UPDATE. Hold a
    # shared lock for the request transaction so READ COMMITTED cannot combine
    # an old grant with newly committed assignments or token_version.
    user = db.scalar(select(SysUser).where(SysUser.username == ctx.user_id)
                     .with_for_update(read=True).execution_options(populate_existing=True))
    if (user is None or not user.is_active or user.deleted_at is not None
            or user.token_version != ctx.token_version):
        raise HTTPException(403, "销售分析实名账号或会话已失效")
    grant = db.scalar(select(SysAuthorizationGrant).where(
        SysAuthorizationGrant.user_id == user.id,
        SysAuthorizationGrant.resource == SALES_RESOURCE,
    ).execution_options(populate_existing=True))
    if grant is None:
        raise HTTPException(403, "未获得销售分析授权")
    try:
        # Do not coerce malformed JSON strings to tuples of characters.
        from app.authz.catalog import validate_sales_grant
        validate_sales_grant(grant.actions, grant.scope, grant.field_permissions)
        checked = engine.SalesGrant(
            tuple(grant.actions), grant.scope, tuple(grant.field_permissions), grant.version,
        )
    except (ValueError, TypeError):
        raise HTTPException(403, "销售分析授权配置无效") from None
    customers = tuple(db.scalars(select(SysCustomerAssignment.customer_id).where(
        SysCustomerAssignment.user_id == user.id,
    ).order_by(SysCustomerAssignment.customer_id)))
    return _LoadedGrant(user, checked, customers)


def sales_access(db: Session, ctx: UserContext, action: str = "read") -> SalesAccess:
    if not enabled():
        raise HTTPException(503, "销售分析新授权模块尚未启用")
    if _suspended():
        raise HTTPException(503, "销售分析已暂停，请稍后重试")
    if action not in SALES_ACTIONS:
        raise HTTPException(403, "未注册的销售分析动作")
    loaded = _load(db, ctx)
    try:
        plan = engine.plan_sales(
            loaded.user.id, loaded.grant, loaded.customer_ids, action,
            FSalesOrder, FSalesOrder.customer_id,
        )
    except engine.AuthorizationDenied as exc:
        raise HTTPException(403, str(exc)) from None
    except engine.AuthorizationUnavailable:
        raise HTTPException(503, "授权服务暂不可用，请稍后重试") from None
    permissions = {field: field in plan.field_permissions for field in SALES_FIELD_PERMISSIONS}
    # Scope is an explicit SQL predicate; never reapply old salesperson-based
    # own_customers_only logic to these customer assignments.
    permissions["own_customers_only"] = False
    permissions["page_sales_analysis"] = True
    context = replace(
        ctx, role=loaded.user.role, salesperson_name=loaded.user.salesperson_name,
        permissions=permissions, explicit_field_policy=True,
    )
    return SalesAccess(plan.predicate, context, loaded.grant.scope, len(loaded.customer_ids))


def sales_snapshot(db: Session, ctx: UserContext) -> dict:
    snapshot = {
        "enabled": enabled(), "read": False, "export": False, "scope": "none",
        "customer_count": 0, "field_permissions": [], "reason": "模块尚未启用",
    }
    if not snapshot["enabled"]:
        return snapshot
    if _suspended():
        snapshot["reason"] = "销售分析已暂停"
        return snapshot
    try:
        loaded = _load(db, ctx)
    except HTTPException as exc:
        if exc.status_code != 403:
            raise
        snapshot["reason"] = exc.detail
        return snapshot
    try:
        decision = engine.check_sales(loaded.user.id, loaded.grant, loaded.customer_ids)
    except engine.AuthorizationDenied:
        snapshot["reason"] = "未获得销售分析读取授权"
        return snapshot
    except engine.AuthorizationUnavailable:
        raise HTTPException(503, "授权服务暂不可用，请稍后重试") from None
    if "read" not in decision.allowed_actions:
        snapshot["reason"] = "未获得销售分析读取授权"
        return snapshot
    snapshot.update({
        "read": True, "export": "export" in decision.allowed_actions,
        "scope": loaded.grant.scope, "customer_count": len(loaded.customer_ids),
        "field_permissions": sorted(decision.field_permissions),
        "reason": ("尚未分配客户，当前数据范围为空"
                   if loaded.grant.scope == "assigned_customers" and not loaded.customer_ids
                   else "已获得显式授权"),
    })
    return snapshot

"""Management of explicit module grants; legacy account permissions are not dual-written."""
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app import permissions
from app.auth import current_identity
from app.authz import service
from app.authz.catalog import SALES_FIELD_PERMISSIONS, SALES_RESOURCE, validate_sales_grant
from app.db import get_db
from app.models.authorization import SysAuthorizationGrant, SysCustomerAssignment
from app.models.dimensions import DimCustomer
from app.models.system import SysAuditLog, SysUser
from app.security import UserContext

router = APIRouter(prefix="/authz", tags=["authorization"])
CATALOG_VERSION = "sales-analysis-v1"
_NO_STORE = {"Cache-Control": "no-store"}
_NOTICE = "启用销售分析新授权后，旧账号权限中的页面和数据勾选不再更新此模块；请在此单独配置。客户可共同分配，归属覆盖该客户全部历史及后续订单。"
_FIELD_ORDER = (
    "data_supplier", "data_customer", "data_purchase_cost", "data_profit",
    "data_pool_price_governance", "data_reconciliation",
)


class ReplaceAuthorization(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: Annotated[StrictInt, Field(ge=0)]
    actions: list[StrictStr]
    scope: Literal["all", "assigned_customers"]
    field_permissions: list[StrictStr]
    customer_ids: list[Annotated[StrictInt, Field(gt=0)]]

    @model_validator(mode="after")
    def validate_registered_values(self):
        validate_sales_grant(self.actions, self.scope, self.field_permissions)
        if len(self.customer_ids) != len(set(self.customer_ids)):
            raise ValueError("客户归属不能重复")
        return self


def _error(code: int, message: str) -> HTTPException:
    return HTTPException(code, message, headers=_NO_STORE)


def _named_user(db: Session, identity: dict, *, lock: bool = False) -> SysUser | None:
    if (identity.get("authn") != "sys_user" or identity.get("fb")
            or not identity.get("sub") or type(identity.get("tv")) is not int):
        return None
    stmt = select(SysUser).where(SysUser.username == identity["sub"])
    if lock:
        stmt = stmt.with_for_update(read=True)
    return db.scalar(stmt.execution_options(populate_existing=True))


def _is_live(user: SysUser | None, identity: dict) -> bool:
    return bool(user is not None and user.is_active and user.deleted_at is None
                and user.token_version == identity.get("tv"))


def _check_admin(user: SysUser | None, identity: dict) -> SysUser:
    if user is None:
        raise _error(403, "模块授权管理需要实名管理员账号")
    if not _is_live(user, identity):
        raise _error(401, "管理员账号或会话已失效，请重新登录")
    if user.role != "admin":
        raise _error(403, "仅实名管理员可管理模块授权")
    return user


def _admin_identity(identity: dict = Depends(current_identity), db: Session = Depends(get_db)) -> dict:
    _check_admin(_named_user(db, identity), identity)
    return identity


def _target(db: Session, username: str, *, lock: bool = False) -> SysUser:
    stmt = select(SysUser).where(SysUser.username == username, SysUser.deleted_at.is_(None))
    if lock:
        stmt = stmt.with_for_update(read=True)
    target = db.scalar(stmt.execution_options(populate_existing=True))
    if target is None:
        raise _error(404, "账号不存在")
    return target


def _grant(db: Session, user_id: int) -> SysAuthorizationGrant | None:
    return db.scalar(select(SysAuthorizationGrant).where(
        SysAuthorizationGrant.user_id == user_id, SysAuthorizationGrant.resource == SALES_RESOURCE,
    ).execution_options(populate_existing=True))


def _customer_name():
    return func.coalesce(func.nullif(DimCustomer.name_normalized, ""), DimCustomer.name_raw)


def _snapshot(db: Session, target: SysUser, grant: SysAuthorizationGrant | None) -> dict:
    customers = db.execute(select(DimCustomer.id, _customer_name().label("name"), DimCustomer.name_raw.label("raw_name"))
                           .join(SysCustomerAssignment, SysCustomerAssignment.customer_id == DimCustomer.id)
                           .where(SysCustomerAssignment.user_id == target.id)
                           .order_by(DimCustomer.id)).mappings().all()
    return {
        "username": target.username, "version": grant.version if grant else 0,
        "actions": list(grant.actions) if grant else [],
        "scope": grant.scope if grant else "assigned_customers",
        "field_permissions": list(grant.field_permissions) if grant else [],
        "customer_ids": [item["id"] for item in customers],
        "customers": [dict(item) for item in customers],
    }


@router.get("/me")
def my_authorization(response: Response, identity: dict = Depends(current_identity), db: Session = Depends(get_db)):
    response.headers.update(_NO_STORE)
    # The same user lock serializes grant, assignments, token version and the
    # service snapshot with management writes, including suspended/off mode.
    user = _named_user(db, identity, lock=True)
    live = _is_live(user, identity)
    # Construct from the mandatory authenticated identity, even when legacy RBAC
    # is disabled. The service independently rechecks the named account and token.
    ctx = UserContext(
        user_id=identity.get("sub"), role=identity.get("role", "guest"),
        salesperson_name=identity.get("name"), permissions=identity.get("perms"),
        is_authenticated=True, authn=identity.get("authn"),
        is_fallback=bool(identity.get("fb")), token_version=identity.get("tv"),
    )
    grant = _grant(db, user.id) if live else None
    return {
        "version": f"{CATALOG_VERSION}:{int(service.enabled())}:{user.token_version if live else 0}:{grant.version if grant else 0}",
        "username": user.username if live else None,
        "sales_analysis": service.sales_snapshot(db, ctx),
        "can_manage": bool(live and user.role == "admin"),
    }


@router.get("/catalog")
def authorization_catalog(response: Response, _identity: dict = Depends(_admin_identity)):
    response.headers.update(_NO_STORE)
    return {"version": CATALOG_VERSION, "modules": [
        {
            "id": SALES_RESOURCE, "label": "销售分析", "status": "migrated", "enabled": service.enabled(),
            "actions": [{"id": "read", "label": "读取"}, {"id": "export", "label": "导出"}],
            "scopes": [{"id": "all", "label": "全部客户"}, {"id": "assigned_customers", "label": "本人负责客户"}],
            "field_permissions": [{"id": key, "label": permissions.LABELS[key]} for key in _FIELD_ORDER
                                  if key in SALES_FIELD_PERMISSIONS],
            "notice": _NOTICE,
        },
        *[{"id": key.removeprefix("page_"), "label": permissions.LABELS.get(key, key),
           "status": "legacy", "permission_key": key}
          for key in permissions.PAGE_KEYS if key != "page_sales_analysis"],
    ]}


@router.get("/customers")
def search_customers(response: Response, q: str = Query("", max_length=256), limit: int = Query(50, ge=1, le=100),
                     _identity: dict = Depends(_admin_identity), db: Session = Depends(get_db)):
    response.headers.update(_NO_STORE)
    stmt = select(DimCustomer.id, _customer_name().label("name"), DimCustomer.name_raw.label("raw_name"))
    if q.strip():
        # Escape SQL wildcards: this is a literal name search, not a pattern API.
        term = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(or_(DimCustomer.name_raw.ilike(f"%{term}%", escape="\\"),
                             DimCustomer.name_normalized.ilike(f"%{term}%", escape="\\")))
    rows = db.execute(stmt.order_by(_customer_name(), DimCustomer.id).limit(limit)).mappings()
    return {"items": [dict(row) for row in rows]}


@router.get("/users/{username}")
def get_authorization(username: str, response: Response, _identity: dict = Depends(_admin_identity),
                      db: Session = Depends(get_db)):
    response.headers.update(_NO_STORE)
    # Keep the displayed version and complete customer list from one authorized
    # state, rather than composing separate READ COMMITTED snapshots.
    target = _target(db, username, lock=True)
    return _snapshot(db, target, _grant(db, target.id))


@router.put("/users/{username}")
def replace_authorization(username: str, body: ReplaceAuthorization, response: Response,
                          identity: dict = Depends(_admin_identity), db: Session = Depends(get_db)):
    response.headers.update(_NO_STORE)
    target = _target(db, username)
    actor = _named_user(db, identity)
    if actor is None:
        raise _error(403, "模块授权管理需要实名管理员账号")
    # Lock in stable order: two administrators editing one another must not
    # deadlock. The target account also serializes first-ever grant creation.
    locked = {user.id: user for user in db.scalars(select(SysUser).where(
        SysUser.id.in_({actor.id, target.id}),
    ).order_by(SysUser.id).with_for_update().execution_options(populate_existing=True))}
    actor = _check_admin(locked.get(actor.id), identity)
    target = locked.get(target.id)
    if target is None or target.deleted_at is not None:
        raise _error(404, "账号不存在")
    grant = _grant(db, target.id)
    if body.expected_version != (grant.version if grant else 0):
        raise _error(409, "授权已被其他管理员修改，请重新载入后再保存")
    if body.customer_ids:
        existing = set(db.scalars(select(DimCustomer.id).where(DimCustomer.id.in_(body.customer_ids))
                                  .with_for_update(read=True, key_share=True)))
        if existing != set(body.customer_ids):
            raise _error(404, "所选客户已不存在，请重新搜索")
    before = _snapshot(db, target, grant)
    if grant is None:
        grant = SysAuthorizationGrant(user_id=target.id, resource=SALES_RESOURCE, version=1)
        db.add(grant)
    else:
        grant.version += 1
    grant.actions = sorted(body.actions)
    grant.scope = body.scope
    grant.field_permissions = sorted(body.field_permissions)
    db.execute(delete(SysCustomerAssignment).where(SysCustomerAssignment.user_id == target.id))
    db.add_all(SysCustomerAssignment(user_id=target.id, customer_id=customer_id)
               for customer_id in sorted(body.customer_ids))
    target.token_version += 1
    db.flush()
    after = _snapshot(db, target, grant)
    db.add(SysAuditLog(
        entity_type="sys_authorization_grant", entity_id=grant.id, action="authorization_replace",
        before_json={"resource": SALES_RESOURCE, **before}, after_json={"resource": SALES_RESOURCE, **after},
        reason="完整替换销售分析模块授权与客户归属", operated_by=actor.username,
    ))
    db.commit()
    return after

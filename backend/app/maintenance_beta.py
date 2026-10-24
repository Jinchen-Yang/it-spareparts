"""维保稳定版与 Beta 工作台之间的服务端发布闸门。"""

from fastapi import Depends, HTTPException, Request, status

from app.auth import current_identity
from app.beta_access import maintenance_beta_whitelisted
from app.config import get_settings


def for_resources(*resources: str):
    """A migrated router keeps its release flag and uses canonical grants."""
    from sqlalchemy.orm import Session
    from app.db import get_db
    from app.authz import runtime
    from app.authz.context_registry import ENTRY_CONTRACTS

    if not resources:
        raise ValueError('A migrated router must declare its resources')

    def dependency(request: Request, db: Session = Depends(get_db), ident: dict = Depends(current_identity)):
        if not get_settings().maintenance_beta_enabled:
            raise HTTPException(status.HTTP_404_NOT_FOUND, '页面不存在')
        route = request.scope.get('route')
        contract = ENTRY_CONTRACTS.get(f"{request.method} {getattr(route, 'path', '')}")
        # This gate only preserves release/named-account availability. The
        # registered facade alone resolves the actual input/object branch and
        # authorizes it. A complete alternative may be migrated independently;
        # one resource in an incomplete AND branch cannot enable that contract.
        branches = [set(branch['requirements'])
                    for branch in (contract or {}).get('branches', {}).values()
                    if not branch.get('authorization_view_only')]
        required = set().union(*branches) if branches else set()
        if (required and required <= set(resources)
                and any(branch and all(runtime.is_authoritative(db, resource)
                                       for resource in branch) for branch in branches)):
            if ident.get('authn') != 'sys_user' or ident.get('fb') or not ident.get('sub'):
                raise HTTPException(status.HTTP_403_FORBIDDEN, '维保管理仅对实名系统账号开放')
            return
        require_maintenance_beta(ident)
    dependency.release_gate = require_maintenance_beta
    return dependency


def require_maintenance_beta(
    ident: dict = Depends(current_identity),
) -> None:
    """Beta 总闸关闭时隐藏路由；打开后仍需稳定版权限与逐账号白名单。"""
    if not get_settings().maintenance_beta_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "页面不存在")
    real_identity = (
        ident.get("authn") == "sys_user"
        and not ident.get("fb")
        and bool(ident.get("sub"))
    )
    if not real_identity:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "维保管理仅对实名系统账号开放",
        )
    if not maintenance_beta_whitelisted(
        role=str(ident.get("role") or "guest"),
        permission_map=ident.get("perms"),
        real_identity=True,
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "未获得维保管理页面权限")

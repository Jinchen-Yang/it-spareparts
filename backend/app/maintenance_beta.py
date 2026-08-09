"""维保稳定版与 Beta 工作台之间的服务端发布闸门。"""

from fastapi import Depends, HTTPException, status

from app.auth import current_identity
from app.beta_access import maintenance_beta_whitelisted
from app.config import get_settings


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
            "维保管理 Beta 仅对实名系统账号白名单开放",
        )
    if not maintenance_beta_whitelisted(
        role=str(ident.get("role") or "guest"),
        permission_map=ident.get("perms"),
        real_identity=True,
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "未加入维保 Beta 试用名单")

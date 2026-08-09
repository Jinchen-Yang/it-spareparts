"""维保稳定版与 Beta 工作台之间的服务端发布闸门。"""

from fastapi import Depends, HTTPException, status

from app.config import get_settings
from app.security import UserContext, get_current_user_context, page_allowed


def require_maintenance_beta(
    ctx: UserContext = Depends(get_current_user_context),
) -> None:
    """Beta 总闸关闭时隐藏路由；打开后仍需稳定版权限与逐账号白名单。"""
    if not get_settings().maintenance_beta_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "页面不存在")
    if not (
        page_allowed(ctx, "page_maintenance")
        and page_allowed(ctx, "page_maintenance_beta")
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "未加入维保 Beta 试用名单")

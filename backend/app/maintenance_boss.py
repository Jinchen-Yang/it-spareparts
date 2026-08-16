"""维保展示板（plan v1.3）服务端发布闸门。

与 maintenance_beta 的区别：本闸只做 flag 404（隐藏路由），页面/动作权限由
router 上的 require_page/require_action 正常把关（page_maintenance_boss /
page_maintenance / action_maintenance_wbdd_import），不做逐账号 Beta 白名单。
回滚 = 关闭 maintenance_boss_dashboard_enabled（铁律 7：不做 downgrade）。
"""

from fastapi import HTTPException, status

from app.config import get_settings


def require_maintenance_boss() -> None:
    """flag 关闭时整组路由 404「页面不存在」，与未发布状态不可区分。"""
    if not get_settings().maintenance_boss_dashboard_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "页面不存在")

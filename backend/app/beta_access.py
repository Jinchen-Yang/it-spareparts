"""Pure helpers for server-owned Beta visibility and whitelist checks."""

from __future__ import annotations

from app import permissions
from app.config import Settings, get_settings


def _page_allowed(
    *,
    role: str,
    permission_map: dict | None,
    page_key: str,
) -> bool:
    return permissions.page_permission_allowed(
        role=role,
        permission_map=permission_map,
        page_key=page_key,
    )


def maintenance_beta_whitelisted(
    *,
    role: str,
    permission_map: dict | None,
    real_identity: bool,
) -> bool:
    """A maintenance Beta tester must be a named account with both pages."""

    return real_identity and _page_allowed(
        role=role,
        permission_map=permission_map,
        page_key="page_maintenance",
    ) and _page_allowed(
        role=role,
        permission_map=permission_map,
        page_key="page_maintenance_beta",
    )


def replenishment_beta_whitelisted(
    *,
    role: str,
    permission_map: dict | None,
    real_identity: bool,
) -> bool:
    """The human replenishment workspace requires a named account allowlist bit."""
    return real_identity and _page_allowed(
        role=role,
        permission_map=permission_map,
        page_key="page_replenishment_beta",
    )


def beta_feature_availability(
    *,
    role: str,
    permission_map: dict | None,
    real_identity: bool,
    settings: Settings | None = None,
) -> dict[str, bool]:
    """Return the exact server-owned Beta navigation capability snapshot."""

    current = settings or get_settings()
    return {
        "maintenance": bool(
            current.maintenance_beta_enabled
            and maintenance_beta_whitelisted(
                role=role,
                permission_map=permission_map,
                real_identity=real_identity,
            )
        ),
        "replenishment": bool(
            current.replenishment_beta_enabled
            and replenishment_beta_whitelisted(
                role=role,
                permission_map=permission_map,
                real_identity=real_identity,
            )
        ),
        # 维保项目看板（plan v1.3）：正式功能（2026-08-17 去 Beta）。
        # 默认开启；maintenance_boss_dashboard_enabled 仅作紧急回滚开关（关→整组 404）。
        # 前端导航已不再据此隐藏（仅按 page_maintenance_boss/page_maintenance 权限展示），
        # 本键保留为服务端状态信号与历史兼容。
        "maintenance_boss": bool(
            current.maintenance_boss_dashboard_enabled
            and maintenance_boss_visible(
                role=role,
                permission_map=permission_map,
                real_identity=real_identity,
            )
        ),
    }


def maintenance_boss_visible(
    *,
    role: str,
    permission_map: dict | None,
    real_identity: bool,
) -> bool:
    """展示板导航可见性：实名账号 + 任一查看键（admin 常规短路）。"""
    if not real_identity:
        return False
    if role == "admin":
        return True
    return bool(
        permissions.page_permission_allowed(
            role=role, permission_map=permission_map,
            page_key="page_maintenance_boss",
        )
        or permissions.page_permission_allowed(
            role=role, permission_map=permission_map,
            page_key="page_maintenance",
        )
    )

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
    if role == "admin":
        return True
    graph = (
        permission_map
        if isinstance(permission_map, dict)
        else permissions.effective(role, None)
    )
    return bool(graph.get(page_key, False))


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
            and real_identity
            and _page_allowed(
                role=role,
                permission_map=permission_map,
                page_key="page_replenishment_beta",
            )
        ),
    }

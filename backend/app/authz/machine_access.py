"""Dedicated machine credentials and closed action sets, never employee JWTs."""
from dataclasses import dataclass
import hashlib
import hmac
from typing import Annotated

from fastapi import Header, HTTPException

SERVICE_ACTIONS = {
    'resale_export': frozenset({'resale_return_feed.read'}),
    'resale_catalog': frozenset({'resale_catalog.resolve', 'resale_catalog.snapshots',
        'resale_catalog.list', 'resale_catalog.search', 'resale_catalog.price_evidence',
        'resale_price_reference.read'}),
}


def validate_service_actions(service_id, values):
    if (service_id not in SERVICE_ACTIONS or type(values) is not list
            or any(type(value) is not str for value in values)
            or len(set(values)) != len(values) or not set(values) <= SERVICE_ACTIONS[service_id]):
        raise ValueError('机器服务动作必须是当前服务的闭集且不能重复')
    return values


@dataclass(frozen=True, slots=True)
class ServiceAccess:
    service_id: str
    resource: str
    action: str


def require_service(service_id, resource, action):
    """Bound only by server route declarations; request JSON cannot select it."""
    operation = f'{resource}.{action}'
    disabled_message = 'PN 只读导出尚未启用' if service_id == 'resale_catalog' else '返件只读导出尚未启用'
    invalid_message = 'PN 只读凭据无效' if service_id == 'resale_catalog' else '只读导出凭据无效'
    if service_id not in SERVICE_ACTIONS or operation not in SERVICE_ACTIONS[service_id]:
        raise ValueError('未登记的机器服务入口')

    def dependency(authorization: Annotated[str | None, Header()] = None) -> ServiceAccess:
        from app.config import get_settings
        settings = get_settings()
        expected = getattr(settings, f'{service_id}_token_sha256').get_secret_value()
        headers = {'Cache-Control': 'no-store'}
        if not expected:
            raise HTTPException(404, disabled_message, headers=headers)
        if type(expected) is not str or len(expected) != 64 or any(c not in '0123456789abcdef' for c in expected):
            raise HTTPException(503, '只读集成配置无效', headers=headers)
        scheme, separator, token = (authorization or '').partition(' ')
        if (separator != ' ' or scheme.lower() != 'bearer' or not 32 <= len(token) <= 256
                or not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), expected)):
            raise HTTPException(401, invalid_message, headers=headers)
        try:
            actions = validate_service_actions(service_id, getattr(settings, f'{service_id}_allowed_actions'))
        except (ValueError, TypeError):
            raise HTTPException(503, '只读集成配置无效', headers=headers) from None
        if operation not in actions:
            raise HTTPException(403, '只读集成动作已暂停', headers=headers)
        return ServiceAccess(service_id, resource, action)

    dependency.__name__ = f'require_{resource}_{action}'
    return dependency

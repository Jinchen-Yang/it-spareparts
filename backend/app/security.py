"""权限扩展点（§8.5）—— 第一期只埋钩子，默认全量、零行为变化。

四个钩子：身份上下文 → 数据范围 → 字段脱敏 → 访问审计。
ENABLE_RBAC=False 时 data_scope/field_visibility 原样返回；ENABLE_ACCESS_LOG=False 时审计 no-op。
field_visibility 的真实递归实现已写好（供将来 + 验收用 ENABLE_RBAC=True 通路验证）。
"""
import logging
from dataclasses import dataclass
from typing import Any

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app import config

_log = logging.getLogger("access")
_bearer_optional = HTTPBearer(auto_error=False)

# 字段名 → 所属字段组（反向索引），用于脱敏
_FIELD_TO_GROUP = {f: g for g, fields in config.FIELD_GROUPS.items() for f in fields}


@dataclass
class UserContext:
    user_id: str | None
    role: str
    ding_user_id: str | None = None
    department_id: str | None = None
    team_id: str | None = None
    is_authenticated: bool = False


def get_current_user_context(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer_optional),
) -> UserContext:
    """身份上下文钩子。第一期：RBAC 关闭时统一返回临时全量上下文。

    将来：从 session/token/钉钉解析真实身份。鉴权本身仍由 auth.current_role/require_admin
    在各接口独立把关，本函数只提供"数据范围/脱敏"所需的上下文,不抢鉴权职责。
    """
    if not config.ENABLE_RBAC:
        return UserContext(user_id=None, role=config.PHASE1_BYPASS_ROLE, is_authenticated=False)
    # RBAC 开启后：尝试从 token 取角色，失败兜底 GUEST（绝不默认 admin）
    role = config.GUEST_ROLE
    authed = False
    if creds is not None:
        try:
            from app.auth import _verify_token
            role = _verify_token(creds.credentials)
            authed = True
        except Exception:  # noqa: BLE001
            role = config.GUEST_ROLE
    return UserContext(user_id=None, role=role, is_authenticated=authed)


def apply_data_scope(query, user_ctx: UserContext):
    """行级数据范围钩子。第一期原样返回。

    将来：销售只能查自己的客户、采购只能查自己的供应商、主管看本组等
    —— 在此对 query 追加 where 条件。
    """
    if not config.ENABLE_RBAC:
        return query
    return query  # TODO: 接入身份后按 user_ctx.role/team_id 追加过滤


def _hidden_fields(role: str) -> set[str]:
    vis = config.ROLE_FIELD_VISIBILITY.get(role)
    if vis is None:
        return set()  # 未知角色/全量角色 → 不隐藏（PHASE1_BYPASS_ROLE 走这里）
    hidden: set[str] = set()
    for group, visible in vis.items():
        if not visible:
            hidden.update(config.FIELD_GROUPS.get(group, []))
    return hidden


def apply_field_visibility(payload: Any, user_ctx: UserContext) -> Any:
    """字段级脱敏钩子。第一期(RBAC 关)原样返回。

    RBAC 开启时递归处理 dict/list：把不可见字段组内的字段值置为 MASK_VALUE。
    part overview/利润聚合返回含嵌套 list，必须递归。
    """
    if not config.ENABLE_RBAC:
        return payload
    hidden = _hidden_fields(user_ctx.role)
    if not hidden:
        return payload
    return _mask(payload, hidden)


def _mask(node: Any, hidden: set[str]) -> Any:
    if isinstance(node, dict):
        return {k: (config.MASK_VALUE if k in hidden else _mask(v, hidden)) for k, v in node.items()}
    if isinstance(node, list):
        return [_mask(item, hidden) for item in node]
    return node


def record_access_log(user_ctx: UserContext, action: str, resource: str,
                      filters: dict | None = None) -> None:
    """访问审计钩子。第一期开关关闭、不写库、不建表。

    将来记录：谁查了哪个 PN/客户利润/供应商报价、谁导出了数据。
    """
    if not config.ENABLE_ACCESS_LOG:
        return
    _log.info("access role=%s action=%s resource=%s filters=%s",
              user_ctx.role, action, resource, filters)

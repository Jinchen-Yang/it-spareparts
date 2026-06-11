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
    salesperson_name: str | None = None   # 对齐 f_sales_order.salesperson，行级过滤用
    ding_user_id: str | None = None
    department_id: str | None = None
    team_id: str | None = None
    is_authenticated: bool = False


# 看全量（不受行级/匿名限制）的角色
FULL_SCOPE_ROLES = {"admin", "boss", "readonly", config.PHASE1_BYPASS_ROLE}


def is_scoped_sales(user_ctx: UserContext | None) -> bool:
    """是否需要按"匿名行情+自己明细"收紧（仅 RBAC 开启且角色=sales）。"""
    return bool(config.ENABLE_RBAC and user_ctx and user_ctx.role == "sales")


def get_current_user_context(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer_optional),
) -> UserContext:
    """身份上下文钩子。RBAC 关 → 临时全量；RBAC 开 → 从服务端校验的 token 取真实身份。

    身份**只来自 token**，绝不信对话/请求体里用户自报的角色（注入防御根基）。
    鉴权本身仍由 auth.current_role/require_admin 各接口独立把关。
    """
    if not config.ENABLE_RBAC:
        return UserContext(user_id=None, role=config.PHASE1_BYPASS_ROLE, is_authenticated=False)
    # RBAC 开启：解析 token，失败兜底 GUEST（绝不默认 admin）
    if creds is not None:
        try:
            from app.auth import verify_token
            data = verify_token(creds.credentials)
            return UserContext(user_id=data.get("sub"), role=data.get("role", config.GUEST_ROLE),
                               salesperson_name=data.get("name"), is_authenticated=True)
        except Exception:  # noqa: BLE001
            pass
    return UserContext(user_id=None, role=config.GUEST_ROLE, is_authenticated=False)


def apply_data_scope(query, user_ctx: UserContext):
    """行级数据范围钩子。保持 pass-through。

    "匿名行情+自己明细"的防恶性竞争不靠"过滤掉同事的行"（那会连匿名行情也看不到），
    而靠**行级匿名化**：保留所有成交价（行情价值），但把非本人成交的客户名抹掉
    （见 part_overview.anonymize_sales_rows）。此处不加 SQL 过滤，避免误伤采购/搜索等
    跨表查询，且把口径集中在一处便于审计。
    """
    return query


def anonymize_sales_rows(rows: list[dict], user_ctx: UserContext | None) -> list[dict]:
    """sales 角色（RBAC 开）：非本人成交的客户名抹成"（其他客户）"，并去掉 salesperson；
    本人成交保留客户名。其余角色原样。rows 需含 'salesperson' 与 'customer' 键。"""
    if not is_scoped_sales(user_ctx):
        return [{k: v for k, v in r.items() if k != "salesperson"} for r in rows]
    me = user_ctx.salesperson_name
    out = []
    for r in rows:
        mine = r.get("salesperson") and r["salesperson"] == me
        r2 = {k: v for k, v in r.items() if k != "salesperson"}
        if not mine:
            r2["customer"] = "（其他客户）"
        out.append(r2)
    return out


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

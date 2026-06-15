"""权限扩展点（§8.5）—— 第一期只埋钩子，默认全量、零行为变化。

四个钩子：身份上下文 → 数据范围 → 字段脱敏 → 访问审计。
ENABLE_RBAC=False 时 data_scope/field_visibility 原样返回；ENABLE_ACCESS_LOG=False 时审计 no-op。
field_visibility 的真实递归实现已写好（供将来 + 验收用 ENABLE_RBAC=True 通路验证）。
"""
import logging
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app import config
from app.db import get_db

_log = logging.getLogger("access")
_bearer_optional = HTTPBearer(auto_error=False)

# 字段名 → 所属字段组（反向索引），用于脱敏
_FIELD_TO_GROUP = {f: g for g, fields in config.FIELD_GROUPS.items() for f in fields}


@dataclass
class UserContext:
    user_id: str | None
    role: str
    salesperson_name: str | None = None   # 对齐 f_sales_order.salesperson，行级过滤用
    permissions: dict | None = None        # 该用户最终权限（来自 token，见 app/permissions.py）
    ding_user_id: str | None = None
    department_id: str | None = None
    team_id: str | None = None
    is_authenticated: bool = False


# 看全量（不受行级/匿名限制）的角色
FULL_SCOPE_ROLES = {"admin", "boss", "readonly", config.PHASE1_BYPASS_ROLE}


def is_scoped_sales(user_ctx: UserContext | None) -> bool:
    """是否按"匿名行情+自己明细"收紧：RBAC 开 + own_customers_only 权限开；
    旧 token（无 perms）回退按角色=sales。"""
    if not (config.ENABLE_RBAC and user_ctx):
        return False
    if user_ctx.permissions is not None:
        return bool(user_ctx.permissions.get("own_customers_only"))
    return user_ctx.role == "sales"


def get_current_user_context(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer_optional),
    db: Session = Depends(get_db),
) -> UserContext:
    """身份上下文钩子。RBAC 关 → 临时全量；RBAC 开 → 从服务端校验的 token 取真实身份。

    身份**只来自 token**，绝不信对话/请求体里用户自报的角色（注入防御根基）。
    鉴权本身仍由 auth.current_role/require_admin 各接口独立把关。
    token 经 verify_token_db 校验吊销（tv/停用）；被吊销/无效 → 兜底 GUEST（数据侧降权，不硬 401）。
    """
    if not config.ENABLE_RBAC:
        return UserContext(user_id=None, role=config.PHASE1_BYPASS_ROLE, is_authenticated=False)
    # RBAC 开启：解析 token，失败/被吊销兜底 GUEST（绝不默认 admin）
    if creds is not None:
        try:
            from app.auth import verify_token_db
            data = verify_token_db(creds.credentials, db)
            return UserContext(user_id=data.get("sub"), role=data.get("role", config.GUEST_ROLE),
                               salesperson_name=data.get("name"),
                               permissions=data.get("perms"), is_authenticated=True)
        except Exception:  # noqa: BLE001
            pass
    return UserContext(user_id=None, role=config.GUEST_ROLE, is_authenticated=False)


def require_page(page_key: str):
    """页面级准入依赖：该用户 page_* 权限为 False → 403。
    admin 恒放行；RBAC 关或旧 token（无 perms）按角色模板回退，避免破坏旧会话。
    page_* 既驱动前端菜单显示，也在此做后端准入（前端藏菜单 ≠ 后端拦接口）。"""
    def _dep(ctx: UserContext = Depends(get_current_user_context)) -> None:
        if not config.ENABLE_RBAC or ctx.role == "admin":
            return
        perms = ctx.permissions
        if perms is None:
            from app import permissions as _perm
            perms = _perm.effective(ctx.role, None)
        if not perms.get(page_key, False):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "无权访问该页面")
    return _dep


def apply_data_scope(query, user_ctx: UserContext):
    """行级数据范围钩子。保持 pass-through。

    受限销售的逐单成交明细整段隐藏（见 anonymize_sales_rows，2026-06-13 收紧：销售只看
    聚合，不看任何逐单成交）。此处不加 SQL 过滤，避免误伤采购/搜索等跨表查询；逐单明细
    的可见性集中在 anonymize_sales_rows 一处便于审计。
    """
    return query


def anonymize_sales_rows(rows: list[dict], user_ctx: UserContext | None) -> list[dict]:
    """逐单销售成交明细的可见性策略（防恶性竞争口径，2026-06-13 甲方收紧）。

    - **受限销售（is_scoped_sales：own_customers_only 权限 / 销售角色）：逐单成交明细
      完全不可见**——返回空列表。销售只能用聚合（平均售价 avg_sale_price / 近期加权
      成交参考价 sale_price_ref），看不到"某件某单卖了多少、卖给谁"。这是数据层兜底：
      即便调用方忘了短路，经此函数后也不泄露任何成交行。
    - 其余角色（admin/boss/采购/只读）：保留成交行，但去掉 salesperson（不暴露是谁卖的）。

    rows 需含 'salesperson' 与 'customer' 键。（注：函数名沿用历史；行为已从"匿名化"
    收紧为"对受限销售整段丢弃"。）"""
    if is_scoped_sales(user_ctx):
        return []
    return [{k: v for k, v in r.items() if k != "salesperson"} for r in rows]


def _hidden_fields(user_ctx: UserContext) -> set[str]:
    """要隐藏的字段：优先按用户权限(perms)的 data_* 开关算；
    旧 token（无 perms）回退按 ROLE_FIELD_VISIBILITY 角色配置。"""
    if user_ctx.permissions is not None:
        from app import permissions as perm
        hidden: set[str] = set()
        for group in perm.hidden_groups(user_ctx.permissions):
            hidden.update(config.FIELD_GROUPS.get(group, []))
        return hidden
    vis = config.ROLE_FIELD_VISIBILITY.get(user_ctx.role)
    if vis is None:
        return set()  # 未知角色/全量角色 → 不隐藏（PHASE1_BYPASS_ROLE 走这里）
    hidden = set()
    for group, visible in vis.items():
        if not visible:
            hidden.update(config.FIELD_GROUPS.get(group, []))
    return hidden


def apply_field_visibility(payload: Any, user_ctx: UserContext) -> Any:
    """字段级脱敏钩子。RBAC 关 → 原样返回。

    RBAC 开启时递归处理 dict/list：把该用户不可见的字段值置为 MASK_VALUE。
    part overview/利润聚合返回含嵌套 list，必须递归。
    """
    if not config.ENABLE_RBAC:
        return payload
    hidden = _hidden_fields(user_ctx)
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
    """访问审计：记到 sys_access_log（账号管理页看子账号活动）。

    best-effort：用独立短连接写，任何失败都不打断业务请求。
    """
    if not config.ENABLE_ACCESS_LOG:
        return
    _log.info("access user=%s role=%s action=%s resource=%s",
              user_ctx.user_id, user_ctx.role, action, resource)
    try:
        from app.db import SessionLocal
        from app.models.system import SysAccessLog
        db = SessionLocal()
        try:
            db.add(SysAccessLog(username=user_ctx.user_id, role=user_ctx.role,
                                action=action, resource=(str(resource)[:500] if resource else None),
                                detail=filters))
            db.commit()
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        _log.warning("access log write failed")

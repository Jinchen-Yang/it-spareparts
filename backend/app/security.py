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


# 可访问任意上传文件（不受归属限制）的角色——仅 agent 文件 ACL 用（下载/预览）。
# readonly 故意不在内：共享口令回退把非 admin 一律发成 readonly，若放行会让任何知道
# ADMIN_PASSWORD 的人凭 12 位 file_id 读他人上传的报价/合同（IDOR，正是 PR#16 要防的）。
# 数据字段可见性另由 permissions 控制，与本文件白名单无关。
FULL_SCOPE_ROLES = {"admin", "boss", config.PHASE1_BYPASS_ROLE}


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


def require_action(action_key: str):
    """动作级准入依赖：该用户 action_* 权限为 False → 403；RBAC 开且未登录 → 401。
    admin 恒放行；旧 token（无 perms）按角色模板回退（与 require_page 同一口径）。
    与 require_page 的区别：page 管"能不能进页面看"，action 管"能不能执行写操作"
    （建池/改约束价等），二者独立授权（互通PN池价格分析 §12）。"""
    def _dep(ctx: UserContext = Depends(get_current_user_context)) -> None:
        if not config.ENABLE_RBAC or ctx.role == "admin":
            return
        if not ctx.is_authenticated:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "请先登录")
        perms = ctx.permissions
        if perms is None:
            from app import permissions as _perm
            perms = _perm.effective(ctx.role, None)
        if not perms.get(action_key, False):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "无此操作权限")
    return _dep


def require_login(ctx: UserContext = Depends(get_current_user_context)) -> UserContext:
    """登录即可的准入依赖（"全员读"接口用）：RBAC 开且匿名 → 401，其余放行并返回 ctx。"""
    if config.ENABLE_RBAC and not ctx.is_authenticated and ctx.role != "admin":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "请先登录")
    return ctx


def page_allowed(ctx: UserContext, page_key: str) -> bool:
    """页面权限的纯函数版（agent 工具层等非 FastAPI 依赖处用；与 require_page 同一逻辑）。"""
    if not config.ENABLE_RBAC or ctx.role == "admin":
        return True
    perms = ctx.permissions
    if perms is None:
        from app import permissions as _perm
        perms = _perm.effective(ctx.role, None)
    return bool(perms.get(page_key, False))


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
    """要隐藏的字段：按用户权限(perms)的 data_* 开关算。

    单一真值源（2026-06-15 收敛）：perms 与 perms 缺失时的回退都走 permissions 模板，
    不再用 config.ROLE_FIELD_VISIBILITY（旧表已删）。避免"同一角色因 token 新旧而脱敏结果相反"。
    无 perms 的旧 token → 按 role 的权限模板回退（与 require_page 的 fallback 口径一致）。
    """
    from app import permissions as perm
    perms = user_ctx.permissions
    if perms is None:
        perms = perm.template_for(user_ctx.role)
    hidden: set[str] = set()
    for group in perm.hidden_groups(perms):
        hidden.update(config.FIELD_GROUPS.get(group, []))
    return hidden


def is_field_hidden(user_ctx: UserContext | None, field: str) -> bool:
    """该字段对此用户是否被脱敏。供服务层做**结构性**收敛用——字段级 mask 只会把
    叶子值置空，但"型号落在赚钱榜还是亏损榜"这类归属本身就泄漏利润结论，需服务层据此
    决定不返回分类结构（复审三轮 P0：data_profit=false 仍能看出哪个型号赚/亏）。"""
    if user_ctx is None or not config.ENABLE_RBAC:
        return False
    return field in _hidden_fields(user_ctx)


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


def record_security_event(username: str | None, role: str | None, action: str,
                          resource: str | None = None, detail: dict | None = None,
                          ip: str | None = None, user_agent: str | None = None) -> None:
    """安全/账号事件审计（登录成功/失败/锁定/停用拦截等）→ sys_access_log。

    与 record_access_log 不同：不依赖 UserContext（失败登录时无身份），可带 ip/user_agent。
    best-effort：独立短连接，任何失败都不打断业务。
    """
    if not config.ENABLE_ACCESS_LOG:
        return
    _log.info("security event user=%s action=%s ip=%s", username, action, ip)
    try:
        from app.db import SessionLocal
        from app.models.system import SysAccessLog
        db = SessionLocal()
        try:
            db.add(SysAccessLog(
                username=username, role=role, action=action,
                resource=(str(resource)[:500] if resource else None), detail=detail,
                ip_address=(ip[:64] if ip else None),
                user_agent=(user_agent[:300] if user_agent else None)))
            db.commit()
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        _log.warning("security event log write failed")

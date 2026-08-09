"""账号与权限中心 v2（建号 / 改密 / 停用 / 权限 / 批量 / 活动）。

权限语义：账号有效权限 = 模板快照(template_perms) ⊕ 稀疏覆盖(perm_overrides)；
admin 角色恒全开。改密/停用/改权限递增 token_version → 旧 token 立即失效。

准入从 require_admin 放宽为权限键（admin 恒通过，行为对 admin 零变化）：
- 读（列表/_meta/活动）→ page_accounts
- 写（建号/改权/改密/停用/批量）→ action_account_manage

防锁死与防提权（_guard_* 系列，四条写路径共用）：
1. 内置 admin 账号：除改密外不可修改，不参与批量/模板。
2. admin 角色账号：仅 admin 操作者可动；批量一律拒绝。
3. 最后有效管理员（active 且 role=admin）不可停用/降级。
4. 操作者不可对自己：停用 / 降出 admin / 撤销账号管理两键。
5. 高风险键（page_accounts/action_account_manage）与 admin 升降：仅 admin 操作者。

批量：单事务全成或全败；先全量校验（任一非法整体 400 并逐账号给原因）；
dry_run 预览 + 指纹（执行时重算比对，不一致 409 防"预览后有人动过"）。
v2 写路径同时把完整有效权限图双写进旧列 permissions——downgrade 后旧代码
effective(role, 完整图) 逐键等于该图，回滚零漂移（设计 §1.6）。
"""
import hashlib
import json

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app import permissions
from app.auth import current_identity, hash_password
from app.db import get_db
from app.models.system import SysAccessLog, SysAuditLog, SysRoleTemplate, SysUser
from app.security import require_action, require_page

router = APIRouter(prefix="/accounts", tags=["accounts"])

_ROLES = ["admin", "boss", "sales", "purchaser", "readonly"]
_page_gate = require_page("page_accounts")
_write_gate = require_action("action_account_manage")


def _read_gate(ident: dict = Depends(current_identity),
               _page: None = Depends(_page_gate)) -> dict:
    """读接口门：先严格验 token（被吊销/停用 → 401 触发前端重登，而不是降级 guest 得 403），
    再查 page_accounts 页面权限（403）。"""
    return ident


# ---------- 请求体 ----------
class CreateAccount(BaseModel):
    username: str
    password: str
    role: str | None = None            # 旧字段：给了且没给 template_code → 套同名内置模板
    template_code: str | None = None   # v2：职位模板（缺省 readonly）
    display_name: str | None = None
    salesperson_name: str | None = None
    overrides: dict | None = None      # v2：相对模板的逐键调整
    permissions: dict | None = None    # 旧字段：完整/部分自定义图（换算成 overrides）


class UpdateAccount(BaseModel):
    role: str | None = None            # 仅两用途：升/降 admin；普通角色=套同名内置模板
    template_code: str | None = None
    display_name: str | None = None
    salesperson_name: str | None = None
    overrides: dict | None = None
    permissions: dict | None = None    # 旧字段兼容


class PasswordReset(BaseModel):
    password: str


class ActiveToggle(BaseModel):
    is_active: bool


class BulkBody(BaseModel):
    usernames: list[str]
    operation: str                     # apply_template / grant / revoke / reset_to_template
    template_code: str | None = None   # apply_template 用
    keys: list[str] | None = None      # grant / revoke 用
    dry_run: bool = True
    fingerprint: str | None = None     # 执行时必带（dry-run 响应里给）


# ---------- 视图与工具 ----------
def _template_map(db: Session) -> dict[str, SysRoleTemplate]:
    rows = db.execute(select(SysRoleTemplate)).scalars().all()
    return {t.code: t for t in rows}


def _view(u: SysUser, tpl: SysRoleTemplate | None = None) -> dict:
    eff = permissions.effective_for_user(u)
    combo = permissions.combo_errors(eff)
    base = permissions.normalize(u.template_perms) if u.template_perms is not None \
        else permissions.effective(u.role, None)
    return {
        "username": u.username, "display_name": u.display_name, "role": u.role,
        "salesperson_name": u.salesperson_name, "is_active": u.is_active,
        "last_login_at": u.last_login_at,
        "permissions": eff,                       # 最终生效（键名沿用旧版，前端兼容）
        # 存量非法组合不自动改库：列表显式标红供管理员修复；登录/字段层按
        # runtime_permissions 失败关闭，旧数据和旧 token 也不能继续泄漏。
        "runtime_permissions": permissions.runtime_safe(eff),
        "permission_combo_errors": combo,
        "template_code": u.template_code,
        "template_version": u.template_version,
        "template_name": tpl.name if tpl else u.template_code,
        "template_current_version": tpl.version if tpl else None,
        # 模板已更新但该账号未同步（admin 恒全开无此概念）
        "template_stale": bool(tpl and u.template_version is not None
                               and tpl.version > u.template_version and u.role != "admin"),
        "template_perms": base,                   # 快照底座（前端画"来自模板/单独调整"）
        "overrides": u.perm_overrides or {},
        "is_custom": bool(u.perm_overrides),
    }


def _get(db: Session, username: str) -> SysUser:
    u = db.scalar(select(SysUser).where(SysUser.username == username))
    if u is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"账号不存在: {username}")
    return u


def _acct_snapshot(u: SysUser) -> dict:
    """账号审计快照——绝不含口令/hash，只记可见属性变化。"""
    return {
        "username": u.username, "role": u.role, "display_name": u.display_name,
        "salesperson_name": u.salesperson_name, "is_active": u.is_active,
        "template_code": u.template_code, "template_version": u.template_version,
        "overrides": u.perm_overrides,
    }


def _audit(db: Session, user_id: int, action: str, before: dict | None,
           after: dict | None, operated_by: str | None, reason: str | None = None) -> None:
    """账号变更留痕 → sys_audit_log(entity_type='sys_user')。随业务事务一起 commit。"""
    db.add(SysAuditLog(entity_type="sys_user", entity_id=user_id, action=action,
                       before_json=before, after_json=after,
                       operated_by=operated_by, reason=reason))


def _dual_write_legacy(u: SysUser) -> None:
    """把完整有效权限图双写进旧列——回滚保险（设计 §1.6）。admin 角色写快照亦无害
    （旧代码对 admin 同样恒全开）。"""
    u.permissions = permissions.effective_for_user(u)


def _apply_template(u: SysUser, tpl: SysRoleTemplate, overrides: dict | None = None) -> None:
    """套用模板 = 快照权限 + 跟随基础角色 + 覆盖重置（不给则清空——"套用"就是回到模板口径）。"""
    u.template_code = tpl.code
    u.template_version = tpl.version
    u.template_perms = permissions.normalize(tpl.permissions)
    u.perm_overrides = permissions.sanitize(overrides) if overrides else None
    u.role = tpl.base_role


def _bump_token(u: SysUser) -> None:
    u.token_version = (u.token_version or 0) + 1


# ---------- 守护规则（防锁死/防提权，单改与批量共用） ----------
def _op_is_admin(ident: dict) -> bool:
    return ident.get("role") == "admin"


def _active_admin_count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(SysUser)
                     .where(SysUser.role == "admin", SysUser.is_active.is_(True))) or 0


def _guard_touch(ident: dict, u: SysUser) -> None:
    """能不能动这个账号（任何修改类操作的第一道门）。"""
    if u.username == "admin":
        raise HTTPException(400, "内置 admin 为系统账号，除重置密码外不可修改")
    if u.role == "admin" and not _op_is_admin(ident):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "仅管理员可操作管理员账号")


def _guard_high_risk_change(ident: dict, before_eff: dict, after_eff: dict, who: str) -> None:
    """高风险键（账号管理两键）的授予/撤销仅限 admin 操作者；
    且操作者不能撤销**自己**的高风险键（防自锁，谁来都不行）。"""
    changed = {k for k in permissions.HIGH_RISK_KEYS
               if bool(before_eff.get(k)) != bool(after_eff.get(k))}
    if not changed:
        return
    if not _op_is_admin(ident):
        labels = "、".join(permissions.LABELS.get(k, k) for k in sorted(changed))
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            f"「{labels}」属高风险权限，仅管理员本人可授予或撤销")
    if ident.get("sub") == who:
        revoked = {k for k in changed if bool(before_eff.get(k)) and not bool(after_eff.get(k))}
        if revoked:
            raise HTTPException(400, "不能撤销当前登录账号自己的账号管理权限，请由另一位管理员操作")


def _guard_admin_role_change(ident: dict, db: Session, u: SysUser, new_role: str) -> None:
    """升/降 admin 的门：仅 admin 操作者；降级受"最后管理员"与"不能降自己"保护。"""
    if new_role == u.role:
        return
    if (new_role == "admin" or u.role == "admin") and not _op_is_admin(ident):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "仅管理员可以升降管理员角色")
    if u.role == "admin" and new_role != "admin":
        if ident.get("sub") == u.username:
            raise HTTPException(400, "不能降级当前登录账号自己，请由另一位管理员操作")
        if u.is_active and _active_admin_count(db) <= 1:
            raise HTTPException(400, "这是最后一个启用状态的管理员，不能降级——请先增设另一位管理员")


def _combo_or_400(eff: dict, who: str | None = None) -> None:
    errs = permissions.combo_errors(eff)
    if errs:
        prefix = f"[{who}] " if who else ""
        raise HTTPException(400, prefix + "；".join(errs))


def _template_or_400(db: Session, code: str, ident: dict) -> SysRoleTemplate:
    tpl = db.scalar(select(SysRoleTemplate).where(SysRoleTemplate.code == code))
    if tpl is None:
        raise HTTPException(400, f"职位模板不存在: {code}")
    if not tpl.is_active:
        raise HTTPException(400, f"职位模板「{tpl.name}」已停用，不能再套用")
    if tpl.code == "admin":
        raise HTTPException(400, "管理员模板为锁定模板不可套用——升管理员请在账号上单独操作")
    # 模板本身带高风险键 → 套用即授予 → 同高风险守护
    if not _op_is_admin(ident) and any(tpl.permissions.get(k) for k in permissions.HIGH_RISK_KEYS):
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            f"模板「{tpl.name}」含账号管理高风险权限，仅管理员可套用")
    return tpl


# ---------- 元信息 ----------
@router.get("/_meta")
def meta(db: Session = Depends(get_db), _: None = Depends(_read_gate)) -> dict:
    """权限项 + 业务语言元数据 + 依赖图 + 分组 + 模板清单，供前端渲染权限矩阵。"""
    tpls = db.execute(select(SysRoleTemplate).order_by(SysRoleTemplate.id)).scalars().all()
    usage = dict(db.execute(
        select(SysUser.template_code, func.count()).group_by(SysUser.template_code)).all())
    return {
        "roles": _ROLES,
        "data_keys": list(permissions.DATA_GROUPS),
        "page_keys": permissions.PAGE_KEYS,
        "action_keys": permissions.ACTION_KEYS,
        "row_keys": permissions.ROW_KEYS,
        "labels": permissions.LABELS,
        # 旧字段保留（前端升级期兜底）；v2 前端用 templates（来自数据库）
        "role_templates": {r: permissions.effective(r, None) for r in _ROLES},
        "groups": permissions.UI_GROUPS,
        "meta": permissions.PERMISSION_META,
        "dependencies": {"action_data": permissions.ACTION_DATA_DEPENDENCIES,
                         "action_page": permissions.ACTION_PAGE_DEPENDENCIES,
                         "action_additional_page": permissions.ACTION_ADDITIONAL_PAGE_DEPENDENCIES,
                         "page_page": permissions.PAGE_PAGE_DEPENDENCIES,
                         "data_data": permissions.DATA_DATA_DEPENDENCIES},
        "high_risk_keys": sorted(permissions.HIGH_RISK_KEYS),
        "all_keys": permissions.ALL_KEYS,
        "templates": [{
            "code": t.code, "name": t.name, "description": t.description,
            "base_role": t.base_role, "permissions": permissions.normalize(t.permissions),
            "permission_combo_errors": permissions.combo_errors(
                permissions.normalize(t.permissions)),
            "is_system": t.is_system, "is_active": t.is_active, "version": t.version,
            "usage_count": usage.get(t.code, 0),
            "locked": t.code == "admin",
        } for t in tpls],
    }


@router.get("")
def list_accounts(db: Session = Depends(get_db), _: None = Depends(_read_gate)) -> list[dict]:
    tpls = _template_map(db)
    users = db.execute(select(SysUser).order_by(SysUser.id)).scalars().all()
    return [_view(u, tpls.get(u.template_code or "")) for u in users]


# ---------- 建号 ----------
@router.post("", status_code=status.HTTP_201_CREATED)
def create_account(body: CreateAccount, db: Session = Depends(get_db),
                   ident: dict = Depends(current_identity),
                   _: None = Depends(_write_gate)) -> dict:
    uname = (body.username or "").strip()
    if not uname:
        raise HTTPException(400, "用户名不能为空")
    if len(body.password or "") < 6:
        raise HTTPException(400, "密码至少 6 位")
    if db.scalar(select(SysUser).where(SysUser.username == uname)):
        raise HTTPException(409, f"用户名已存在: {uname}")
    if body.role is not None and body.role not in _ROLES:
        raise HTTPException(400, f"角色非法: {body.role}")

    u = SysUser(username=uname, role="readonly", display_name=body.display_name,
                salesperson_name=body.salesperson_name,
                password_hash=hash_password(body.password))
    if body.role == "admin":
        # 直接建管理员：仅 admin 操作者（模板层无 admin 套用路径）
        if not _op_is_admin(ident):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "仅管理员可以创建管理员账号")
        tpl = db.scalar(select(SysRoleTemplate).where(SysRoleTemplate.code == "admin"))
        u.role = "admin"
        u.template_code, u.template_version = "admin", (tpl.version if tpl else 1)
        u.template_perms = permissions.normalize(tpl.permissions) if tpl else permissions._full()
        u.perm_overrides = None
    else:
        code = body.template_code or body.role or "readonly"
        tpl = _template_or_400(db, code, ident)
        overrides = body.overrides
        if overrides is None and body.permissions is not None:
            # 旧字段：给的是期望的最终图 → 换算成相对快照的稀疏 diff
            desired = permissions.effective_from_snapshot(
                permissions.normalize(tpl.permissions), permissions.sanitize(body.permissions))
            overrides = permissions.diff_overrides(permissions.normalize(tpl.permissions), desired)
        _apply_template(u, tpl, overrides)
        eff = permissions.effective_for_user(u)
        _guard_high_risk_change(ident, {}, eff, uname)
        _combo_or_400(eff)
    _dual_write_legacy(u)
    db.add(u)
    db.flush()   # 取 u.id 供审计
    _audit(db, u.id, "account_create", None, _acct_snapshot(u), ident["sub"])
    db.commit()
    tpls = _template_map(db)
    return _view(u, tpls.get(u.template_code or ""))


# ---------- 单账号修改 ----------
@router.put("/{username}")
def update_account(username: str, body: UpdateAccount, db: Session = Depends(get_db),
                   ident: dict = Depends(current_identity),
                   _: None = Depends(_write_gate)) -> dict:
    u = _get(db, username)
    perm_change = (body.role is not None or body.template_code is not None
                   or body.overrides is not None or body.permissions is not None)
    if perm_change:
        _guard_touch(ident, u)
    elif u.username == "admin":
        raise HTTPException(400, "内置 admin 为系统账号，除重置密码外不可修改")
    before = _acct_snapshot(u)
    before_eff = permissions.effective_for_user(u)

    if body.display_name is not None:
        u.display_name = body.display_name
    if body.salesperson_name is not None:
        u.salesperson_name = body.salesperson_name

    if body.role is not None and body.role not in _ROLES:
        raise HTTPException(400, f"角色非法: {body.role}")

    if body.role == "admin" and u.role != "admin":
        # 升管理员：唯一路径（模板不可套 admin）
        _guard_admin_role_change(ident, db, u, "admin")
        tpl = db.scalar(select(SysRoleTemplate).where(SysRoleTemplate.code == "admin"))
        u.role = "admin"
        u.template_code, u.template_version = "admin", (tpl.version if tpl else 1)
        u.template_perms = permissions.normalize(tpl.permissions) if tpl else permissions._full()
        u.perm_overrides = None
    elif perm_change:
        # 降 admin / 换模板 / 调 overrides：统一走模板语义
        code = body.template_code
        if code is None and body.role is not None and body.role != u.role:
            code = body.role                      # 旧口径"改角色" = 套同名内置模板
        if code is not None:
            tpl = _template_or_400(db, code, ident)
            _guard_admin_role_change(ident, db, u, tpl.base_role)
            keep = body.overrides if body.overrides is not None else None
            _apply_template(u, tpl, keep)
        if body.overrides is not None and code is None:
            u.perm_overrides = permissions.sanitize(body.overrides) or None
        if body.permissions is not None and body.overrides is None:
            # 旧字段：期望的最终图 → 相对当前快照换算 diff
            base = permissions.normalize(u.template_perms) if u.template_perms is not None \
                else permissions.effective(u.role, None)
            desired = permissions.effective_from_snapshot(base, permissions.sanitize(body.permissions))
            u.perm_overrides = permissions.diff_overrides(base, desired) or None
        after_eff = permissions.effective_for_user(u)
        _guard_high_risk_change(ident, before_eff, after_eff, u.username)
        if u.role != "admin":
            # 400 时依赖 get_db 关闭会话丢弃未提交改动，账号保持原样
            _combo_or_400(after_eff)

    if perm_change:
        _bump_token(u)   # 权限变了 → 吊销旧 token 即时生效
        _dual_write_legacy(u)
    _audit(db, u.id, "account_update", before, _acct_snapshot(u), ident["sub"])
    db.commit()
    tpls = _template_map(db)
    return _view(u, tpls.get(u.template_code or ""))


# ---------- 改密 / 停用 ----------
@router.put("/{username}/password")
def reset_password(username: str, body: PasswordReset, db: Session = Depends(get_db),
                   ident: dict = Depends(current_identity),
                   _: None = Depends(_write_gate)) -> dict:
    if len(body.password or "") < 6:
        raise HTTPException(400, "密码至少 6 位")
    u = _get(db, username)
    if u.role == "admin" and not _op_is_admin(ident):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "仅管理员可操作管理员账号")
    u.password_hash = hash_password(body.password)
    _bump_token(u)   # 改密即吊销旧 token
    # 审计只记"发生了改密"事件，绝不记口令/hash
    _audit(db, u.id, "account_reset_password", None, None, ident["sub"])
    db.commit()
    return {"username": username, "reset": True}


@router.put("/{username}/active")
def set_active(username: str, body: ActiveToggle, db: Session = Depends(get_db),
               ident: dict = Depends(current_identity),
               _: None = Depends(_write_gate)) -> dict:
    u = _get(db, username)
    _guard_touch(ident, u)
    if not body.is_active:
        if ident.get("sub") == u.username:
            raise HTTPException(400, "不能停用当前登录账号自己，请由另一位管理员操作")
        if u.role == "admin" and u.is_active and _active_admin_count(db) <= 1:
            raise HTTPException(400, "这是最后一个启用状态的管理员，不能停用——请先增设另一位管理员")
    before = _acct_snapshot(u)
    u.is_active = body.is_active
    if not body.is_active:
        _bump_token(u)   # 停用即吊销旧 token（立即踢线）
    _audit(db, u.id, "account_set_active", before, _acct_snapshot(u), ident["sub"])
    db.commit()
    return {"username": username, "is_active": u.is_active}


# ---------- 批量（单事务，全成或全败，预览指纹） ----------
_BULK_OPS = {"apply_template", "grant", "revoke", "reset_to_template"}


def _fingerprint(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()[:16]


def _plan_bulk(db: Session, ident: dict, body: BulkBody) -> tuple[list[dict], str]:
    """校验并生成批量计划（不写库）。任一账号非法 → 400 逐账号原因，零变化。
    返回 (plan_items, fingerprint)。plan_item 含 user 对象与变更后三件套。"""
    if body.operation not in _BULK_OPS:
        raise HTTPException(400, f"不支持的批量操作: {body.operation}")
    usernames = list(dict.fromkeys(body.usernames or []))
    if not usernames:
        raise HTTPException(400, "未选择任何账号")

    tpl: SysRoleTemplate | None = None
    if body.operation == "apply_template":
        if not body.template_code:
            raise HTTPException(400, "批量套用模板必须指定 template_code")
        tpl = _template_or_400(db, body.template_code, ident)
    keys: list[str] = []
    if body.operation in ("grant", "revoke"):
        keys = [k for k in (body.keys or [])]
        if not keys:
            raise HTTPException(400, "批量增加/取消权限必须指定权限键")
        bad = [k for k in keys if k not in permissions.ALL_KEYS]
        if bad:
            raise HTTPException(400, f"未知权限键: {', '.join(bad)}")
        if not _op_is_admin(ident) and set(keys) & permissions.HIGH_RISK_KEYS:
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                "批量操作包含账号管理高风险权限，仅管理员可执行")

    users = {u.username: u for u in db.execute(
        select(SysUser).where(SysUser.username.in_(usernames))).scalars().all()}
    tpls = _template_map(db)

    errors: list[dict] = []
    plan: list[dict] = []
    for name in usernames:
        u = users.get(name)
        if u is None:
            errors.append({"username": name, "reason": "账号不存在"})
            continue
        if u.username == "admin":
            errors.append({"username": name, "reason": "内置 admin 为系统账号，不参与批量操作"})
            continue
        if u.role == "admin":
            errors.append({"username": name, "reason": "管理员账号不参与批量操作，请单独设置"})
            continue

        before_eff = permissions.effective_for_user(u)
        base = permissions.normalize(u.template_perms) if u.template_perms is not None \
            else permissions.effective(u.role, None)

        if body.operation == "apply_template":
            new_code, new_ver = tpl.code, tpl.version
            new_base = permissions.normalize(tpl.permissions)
            new_over: dict = {}
            new_role = tpl.base_role
        elif body.operation == "reset_to_template":
            cur = tpls.get(u.template_code or "")
            if cur is None:
                errors.append({"username": name, "reason": "该账号未关联职位模板，无法恢复默认"})
                continue
            new_code, new_ver = cur.code, u.template_version or cur.version
            new_base = base
            new_over = {}
            new_role = u.role
        else:   # grant / revoke
            desired = dict(before_eff)
            for k in keys:
                desired[k] = body.operation == "grant"
            new_code, new_ver = u.template_code, u.template_version
            new_base = base
            new_over = permissions.diff_overrides(base, desired)
            new_role = u.role

        after_eff = permissions.effective_from_snapshot(new_base, new_over)
        hr = {k for k in permissions.HIGH_RISK_KEYS
              if bool(before_eff.get(k)) != bool(after_eff.get(k))}
        if hr and not _op_is_admin(ident):
            errors.append({"username": name, "reason": "涉及账号管理高风险权限，仅管理员可执行"})
            continue
        if hr and ident.get("sub") == name and any(
                bool(before_eff.get(k)) and not bool(after_eff.get(k)) for k in hr):
            errors.append({"username": name, "reason": "不能撤销当前登录账号自己的账号管理权限"})
            continue
        combo = permissions.combo_errors(after_eff)
        if combo:
            errors.append({"username": name, "reason": "；".join(combo)})
            continue

        changed = [{"key": k, "from": bool(before_eff.get(k)), "to": bool(after_eff.get(k)),
                    "label": permissions.LABELS.get(k, k)}
                   for k in permissions.ALL_KEYS
                   if bool(before_eff.get(k)) != bool(after_eff.get(k))]
        plan.append({
            "user": u, "before_eff": before_eff, "after_eff": after_eff,
            "new_code": new_code, "new_ver": new_ver, "new_base": new_base,
            "new_over": new_over, "new_role": new_role, "changed": changed,
        })

    if errors:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, {
            "message": "部分账号不满足条件，本次批量整体未执行（全成功或全失败）",
            "errors": errors,
        })
    fp = _fingerprint({
        "op": body.operation, "template_code": body.template_code,
        "keys": sorted(keys),
        "accounts": [{"u": p["user"].username, "after": p["after_eff"]} for p in plan],
    })
    return plan, fp


@router.post("/bulk")
def bulk_update(body: BulkBody, db: Session = Depends(get_db),
                ident: dict = Depends(current_identity),
                _: None = Depends(_write_gate)) -> dict:
    plan, fp = _plan_bulk(db, ident, body)
    preview = [{
        "username": p["user"].username, "display_name": p["user"].display_name,
        "role_before": p["user"].role, "role_after": p["new_role"],
        "template_before": p["user"].template_code, "template_after": p["new_code"],
        "changed_keys": p["changed"], "will_relogin": bool(p["changed"]) or p["new_role"] != p["user"].role,
    } for p in plan]
    if body.dry_run:
        return {"dry_run": True, "fingerprint": fp, "affected": len(plan),
                "changed": sum(1 for p in plan if p["changed"]), "preview": preview}

    if body.fingerprint != fp:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "账号或模板在预览后被他人修改过，本次批量整体未执行——请重新预览确认")
    op_label = {"apply_template": f"套用模板 {body.template_code}",
                "grant": f"增加权限 {','.join(body.keys or [])}",
                "revoke": f"取消权限 {','.join(body.keys or [])}",
                "reset_to_template": "恢复模板默认值"}[body.operation]
    for p in plan:
        u: SysUser = p["user"]
        before = _acct_snapshot(u)
        u.template_code, u.template_version = p["new_code"], p["new_ver"]
        u.template_perms = p["new_base"]
        u.perm_overrides = p["new_over"] or None
        u.role = p["new_role"]
        if p["changed"] or before["role"] != u.role:
            _bump_token(u)
        _dual_write_legacy(u)
        _audit(db, u.id, "account_bulk_update", before, _acct_snapshot(u), ident["sub"],
               reason=f"批量操作：{op_label}（{len(plan)} 个账号）")
    db.commit()   # 单事务：到这里才落库，前面任何 raise 都零变化
    return {"dry_run": False, "applied": len(plan),
            "results": [{"username": p["user"].username, "ok": True,
                         "changed_keys": len(p["changed"])} for p in plan]}


# ---------- 活动 ----------
@router.get("/{username}/activity")
def activity(username: str, limit: int = 50, db: Session = Depends(get_db),
             _: None = Depends(_read_gate)) -> dict:
    u = _get(db, username)
    n = min(max(limit, 1), 200)
    rows = db.execute(
        select(SysAccessLog).where(SysAccessLog.username == username)
        .order_by(desc(SysAccessLog.id)).limit(n)).scalars().all()
    total = db.scalar(select(func.count()).select_from(SysAccessLog)
                      .where(SysAccessLog.username == username))
    # 该账号被谁改过（建号/改权/改密/停用/批量）——entity_type='sys_user' 按 user_id 取
    changes = db.execute(
        select(SysAuditLog).where(SysAuditLog.entity_type == "sys_user",
                                  SysAuditLog.entity_id == u.id)
        .order_by(desc(SysAuditLog.id)).limit(50)).scalars().all()
    return {
        "username": username, "last_login_at": u.last_login_at, "total_actions": total or 0,
        "recent": [{"action": r.action, "resource": r.resource, "ip": r.ip_address,
                    "at": r.created_at} for r in rows],
        "changes": [{"action": c.action, "by": c.operated_by, "at": c.operated_at,
                     "reason": c.reason, "before": c.before_json, "after": c.after_json}
                    for c in changes],
    }

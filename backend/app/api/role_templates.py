"""职位模板管理（权限中心 v2）：CRUD / 复制 / 停用 / 使用账号 / 同步。

模板语义=复制快照（设计 §1.1/§1.2）：
- 「仅保存模板」（PUT）：version+1，不动任何账号——已套用账号仍持旧快照，
  列表里显示"模板已更新，账号未同步"。
- 「保存并同步账号」（PUT 后调 /sync）：dry_run 预览逐账号 diff + 指纹 →
  确认执行。同步=替换账号快照，保留（或显式清除）个别调整。

保护：内置 5 模板不可删不可停用；admin 模板完全锁定（不可编辑/套用/同步）；
乐观锁 version 防两个管理员互相覆盖；非 admin 操作者不能保存含高风险键的模板；
模板权限组合过 combo_errors（存不进非法组合，套用/同步自然安全）。
审计 → sys_audit_log(entity_type='sys_role_template')。
"""
import re
import secrets

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import permissions
from app.api.accounts import (
    _acct_snapshot,
    _audit,
    _bump_token,
    _dual_write_legacy,
    _fingerprint,
    _read_gate,
)
from app.auth import current_identity
from app.db import get_db
from app.models.system import SysAuditLog, SysRoleTemplate, SysUser
from app.security import require_action

router = APIRouter(prefix="/role-templates", tags=["role-templates"])

_write_gate = require_action("action_account_manage")
_BASE_ROLES = ["boss", "sales", "purchaser", "readonly"]
_CODE_RE = re.compile(r"^[a-z0-9_-]{2,64}$")


class TemplateCreate(BaseModel):
    name: str
    description: str | None = None
    base_role: str = "readonly"
    code: str | None = None          # 缺省自动生成 tpl-xxxxxx
    permissions: dict | None = None  # 缺省全 False；或从 copy_from 复制
    copy_from: str | None = None     # 复制既有模板


class TemplateUpdate(BaseModel):
    version: int                     # 乐观锁：必带当前版本
    name: str | None = None
    description: str | None = None
    base_role: str | None = None     # 仅自定义模板可改
    permissions: dict | None = None


class SyncBody(BaseModel):
    usernames: list[str] | None = None   # 缺省=该模板全部账号
    clear_overrides: bool = False        # true=同步时一并清除个别调整
    dry_run: bool = True
    fingerprint: str | None = None


def _taudit(db: Session, tpl_id: int, action: str, before: dict | None, after: dict | None,
            operated_by: str | None, reason: str | None = None) -> None:
    db.add(SysAuditLog(entity_type="sys_role_template", entity_id=tpl_id, action=action,
                       before_json=before, after_json=after,
                       operated_by=operated_by, reason=reason))


def _tpl_snapshot(t: SysRoleTemplate) -> dict:
    return {"code": t.code, "name": t.name, "description": t.description,
            "base_role": t.base_role, "permissions": t.permissions,
            "is_active": t.is_active, "version": t.version}


def _get(db: Session, code: str) -> SysRoleTemplate:
    t = db.scalar(select(SysRoleTemplate).where(SysRoleTemplate.code == code))
    if t is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"职位模板不存在: {code}")
    return t


def _op_is_admin(ident: dict) -> bool:
    return ident.get("role") == "admin"


def _guard_perms_payload(ident: dict, perms: dict) -> dict[str, bool]:
    """模板权限入库前统一处理：规范化 + 组合校验 + 高风险键守护。
    含高风险键=True 的模板等于"可批发的提权钥匙"，仅 admin 操作者可保存。"""
    normalized = permissions.normalize(permissions.sanitize(perms))
    errs = permissions.combo_errors(normalized)
    if errs:
        raise HTTPException(400, "；".join(errs))
    if not _op_is_admin(ident) and any(normalized.get(k) for k in permissions.HIGH_RISK_KEYS):
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "含账号管理高风险权限的模板仅管理员可保存")
    return normalized


def _usage(db: Session, code: str) -> dict:
    total = db.scalar(select(func.count()).select_from(SysUser)
                      .where(SysUser.template_code == code)) or 0
    active = db.scalar(select(func.count()).select_from(SysUser)
                       .where(SysUser.template_code == code, SysUser.is_active.is_(True))) or 0
    return {"usage_count": total, "usage_active": active}


def _tpl_view(db: Session, t: SysRoleTemplate) -> dict:
    normalized = permissions.normalize(t.permissions)
    return {
        "code": t.code, "name": t.name, "description": t.description,
        "base_role": t.base_role, "permissions": normalized,
        "permission_combo_errors": permissions.combo_errors(normalized),
        "is_system": t.is_system, "is_active": t.is_active, "version": t.version,
        "locked": t.code == "admin",
        "created_by": t.created_by, "created_at": t.created_at,
        "updated_by": t.updated_by, "updated_at": t.updated_at,
        **_usage(db, t.code),
    }


@router.get("")
def list_templates(include_archived: bool = True, db: Session = Depends(get_db),
                   _: None = Depends(_read_gate)) -> list[dict]:
    q = select(SysRoleTemplate).order_by(SysRoleTemplate.is_system.desc(), SysRoleTemplate.id)
    if not include_archived:
        q = q.where(SysRoleTemplate.is_active.is_(True))
    return [_tpl_view(db, t) for t in db.execute(q).scalars().all()]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_template(body: TemplateCreate, db: Session = Depends(get_db),
                    ident: dict = Depends(current_identity),
                    _: None = Depends(_write_gate)) -> dict:
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "模板名称不能为空")
    if body.base_role not in _BASE_ROLES:
        raise HTTPException(400, f"基础角色必须是 {'/'.join(_BASE_ROLES)} 之一（管理员不经模板产生）")
    code = (body.code or f"tpl-{secrets.token_hex(3)}").strip().lower()
    if not _CODE_RE.match(code):
        raise HTTPException(400, "模板编码只能用小写字母/数字/中划线/下划线（2-64 位）")
    if code == "admin":
        raise HTTPException(400, "不能使用保留编码 admin")
    if db.scalar(select(SysRoleTemplate).where(SysRoleTemplate.code == code)):
        raise HTTPException(409, f"模板编码已存在: {code}")

    perms = body.permissions
    if body.copy_from:
        src = _get(db, body.copy_from)
        perms = dict(src.permissions)
        if body.permissions:
            perms.update(permissions.sanitize(body.permissions))
    normalized = _guard_perms_payload(ident, perms or {})

    t = SysRoleTemplate(code=code, name=name, description=body.description,
                        base_role=body.base_role, permissions=normalized,
                        is_system=False, is_active=True, version=1,
                        created_by=ident["sub"])
    db.add(t)
    db.flush()
    _taudit(db, t.id, "template_create", None, _tpl_snapshot(t), ident["sub"],
            reason=f"复制自 {body.copy_from}" if body.copy_from else None)
    db.commit()
    return _tpl_view(db, t)


@router.put("/{code}")
def update_template(code: str, body: TemplateUpdate, db: Session = Depends(get_db),
                    ident: dict = Depends(current_identity),
                    _: None = Depends(_write_gate)) -> dict:
    """「仅保存模板」：version+1，不动任何账号。要让已有账号跟上 → 另调 /sync。"""
    t = _get(db, code)
    if t.code == "admin":
        raise HTTPException(400, "管理员模板为锁定模板，不可编辑")
    if body.version != t.version:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"模板已被他人改到 v{t.version}（你基于 v{body.version} 编辑）——请刷新后重做")
    before = _tpl_snapshot(t)
    if body.name is not None:
        if not body.name.strip():
            raise HTTPException(400, "模板名称不能为空")
        t.name = body.name.strip()
    if body.description is not None:
        t.description = body.description
    if body.base_role is not None and body.base_role != t.base_role:
        if t.is_system:
            raise HTTPException(400, "内置模板的基础角色不可修改")
        if body.base_role not in _BASE_ROLES:
            raise HTTPException(400, f"基础角色必须是 {'/'.join(_BASE_ROLES)} 之一")
        t.base_role = body.base_role
    if body.permissions is not None:
        t.permissions = _guard_perms_payload(ident, body.permissions)
    t.version += 1
    t.updated_by = ident["sub"]
    t.updated_at = func.now()
    _taudit(db, t.id, "template_update", before, _tpl_snapshot(t), ident["sub"],
              reason="仅保存模板（未同步账号）")
    db.commit()
    db.refresh(t)
    return _tpl_view(db, t)


@router.post("/{code}/archive")
def archive_template(code: str, db: Session = Depends(get_db),
                     ident: dict = Depends(current_identity),
                     _: None = Depends(_write_gate)) -> dict:
    t = _get(db, code)
    if t.is_system:
        raise HTTPException(400, "内置模板不可停用（它是角色兜底口径）")
    if not t.is_active:
        return _tpl_view(db, t)
    before = _tpl_snapshot(t)
    t.is_active = False
    t.version += 1
    t.updated_by, t.updated_at = ident["sub"], func.now()
    _taudit(db, t.id, "template_archive", before, _tpl_snapshot(t), ident["sub"],
              reason="停用：不可再套用/同步；已套用账号不受影响")
    db.commit()
    db.refresh(t)
    return _tpl_view(db, t)


@router.post("/{code}/restore")
def restore_template(code: str, db: Session = Depends(get_db),
                     ident: dict = Depends(current_identity),
                     _: None = Depends(_write_gate)) -> dict:
    t = _get(db, code)
    if t.is_active:
        return _tpl_view(db, t)
    before = _tpl_snapshot(t)
    t.is_active = True
    t.version += 1
    t.updated_by, t.updated_at = ident["sub"], func.now()
    _taudit(db, t.id, "template_restore", before, _tpl_snapshot(t), ident["sub"])
    db.commit()
    db.refresh(t)
    return _tpl_view(db, t)


@router.get("/{code}/accounts")
def template_accounts(code: str, db: Session = Depends(get_db),
                      _: None = Depends(_read_gate)) -> dict:
    t = _get(db, code)
    users = db.execute(select(SysUser).where(SysUser.template_code == code)
                       .order_by(SysUser.id)).scalars().all()
    return {
        "code": t.code, "name": t.name, "version": t.version,
        "accounts": [{
            "username": u.username, "display_name": u.display_name, "role": u.role,
            "is_active": u.is_active, "template_version": u.template_version,
            "stale": bool(u.template_version is not None and t.version > u.template_version
                          and u.role != "admin"),
            "override_count": len(u.perm_overrides or {}),
        } for u in users],
    }


@router.post("/{code}/sync")
def sync_template(code: str, body: SyncBody, db: Session = Depends(get_db),
                  ident: dict = Depends(current_identity),
                  _: None = Depends(_write_gate)) -> dict:
    """「保存并同步账号」的同步半场：把模板当前权限刷进已套用账号的快照。
    dry_run 预览逐账号 diff → 带指纹执行。全成或全败：任一账号同步后组合非法
    （个别调整与新模板相抵触）→ 整体 400。"""
    t = _get(db, code)
    if t.code == "admin":
        raise HTTPException(400, "管理员模板为锁定模板，无同步概念")
    if not t.is_active:
        raise HTTPException(400, f"模板「{t.name}」已停用，不能同步")
    if not _op_is_admin(ident) and any(
            permissions.normalize(t.permissions).get(k) for k in permissions.HIGH_RISK_KEYS):
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            f"模板「{t.name}」含账号管理高风险权限，仅管理员可同步")

    q = select(SysUser).where(SysUser.template_code == code, SysUser.role != "admin",
                              SysUser.username != "admin").order_by(SysUser.id)
    users = db.execute(q).scalars().all()
    if body.usernames is not None:
        wanted = set(body.usernames)
        missing = wanted - {u.username for u in users}
        if missing:
            raise HTTPException(400, f"这些账号不使用该模板或不可同步: {', '.join(sorted(missing))}")
        users = [u for u in users if u.username in wanted]

    new_base = permissions.normalize(t.permissions)
    errors: list[dict] = []
    plan: list[dict] = []
    for u in users:
        before_eff = permissions.effective_for_user(u)
        new_over = {} if body.clear_overrides else dict(u.perm_overrides or {})
        # 覆盖里与新快照相同的键收敛掉（保持稀疏）
        new_over = {k: v for k, v in new_over.items()
                    if k in permissions.ALL_KEYS and bool(v) != bool(new_base.get(k, False))}
        after_eff = permissions.effective_from_snapshot(new_base, new_over)
        combo = permissions.combo_errors(after_eff)
        if combo:
            errors.append({"username": u.username,
                           "reason": "同步后组合非法：" + "；".join(combo)
                                     + "（可勾选「同步时清除个别调整」或先改模板）"})
            continue
        changed = [{"key": k, "from": bool(before_eff.get(k)), "to": bool(after_eff.get(k)),
                    "label": permissions.LABELS.get(k, k)}
                   for k in permissions.ALL_KEYS
                   if bool(before_eff.get(k)) != bool(after_eff.get(k))]
        plan.append({"user": u, "after_eff": after_eff, "new_over": new_over, "changed": changed})

    if errors:
        raise HTTPException(400, {
            "message": "部分账号同步后权限组合非法，本次同步整体未执行（全成功或全失败）",
            "errors": errors,
        })

    fp = _fingerprint({"op": "template_sync", "code": code, "tpl_version": t.version,
                       "clear": body.clear_overrides,
                       "accounts": [{"u": p["user"].username, "after": p["after_eff"]}
                                    for p in plan]})
    preview = [{
        "username": p["user"].username, "display_name": p["user"].display_name,
        "from_version": p["user"].template_version, "to_version": t.version,
        "changed_keys": p["changed"], "will_relogin": bool(p["changed"]),
    } for p in plan]
    if body.dry_run:
        return {"dry_run": True, "fingerprint": fp, "template_version": t.version,
                "affected": len(plan), "changed": sum(1 for p in plan if p["changed"]),
                "preview": preview}

    if body.fingerprint != fp:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "预览之后模板或账号被他人修改过，本次同步整体未执行——请重新预览确认")
    for p in plan:
        u: SysUser = p["user"]
        before = _acct_snapshot(u)
        u.template_version = t.version
        u.template_perms = new_base
        u.perm_overrides = p["new_over"] or None
        if p["changed"]:
            _bump_token(u)
        _dual_write_legacy(u)
        _audit(db, u.id, "account_template_sync", before, _acct_snapshot(u), ident["sub"],
               reason=f"模板「{t.name}」同步到 v{t.version}"
                      + ("（清除个别调整）" if body.clear_overrides else "（保留个别调整）"))
    _taudit(db, t.id, "template_sync", None,
              {"synced": len(plan), "to_version": t.version,
               "clear_overrides": body.clear_overrides}, ident["sub"])
    db.commit()
    return {"dry_run": False, "applied": len(plan), "template_version": t.version,
            "results": [{"username": p["user"].username, "ok": True,
                         "changed_keys": len(p["changed"])} for p in plan]}

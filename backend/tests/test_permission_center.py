"""权限中心 v2：模板持久化 / 快照语义 / 批量原子性 / 防锁死 / 高风险守护 / 迁移对账。

对应任务书第九章衡量指标逐条落测（docs/权限中心v2-编码前设计方案 §7）。
"""
import importlib.util
import os

import pytest
from fastapi.testclient import TestClient

from app import permissions
from app.auth import hash_password
from app.db import SessionLocal
from app.main import app
from app.models.system import SysAuditLog, SysRoleTemplate, SysUser


# ---------- 夹具 ----------
@pytest.fixture()
def admin_client(db):
    db.add(SysUser(username="admin", role="admin", display_name="管理员",
                   password_hash=hash_password("adminpw")))
    db.commit()
    c = TestClient(app)
    tok = c.post("/api/auth/login", json={"username": "admin", "password": "adminpw"}).json()["token"]
    c.headers.update({"Authorization": f"Bearer {tok}"})
    return c


def _login(username, password):
    return TestClient(app).post("/api/auth/login", json={"username": username, "password": password})


def _client_as(username, password) -> TestClient:
    c = TestClient(app)
    tok = _login(username, password).json()["token"]
    c.headers.update({"Authorization": f"Bearer {tok}"})
    return c


def _mk_account(client, username, template="sales", **kw):
    r = client.post("/api/accounts", json={"username": username, "password": "pw123456",
                                           "template_code": template, **kw})
    assert r.status_code == 201, r.text
    return r.json()


# ---------- 1. 模板持久化（重启存活） ----------
def test_template_persists_across_sessions(db, admin_client):
    r = admin_client.post("/api/role-templates", json={
        "name": "仓库管理员", "base_role": "readonly", "copy_from": "readonly",
        "description": "看库存与型号"})
    assert r.status_code == 201, r.text
    code = r.json()["code"]
    # 全新 Session（等价服务重启后重读数据库）
    s = SessionLocal()
    try:
        row = s.query(SysRoleTemplate).filter_by(code=code).one()
        assert row.name == "仓库管理员" and row.is_system is False
        assert row.permissions["page_inventory"] is True
    finally:
        s.close()


# ---------- 2. 模板 CRUD / 乐观锁 / 停用保护 ----------
def test_template_crud_optimistic_lock_and_archive(db, admin_client):
    c = admin_client
    r = c.post("/api/role-templates", json={"name": "T1", "base_role": "sales"})
    code, ver = r.json()["code"], r.json()["version"]
    # 乐观锁：错版本 409，不落库
    assert c.put(f"/api/role-templates/{code}",
                 json={"version": ver + 5, "name": "T1x"}).status_code == 409
    r2 = c.put(f"/api/role-templates/{code}",
               json={"version": ver, "name": "T1改", "permissions": {"page_parts": True}})
    assert r2.status_code == 200 and r2.json()["version"] == ver + 1
    assert r2.json()["permissions"]["page_parts"] is True
    # 内置不可停用；自定义可停用后不可套用
    assert c.post("/api/role-templates/sales/archive").status_code == 400
    assert c.post(f"/api/role-templates/{code}/archive").status_code == 200
    r3 = c.post("/api/accounts", json={"username": "u1", "password": "pw123456",
                                       "template_code": code})
    assert r3.status_code == 400 and "停用" in r3.json()["detail"]
    assert c.post(f"/api/role-templates/{code}/restore").status_code == 200


def test_admin_template_locked(db, admin_client):
    c = admin_client
    assert c.put("/api/role-templates/admin", json={"version": 1, "name": "x"}).status_code == 400
    assert c.post("/api/role-templates/admin/archive").status_code == 400
    assert c.post("/api/role-templates/admin/sync", json={}).status_code == 400
    r = c.post("/api/accounts", json={"username": "u2", "password": "pw123456",
                                      "template_code": "admin"})
    assert r.status_code == 400 and "锁定" in r.json()["detail"]
    # 模板非法组合存不进去（动作无数据依赖）
    r = c.post("/api/role-templates", json={
        "name": "坏模板", "base_role": "sales",
        "permissions": {"action_pool_set_policy": True, "data_pool_price_governance": False}})
    assert r.status_code == 400


# ---------- 3. 仅保存模板：不静默改账号 ----------
def test_edit_template_does_not_change_accounts(db, admin_client):
    c = admin_client
    _mk_account(c, "liu", template="sales")
    before = next(u for u in c.get("/api/accounts").json() if u["username"] == "liu")
    tpl = next(t for t in c.get("/api/role-templates").json() if t["code"] == "sales")
    # 翻转一个键（sales 模板 page_governance=False → True）
    r = c.put("/api/role-templates/sales", json={
        "version": tpl["version"],
        "permissions": {**tpl["permissions"], "page_governance": True}})
    assert r.status_code == 200
    after = next(u for u in c.get("/api/accounts").json() if u["username"] == "liu")
    assert after["permissions"] == before["permissions"]          # 有效权限逐键不变
    assert after["template_stale"] is True                        # 但显示"模板已更新未同步"
    # 该用户重新登录权限也不变（快照语义，不吃现行模板）
    assert _login("liu", "pw123456").json()["permissions"]["page_governance"] is False


# ---------- 4. 同步：预览=实际、指纹防错、覆盖保留/清除 ----------
def test_sync_preview_matches_execution(db, admin_client):
    c = admin_client
    _mk_account(c, "s1", template="sales")
    _mk_account(c, "s2", template="sales",
                overrides={"page_import": True})                  # s2 带个别调整
    tpl = next(t for t in c.get("/api/role-templates").json() if t["code"] == "sales")
    c.put("/api/role-templates/sales", json={
        "version": tpl["version"],
        "permissions": {**tpl["permissions"], "page_governance": True}})
    # 预览
    r = c.post("/api/role-templates/sales/sync", json={"dry_run": True})
    body = r.json()
    assert r.status_code == 200 and body["dry_run"] is True
    fp = body["fingerprint"]
    previewed = {p["username"]: {ck["key"]: ck["to"] for ck in p["changed_keys"]}
                 for p in body["preview"]}
    assert previewed["s1"]["page_governance"] is True
    # 错指纹 → 409 整体不动
    assert c.post("/api/role-templates/sales/sync",
                  json={"dry_run": False, "fingerprint": "bogus"}).status_code == 409
    u = next(x for x in c.get("/api/accounts").json() if x["username"] == "s1")
    assert u["permissions"]["page_governance"] is False
    # 正确指纹 → 执行，实际结果与预览一致
    r2 = c.post("/api/role-templates/sales/sync", json={"dry_run": False, "fingerprint": fp})
    assert r2.status_code == 200 and r2.json()["applied"] == 2
    rows = {x["username"]: x for x in c.get("/api/accounts").json()}
    assert rows["s1"]["permissions"]["page_governance"] is True
    assert rows["s1"]["template_stale"] is False
    assert rows["s2"]["permissions"]["page_import"] is True       # 个别调整默认保留
    assert rows["s2"]["overrides"] == {"page_import": True}
    # clear_overrides=真：一并清除个别调整
    tpl2 = next(t for t in c.get("/api/role-templates").json() if t["code"] == "sales")
    c.put("/api/role-templates/sales", json={"version": tpl2["version"], "description": "again"})
    pv = c.post("/api/role-templates/sales/sync",
                json={"dry_run": True, "clear_overrides": True}).json()
    r3 = c.post("/api/role-templates/sales/sync",
                json={"dry_run": False, "clear_overrides": True, "fingerprint": pv["fingerprint"]})
    assert r3.status_code == 200
    s2 = next(x for x in c.get("/api/accounts").json() if x["username"] == "s2")
    assert s2["permissions"]["page_import"] is False and s2["overrides"] == {}


# ---------- 5. 批量：全成或全败、指纹、审计、token 失效 ----------
def test_bulk_all_or_nothing(db, admin_client):
    c = admin_client
    _mk_account(c, "b1", template="sales")
    _mk_account(c, "b2", template="sales")
    # 含不存在账号 → 整体 400，逐账号原因，零变化
    r = c.post("/api/accounts/bulk", json={
        "usernames": ["b1", "b2", "ghost"], "operation": "grant",
        "keys": ["page_governance"], "dry_run": False})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert any(e["username"] == "ghost" for e in detail["errors"])
    assert all(not u["permissions"]["page_governance"]
               for u in c.get("/api/accounts").json() if u["username"] in ("b1", "b2"))
    # 组合非法（动作缺页面依赖：授账号管理却不给账号中心页）→ 整体拒绝
    r = c.post("/api/accounts/bulk", json={
        "usernames": ["b1", "b2"], "operation": "grant",
        "keys": ["action_account_manage"], "dry_run": False})
    assert r.status_code == 400
    assert all(not u["permissions"]["action_account_manage"]
               for u in c.get("/api/accounts").json() if u["username"].startswith("b"))
    # 不带指纹直接执行 → 409（必须先预览）
    r = c.post("/api/accounts/bulk", json={
        "usernames": ["b1", "b2"], "operation": "grant",
        "keys": ["action_pool_set_policy"], "dry_run": False})
    assert r.status_code == 409
    # 合法（sales 模板本就带 data_pool_price_governance）→ dry-run 预览 + 指纹执行全部生效
    pv = c.post("/api/accounts/bulk", json={
        "usernames": ["b1", "b2"], "operation": "grant",
        "keys": ["action_pool_set_policy"]}).json()
    assert pv["dry_run"] is True and pv["affected"] == 2
    r = c.post("/api/accounts/bulk", json={
        "usernames": ["b1", "b2"], "operation": "grant",
        "keys": ["action_pool_set_policy"],
        "dry_run": False, "fingerprint": pv["fingerprint"]})
    assert r.status_code == 200 and r.json()["applied"] == 2
    assert all(u["permissions"]["action_pool_set_policy"]
               for u in c.get("/api/accounts").json() if u["username"].startswith("b"))


def test_bulk_fingerprint_stale_409(db, admin_client):
    c = admin_client
    _mk_account(c, "f1", template="sales")
    pv = c.post("/api/accounts/bulk", json={
        "usernames": ["f1"], "operation": "grant", "keys": ["page_governance"]}).json()
    # 预览后有人单独改了 f1 → 指纹失配 409
    c.put("/api/accounts/f1", json={"overrides": {"page_import": True}})
    r = c.post("/api/accounts/bulk", json={
        "usernames": ["f1"], "operation": "grant", "keys": ["page_governance"],
        "dry_run": False, "fingerprint": pv["fingerprint"]})
    assert r.status_code == 409
    u = next(x for x in c.get("/api/accounts").json() if x["username"] == "f1")
    assert u["permissions"]["page_governance"] is False


def test_bulk_apply_template_and_reset(db, admin_client):
    c = admin_client
    _mk_account(c, "t1", template="sales", overrides={"page_import": True})
    # 套用采购模板：角色跟随 base_role、覆盖清空
    pv = c.post("/api/accounts/bulk", json={
        "usernames": ["t1"], "operation": "apply_template", "template_code": "purchaser"}).json()
    r = c.post("/api/accounts/bulk", json={
        "usernames": ["t1"], "operation": "apply_template", "template_code": "purchaser",
        "dry_run": False, "fingerprint": pv["fingerprint"]})
    assert r.status_code == 200
    u = next(x for x in c.get("/api/accounts").json() if x["username"] == "t1")
    assert u["role"] == "purchaser" and u["template_code"] == "purchaser"
    assert u["overrides"] == {} and u["permissions"]["page_master_data"] is True
    # 个别调整后恢复模板默认
    c.put("/api/accounts/t1", json={"overrides": {"page_governance": True}})
    pv = c.post("/api/accounts/bulk", json={
        "usernames": ["t1"], "operation": "reset_to_template"}).json()
    r = c.post("/api/accounts/bulk", json={
        "usernames": ["t1"], "operation": "reset_to_template",
        "dry_run": False, "fingerprint": pv["fingerprint"]})
    assert r.status_code == 200
    u = next(x for x in c.get("/api/accounts").json() if x["username"] == "t1")
    assert u["overrides"] == {} and u["permissions"]["page_governance"] is False


def test_bulk_rejects_admin_accounts(db, admin_client):
    c = admin_client
    r = c.post("/api/accounts/bulk", json={
        "usernames": ["admin"], "operation": "grant", "keys": ["page_parts"]})
    assert r.status_code == 400
    assert "admin" in str(r.json()["detail"])


def test_token_invalidated_after_bulk(db, admin_client):
    c = admin_client
    _mk_account(c, "tk1", template="sales")
    user_client = _client_as("tk1", "pw123456")
    assert user_client.get("/api/accounts").status_code == 403   # 无 page_accounts
    pv = c.post("/api/accounts/bulk", json={
        "usernames": ["tk1"], "operation": "revoke", "keys": ["page_chat"]}).json()
    c.post("/api/accounts/bulk", json={
        "usernames": ["tk1"], "operation": "revoke", "keys": ["page_chat"],
        "dry_run": False, "fingerprint": pv["fingerprint"]})
    # 旧 token 立即失效（tv 递增）
    assert user_client.get("/api/accounts").status_code == 401
    # 重新登录取到新权限
    assert _login("tk1", "pw123456").json()["permissions"]["page_chat"] is False


# ---------- 6. 防锁死 ----------
def test_last_admin_protection(db):
    # 场景：sys_user 里唯一 admin 角色账号 + 共享口令 admin（无实名行）操作
    db.add(SysUser(username="onlyadm", role="admin", password_hash=hash_password("pw123456")))
    db.commit()
    ghost = TestClient(app)
    tok = ghost.post("/api/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
    ghost.headers.update({"Authorization": f"Bearer {tok}"})
    assert ghost.put("/api/accounts/onlyadm/active",
                     json={"is_active": False}).status_code == 400
    assert ghost.put("/api/accounts/onlyadm", json={"role": "sales"}).status_code == 400
    # 增设第二管理员后即可动第一个
    ghost.post("/api/accounts", json={"username": "adm2", "password": "pw123456", "role": "admin"})
    assert ghost.put("/api/accounts/onlyadm/active",
                     json={"is_active": False}).status_code == 200


def test_cannot_operate_self(db, admin_client):
    db.add(SysUser(username="adm2", role="admin", password_hash=hash_password("pw123456")))
    db.commit()
    me = _client_as("adm2", "pw123456")
    assert me.put("/api/accounts/adm2/active", json={"is_active": False}).status_code == 400
    assert me.put("/api/accounts/adm2", json={"role": "sales"}).status_code == 400


def test_builtin_admin_untouchable(db, admin_client):
    c = admin_client
    assert c.put("/api/accounts/admin/active", json={"is_active": False}).status_code == 400
    assert c.put("/api/accounts/admin", json={"role": "sales"}).status_code == 400
    assert c.put("/api/accounts/admin", json={"template_code": "sales"}).status_code == 400


# ---------- 7. 高风险键与委派边界 ----------
@pytest.fixture()
def delegate_client(db, admin_client):
    """非 admin 的账号管理代理：readonly 模板 + 两把账号管理钥匙（admin 授予）。"""
    _mk_account(admin_client, "deleg", template="readonly",
                overrides={"page_accounts": True, "action_account_manage": True})
    return _client_as("deleg", "pw123456")


def test_delegate_can_manage_normal_accounts(db, admin_client, delegate_client):
    d = delegate_client
    assert d.get("/api/accounts").status_code == 200
    r = d.post("/api/accounts", json={"username": "n1", "password": "pw123456",
                                      "template_code": "sales"})
    assert r.status_code == 201
    assert d.put("/api/accounts/n1", json={"overrides": {"page_import": True}}).status_code == 200


def test_delegate_cannot_escalate(db, admin_client, delegate_client):
    d = delegate_client
    _mk_account(admin_client, "n2", template="sales")
    # 授高风险键 → 403
    r = d.put("/api/accounts/n2", json={"overrides": {"page_accounts": True,
                                                      "action_account_manage": True}})
    assert r.status_code == 403
    # 升管理员 → 403
    assert d.put("/api/accounts/n2", json={"role": "admin"}).status_code == 403
    # 动管理员账号 → 403（admin 由 400 规则挡，再造一个实名管理员）
    db.add(SysUser(username="adm3", role="admin", password_hash=hash_password("pw123456")))
    db.commit()
    assert d.put("/api/accounts/adm3", json={"overrides": {}}).status_code == 403
    assert d.put("/api/accounts/adm3/password", json={"password": "hack1234"}).status_code == 403
    # 存含高风险键的模板 → 403
    r = d.post("/api/role-templates", json={
        "name": "提权模板", "base_role": "readonly",
        "permissions": {"page_accounts": True, "action_account_manage": True}})
    assert r.status_code == 403
    # 批量授高风险键 → 403
    r = d.post("/api/accounts/bulk", json={
        "usernames": ["n2"], "operation": "grant", "keys": ["action_account_manage"]})
    assert r.status_code == 403
    # 撤销自己的管理钥匙 → 400（防自锁）
    r = d.put("/api/accounts/deleg", json={"overrides": {"page_accounts": True,
                                                         "action_account_manage": False}})
    assert r.status_code in (400, 403)


def test_admin_template_highrisk_apply_guard(db, admin_client, delegate_client):
    # admin 造一个含高风险键的模板，代理不能套用它（套用=授予）
    r = admin_client.post("/api/role-templates", json={
        "name": "管理代理", "base_role": "readonly",
        "permissions": {"page_accounts": True, "action_account_manage": True,
                        "page_parts": True}})
    code = r.json()["code"]
    _mk_account(admin_client, "n3", template="sales")
    r = delegate_client.put("/api/accounts/n3", json={"template_code": code})
    assert r.status_code == 403


# ---------- 8. 受限账号直调 API ----------
def test_restricted_cannot_call_api(db, admin_client):
    _mk_account(admin_client, "ro", template="readonly")
    ro = _client_as("ro", "pw123456")
    assert ro.get("/api/accounts").status_code == 403
    assert ro.get("/api/accounts/_meta").status_code == 403
    assert ro.get("/api/role-templates").status_code == 403
    assert ro.post("/api/accounts", json={"username": "x", "password": "pw123456"}).status_code == 403
    assert ro.post("/api/accounts/bulk", json={"usernames": ["ro"], "operation": "grant",
                                               "keys": ["page_import"]}).status_code == 403
    assert ro.post("/api/role-templates", json={"name": "x", "base_role": "readonly"}).status_code == 403
    assert ro.put("/api/role-templates/sales", json={"version": 1}).status_code == 403
    assert ro.post("/api/role-templates/sales/sync", json={}).status_code == 403


# ---------- 9. 审计 ----------
def test_audit_trail_for_templates_and_bulk(db, admin_client):
    c = admin_client
    r = c.post("/api/role-templates", json={"name": "审计用", "base_role": "sales"})
    code = r.json()["code"]
    c.put(f"/api/role-templates/{code}", json={"version": 1, "name": "审计用2"})
    _mk_account(c, "au1", template="sales")
    pv = c.post("/api/accounts/bulk", json={
        "usernames": ["au1"], "operation": "grant", "keys": ["page_governance"]}).json()
    c.post("/api/accounts/bulk", json={
        "usernames": ["au1"], "operation": "grant", "keys": ["page_governance"],
        "dry_run": False, "fingerprint": pv["fingerprint"]})
    logs = db.query(SysAuditLog).all()
    actions = {(l.entity_type, l.action) for l in logs}
    assert ("sys_role_template", "template_create") in actions
    assert ("sys_role_template", "template_update") in actions
    assert ("sys_user", "account_bulk_update") in actions
    assert all(l.operated_by == "admin" for l in logs
               if l.action in ("template_create", "template_update", "account_bulk_update"))


# ---------- 10. 迁移对账（直接测迁移文件里的回填纯函数） ----------
def _load_migration():
    path = os.path.join(os.path.dirname(__file__), "..", "alembic", "versions",
                        "a3f8c1d9e5b2_permission_center_v2.py")
    spec = importlib.util.spec_from_file_location("mig_pcv2", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("role", ["boss", "sales", "purchaser", "readonly", "ghostrole"])
@pytest.mark.parametrize("custom", [
    None, {}, {"data_supplier": True}, {"page_governance": True, "data_profit": False},
    {"own_customers_only": False, "action_pool_manage": True,
     "data_pool_price_governance": True},
])
def test_migration_reconciliation(role, custom):
    """逐账号对账：回填出的 快照⊕覆盖 与旧口径 effective(role, custom) 逐键一致。"""
    mig = _load_migration()
    filled = mig._backfill_one(role, custom)
    new_eff = permissions.effective_from_snapshot(filled["template_perms"],
                                                  filled["perm_overrides"])
    old_eff = permissions.effective(role, custom)
    assert new_eff == old_eff


def test_migration_admin_short_circuit():
    mig = _load_migration()
    filled = mig._backfill_one("admin", {"data_supplier": False})

    class U:  # noqa: D401 — 轻量对象模拟 SysUser 属性
        role = "admin"
        permissions = {"data_supplier": False}
        template_perms = filled["template_perms"]
        perm_overrides = filled["perm_overrides"]

    # 新旧口径对 admin 都强制全开，自定义锁不住
    assert permissions.effective_for_user(U()) == permissions.effective("admin", U.permissions)


def test_frozen_templates_match_current_code():
    """冻结模板 = 编写时刻 effective(role, None)——若后来代码模板漂移，此测提醒补新迁移。"""
    mig = _load_migration()
    for role, frozen in mig.FROZEN_TEMPLATES.items():
        assert frozen == permissions.effective(role, None), f"角色 {role} 模板与迁移冻结值漂移"


# ---------- 11. 旧列双写（回滚保险） ----------
def test_dual_write_legacy_column(db, admin_client):
    c = admin_client
    _mk_account(c, "dw1", template="sales")
    c.put("/api/accounts/dw1", json={"overrides": {"page_import": True}})
    s = SessionLocal()
    try:
        u = s.query(SysUser).filter_by(username="dw1").one()
        # 旧列=完整有效图 → downgrade 后旧代码 effective(role, 完整图) 逐键等于它
        assert u.permissions == permissions.effective_for_user(u)
        assert permissions.effective(u.role, u.permissions) == permissions.effective_for_user(u)
    finally:
        s.close()


# ---------- 12. 元信息完整性（前端矩阵的地基） ----------
def test_meta_has_business_language(db, admin_client):
    m = admin_client.get("/api/accounts/_meta").json()
    assert {g["key"] for g in m["groups"]} == {"page", "data", "action", "row", "admin"}
    grouped = [k for g in m["groups"] for k in g["keys"]]
    assert sorted(grouped) == sorted(permissions.ALL_KEYS)        # 五组无遗漏无重复
    for k in permissions.ALL_KEYS:
        meta = m["meta"][k]
        for field in ("label", "summary", "can", "cannot", "typical", "sensitivity", "risk"):
            assert meta.get(field), f"{k} 缺业务语言字段 {field}"
    assert m["dependencies"]["action_data"]["action_pool_set_policy"] == "data_pool_price_governance"
    assert m["dependencies"]["action_page"]["action_account_manage"] == "page_accounts"
    assert {t["code"] for t in m["templates"]} >= {"admin", "boss", "sales", "purchaser", "readonly"}

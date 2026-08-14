"""权限中心 v2：模板持久化 / 快照语义 / 批量原子性 / 防锁死 / 高风险守护 / 迁移对账。

对应任务书第九章衡量指标逐条落测（docs/权限中心v2-编码前设计方案 §7）。
"""
import importlib.util
import os

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import permissions, security
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


def test_financial_dependency_blocks_account_template_bulk_and_sync_paths(db, admin_client):
    """data_profit → data_purchase_cost 必须覆盖权限中心 v2 的所有保存路径。"""
    c = admin_client

    # 建号：sales 模板原本成本/利润双开，只关成本会留下可反推组合。
    r = c.post("/api/accounts", json={
        "username": "dep-create", "password": "pw123456", "template_code": "sales",
        "overrides": {"data_purchase_cost": False},
    })
    assert r.status_code == 400 and "反推出采购成本" in r.json()["detail"]

    # 单账号更新：拒绝后原权限保持双开。
    _mk_account(c, "dep-update", template="sales")
    r = c.put("/api/accounts/dep-update", json={
        "overrides": {"data_purchase_cost": False},
    })
    assert r.status_code == 400
    row = next(x for x in c.get("/api/accounts").json() if x["username"] == "dep-update")
    assert row["permissions"]["data_purchase_cost"] is True
    assert row["permissions"]["data_profit"] is True

    # 模板新建与编辑：非法图都不能入库，编辑拒绝后版本和权限不变。
    r = c.post("/api/role-templates", json={
        "name": "成本反推坏模板", "base_role": "sales",
        "permissions": {"data_purchase_cost": False, "data_profit": True},
    })
    assert r.status_code == 400
    created = c.post("/api/role-templates", json={
        "name": "依赖测试模板", "base_role": "sales", "copy_from": "sales",
    }).json()
    bad = {**created["permissions"], "data_purchase_cost": False, "data_profit": True}
    r = c.put(f"/api/role-templates/{created['code']}", json={
        "version": created["version"], "permissions": bad,
    })
    assert r.status_code == 400
    unchanged = next(t for t in c.get("/api/role-templates").json()
                     if t["code"] == created["code"])
    assert unchanged["version"] == created["version"]
    assert unchanged["permissions"]["data_purchase_cost"] is True

    # 批量：从合法 sales 双开图批量关成本，整批在预览阶段拒绝且账号不变。
    r = c.post("/api/accounts/bulk", json={
        "usernames": ["dep-update"], "operation": "revoke",
        "keys": ["data_purchase_cost"], "dry_run": True,
    })
    assert r.status_code == 400
    row = next(x for x in c.get("/api/accounts").json() if x["username"] == "dep-update")
    assert row["permissions"]["data_purchase_cost"] is True

    # 同步：模拟上线前已落库的脏覆盖；保留覆盖同步时必须整体拒绝。
    dirty = db.query(SysUser).filter_by(username="dep-update").one()
    dirty.perm_overrides = {"data_purchase_cost": False}
    db.commit()
    r = c.post("/api/role-templates/sales/sync", json={"dry_run": True})
    assert r.status_code == 400
    assert "同步后组合非法" in str(r.json()["detail"])


def test_historical_invalid_combo_is_audited_and_runtime_fail_closed(db, admin_client):
    """存量脏账号不自动改库，但列表标红，登录/token/字段层都关闭利润。"""
    base = permissions.effective("sales", None)
    dirty = SysUser(
        username="legacy-infer", role="sales", display_name="历史脏账号",
        password_hash=hash_password("pw123456"), template_code="sales", template_version=1,
        template_perms=base, perm_overrides={"data_purchase_cost": False},
        permissions={**base, "data_purchase_cost": False},
    )
    db.add(dirty)
    db.add(SysRoleTemplate(
        code="legacy-bad-template", name="历史脏模板", base_role="sales",
        permissions={**base, "data_purchase_cost": False}, is_system=False,
        is_active=True, version=1, created_by="legacy",
    ))
    db.commit()

    account = next(x for x in admin_client.get("/api/accounts").json()
                   if x["username"] == "legacy-infer")
    assert account["permissions"]["data_profit"] is True       # 原始存量图可审计/可修
    assert account["permissions"]["data_purchase_cost"] is False
    assert account["runtime_permissions"]["data_profit"] is False
    assert account["permission_combo_errors"]
    template = next(x for x in admin_client.get("/api/role-templates").json()
                    if x["code"] == "legacy-bad-template")
    assert template["permission_combo_errors"]

    login = _login("legacy-infer", "pw123456")
    assert login.status_code == 200
    assert login.json()["permissions"]["data_purchase_cost"] is False
    assert login.json()["permissions"]["data_profit"] is False

    ctx = security.UserContext(
        user_id="legacy-infer", role="sales", is_authenticated=True,
        permissions={**base, "data_purchase_cost": False},
    )
    masked = security.apply_field_visibility(
        {"total_revenue": 100.0, "total_ex_tax": 60.0, "total_gross_profit": 40.0},
        ctx,
    )
    assert masked == {
        "total_revenue": 100.0, "total_ex_tax": None, "total_gross_profit": None,
    }


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
    actions = {(log.entity_type, log.action) for log in logs}
    assert ("sys_role_template", "template_create") in actions
    assert ("sys_role_template", "template_update") in actions
    assert ("sys_user", "account_bulk_update") in actions
    assert all(log.operated_by == "admin" for log in logs
               if log.action in ("template_create", "template_update", "account_bulk_update"))


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
    """历史迁移创建时已有的键逐项对账；后续新键由各自迁移显式补入。"""
    mig = _load_migration()
    filled = mig._backfill_one(role, custom)
    new_eff = permissions.effective_from_snapshot(filled["template_perms"],
                                                  filled["perm_overrides"])
    old_eff = permissions.effective(role, custom)
    assert {key: new_eff[key] for key in mig._KEYS} == {
        key: old_eff[key] for key in mig._KEYS
    }


def test_migration_admin_short_circuit():
    mig = _load_migration()
    filled = mig._backfill_one("admin", {"data_supplier": False})

    class U:  # noqa: D401 — 轻量对象模拟 SysUser 属性
        role = "admin"
        permissions = {"data_supplier": False}
        template_perms = filled["template_perms"]
        perm_overrides = filled["perm_overrides"]

    # 常规 admin 权限仍强制全开、自定义锁不住；后续新增的两个生产 Beta 页面
    # 没有出现在历史快照中，因此按账号白名单失败关闭。
    effective = permissions.effective_for_user(U())
    legacy_full = permissions.effective("admin", U.permissions)
    assert all(
        effective[key] == legacy_full[key]
        for key in permissions.ALL_KEYS
        if key not in permissions.ACCOUNT_SCOPED_BETA_PAGE_KEYS
    )
    assert all(effective[key] is False for key in permissions.ACCOUNT_SCOPED_BETA_PAGE_KEYS)


def test_frozen_templates_match_current_code():
    """历史冻结键保持原值；新权限键必须由后续迁移显式补入。"""
    mig = _load_migration()
    for role, frozen in mig.FROZEN_TEMPLATES.items():
        current = permissions.effective(role, None)
        assert {key: current[key] for key in frozen} == frozen, f"角色 {role} 历史模板键漂移"


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
    assert (
        m["dependencies"]["action_additional_page"]["action_maintenance_project_manage"]
        == "page_maintenance_beta"
    )
    assert m["dependencies"]["page_page"]["page_maintenance_beta"] == "page_maintenance"
    assert m["dependencies"]["data_data"]["data_profit"] == "data_purchase_cost"
    assert m["meta"]["page_maintenance_beta"]["label"] == "维保管理"
    assert m["meta"]["page_replenishment_beta"]["label"] == "补库申请"
    for key in ("page_maintenance_beta", "page_replenishment_beta"):
        visible_copy = " ".join(
            str(m["meta"][key][field])
            for field in ("label", "summary", "can", "cannot", "typical", "risk")
        )
        assert "Beta" not in visible_copy
        assert "试用" not in visible_copy
    assert {t["code"] for t in m["templates"]} >= {"admin", "boss", "sales", "purchaser", "readonly"}


# ---------- 13. 回款提醒新动作：全部模板默认 false + 显式账号门禁 ----------
_FOLLOW_UP_KEY = "action_maintenance_collection_follow_up"
_IMPORT_KEY = "action_maintenance_collection_plan_import"


def test_collection_reminder_actions_exist_and_default_closed_in_all_templates():
    """设计 §9：两个新 action 在所有权限模板中默认 false。"""
    for key in (_FOLLOW_UP_KEY, _IMPORT_KEY):
        assert key in permissions.ACTION_KEYS
        assert key in permissions.LABELS
        assert key in permissions.HIGH_RISK_KEYS
        # 依赖稳定版维保页 + Beta 白名单页
        assert permissions.ACTION_PAGE_DEPENDENCIES[key] == "page_maintenance"
        assert key in permissions.ACTION_ADDITIONAL_PAGE_DEPENDENCIES
        assert permissions.ACTION_ADDITIONAL_PAGE_DEPENDENCIES[key] == "page_maintenance_beta"
        # 所有角色模板（含 admin）默认 false
        for role, template in permissions.ROLE_TEMPLATES.items():
            assert template[key] is False, f"{role} 模板的 {key} 必须默认 false"
        for role in ("boss", "sales", "purchaser", "readonly", "guest"):
            assert permissions.effective(role, None)[key] is False
        meta = permissions.PERMISSION_META[key]
        for field in ("label", "summary", "can", "cannot", "typical", "sensitivity", "risk"):
            assert meta.get(field), f"{key} 缺业务语言字段 {field}"
    # 导入额外依赖 data_profit（能改必须能看）；follow-up 不需要金额可见性
    assert permissions.ACTION_DATA_DEPENDENCIES[_IMPORT_KEY] == "data_profit"
    assert _FOLLOW_UP_KEY not in permissions.ACTION_DATA_DEPENDENCIES


def test_collection_reminder_actions_false_in_database_templates_and_snapshots(db, admin_client):
    """sys_role_template 行与 sys_user.permissions 快照中两个新键必须为 false。"""
    c = admin_client
    _mk_account(c, "cr-sales", template="sales")
    _mk_account(c, "cr-purchaser", template="purchaser")
    _mk_account(c, "cr-readonly", template="readonly")

    templates = {
        template["code"]: template["permissions"]
        for template in c.get("/api/role-templates").json()
    }
    accounts = {
        account["username"]: account["permissions"]
        for account in c.get("/api/accounts").json()
    }
    for key in (_FOLLOW_UP_KEY, _IMPORT_KEY):
        for code in ("admin", "boss", "sales", "purchaser", "readonly"):
            assert templates[code][key] is False, f"sys_role_template {code} 的 {key} 必须 false"
        for username in ("cr-sales", "cr-purchaser", "cr-readonly"):
            assert accounts[username][key] is False, f"{username} 快照的 {key} 必须 false"


# ---------- 14. 显式账号动作门禁修复靶（P1-1：RBAC 关短路 / 构造期不校验 key） ----------
def test_require_explicit_account_action_always_enforced_when_rbac_disabled(db, monkeypatch):
    """RBAC 总闸关闭也不能短路实名白名单写门（设计 §9 失败关闭）。

    当前 ``security.require_explicit_account_action`` 在 ``config.ENABLE_RBAC``
    为 False 时提前 return，本测试当前红。
    """
    monkeypatch.setattr(security.config, "ENABLE_RBAC", False)
    db.add(SysUser(username="rbac-off-user", role="sales", display_name="RBAC 关闭账号",
                   password_hash=hash_password("pw123456")))
    db.commit()
    ctx = security.UserContext(
        user_id="rbac-off-user", role="sales", is_authenticated=True,
    )
    dep = security.require_explicit_account_action("action_maintenance_collection_follow_up")
    with pytest.raises(HTTPException) as exc_info:
        dep(ctx=ctx, db=db)
    assert exc_info.value.status_code == 403


def test_require_explicit_account_action_rejects_anonymous_and_missing_user_id(db):
    """未登录或 user_id 缺失一律 401（守卫）。"""
    dep = security.require_explicit_account_action("action_maintenance_collection_follow_up")
    with pytest.raises(HTTPException) as exc_info:
        dep(ctx=security.UserContext(user_id=None, role="guest", is_authenticated=False), db=db)
    assert exc_info.value.status_code == 401
    with pytest.raises(HTTPException) as exc_info:
        dep(ctx=security.UserContext(user_id=None, role="admin", is_authenticated=True), db=db)
    assert exc_info.value.status_code == 401


def test_require_explicit_account_action_rejects_disabled_and_missing_account(db):
    """账号不存在或已停用一律 403（守卫）。"""
    dep = security.require_explicit_account_action("action_maintenance_collection_follow_up")
    with pytest.raises(HTTPException) as exc_info:
        dep(ctx=security.UserContext(user_id="ghost-account", role="sales",
                                     is_authenticated=True), db=db)
    assert exc_info.value.status_code == 403
    db.add(SysUser(username="inactive-account", role="sales", is_active=False,
                   password_hash=hash_password("pw123456")))
    db.commit()
    with pytest.raises(HTTPException) as exc_info:
        dep(ctx=security.UserContext(user_id="inactive-account", role="sales",
                                     is_authenticated=True), db=db)
    assert exc_info.value.status_code == 403


def test_require_explicit_account_action_admin_without_explicit_action_denied(db):
    """admin 角色没有显式授权同样 403，不得走常规全开短路（守卫）。"""
    db.add(SysUser(username="admin-no-grant", role="admin", display_name="未授权管理员",
                   password_hash=hash_password("pw123456"),
                   template_perms={"sentinel": True}, perm_overrides={}))
    db.commit()
    dep = security.require_explicit_account_action("action_maintenance_collection_follow_up")
    with pytest.raises(HTTPException) as exc_info:
        dep(ctx=security.UserContext(user_id="admin-no-grant", role="admin",
                                     is_authenticated=True), db=db)
    assert exc_info.value.status_code == 403


def test_require_explicit_account_action_admin_with_explicit_action_allowed(db):
    """快照⊕覆盖显式授权后放行；无快照的旧账号回退 legacy permissions 图（守卫）。"""
    db.add(SysUser(username="admin-granted", role="admin", display_name="已授权管理员",
                   password_hash=hash_password("pw123456"),
                   template_perms={"action_maintenance_collection_follow_up": True},
                   perm_overrides={}))
    db.add(SysUser(username="legacy-granted", role="sales", display_name="旧图授权账号",
                   password_hash=hash_password("pw123456"),
                   permissions={"action_maintenance_collection_follow_up": True}))
    db.commit()
    dep = security.require_explicit_account_action("action_maintenance_collection_follow_up")
    dep(ctx=security.UserContext(user_id="admin-granted", role="admin",
                                 is_authenticated=True), db=db)
    dep(ctx=security.UserContext(user_id="legacy-granted", role="sales",
                                 is_authenticated=True), db=db)
    granted = db.query(SysUser).filter_by(username="admin-granted").one()
    assert (
        security.explicit_account_action_allowed(
            granted, "action_maintenance_collection_follow_up"
        )
        is True
    )


def test_require_explicit_account_action_only_constructed_for_account_scoped_keys(db):
    """构造期必须校验 key 属于 ACCOUNT_SCOPED_ACTION_KEYS（P1-1）。

    当前实现不校验任意字符串，本测试当前红；实现后非白名单 key 必须 ValueError。
    """
    db.add(SysUser(username="key-validation-user", role="sales",
                   password_hash=hash_password("pw123456")))
    db.commit()
    user = db.query(SysUser).filter_by(username="key-validation-user").one()
    with pytest.raises(ValueError):
        security.require_explicit_account_action("page_maintenance")
    with pytest.raises(ValueError):
        security.explicit_account_action_allowed(user, "page_maintenance")


def test_require_action_rejects_account_scoped_key_ordinary_bypass(db, monkeypatch):
    """普通动作门不得放行实名白名单动作：admin 与 RBAC 关闭都不能短路（守卫）。"""
    for enabled in (True, False):
        monkeypatch.setattr(security.config, "ENABLE_RBAC", enabled)
        dep = security.require_action("action_maintenance_collection_follow_up")
        with pytest.raises(HTTPException) as exc_info:
            dep(ctx=security.UserContext(user_id="admin", role="admin", is_authenticated=True))
        assert exc_info.value.status_code == 403

"""账号变更审计(sys_audit_log) + 登录事件审计(sys_access_log)——PR-审计A。

- 账号管理(建号/改权/改密/停用) → sys_audit_log(entity_type='sys_user')，记 operated_by + before/after，绝不记口令。
- 登录(成功/失败/锁定/停用拦截) → sys_access_log，带源 IP，供暴力破解排查；账号活动页可见。
"""
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import auth
from app.auth import LoginRequest, hash_password
from app.main import app
from app.models.system import SysAccessLog, SysAuditLog, SysUser
from tests.test_auth_lockout import _req


def _admin(db):
    db.add(SysUser(username="admin", role="admin", display_name="管理员",
                   password_hash=hash_password("adminpw")))
    db.commit()
    c = TestClient(app)
    tok = c.post("/api/auth/login", json={"username": "admin", "password": "adminpw"}).json()["token"]
    c.headers.update({"Authorization": f"Bearer {tok}"})
    return c


def _audits(db):
    return db.execute(
        select(SysAuditLog).where(SysAuditLog.entity_type == "sys_user")
        .order_by(SysAuditLog.id)).scalars().all()


def test_account_changes_audited(db):
    c = _admin(db)
    c.post("/api/accounts", json={"username": "sales_x", "password": "pw123456", "role": "sales"})
    # data_supplier 是 sales 模板默认关的键 → 会真正产生 overrides 变化
    c.put("/api/accounts/sales_x", json={"permissions": {"data_supplier": True}})
    c.put("/api/accounts/sales_x/password", json={"password": "newpw123"})
    c.put("/api/accounts/sales_x/active", json={"is_active": False})

    rows = _audits(db)
    assert [r.action for r in rows] == [
        "account_create", "account_update", "account_reset_password", "account_set_active"]
    assert all(r.operated_by == "admin" for r in rows)   # 操作者来自 token，不可自报

    create = rows[0]
    assert create.before_json is None and create.after_json["role"] == "sales"
    # 快照绝不含口令/hash
    assert "password" not in create.after_json and "password_hash" not in create.after_json

    upd = rows[1]
    # v2 快照记 模板+个别调整（不再是整图 permissions）；本次 PUT 应产生 overrides 变化
    assert upd.before_json["overrides"] != upd.after_json["overrides"]

    pw = rows[2]
    assert pw.before_json is None and pw.after_json is None   # 改密只记事件，不记口令

    act = rows[3]
    assert act.before_json["is_active"] is True and act.after_json["is_active"] is False


def test_login_events_recorded_with_ip(db):
    db.add(SysUser(username="liu", role="sales", password_hash=hash_password("pw123456")))
    db.commit()
    c = TestClient(app)
    assert c.post("/api/auth/login", json={"username": "liu", "password": "wrong"}).status_code == 401
    assert c.post("/api/auth/login", json={"username": "liu", "password": "pw123456"}).status_code == 200

    rows = db.execute(select(SysAccessLog).where(SysAccessLog.username == "liu")
                      .order_by(SysAccessLog.id)).scalars().all()
    actions = [r.action for r in rows]
    assert "login_failed" in actions and "login_success" in actions
    assert all(r.ip_address for r in rows)   # 每条都带源 IP


def test_login_events_visible_in_activity(db):
    c = _admin(db)
    db.add(SysUser(username="liu", role="sales", password_hash=hash_password("pw123456")))
    db.commit()
    TestClient(app).post("/api/auth/login", json={"username": "liu", "password": "pw123456"})
    body = c.get("/api/accounts/liu/activity").json()
    assert any(r["action"] == "login_success" for r in body["recent"])
    assert body["recent"][0]["ip"] is not None


def test_lockout_emits_locked_event(db):
    db.add(SysUser(username="bob", role="sales", password_hash=hash_password("right-pw-here")))
    db.commit()
    for _ in range(5):
        with pytest.raises(HTTPException):
            auth.login(LoginRequest(username="bob", password="x"), _req("9.9.9.9"), db)
    rows = db.execute(select(SysAccessLog).where(SysAccessLog.username == "bob")).scalars().all()
    actions = [r.action for r in rows]
    assert actions.count("login_failed") == 5
    assert "login_locked" in actions
    assert any(r.ip_address == "9.9.9.9" for r in rows)


def test_inactive_login_blocked_event(db):
    db.add(SysUser(username="zoe", role="sales", is_active=False,
                   password_hash=hash_password("pw123456")))
    db.commit()
    with pytest.raises(HTTPException) as e:
        auth.login(LoginRequest(username="zoe", password="pw123456"), _req(), db)
    assert e.value.status_code == 401
    rows = db.execute(select(SysAccessLog).where(SysAccessLog.username == "zoe")).scalars().all()
    assert [r.action for r in rows] == ["login_blocked"]

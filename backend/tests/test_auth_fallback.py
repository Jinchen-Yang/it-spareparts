"""共享口令回退登录的安全收紧（2026-06-15）。

- 已存在的用户名（含停用）绝不跌入共享口令回退：否则停用账号可凭 ADMIN_PASSWORD 复活
  登录、且 fb token 绕过 token 吊销/停用校验（永久有效）。
- 仅 sys_user 完全没有的用户名才走回退（兼容尚未建命名账号的旧部署）。
"""
from fastapi.testclient import TestClient

from app.auth import hash_password
from app.config import get_settings
from app.main import app
from app.models.system import SysUser


def _login(username, password):
    return TestClient(app).post("/api/auth/login",
                                json={"username": username, "password": password})


def test_disabled_account_cannot_revive_via_shared_password(db):
    db.add(SysUser(username="sales_x", role="sales", is_active=False,
                   password_hash=hash_password("own_pw_123456")))
    db.commit()
    r = _login("sales_x", get_settings().admin_password)
    assert r.status_code == 401   # 停用账号用 ADMIN_PASSWORD 也不能复活，绝不回退


def test_existing_username_does_not_fall_to_shared_password(db):
    db.add(SysUser(username="sales_y", role="sales", is_active=True,
                   password_hash=hash_password("own_pw_123456")))
    db.commit()
    # 用本人用户名 + 共享口令（非本人密码）→ 401（走实名分支的密码校验失败），不得回退成 readonly
    r = _login("sales_y", get_settings().admin_password)
    assert r.status_code == 401


def test_unknown_username_with_admin_password_still_falls_back(db):
    # sys_user 完全没有的用户名 + 正确共享口令 → 回退成功（兼容旧部署，readonly）
    r = _login("ghost_user_never_seeded", get_settings().admin_password)
    assert r.status_code == 200 and r.json()["role"] == "readonly"


def test_shared_admin_keeps_existing_chat_session_access(db):
    login = _login("admin", get_settings().admin_password)
    assert login.status_code == 200
    client = TestClient(app)
    response = client.get(
        "/api/agent/sessions",
        headers={"Authorization": f"Bearer {login.json()['token']}"},
    )
    assert response.status_code == 200, response.text

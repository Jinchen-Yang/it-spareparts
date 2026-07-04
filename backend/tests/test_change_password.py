"""自助改密（PUT /auth/change-password）：验旧密码 / 新密码约束 / 踢其他会话 /
当前会话热续（新 token）/ 共享口令拒绝 / 审计留痕（不落明文）。"""
from fastapi.testclient import TestClient

from app.auth import hash_password
from app.main import app
from app.models.system import SysAuditLog, SysUser


def _mk(db, username="liu", pw="pw123456", role="sales", active=True):
    db.add(SysUser(username=username, role=role, is_active=active,
                   password_hash=hash_password(pw)))
    db.commit()


def _login(username, password):
    return TestClient(app).post("/api/auth/login",
                                json={"username": username, "password": password})


def _bearer(tok):
    return {"Authorization": f"Bearer {tok}"}


def _change(tok, current, new):
    return TestClient(app).post("/api/auth/change-password", headers=_bearer(tok),
                                json={"current_password": current, "new_password": new})


def test_change_password_happy_path(db):
    _mk(db)
    tok = _login("liu", "pw123456").json()["token"]
    r = _change(tok, "pw123456", "newpw789")
    assert r.status_code == 200, r.text
    new_tok = r.json()["token"]
    assert new_tok and "expires_at" in r.json()
    # 新密码可登录、旧密码不行
    assert _login("liu", "newpw789").status_code == 200
    assert _login("liu", "pw123456").status_code == 401
    # 返回的新 token 立即可用（当前会话不掉线）：再用它改一次应成功
    assert _change(new_tok, "newpw789", "again123").status_code == 200


def test_wrong_current_password_rejected(db):
    _mk(db)
    tok = _login("liu", "pw123456").json()["token"]
    r = _change(tok, "WRONGpw", "newpw789")
    assert r.status_code == 400 and "当前密码" in r.json()["detail"]
    assert _login("liu", "pw123456").status_code == 200   # 未被改动


def test_new_password_too_short(db):
    _mk(db)
    tok = _login("liu", "pw123456").json()["token"]
    r = _change(tok, "pw123456", "abc")
    assert r.status_code == 400 and "至少" in r.json()["detail"]
    assert _login("liu", "pw123456").status_code == 200


def test_new_same_as_current_rejected(db):
    _mk(db)
    tok = _login("liu", "pw123456").json()["token"]
    r = _change(tok, "pw123456", "pw123456")
    assert r.status_code == 400 and "相同" in r.json()["detail"]


def test_old_token_revoked_after_change(db):
    """改密递增 token_version：其他设备持旧 token 下次请求即 401。"""
    _mk(db)
    old_tok = _login("liu", "pw123456").json()["token"]
    r = _change(old_tok, "pw123456", "newpw789")
    assert r.status_code == 200
    # 旧 token（改密所用的那把）已随 tv 递增失效——换个受保护接口验证被踢
    probe = TestClient(app).post("/api/auth/change-password", headers=_bearer(old_tok),
                                 json={"current_password": "newpw789", "new_password": "zzz99999"})
    assert probe.status_code == 401


def test_shared_password_admin_cannot_self_change(db):
    """ADMIN_PASSWORD 登录的 admin（无 sys_user 行）→ 明确拒绝，不 500。"""
    tok = _login("admin", "admin").json()["token"]   # 走共享口令回退
    r = _change(tok, "admin", "newadmin1")
    assert r.status_code == 400 and "无法自助改密" in r.json()["detail"]


def test_fallback_nonadmin_cannot_self_change(db):
    """共享口令回退的非 admin（fb=True token）→ 拒绝。"""
    tok = _login("someone", "admin").json()["token"]   # readonly + fb=True
    assert _login("someone", "admin").json()["role"] == "readonly"
    r = _change(tok, "admin", "newpw123")
    assert r.status_code == 400


def test_no_token_401(db):
    r = TestClient(app).post("/api/auth/change-password",
                             json={"current_password": "x", "new_password": "yyyyyy"})
    assert r.status_code == 401


def test_audit_records_no_plaintext(db):
    _mk(db)
    tok = _login("liu", "pw123456").json()["token"]
    _change(tok, "pw123456", "secretPw42")
    rows = db.query(SysAuditLog).filter(SysAuditLog.action == "account_change_password").all()
    assert len(rows) == 1
    blob = f"{rows[0].before_json}{rows[0].after_json}{rows[0].reason}"
    assert "secretPw42" not in blob and "pw123456" not in blob
    assert rows[0].operated_by == "liu"

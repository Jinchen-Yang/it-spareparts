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


def test_bruteforce_current_password_locks_out(db):
    """连续猜错当前密码达阈值 → 锁定（429），与登录共用锁定机制（安全审查 CP-1）。"""
    _mk(db)
    tok = _login("liu", "pw123456").json()["token"]
    for _ in range(5):
        assert _change(tok, "guess_wrong", "newpw789").status_code == 400
    # 第 6 次即便当前密码正确也被锁定拒绝
    r = _change(tok, "pw123456", "newpw789")
    assert r.status_code == 429 and "分钟" in r.json()["detail"]
    assert _login("liu", "pw123456").status_code == 429   # 锁定对登录同样生效（共用计数）


def test_successful_change_resets_failed_counter(db):
    """成功改密清零失败计数：之前几次猜错不该累积到锁定。"""
    _mk(db)
    tok = _login("liu", "pw123456").json()["token"]
    assert _change(tok, "wrong1", "newpw789").status_code == 400
    assert _change(tok, "wrong2", "newpw789").status_code == 400
    assert _change(tok, "pw123456", "newpw789").status_code == 200   # 成功
    # 计数已清零：新会话再连错 4 次仍不锁（阈值 5）
    tok2 = _login("liu", "newpw789").json()["token"]
    for _ in range(4):
        assert _change(tok2, "nope", "another99").status_code == 400
    assert _login("liu", "newpw789").status_code == 200   # 未锁定


def test_no_token_401(db):
    r = TestClient(app).post("/api/auth/change-password",
                             json={"current_password": "x", "new_password": "yyyyyy"})
    assert r.status_code == 401


# ---------- 登录页改密（未登录 /auth/change-password-unauth）----------
def _change_unauth(username, current, new):
    return TestClient(app).post("/api/auth/change-password-unauth",
                                json={"username": username, "current_password": current,
                                      "new_password": new})


def test_unauth_change_happy_path(db):
    _mk(db)
    r = _change_unauth("liu", "pw123456", "newpw789")
    assert r.status_code == 200 and r.json()["changed"] is True
    assert _login("liu", "newpw789").status_code == 200
    assert _login("liu", "pw123456").status_code == 401


def test_unauth_wrong_current_generic_401(db):
    _mk(db)
    r = _change_unauth("liu", "WRONG", "newpw789")
    assert r.status_code == 401 and "用户名或当前密码" in r.json()["detail"]
    assert _login("liu", "pw123456").status_code == 200   # 未改动


def test_unauth_unknown_user_same_as_wrong_password(db):
    """未知用户名与密码错返回同一 401 文案（防用户名枚举）。"""
    _mk(db, username="liu", pw="pw123456")
    r_unknown = _change_unauth("ghost", "whatever", "newpw789")
    r_wrongpw = _change_unauth("liu", "WRONG", "newpw789")
    assert r_unknown.status_code == r_wrongpw.status_code == 401
    assert r_unknown.json()["detail"] == r_wrongpw.json()["detail"]


def test_unauth_short_and_same_password(db):
    _mk(db)
    assert _change_unauth("liu", "pw123456", "abc").status_code == 400
    assert _change_unauth("liu", "pw123456", "pw123456").status_code == 400
    assert _login("liu", "pw123456").status_code == 200


def test_unauth_inactive_account(db):
    _mk(db, username="gone", active=False)
    r = _change_unauth("gone", "pw123456", "newpw789")
    assert r.status_code == 401 and "停用" in r.json()["detail"]


def test_unauth_lockout_shared_with_login(db):
    """登录页改密连错达阈值 → 锁定，且与登录共用计数。"""
    _mk(db)
    for _ in range(5):
        assert _change_unauth("liu", "nope", "newpw789").status_code == 401
    assert _change_unauth("liu", "pw123456", "newpw789").status_code == 429   # 已锁
    assert _login("liu", "pw123456").status_code == 429                        # 登录同锁


def test_unauth_shared_password_admin_rejected(db):
    """无 sys_user 行的 admin（ADMIN_PASSWORD 登录）→ 登录页改密也拒绝（不泄露存在性）。"""
    r = _change_unauth("admin", "admin", "newadmin1")
    assert r.status_code == 401


def test_unauth_change_revokes_existing_sessions(db):
    """登录页改密递增 token_version：改前签发的会话立即失效。"""
    _mk(db)
    old_tok = _login("liu", "pw123456").json()["token"]
    assert _change_unauth("liu", "pw123456", "newpw789").status_code == 200
    # 改前的 token 已失效（tv 不匹配）
    r = TestClient(app).post("/api/auth/change-password", headers=_bearer(old_tok),
                             json={"current_password": "newpw789", "new_password": "zzz99999"})
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

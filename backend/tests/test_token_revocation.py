"""Token 即时吊销（PR-B）：改密/停用/改权限递增 token_version → 旧 token 立即失效。"""
import base64
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import auth, permissions
from app.api import accounts
from app.auth import _make_token, _sign, hash_password, verify_token_db
from app.db import SessionLocal
from app.main import app
from app.models.system import SysUser


def _user(db, username="bob", pw="pw123456", role="sales", tv=0):
    u = SysUser(username=username, password_hash=hash_password(pw), role=role,
                is_active=True, token_version=tv)
    db.add(u)
    db.commit()
    return u


def _token_for(db, username="bob"):
    u = db.scalar(select(SysUser).where(SysUser.username == username))
    tok, _ = _make_token(u.role, u.username, u.salesperson_name, token_version=u.token_version or 0)
    return tok


def test_valid_token_passes(db):
    _user(db)
    assert verify_token_db(_token_for(db), db)["sub"] == "bob"


def test_password_reset_revokes_old_token(db):
    _user(db)
    tok = _token_for(db)
    assert verify_token_db(tok, db)["sub"] == "bob"          # 改密前有效
    accounts.reset_password("bob", accounts.PasswordReset(password="newpw12345"), db,
                            ident={"sub": "admin"}, _="admin")
    db.expire_all()
    with pytest.raises(HTTPException) as e:
        verify_token_db(tok, db)                              # 改密后旧 token 失效
    assert e.value.status_code == 401


def test_disable_revokes_old_token(db):
    _user(db)
    tok = _token_for(db)
    accounts.set_active("bob", accounts.ActiveToggle(is_active=False), db,
                        ident={"sub": "admin"}, _="admin")
    db.expire_all()
    with pytest.raises(HTTPException) as e:
        verify_token_db(tok, db)                              # 停用后旧 token 失效
    assert e.value.status_code == 401


def test_permission_change_revokes_old_token(db):
    _user(db)
    tok = _token_for(db)
    accounts.update_account("bob", accounts.UpdateAccount(permissions={"data_profit": False}),
                            db, ident={"sub": "admin"}, _="admin")
    db.expire_all()
    with pytest.raises(HTTPException):
        verify_token_db(tok, db)                              # 改权限后旧 token 失效


def test_displayname_change_does_not_revoke(db):
    _user(db)
    tok = _token_for(db)
    accounts.update_account("bob", accounts.UpdateAccount(display_name="Bob B"), db,
                            ident={"sub": "admin"}, _="admin")
    db.expire_all()
    assert verify_token_db(tok, db)["sub"] == "bob"           # 仅改显示名不踢线


def test_salesperson_mapping_change_revokes_old_token(db):
    user = _user(db)
    user.salesperson_name = "原销售"
    db.commit()
    tok = _token_for(db)

    accounts.update_account(
        "bob",
        accounts.UpdateAccount(salesperson_name="新销售"),
        db,
        ident={"sub": "admin"},
        _="admin",
    )

    db.expire_all()
    with pytest.raises(HTTPException) as exc:
        verify_token_db(tok, db)
    assert exc.value.status_code == 401


def test_concurrent_scope_changes_preserve_every_token_version_increment(db, monkeypatch):
    _user(db, username="concurrent_scope_user")
    start = Barrier(2)
    original_get = accounts._get

    def synchronized_get(session, username, *, lock=False):
        # With the required row lock both workers start the SELECT together and
        # then serialize.  If a future change drops lock=True, force both
        # unlocked reads to capture the same token_version before either write.
        if lock:
            start.wait(timeout=5)
            return original_get(session, username, lock=True)
        user = original_get(session, username)
        start.wait(timeout=5)
        return user

    monkeypatch.setattr(accounts, "_get", synchronized_get)

    def change_salesperson(value: str) -> None:
        with SessionLocal() as session:
            # Mirror the real dependency path: authentication/action checks may
            # preload this same account before the writer takes its row lock.
            assert session.scalar(
                select(SysUser).where(
                    SysUser.username == "concurrent_scope_user"
                )
            ) is not None
            accounts.update_account(
                "concurrent_scope_user",
                accounts.UpdateAccount(salesperson_name=value),
                session,
                ident={"sub": "admin"},
                _="admin",
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(change_salesperson, "并发销售甲"),
            executor.submit(change_salesperson, "并发销售乙"),
        ]
        for future in futures:
            future.result(timeout=10)

    db.expire_all()
    user = db.scalar(
        select(SysUser).where(SysUser.username == "concurrent_scope_user")
    )
    assert user is not None
    assert user.token_version == 2
    assert user.salesperson_name in {"并发销售甲", "并发销售乙"}


def test_concurrent_login_cannot_survive_password_reset_with_stale_claims(
    db,
    monkeypatch,
):
    _user(
        db,
        username="concurrent_login_user",
        pw="login-old-password",
    )
    password_check_entered = Event()
    release_password_check = Event()
    reset_started = Event()
    original_verify = auth.verify_password

    def blocking_verify(candidate: str, encoded: str) -> bool:
        if candidate == "login-old-password":
            password_check_entered.set()
            assert release_password_check.wait(timeout=10)
        return original_verify(candidate, encoded)

    monkeypatch.setattr(auth, "verify_password", blocking_verify)

    def run_login():
        with TestClient(app) as client:
            return client.post(
                "/api/auth/login",
                json={
                    "username": "concurrent_login_user",
                    "password": "login-old-password",
                },
            )

    def reset_password() -> None:
        reset_started.set()
        with SessionLocal() as session:
            accounts.reset_password(
                "concurrent_login_user",
                accounts.PasswordReset(password="login-new-password"),
                session,
                ident={"sub": "admin", "role": "admin"},
                _="admin",
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        login_future = executor.submit(run_login)
        assert password_check_entered.wait(timeout=10)
        reset_future = executor.submit(reset_password)
        assert reset_started.wait(timeout=10)
        release_password_check.set()
        response = login_future.result(timeout=15)
        reset_future.result(timeout=15)

    assert response.status_code == 200, response.text
    with SessionLocal() as session:
        with pytest.raises(HTTPException) as exc:
            verify_token_db(response.json()["token"], session)
    assert exc.value.status_code == 401


def test_fallback_token_skips_revocation(db):
    # 共享口令回退 token（fb=True，无实名用户）只验签名/过期，不做吊销校验
    tok, _ = _make_token("readonly", "ghost", None, fallback=True)
    assert verify_token_db(tok, db)["sub"] == "ghost"


def test_legacy_token_without_tv_not_kicked(db):
    # 部署前签发的旧 token 无 tv 字段 → 视作 tv=0，与用户初值 0 匹配，平滑升级不误踢
    _user(db)
    payload = {"role": "sales", "sub": "bob", "name": None, "exp": 9999999999}
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    tok = f"{base64.urlsafe_b64encode(body).decode().rstrip('=')}.{_sign(body)}"
    verified = verify_token_db(tok, db)
    assert verified["sub"] == "bob"
    assert verified["perms"] == permissions.runtime_safe(
        permissions.effective("sales", None)
    )


def test_legacy_signed_token_without_perms_uses_current_db_snapshot(db):
    """旧 JWT 的 role 不能覆盖账号当前模板快照/个别调整。"""
    u = _user(db)
    # token 仍写 sales，但 DB 当前快照已改成 purchaser（成本可见、利润不可见）。
    # 若错误按 token role 回退，sales 会把利润重新放开。
    u.template_perms = permissions.effective("purchaser", None)
    u.perm_overrides = {"page_governance": True}
    db.commit()
    tok, _ = _make_token("sales", "bob", None, token_version=0)  # 真实签名、无 perms
    verified = verify_token_db(tok, db)
    assert verified["perms"] == permissions.runtime_safe(
        permissions.effective_for_user(u)
    )
    assert verified["perms"]["data_purchase_cost"] is True
    assert verified["perms"]["data_profit"] is False
    assert verified["perms"]["page_governance"] is True


def test_existing_token_with_inferable_financial_combo_is_clamped(db):
    """部署前已签发的 token 也不能等到重新登录才收紧。"""
    _user(db)
    tok, _ = _make_token(
        "sales", "bob", None, token_version=0,
        perms={"data_purchase_cost": False, "data_profit": True},
    )
    verified = verify_token_db(tok, db)
    assert verified["perms"]["data_purchase_cost"] is False
    assert verified["perms"]["data_profit"] is False

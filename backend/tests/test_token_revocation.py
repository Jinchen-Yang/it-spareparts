"""Token 即时吊销（PR-B）：改密/停用/改权限递增 token_version → 旧 token 立即失效。"""
import base64
import json

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api import accounts
from app.auth import _make_token, _sign, hash_password, verify_token_db
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
    accounts.update_account("bob", accounts.UpdateAccount(permissions={"data_purchase_cost": False}),
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
    assert verify_token_db(tok, db)["sub"] == "bob"

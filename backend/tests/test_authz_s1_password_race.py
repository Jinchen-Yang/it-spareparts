"""A verified session must still be valid after a credential writer locks it."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Event

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select, update

from app import auth
from app.main import app
from app.models.system import SysAuditLog, SysUser


@pytest.mark.parametrize("revocation", ["epoch", "deleted"])
def test_self_password_change_rechecks_session_after_authorization_change(db, monkeypatch, revocation):
    user = SysUser(username="password-race", role="sales",
                   password_hash=auth.hash_password("original-password"))
    db.add(user)
    db.commit()
    user_id = user.id
    token, _ = auth._make_token("sales", user.username, None,
                                authn="sys_user", token_version=0)
    verified, resume = Event(), Event()
    original = auth.verify_token_db

    def pause_after_verification(*args, **kwargs):
        claims = original(*args, **kwargs)
        verified.set()
        assert resume.wait(10)
        return claims

    monkeypatch.setattr(auth, "verify_token_db", pause_after_verification)
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.post, "/api/auth/change-password",
                             headers={"Authorization": f"Bearer {token}"},
                             json={"current_password": "original-password", "new_password": "replacement-password"})
        try:
            assert verified.wait(10)
            values = {"token_version": 1} if revocation == "epoch" else {"deleted_at": datetime.now(timezone.utc)}
            db.execute(update(SysUser).where(SysUser.id == user_id).values(**values))
            db.commit()
        finally:
            resume.set()
        response = future.result(timeout=15)
    assert response.status_code == 401
    db.refresh(user)
    assert auth.verify_password("original-password", user.password_hash)
    assert user.token_version == (1 if revocation == "epoch" else 0)
    assert db.scalar(select(func.count()).select_from(SysAuditLog)
                     .where(SysAuditLog.action == "account_change_password")) == 0

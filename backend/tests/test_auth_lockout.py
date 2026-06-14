"""登录暴力破解防护：失败计数 + 锁定 + 成功重置 + 锁定过期 + 时序抹平（PR-A 认证加固）。"""
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app import auth
from app.auth import LoginRequest, hash_password
from app.models.system import SysUser


def _seed(db, username="alice", pw="correct-horse-battery", role="sales"):
    u = SysUser(username=username, password_hash=hash_password(pw), role=role, is_active=True)
    db.add(u)
    db.commit()
    return u


def _get(db, username="alice"):
    return db.scalar(select(SysUser).where(SysUser.username == username))


def test_wrong_password_increments_then_locks(db):
    _seed(db)
    for i in range(1, 5):
        with pytest.raises(HTTPException) as e:
            auth.login(LoginRequest(username="alice", password="nope"), db)
        assert e.value.status_code == 401
        assert _get(db).failed_attempts == i
    # 第 5 次失败 → 锁定
    with pytest.raises(HTTPException) as e:
        auth.login(LoginRequest(username="alice", password="nope"), db)
    assert e.value.status_code == 401
    assert _get(db).locked_until is not None


def test_locked_account_rejects_even_correct_password(db):
    u = _seed(db)
    u.failed_attempts = 5
    u.locked_until = datetime.now(timezone.utc).replace(year=2999)   # 远未来=锁定中
    db.commit()
    with pytest.raises(HTTPException) as e:
        auth.login(LoginRequest(username="alice", password="correct-horse-battery"), db)
    assert e.value.status_code == 429   # 锁定期内即便口令正确也拒绝


def test_success_resets_counter(db):
    _seed(db)
    for _ in range(2):
        with pytest.raises(HTTPException):
            auth.login(LoginRequest(username="alice", password="nope"), db)
    assert _get(db).failed_attempts == 2
    resp = auth.login(LoginRequest(username="alice", password="correct-horse-battery"), db)
    assert resp.role == "sales"
    u = _get(db)
    assert u.failed_attempts == 0 and u.locked_until is None and u.last_login_at is not None


def test_expired_lock_allows_login_and_resets(db):
    u = _seed(db)
    u.failed_attempts = 5
    u.locked_until = datetime(2020, 1, 1, tzinfo=timezone.utc)   # 过去=锁已过期
    db.commit()
    resp = auth.login(LoginRequest(username="alice", password="correct-horse-battery"), db)
    assert resp.role == "sales"
    u = _get(db)
    assert u.failed_attempts == 0 and u.locked_until is None


def test_unknown_user_runs_dummy_verify_and_rejects(db):
    # 未知用户：跑 dummy pbkdf2 抹平时序后走共享口令回退；错误口令 → 401，不崩
    with pytest.raises(HTTPException) as e:
        auth.login(LoginRequest(username="ghost", password="definitely-wrong"), db)
    assert e.value.status_code == 401
    # 未知用户不应在 sys_user 留下任何行/锁定
    assert _get(db, "ghost") is None

"""会话级生成互斥与取消（api.chat_sessions 进程内注册表）。"""
from app.api import chat_sessions as cs


def _cleanup(sid: int):
    cs.release_session(sid)


def test_acquire_is_exclusive_per_session():
    ev = cs.acquire_session(90001)
    try:
        assert ev is not None
        assert cs.acquire_session(90001) is None      # 同会话第二轮被拒
        ev2 = cs.acquire_session(90002)               # 不同会话互不影响
        assert ev2 is not None
        cs.release_session(90002)
    finally:
        _cleanup(90001)


def test_release_allows_next_round():
    assert cs.acquire_session(90003) is not None
    cs.release_session(90003)
    ev = cs.acquire_session(90003)
    assert ev is not None
    _cleanup(90003)


def test_cancel_sets_event_only_when_active():
    assert cs.request_cancel(90004) is False          # 无生成在进行
    ev = cs.acquire_session(90004)
    try:
        assert cs.request_cancel(90004) is True
        assert ev.is_set()                            # worker 据此收束
    finally:
        _cleanup(90004)


def test_release_is_idempotent():
    cs.release_session(90005)                         # 未持有也不抛
    assert cs.acquire_session(90005) is not None
    cs.release_session(90005)
    cs.release_session(90005)

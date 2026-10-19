"""请求数据库边界：真实 PostgreSQL 取消、事务回收与 HTTP 错误契约。"""

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from psycopg.pq import TransactionStatus
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app import db as app_db


@pytest.fixture()
def request_db(monkeypatch):
    # 使用 conftest 为本 pytest 进程创建的独立数据库，不接共享开发库。
    # 单连接池证明失败请求归还的确实是同一条可再次使用的连接。
    engine = create_engine(
        app_db.engine.url, pool_size=1, max_overflow=0, pool_timeout=0.05,
        hide_parameters=True,
    )
    events = []

    @event.listens_for(engine, "checkin")
    def checked_in(connection, _record):
        events.append(("checkin", connection.info.transaction_status))

    def session_factory():
        session = Session(engine, autoflush=False)

        @event.listens_for(session, "after_rollback")
        def rolled_back(_session):
            events.append(("rollback", None))

        return session

    monkeypatch.setattr(app_db, "SessionLocal", session_factory)
    monkeypatch.setattr(app_db, "REQUEST_STATEMENT_TIMEOUT_MS", 25, raising=False)
    app = FastAPI()
    try:
        yield app, engine, events
    finally:
        engine.dispose()


def test_query_timeout_returns_504_and_rolls_back_before_connection_checkin(request_db):
    app, engine, events = request_db
    backend_pids = []

    @app.get("/slow")
    def slow(db: Session = Depends(app_db.get_db)):
        backend_pids.append(db.scalar(text("SELECT pg_backend_pid()")))
        db.execute(text("SELECT pg_advisory_xact_lock(20261019)"))
        db.execute(text("SELECT pg_sleep(0.2)"))
        return {"ok": True}

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/slow")

    assert response.status_code == 504, response.text
    assert "超时" in response.json()["detail"]
    assert "pg_sleep" not in response.text
    assert events == [("rollback", None), ("checkin", TransactionStatus.IDLE)]

    with engine.connect() as conn:
        assert conn.scalar(text("SELECT pg_backend_pid()")) == backend_pids[0]
        assert conn.scalar(text("SELECT 1")) == 1
        assert conn.scalar(text("SHOW statement_timeout")) == "0"
        assert conn.scalar(text(
            "SELECT count(*) FROM pg_locks "
            "WHERE locktype = 'advisory' AND pid = pg_backend_pid()"
        )) == 0


@pytest.mark.parametrize("transaction_end", ["commit", "rollback"])
def test_timeout_reapplied_after_request_starts_another_transaction(
    request_db, transaction_end,
):
    app, _engine, _events = request_db
    timeouts = []

    @app.get("/slow")
    def slow(db: Session = Depends(app_db.get_db)):
        timeouts.append(db.scalar(text("SHOW statement_timeout")))
        getattr(db, transaction_end)()
        timeouts.append(db.scalar(text("SHOW statement_timeout")))
        db.execute(text("SELECT pg_sleep(0.2)"))
        return {"ok": True}

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/slow")

    assert response.status_code == 504, response.text
    assert timeouts == ["25ms", "25ms"]


def test_successful_request_timeout_does_not_leak_into_background_session(request_db):
    app, engine, _events = request_db

    @app.get("/ok")
    def ok(db: Session = Depends(app_db.get_db)):
        return {
            "timeout": db.scalar(text("SHOW statement_timeout")),
            "pid": db.scalar(text("SELECT pg_backend_pid()")),
        }

    with TestClient(app) as client:
        response = client.get("/ok")
    assert response.status_code == 200
    assert response.json()["timeout"] == "25ms"

    # 后台任务与导入显式创建 Session，不继承 HTTP 请求的限额。
    with Session(engine) as background_session:
        assert background_session.scalar(text("SHOW statement_timeout")) == "0"
        assert background_session.scalar(text("SELECT pg_backend_pid()")) == response.json()["pid"]


def test_non_timeout_database_error_is_preserved_and_rolled_back(request_db):
    app, engine, events = request_db

    @app.get("/invalid")
    def invalid(db: Session = Depends(app_db.get_db)):
        db.execute(text("SELECT 1 / 0"))

    with TestClient(app) as client:
        with pytest.raises(DBAPIError) as error:
            client.get("/invalid")

    assert error.value.orig.sqlstate == "22012"
    assert events == [("rollback", None), ("checkin", TransactionStatus.IDLE)]
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT 1")) == 1


def test_pool_exhaustion_returns_clear_503(request_db):
    app, engine, _events = request_db

    @app.get("/query")
    def query(db: Session = Depends(app_db.get_db)):
        return {"value": db.scalar(text("SELECT 1"))}

    with engine.connect() as held_connection, TestClient(
        app, raise_server_exceptions=False,
    ) as client:
        response = client.get("/query")
        assert response.status_code == 503, response.text
        assert "繁忙" in response.json()["detail"]
        assert "QueuePool" not in response.text
        assert held_connection.scalar(text("SELECT 1")) == 1

    with TestClient(app) as client:
        assert client.get("/query").json() == {"value": 1}


def test_authentication_dependency_query_timeout_also_returns_504(request_db):
    app, _engine, events = request_db

    def database_authentication(db: Session = Depends(app_db.get_db)):
        db.execute(text("SELECT pg_sleep(0.2)"))

    @app.get("/authenticated", dependencies=[Depends(database_authentication)])
    def authenticated():
        pytest.fail("查询超时的鉴权依赖不应进入业务处理")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/authenticated")
    assert response.status_code == 504, response.text
    assert "超时" in response.json()["detail"]
    assert events == [("rollback", None), ("checkin", TransactionStatus.IDLE)]


def _add_real_permission_route(app):
    from app import auth, security

    @app.get("/protected", dependencies=[Depends(security.require_page("page_boss_board"))])
    def protected():
        return {"ok": True}

    token, _ = auth._make_token("admin", "timeout-contract-user", None)
    return {"Authorization": f"Bearer {token}"}


def test_real_user_context_does_not_downgrade_database_timeout_to_guest(request_db):
    app, _engine, events = request_db
    headers = _add_real_permission_route(app)

    # 真正执行 verify_token_db 的 sys_user 查询。另一个事务锁住账号表，
    # 让 PostgreSQL 在鉴权 SQL 的锁等待中触发同一个 statement_timeout。
    with app_db.engine.begin() as blocker, TestClient(app) as client:
        blocker.execute(text("LOCK TABLE sys_user IN ACCESS EXCLUSIVE MODE"))
        response = client.get("/protected", headers=headers)

    assert response.status_code == 504, response.text
    assert "超时" in response.json()["detail"]
    assert events == [("rollback", None), ("checkin", TransactionStatus.IDLE)]


def test_real_user_context_does_not_downgrade_pool_timeout_to_guest(request_db):
    app, engine, _events = request_db
    headers = _add_real_permission_route(app)

    with engine.connect(), TestClient(app) as client:
        response = client.get("/protected", headers=headers)
    assert response.status_code == 503, response.text
    assert "繁忙" in response.json()["detail"]

    with TestClient(app) as client:
        assert client.get("/protected", headers=headers).status_code == 200


def test_real_user_context_still_treats_invalid_token_as_guest(request_db):
    app, _engine, events = request_db
    _add_real_permission_route(app)

    with TestClient(app) as client:
        response = client.get("/protected", headers={"Authorization": "Bearer invalid"})
    assert response.status_code == 403
    assert response.json() == {"detail": "无权访问该页面"}
    assert events == []


def test_local_service_override_survives_savepoint_and_resets_next_transaction(request_db):
    app, _engine, _events = request_db

    @app.get("/override")
    def override(db: Session = Depends(app_db.get_db)):
        db.execute(text("SET LOCAL statement_timeout = '100ms'"))
        with db.begin_nested():
            nested_timeout = db.scalar(text("SHOW statement_timeout"))
            db.execute(text("SELECT pg_sleep(0.05)"))
        db.commit()
        return {
            "nested_timeout": nested_timeout,
            "next_timeout": db.scalar(text("SHOW statement_timeout")),
        }

    with TestClient(app) as client:
        response = client.get("/override")
    assert response.status_code == 200, response.text
    assert response.json() == {"nested_timeout": "100ms", "next_timeout": "25ms"}


def test_rejected_request_keeps_lazy_connection_and_original_http_error(request_db):
    app, _engine, events = request_db

    @app.get("/forbidden")
    def forbidden(db: Session = Depends(app_db.get_db)):
        raise HTTPException(status_code=403, detail="权限不足")

    with TestClient(app) as client:
        response = client.get("/forbidden")
    assert response.status_code == 403
    assert response.json() == {"detail": "权限不足"}
    assert events == []

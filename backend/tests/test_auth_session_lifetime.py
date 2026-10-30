"""真实 PostgreSQL 单连接池：鉴权结束即归还，不随业务/流式响应存活。"""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import sleep

import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import Session

from app import auth, db as app_db, security


@pytest.fixture()
def auth_pool(monkeypatch):
    engine = create_engine(
        app_db.engine.url, pool_size=1, max_overflow=0, pool_timeout=0.1,
        pool_pre_ping=True, hide_parameters=True,
    )
    monkeypatch.setattr(app_db, "SessionLocal", lambda: Session(engine, autoflush=False))
    token, _ = auth._make_token("admin", "auth-lifetime-contract", None)
    try:
        yield FastAPI(), engine, {"Authorization": f"Bearer {token}"}
    finally:
        engine.dispose()


@pytest.mark.parametrize("stream", [False, True])
def test_auth_releases_only_connection_while_handler_or_stream_waits(auth_pool, stream):
    app, engine, headers = auth_pool
    entered, release = Event(), Event()

    @app.get("/waiting")
    def waiting(_identity=Depends(auth.current_identity)):
        def body():
            entered.set()
            assert release.wait(5)
            yield b"done"

        if stream:
            return StreamingResponse(body())
        return {"body": b"".join(body()).decode()}

    @app.get("/probe")
    def probe(_ctx=Depends(security.get_current_user_context), db=Depends(app_db.get_db)):
        return {"value": db.scalar(text("SELECT 1"))}

    with TestClient(app) as client, ThreadPoolExecutor(max_workers=1) as executor:
        request = executor.submit(client.get, "/waiting", headers=headers)
        try:
            assert entered.wait(5)
            assert engine.pool.checkedout() == 0
            response = client.get("/probe", headers=headers)
            assert response.status_code == 200, response.text
            assert response.json() == {"value": 1}
        finally:
            release.set()
        assert request.result(timeout=5).status_code == 200
    assert engine.pool.checkedout() == 0


def test_handler_cancellation_does_not_hold_auth_connection(auth_pool):
    app, engine, headers = auth_pool

    async def exercise():
        entered = asyncio.Event()

        @app.get("/cancel")
        async def cancel(_identity=Depends(auth.current_identity)):
            entered.set()
            await asyncio.Event().wait()

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            task = asyncio.create_task(client.get("/cancel", headers=headers))
            try:
                await asyncio.wait_for(entered.wait(), timeout=5)
                assert engine.pool.checkedout() == 0
            finally:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

    asyncio.run(exercise())
    assert engine.pool.checkedout() == 0


@pytest.mark.parametrize("failure", [HTTPException(401, "revoked"), ValueError("failed"), asyncio.CancelledError()])
def test_auth_failure_or_cancellation_releases_connection(auth_pool, monkeypatch, failure):
    _app, engine, headers = auth_pool

    def fail(_token, db):
        db.execute(text("SELECT 1"))
        raise failure

    monkeypatch.setattr(auth, "verify_token_db", fail)
    with pytest.raises(type(failure)):
        auth.verify_request_token(headers["Authorization"].removeprefix("Bearer "))
    assert engine.pool.checkedout() == 0
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT 1")) == 1
        assert conn.scalar(text("SHOW idle_in_transaction_session_timeout")) == "0"


def test_auth_local_timeouts_reset_and_do_not_change_business_transaction(auth_pool):
    _app, engine, _headers = auth_pool
    with app_db.auth_db_session() as db:
        assert db.scalar(text("SHOW idle_in_transaction_session_timeout")) == "1min"
        assert db.scalar(text("SHOW statement_timeout")) == "30s"
        pid = db.scalar(text("SELECT pg_backend_pid()"))
    business = app_db.get_db()
    db = next(business)
    try:
        assert db.scalar(text("SELECT pg_backend_pid()")) == pid
        assert db.scalar(text("SHOW idle_in_transaction_session_timeout")) == "0"
        assert db.scalar(text("SHOW statement_timeout")) == "30s"
        db.execute(text("CREATE TEMP TABLE auth_lifetime_business (id int) ON COMMIT DROP"))
        db.execute(text("INSERT INTO auth_lifetime_business VALUES (1)"))
        # 持有业务事务时，独立鉴权只会池满；不能结束/回滚业务的未提交工作。
        with pytest.raises(HTTPException) as error:
            auth.verify_request_token(auth._make_token("admin", "auth-lifetime-contract", None)[0])
        assert error.value.status_code == 503
        assert db.scalar(text("SELECT count(*) FROM auth_lifetime_business")) == 1
        db.commit()
    finally:
        business.close()
    with Session(engine) as background:
        assert background.scalar(text("SHOW idle_in_transaction_session_timeout")) == "0"
        assert background.scalar(text("SHOW statement_timeout")) == "0"


@pytest.mark.parametrize("query_after_idle", [True, False])
def test_auth_idle_timeout_invalidates_connection_and_returns_503(
    auth_pool, monkeypatch, query_after_idle,
):
    app, engine, headers = auth_pool
    monkeypatch.setattr(app_db, "AUTH_IDLE_TRANSACTION_TIMEOUT_MS", 25)
    original = auth.verify_token_db
    pids = []
    sqlstates = []

    @event.listens_for(engine, "handle_error")
    def record_error(context):
        sqlstates.append(getattr(context.original_exception, "sqlstate", None))

    def idle(token, db):
        identity = original(token, db)
        pids.append(db.scalar(text("SELECT pg_backend_pid()")))
        sleep(0.1)
        if query_after_idle:
            db.execute(text("SELECT 1"))
        # 不再执行 SQL 时，归还连接的 rollback 也可能首次读到 25P03。
        return identity

    monkeypatch.setattr(auth, "verify_token_db", idle)

    @app.get("/protected")
    def protected(_ctx=Depends(security.get_current_user_context)):
        pytest.fail("数据库超时不能降级为 guest")

    with TestClient(app) as client:
        response = client.get("/protected", headers=headers)
    assert response.status_code == 503, response.text
    assert "25P03" in sqlstates
    assert "SELECT" not in response.text
    assert engine.pool.checkedout() == 0
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT pg_backend_pid()")) != pids[0]
        assert conn.scalar(text("SELECT 1")) == 1


@pytest.mark.parametrize("revocation", ["token_version", "is_active"])
def test_short_auth_materializes_permissions_and_rechecks_revocation(auth_pool, db, revocation):
    from app.models.system import SysUser

    _app, engine, headers = auth_pool
    user = SysUser(username="auth-lifetime-contract", role="sales", password_hash="unused",
                   is_active=True, token_version=0, permissions={"page_boss_board": False})
    db.add(user)
    db.commit()
    token = headers["Authorization"].removeprefix("Bearer ")
    identity = auth.verify_request_token(token)
    assert engine.pool.checkedout() == 0
    assert identity["perms"]["page_boss_board"] is False
    assert json.loads(json.dumps(identity)) == identity
    setattr(user, revocation, 1 if revocation == "token_version" else False)
    db.commit()
    with pytest.raises(HTTPException) as error:
        auth.verify_request_token(token)
    assert error.value.status_code == 401
    assert engine.pool.checkedout() == 0


def test_health_db_reports_503_without_leaking_connection_details(monkeypatch):
    from app import main

    class UnavailableEngine:
        def connect(self):
            raise RuntimeError("secret://private-host/password")

    monkeypatch.setattr(main, "engine", UnavailableEngine())
    app = FastAPI()
    app.get("/health/db")(main.health_db)
    with TestClient(app) as client:
        response = client.get("/health/db")
    assert response.status_code == 503
    assert response.json() == {"status": "error", "db": "unreachable"}
    assert "secret" not in response.text


def test_health_db_reports_real_pool_exhaustion_and_recovers(auth_pool, monkeypatch):
    from app import main

    app, engine, _headers = auth_pool
    monkeypatch.setattr(main, "engine", engine)
    app.get("/health/db")(main.health_db)
    with TestClient(app) as client:
        assert client.get("/health/db").json() == {"status": "ok", "db": "reachable"}
        with engine.connect():
            response = client.get("/health/db")
            assert response.status_code == 503
            assert response.json() == {"status": "error", "db": "unreachable"}
        assert client.get("/health/db").status_code == 200


def test_internal_auth_error_is_not_downgraded_to_guest(auth_pool, monkeypatch):
    app, _engine, headers = auth_pool

    def fail(_token):
        raise InvalidRequestError("internal authentication failure")

    monkeypatch.setattr(auth, "verify_request_token", fail)

    @app.get("/protected", dependencies=[Depends(security.require_page("page_boss_board"))])
    def protected():
        pytest.fail("鉴权内部错误不应放行业务请求")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/protected", headers=headers)
    assert response.status_code == 500  # 不应伪装为 guest 的权限不足 403。

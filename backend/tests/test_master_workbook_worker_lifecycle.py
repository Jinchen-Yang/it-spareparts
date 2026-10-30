"""Actual upload routes must keep ASGI responsive and own DB until worker exit."""
import asyncio
import threading
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import db as db_module
from app.api import maintenance_project_master_workbook as api
from app.security import UserContext


class RequestDB:
    def __init__(self):
        self.closed = False
        self.running = False
        self.rollbacks = 0

    def rollback(self):
        assert not self.closed
        self.rollbacks += 1


def route_app(monkeypatch, *, v2, operation, blocking, db=None):
    app = FastAPI()
    app.include_router(api.router, prefix="/api")
    db = db or RequestDB()
    ctx = UserContext(user_id="worker-test", role="admin", is_authenticated=True)
    values = {"ident": {"authn": "sys_user", "sub": "worker-test"},
              "ctx": ctx, "_auth": "admin"}

    def override(value):
        async def dependency():
            return value
        return dependency

    async def request_db():
        try:
            yield db
        finally:
            assert not db.running, "request DB closed before worker terminal state"
            db.closed = True

    for route in app.routes:
        if getattr(route, "endpoint", None) in (
                api.apply_project_master, api.validate_project_master):
            for dep in route.dependant.dependencies:
                app.dependency_overrides[dep.call] = (
                    request_db if dep.name == "db" else override(values.get(dep.name)))

    @app.get("/probe")
    async def probe():
        return {"worker_running": db.running}

    async def upload(_request):
        return b"controlled-upload", False

    plan = SimpleNamespace(
        contract_amount_change=None, project_note_change=None, summary={}, sheets=(),
        site_issue_merges=(), field_changes=(), conflicts=(), overridden=(),
        will_void_rows=(), assignment_changes=(), warnings=(), voided_rows=(), site_flags=())

    def validate(request_db, *_args, **_kwargs):
        if operation == "validate":
            blocking(request_db)
        return plan

    def apply(request_db, *_args, **_kwargs):
        blocking(request_db)
        return {"applied": True}

    monkeypatch.setattr(api.get_settings(), "maintenance_project_master_v2_enabled", v2)
    monkeypatch.setattr(api, "_read_upload_with_takeover", upload)
    monkeypatch.setattr(api, "record_access_log", lambda *_a, **_k: None)
    monkeypatch.setattr(api.master, "validate", validate)
    monkeypatch.setattr(api.master, "validate_project_master_v2", validate)
    monkeypatch.setattr(api.master, "apply", apply)
    monkeypatch.setattr(api.master, "apply_project_master_v2", apply)
    monkeypatch.setattr(api.master, "get_project_master_v2_replay", lambda *_a, **_k: None)
    monkeypatch.setattr(api.master, "verify_site_issue_merge_token", lambda *_a, **_k: None)
    return app, db


async def wait_started(started):
    async with asyncio.timeout(3):
        while not started.is_set():
            await asyncio.sleep(0.005)


@pytest.mark.parametrize("v2", [False, True])
@pytest.mark.parametrize("operation", ["validate", "apply"])
def test_upload_route_does_not_block_independent_probe(monkeypatch, v2, operation):
    started, release = threading.Event(), threading.Event()

    def blocking(db):
        assert not db.closed
        db.running = True
        started.set()
        try:
            release.wait(1)
        finally:
            db.running = False

    app, db = route_app(monkeypatch, v2=v2, operation=operation, blocking=blocking)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url="http://test") as client:
            task = asyncio.create_task(client.post(
                f"/api/maintenance/projects/stable/test/master-workbook/{operation}"))
            try:
                await wait_started(started)
                probe = await asyncio.wait_for(client.get("/probe"), timeout=0.5)
                assert probe.json()["worker_running"], "ASGI loop stalled until blocking work finished"
            finally:
                release.set()
                response = await task
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            assert db.closed

    asyncio.run(run())


@pytest.mark.parametrize("cancel_request", [False, True])
def test_real_postgres_advisory_wait_preserves_probe_timeout_and_cleanup(
        monkeypatch, cancel_request):
    """Keep the real get_db/psycopg timeout path, with only apply's lock seam replaced."""
    started = threading.Event()
    control = RequestDB()
    backend_pid = []
    closed = []

    class TrackedSession(Session):
        def close(self):
            assert not control.running, "get_db closed a Session still executing SQL"
            super().close()
            closed.append(True)

    monkeypatch.setattr(db_module, "SessionLocal", lambda: TrackedSession(bind=db_module.engine))
    # Production remains 30 seconds; shorten only this isolated contention test.
    monkeypatch.setattr(db_module, "REQUEST_STATEMENT_TIMEOUT_MS", 1000)
    lock_key = 752089134

    def blocking(db):
        backend_pid.append(db.scalar(text("SELECT pg_backend_pid()")))
        control.running = True
        started.set()
        try:
            db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
        finally:
            control.running = False

    app, _ = route_app(monkeypatch, v2=True, operation="apply", blocking=blocking, db=control)
    del app.dependency_overrides[api.get_db]

    def actually_waiting():
        waiting = observer.execute(text(
            "SELECT wait_event_type = 'Lock' AND wait_event = 'advisory' "
            "FROM pg_stat_activity WHERE pid = :pid"), {"pid": backend_pid[0]}).scalar()
        observer.rollback()  # Refresh PostgreSQL's transaction-local activity snapshot.
        return waiting

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url="http://test") as client:
            task = asyncio.create_task(client.post(
                "/api/maintenance/projects/stable/test/master-workbook/apply"))
            await wait_started(started)
            async with asyncio.timeout(0.8):
                while not await asyncio.to_thread(actually_waiting):
                    await asyncio.sleep(0.005)
            if cancel_request:
                for _ in range(3):
                    task.cancel()
                    await asyncio.sleep(0.01)
                    assert not closed and not task.done()
            probe = await asyncio.wait_for(client.get("/probe"), timeout=0.3)
            assert probe.json()["worker_running"]
            if cancel_request:
                with pytest.raises(asyncio.CancelledError) as cancelled:
                    await task
                assert getattr(cancelled.value.__cause__.orig, "sqlstate", None) == "57014"
            else:
                response = await task
                assert response.status_code == 504
                assert "数据库查询超时" in response.json()["detail"]
            assert closed == [True]
            assert not control.running

    # Keep the observer checked out so it cannot borrow the tested request's
    # returned connection and accidentally observe its own active transaction.
    with db_module.engine.begin() as lock_holder, db_module.engine.connect() as observer:
        lock_holder.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
        asyncio.run(run())
        state = observer.execute(text(
            "SELECT state, xact_start IS NULL FROM pg_stat_activity WHERE pid = :pid"),
            {"pid": backend_pid[0]}).one()
        assert state == ("idle", True)


@pytest.mark.parametrize("v2", [False, True])
@pytest.mark.parametrize("operation", ["validate", "apply"])
@pytest.mark.parametrize("worker_fails", [False, True])
def test_repeated_cancellation_drains_worker_before_db_cleanup(
        monkeypatch, v2, operation, worker_fails):
    started, release = threading.Event(), threading.Event()
    terminal = threading.Event()

    def blocking(db):
        assert not db.closed
        db.running = True
        started.set()
        try:
            assert release.wait(3), "test failed to release worker"
            assert not db.closed
            if worker_fails:
                raise RuntimeError("controlled worker failure")
        finally:
            db.running = False
            terminal.set()

    app, db = route_app(monkeypatch, v2=v2, operation=operation, blocking=blocking)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url="http://test") as client:
            task = asyncio.create_task(client.post(
                f"/api/maintenance/projects/stable/test/master-workbook/{operation}"))
            try:
                await wait_started(started)
                for _ in range(3):
                    task.cancel()
                    await asyncio.sleep(0.01)
                    assert db.running and not db.closed and not task.done()
                assert (await client.get("/probe")).json()["worker_running"]
            finally:
                release.set()
                with pytest.raises(asyncio.CancelledError) as cancelled:
                    await task
            assert terminal.is_set() and db.closed
            if worker_fails:
                assert isinstance(cancelled.value.__cause__, RuntimeError)

    asyncio.run(run())

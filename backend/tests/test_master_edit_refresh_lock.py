"""真实 PostgreSQL：人工事务与全量刷新共用锁，编辑快照不阻塞写者。"""
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Event
from time import monotonic, sleep

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, text

from app import permissions
from app.auth import _make_token
from app.db import SessionLocal, engine
from app.main import app
from app.models.dimensions import DimPart
from app.models.master_data import ProductSpec
from app.models.system import SysAuditLog, SysUser
from app.services import master_data, master_edit


def _limit_waits(db):
    db.execute(text("SET LOCAL statement_timeout = '10s'"))
    db.execute(text("SET LOCAL lock_timeout = '8s'"))


def _seed(db):
    part = DimPart(pn_std="REFRESH-SEED", description="600GB SAS HDD", brand="刷新前品牌")
    db.add(part)
    db.commit()
    return part.id


def _manual(db, operation, part_id):
    if operation == "create":
        return master_edit.create_part_in_transaction(
            db, pn_std="REFRESH-NEW", description="1.2TB SAS HDD", brand="人工品牌",
            force=True, operated_by="real-editor")
    return master_edit.edit_part_in_transaction(
        db, part_id=part_id, pn_std="REFRESH-SEED",
        updates={"description": "1.2TB SAS HDD", "brand": "人工品牌"},
        operated_by="real-editor")


def _assert_waiting_for_advisory_lock(pid, blocker_pid, future):
    deadline = monotonic() + 5
    with SessionLocal() as monitor:
        while True:
            blockers = monitor.scalar(text("SELECT pg_blocking_pids(:pid)"), {"pid": pid})
            if blockers:
                assert blocker_pid in blockers
                assert monitor.scalar(text(
                    "SELECT EXISTS (SELECT 1 FROM pg_locks"
                    " WHERE pid = :pid AND locktype = 'advisory' AND NOT granted)"
                ), {"pid": pid}), "必须在字典/规格/父行写入前等待共同事务锁"
                return
            if future.done():
                future.result()  # 工作线程异常直接报告，避免把异常伪装成等待超时。
                pytest.fail("并发写命令未等待共同事务锁")
            assert monotonic() < deadline, "未观察到真实 PostgreSQL 锁等待"
            sleep(0.01)


def _assert_no_partial_manual_write(operation):
    with SessionLocal() as observer:
        seed = observer.scalar(select(DimPart).where(DimPart.pn_std == "REFRESH-SEED"))
        assert seed.description == "600GB SAS HDD"
        assert seed.brand == "刷新前品牌"
        if operation == "create":
            assert observer.scalar(select(DimPart.id).where(DimPart.pn_std == "REFRESH-NEW")) is None
        assert observer.scalar(select(func.count()).select_from(SysAuditLog)) == 0


def _assert_committed_manual_write(operation):
    with SessionLocal() as observer:
        pn = "REFRESH-NEW" if operation == "create" else "REFRESH-SEED"
        part = observer.scalar(select(DimPart).where(DimPart.pn_std == pn))
        assert (part.description, part.brand) == ("1.2TB SAS HDD", "人工品牌")
        assert part.brand_id is not None
        capacity = observer.scalar(select(ProductSpec.spec_value).where(
            ProductSpec.part_id == part.id, ProductSpec.spec_key == "capacity"))
        assert capacity == "1.2TB"
        assert observer.scalar(select(func.count()).select_from(SysAuditLog)) == 1


@pytest.mark.parametrize("operation", ["edit", "create"])
def test_refresh_waits_for_manual_transaction_before_writing_dictionaries_or_specs(db, operation):
    part_id = _seed(db)
    _limit_waits(db)
    owner_pid = db.scalar(text("SELECT pg_backend_pid()"))
    _manual(db, operation, part_id)
    started = Queue()

    def refresh():
        with SessionLocal() as other:
            _limit_waits(other)
            started.put(other.scalar(text("SELECT pg_backend_pid()")))
            return master_data.refresh(other)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(refresh)
        try:
            _assert_waiting_for_advisory_lock(started.get(timeout=5), owner_pid, future)
            _assert_no_partial_manual_write(operation)
            with SessionLocal() as observer:
                assert observer.get(DimPart, part_id).data_quality_score is None
            db.commit()
            assert future.result(timeout=15)["quality_scored"] >= 1
        finally:
            db.rollback()
    _assert_committed_manual_write(operation)


@pytest.mark.parametrize("operation", ["edit", "create"])
def test_manual_transaction_waits_for_refresh_before_locking_parent_or_creating_part(db, operation):
    part_id = _seed(db)
    ready = Event()
    release = Event()
    refresh_pid = Queue()
    writer_pid = Queue()

    def refresh():
        with SessionLocal() as other:
            _limit_waits(other)
            refresh_pid.put(other.scalar(text("SELECT pg_backend_pid()")))

            def pause_before_commit(_session):
                ready.set()
                assert release.wait(timeout=10), "测试未释放刷新提交屏障"

            event.listen(other, "before_commit", pause_before_commit)
            return master_data.refresh(other)

    def write():
        with SessionLocal() as other:
            _limit_waits(other)
            writer_pid.put(other.scalar(text("SELECT pg_backend_pid()")))
            result = _manual(other, operation, part_id)
            other.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        refresh_future = pool.submit(refresh)
        try:
            assert ready.wait(timeout=5), "刷新未到达真实提交屏障"
            write_future = pool.submit(write)
            _assert_waiting_for_advisory_lock(
                writer_pid.get(timeout=5), refresh_pid.get(timeout=5), write_future)
            _assert_no_partial_manual_write(operation)
            with SessionLocal() as observer:
                seed = observer.get(DimPart, part_id)
                assert seed.brand_id is None
                assert seed.data_quality_score is None
                assert observer.scalar(select(func.count()).select_from(ProductSpec)) == 0
            release.set()
            assert refresh_future.result(timeout=15)["quality_scored"] == 1
            assert write_future.result(timeout=15)["id"] > 0
        finally:
            release.set()
    _assert_committed_manual_write(operation)


def test_http_edit_state_reads_one_read_only_snapshot_without_blocking_writer(db):
    created = master_edit.create_part(
        db, pn_std="SNAPSHOT-READ", description="600GB SAS HDD", force=True)
    db.add(SysUser(username="snapshot-reader", password_hash="unused", role="purchaser",
                   display_name="快照读取人", is_active=True))
    db.commit()
    token, _ = _make_token("purchaser", "snapshot-reader", "快照读取人",
                           perms=permissions.effective("purchaser", None))
    observed = {}

    def interleave_writer(connection, _cursor, statement, _parameters, _context, _many):
        if observed or "FROM dim_part" not in statement or "WHERE dim_part.id =" not in statement:
            return
        # 首个父行 SELECT 已完成、规格 SELECT 未开始：在真实连接间制造提交窗口。
        observed["isolation"] = connection.exec_driver_sql("SHOW transaction_isolation").scalar_one()
        observed["read_only"] = connection.exec_driver_sql("SHOW transaction_read_only").scalar_one()
        with SessionLocal() as writer:
            writer.execute(text("SET LOCAL lock_timeout = '500ms'"))
            writer.execute(text("SET LOCAL statement_timeout = '2s'"))
            try:
                master_edit.edit_part(writer, part_id=created["id"], pn_std="SNAPSHOT-READ",
                                      updates={"description": "1.2TB SAS HDD"},
                                      operated_by="other-editor")
            except Exception as exc:
                writer.rollback()
                observed["writer_error"] = exc

    event.listen(engine, "after_cursor_execute", interleave_writer)
    try:
        response = TestClient(app).get(
            f'/api/parts/master/{created["id"]}/edit-state',
            headers={"Authorization": f"Bearer {token}"})
    finally:
        event.remove(engine, "after_cursor_execute", interleave_writer)

    assert response.status_code == 200, response.text
    assert "writer_error" not in observed, f'读快照阻塞了写者: {observed.get("writer_error")}'
    assert observed == {"isolation": "repeatable read", "read_only": "on"}
    snapshot = response.json()
    assert snapshot["fields"]["description"] == "600GB SAS HDD"
    assert snapshot["specs"]["capacity"]["value"] == "600GB"
    with SessionLocal() as observer:
        assert observer.get(DimPart, created["id"]).description == "1.2TB SAS HDD"
        assert observer.scalar(select(ProductSpec.spec_value).where(
            ProductSpec.part_id == created["id"], ProductSpec.spec_key == "capacity")) == "1.2TB"

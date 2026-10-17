"""回填脚本 scripts/backfill_sales_reconciliation（D-40/D-41）契约：只读 dry-run、
execute 双确认护栏、幂等重跑、坏档隔离、税列冲突不覆盖、批次口径、advisory
lock 连接绑定（锁不随事务边界滞留连接池）。

xlsx 构造复用 test_sales_reconciliation_import 的 helpers（同包跨测试模块导入
是该仓既有惯例，见 test_audit_trail.py）。
"""
import hashlib
import json
import threading
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import delete, event, func, select, text, update
from sqlalchemy.exc import ResourceClosedError

from app.db import SessionLocal
from app.etl import pipeline
from app.models.sales import FSalesOrder, FSalesReconciliationSnapshot
from app.models.system import SysAuditLog, SysRawFile, SysUser
from app.services.sales_reconciliation import latest_snapshots_by_raw_order_id
from scripts import backfill_sales_reconciliation as bs
from tests.test_sales_reconciliation_import import LINE_ID, _row, _write_xlsx

# 第二张订单（不同数据ID/订单编号/行ID），用于多文件执行场景
ROW2 = {"订单编号(必填)": "XSDD-20260630-0002", "数据ID(不可修改)": "RAW-RECON-2",
        LINE_ID: "LINE-RECON-2"}


@pytest.fixture(autouse=True)
def _statement_timeout_guard(db):
    """S8 防挂死守护（不新增依赖的 SET 方案）：pytest-timeout 不在本仓 dev 依赖
    （pyproject/uv.lock 均无），故在**连接池 checkout 事件**上给每条检出的连接
    ``set_config('statement_timeout'/'lock_timeout', …, is_local=true)``。

    本文件的历史事故是回填退回阻塞式取锁 → CI 全量确定性卡死；statement_timeout
    兜住一切等待锁的语句（advisory lock / 行锁），超时由服务端取消报错，测试以
    失败结束而不是拖死进程。选 is_local=true（事务级）：连接归还池时的 ROLLBACK
    自动还原，绝不把超时泄漏给同引擎的其他测试模块；checkout 覆盖 Session 跨
    事务边界轮换出的每条连接（commit 归还 → 下次事务重新检出即重设）。脚本内部
    的专用锁连接不经本池、取锁又是 try 模式——其阻塞回归由
    test_execute_refused_immediately_when_lock_held_elsewhere 的线程 join 超时兜底。
    """
    engine = db.get_bind()

    def _set_timeouts(dbapi_conn, _record, _proxy) -> None:
        with dbapi_conn.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('statement_timeout', '30000', true), "
                "set_config('lock_timeout', '10000', true)")

    event.listen(engine.pool, "checkout", _set_timeouts)
    try:
        yield
    finally:
        event.remove(engine.pool, "checkout", _set_timeouts)


def _import_one(db, tmp_path, name, rows):
    path = _write_xlsx(tmp_path / name, rows)
    batch = pipeline.run_import(db, path, name)
    db.commit()
    return batch


def _degrade_to_pre_pr2(db):
    """把库降级回 PR-2 之前：清空快照表、抹掉含税三件套（模拟历史库）。"""
    db.execute(delete(FSalesReconciliationSnapshot))
    db.execute(update(FSalesOrder).values(
        is_tax_inclusive=None, tax_amount=None, amount_inc_tax=None))
    db.commit()


def _operator(db, username="backfill-op", *, active=True):
    db.add(SysUser(username=username, password_hash="pbkdf2$1$salt$hash",
                   role="admin", is_active=active))
    db.commit()
    return username


def _order(db, raw_id="RAW-RECON-1"):
    return db.scalar(select(FSalesOrder).where(FSalesOrder.raw_order_id == raw_id))


def _snap_count(db):
    return db.scalar(select(func.count()).select_from(FSalesReconciliationSnapshot))


def _raw_file(db, batch_id):
    return db.scalar(select(SysRawFile).where(SysRawFile.batch_id == batch_id))


# ---------- dry-run：只读 + 计数 ----------

def test_dry_run_writes_nothing_and_reports_counts(db, tmp_path):
    batch = _import_one(db, tmp_path, "a.xlsx", [_row()])
    _degrade_to_pre_pr2(db)

    report = bs.run_backfill(db)

    assert report["status"] == "ready"
    assert report["dry_run"] is True
    assert len(report["manifest_sha256"]) == 64
    # 只读：快照仍为 0、税列仍为 NULL
    assert _snap_count(db) == 0
    order = _order(db)
    assert order.amount_inc_tax is None and order.tax_amount is None
    assert order.is_tax_inclusive is None

    file0 = report["files"][0]
    assert file0["batch_id"] == batch.id
    assert file0["status"] == "planned"
    assert file0["snapshots_planned"] == 1
    assert file0["tax_fills_planned"] == 3
    assert file0["snapshot_amount_totals"]["reconciled_amount"] == "100.00"
    totals = report["totals"]
    assert totals["snapshots_planned"] == 1
    assert totals["orders_with_planned_snapshots"] == 1
    assert totals["tax_fills_planned_by_column"] == {
        "is_tax_inclusive": 1, "tax_amount": 1, "amount_inc_tax": 1}
    assert totals["latest_per_order"]["reconciled_amount"] == "100.00"
    assert totals["tax_unresolved"] == 0


# ---------- execute：与 dry-run 一致 + 落库 + 审计 ----------

def test_execute_matches_dry_run_and_writes(db, tmp_path):
    batch = _import_one(db, tmp_path, "a.xlsx", [_row()])
    _degrade_to_pre_pr2(db)
    dry = bs.run_backfill(db)
    operator = _operator(db)

    applied = bs.run_backfill(db, execute=True, operator=operator,
                              confirm_manifest_sha256=dry["manifest_sha256"])

    assert applied["status"] == "applied"
    assert applied["totals"]["files_executed"] == 1
    assert applied["totals"]["snapshots_inserted"] == dry["totals"]["snapshots_planned"]
    assert applied["totals"]["snapshots_inserted"] == 1
    assert applied["totals"]["tax_columns_filled"] == 3

    snap = db.scalar(select(FSalesReconciliationSnapshot))
    assert snap.source == "backfill"
    assert snap.import_batch_id == batch.id
    assert snap.sales_order_id == _order(db).id
    assert snap.reconciled_amount == Decimal("100.00")
    assert snap.order_amount == Decimal("113.00")

    order = _order(db)
    assert order.amount_inc_tax == Decimal("113.00")
    assert order.tax_amount == Decimal("13.00")
    assert order.is_tax_inclusive is True

    audit = db.scalars(select(SysAuditLog).where(
        SysAuditLog.entity_type == "sales_reconciliation_backfill")).all()
    assert len(audit) == 1
    assert audit[0].entity_id == batch.id
    assert audit[0].operated_by == operator
    assert dry["manifest_sha256"] in (audit[0].reason or "")


def test_second_execute_adds_zero(db, tmp_path):
    _import_one(db, tmp_path, "a.xlsx", [_row()])
    _degrade_to_pre_pr2(db)
    operator = _operator(db)
    sha1 = bs.run_backfill(db)["manifest_sha256"]
    bs.run_backfill(db, execute=True, operator=operator, confirm_manifest_sha256=sha1)

    dry2 = bs.run_backfill(db)
    assert dry2["totals"]["snapshots_planned"] == 0    # 同批次已记观测 → 重跑 0 新增
    assert dry2["totals"]["tax_fills_planned"] == 0    # 税列非空 → 无可填
    applied2 = bs.run_backfill(db, execute=True, operator=operator,
                               confirm_manifest_sha256=dry2["manifest_sha256"])

    assert applied2["totals"]["snapshots_inserted"] == 0
    assert applied2["totals"]["tax_columns_filled"] == 0
    assert _snap_count(db) == 1
    assert _order(db).tax_amount == Decimal("13.00")   # 第一次的成果不被破坏


# ---------- execute 护栏 ----------

def test_execute_requires_operator_and_confirmation(db, tmp_path):
    _import_one(db, tmp_path, "a.xlsx", [_row()])
    _degrade_to_pre_pr2(db)
    with pytest.raises(bs.BackfillError) as ei:
        bs.run_backfill(db, execute=True)
    assert ei.value.code == "execution_confirmation_required"


def test_execute_refused_for_unknown_or_disabled_operator(db, tmp_path):
    _import_one(db, tmp_path, "a.xlsx", [_row()])
    _degrade_to_pre_pr2(db)
    _operator(db, "disabled-op", active=False)

    with pytest.raises(bs.BackfillError) as ei:
        bs.run_backfill(db, execute=True, operator="nobody",
                        confirm_manifest_sha256="0" * 64)
    assert ei.value.code == "operator_not_active"
    with pytest.raises(bs.BackfillError) as ei:
        bs.run_backfill(db, execute=True, operator="disabled-op",
                        confirm_manifest_sha256="0" * 64)
    assert ei.value.code == "operator_not_active"
    assert _snap_count(db) == 0


def test_execute_sha_mismatch_refused_without_writes(db, tmp_path):
    _import_one(db, tmp_path, "a.xlsx", [_row()])
    _degrade_to_pre_pr2(db)
    operator = _operator(db)

    with pytest.raises(bs.BackfillError) as ei:
        bs.run_backfill(db, execute=True, operator=operator,
                        confirm_manifest_sha256="deadbeef")
    assert ei.value.code == "manifest_confirmation_mismatch"
    assert _snap_count(db) == 0
    assert _order(db).tax_amount is None


# ---------- advisory lock：连接绑定与并发拒绝（CI 卡死回归） ----------

def _lock_is_free(probe) -> bool:
    """用连接探测回填锁是否空闲：拿到即放，返回能否拿到。

    probe 必须是一条在回填开始前就检出、并全程持在池外的连接——从池内探测
    可能恰好检出滞留锁的持有者（同一会话重复取同一把锁会成功），测不出滞留。
    """
    got = probe.execute(select(func.pg_try_advisory_lock(bs._lock_key()))).scalar_one()
    if got:
        probe.execute(select(func.pg_advisory_unlock(bs._lock_key())))
    return got is True


def test_execute_releases_lock_despite_pool_connection_rotation(db, tmp_path):
    """回归（CI 卡死根因）：池内多条连接轮换时，execute 全程结束后锁必须已释放。

    旧实现把会话级锁记在 ORM Session 的连接上，commit/rollback 归还连接后，
    解锁可能落在另一条连接上：解锁失效、锁随池内空闲连接滞留永不释放，之后
    任何取同一把锁的语句永久阻塞。预热四条空闲连接（QueuePool FIFO）：探测
    连接钉走一条、专用锁连接再占一条，Session 的「计划事务 → 单文件事务 →
    收尾」仍轮换两条连接；若退化为把锁记在 Session 连接上（旧实现），计划
    事务与解锁事务必然落在不同连接（池内三条空闲、三次检出），解锁失效→
    探测连接（全程持在池外，绝不可能是锁持有者）取不到锁而变红。
    """
    _import_one(db, tmp_path, "good.xlsx", [_row()])
    _degrade_to_pre_pr2(db)
    operator = _operator(db)

    engine = db.get_bind()
    warm = [engine.connect() for _ in range(4)]   # 同时检出四条再还回 → 池内四条空闲
    for conn in warm:
        conn.exec_driver_sql("SELECT 1")
    for conn in warm:
        conn.close()
    probe = engine.connect()                      # 池外钉住：回填全程拿不到它
    seen: set[int] = set()

    def _record_checkout(dbapi_conn, _record, _proxy) -> None:
        seen.add(id(dbapi_conn))

    try:
        event.listen(engine.pool, "checkout", _record_checkout)
        dry = bs.run_backfill(db)
        seen.clear()                              # 只统计 execute 期间的连接检出
        applied = bs.run_backfill(db, execute=True, operator=operator,
                                  confirm_manifest_sha256=dry["manifest_sha256"])

        assert applied["status"] == "applied"
        assert applied["totals"]["files_executed"] == 1
        assert _snap_count(db) == 1
        # 前提锁死：execute 期间至少 3 条不同底层连接被检出（专用锁连接 +
        # Session 跨事务边界轮换的 ≥2 条）。池策略（如改 LIFO）或池容量变化
        # 让轮换消失时，这里先红——而不是回归覆盖静默失效、缺陷重新溜进来。
        # 专用锁连接已改为独立 NullPool engine（不进池、不计入 seen），故此处只要求
        # Session 跨事务边界轮换 ≥2 条——旧实现的锁就落在这些轮换连接上。
        assert len(seen) >= 2
        assert _lock_is_free(probe) is True       # 执行结束后锁不滞留
    finally:
        event.remove(engine.pool, "checkout", _record_checkout)
        probe.close()


def test_execute_refused_immediately_when_lock_held_elsewhere(db, tmp_path, capsys):
    """并发护栏：锁被另一数据库会话持有时，execute 立即失败 backfill_already_running。

    线程超时是防回归的 CI 卡死保险：万一退回阻塞式取锁，本用例以失败告终而
    不是拖死整个测试进程（daemon 线程不阻塞进程退出）。输入全部有效，拒绝的
    唯一原因就是锁被占。
    """
    _import_one(db, tmp_path, "a.xlsx", [_row()])
    _degrade_to_pre_pr2(db)
    operator = _operator(db)
    dry = bs.run_backfill(db)

    other = db.get_bind().engine.connect()        # 另一数据库会话先持锁
    worker: threading.Thread | None = None
    try:
        # pg_advisory_lock 返回 void 不可断言；try 版返回 bool（此刻锁空闲，必真）
        assert other.execute(
            select(func.pg_try_advisory_lock(bs._lock_key()))).scalar_one() is True
        other.rollback()                          # 会话级锁跨事务存活，连接回到 idle

        outcome: dict[str, object] = {}

        def _attempt() -> None:
            try:
                with SessionLocal() as session:
                    # sha 故意给错：锁拒绝先于 manifest 校验（拒绝路径不写库）；
                    # 若退回阻塞式取锁、超时后 other 失效放锁，解除阻塞的 worker
                    # 也会在 manifest 校验被拒——僵尸线程在任何情况下都写不了库，
                    # 不会与后续用例的 TRUNCATE 交错扩散假失败。
                    outcome["result"] = bs.run_backfill(
                        session, execute=True, operator=operator,
                        confirm_manifest_sha256="0" * 64)
            except BaseException as exc:   # noqa: BLE001 —— 原样带回主线程断言
                outcome["error"] = exc

        worker = threading.Thread(target=_attempt, daemon=True, name="backfill-lock-contention")
        worker.start()
        worker.join(timeout=15)
        assert not worker.is_alive(), "run_backfill 阻塞在锁上（应立即 backfill_already_running）"
        assert "result" not in outcome, f"应拒绝执行，却返回了 {outcome.get('result')!r}"
        assert isinstance(outcome.get("error"), bs.BackfillError)
        assert outcome["error"].code == "backfill_already_running"

        assert _snap_count(db) == 0               # 拒绝发生在任何写入之前
        assert _order(db).tax_amount is None
        # CLI 同口径拒绝：退出码 2 + JSON 带 backfill_already_running（与其他错误码一致）
        argv = ["--execute", "--operator", operator,
                "--confirm-manifest-sha256", dry["manifest_sha256"]]
        assert bs.main(argv) == 2
        failed = json.loads(capsys.readouterr().out)
        assert failed["code"] == "backfill_already_running"
        # other 的锁没有被误放：同会话解锁成功（返回 true）
        assert other.execute(
            select(func.pg_advisory_unlock(bs._lock_key()))).scalar_one() is True
    finally:
        if worker is not None and worker.is_alive():
            # 阻塞式取锁回归（worker 超时未归）：保持持锁连接**脱离池且不放锁**，
            # worker 无害阻塞到进程退出（服务端会话随之结束、锁必然释放）。此刻
            # invalidate 放锁反而会唤醒 worker 跑完整回填，回归代码下其解锁还可能
            # 再次滞留锁——把单点红放大成后续用例挂死/污染。
            other.detach()
        else:
            # invalidate 强制断开：无论断言在哪一步失败，服务端会话结束必然放锁，
            # 绝不把可能持锁的连接还回池污染后续用例
            other.invalidate()

    # 锁让出后同一指纹可正常执行（并发拒绝不留残余状态）
    applied = bs.run_backfill(db, execute=True, operator=operator,
                              confirm_manifest_sha256=dry["manifest_sha256"])
    assert applied["status"] == "applied"
    assert applied["totals"]["snapshots_inserted"] == 1


def test_execute_releases_lock_after_guardrail_refusals(db, tmp_path):
    """异常出口（operator 无效 / sha 不符）同样放锁——finally 路径不留滞留锁。"""
    _import_one(db, tmp_path, "a.xlsx", [_row()])
    _degrade_to_pre_pr2(db)
    _operator(db, "disabled-op", active=False)
    operator = _operator(db)
    dry = bs.run_backfill(db)

    probe = db.get_bind().engine.connect()
    try:
        with pytest.raises(bs.BackfillError) as ei:
            bs.run_backfill(db, execute=True, operator="disabled-op",
                            confirm_manifest_sha256=dry["manifest_sha256"])
        assert ei.value.code == "operator_not_active"
        assert _lock_is_free(probe) is True

        with pytest.raises(bs.BackfillError) as ei:
            bs.run_backfill(db, execute=True, operator=operator,
                            confirm_manifest_sha256="deadbeef")
        assert ei.value.code == "manifest_confirmation_mismatch"
        assert _lock_is_free(probe) is True
    finally:
        probe.close()


def test_lock_connection_has_tcp_keepalives_and_leaves_pool(db):
    """专用锁连接：独立 NullPool（不进业务连接池）+ libpq TCP keepalive 已生效。"""
    lock = bs._acquire_backfill_lock(db)
    try:
        params = lock.conn.connection.dbapi_connection.info.get_parameters()
        for key, value in bs.LOCK_CONNECT_ARGS.items():
            assert params.get(key) == str(value), key
        assert lock.conn.engine is not db.get_bind().engine
        app_name = db.execute(text(
            "SELECT application_name FROM pg_stat_activity WHERE pid = :pid"),
            {"pid": lock.pid}).scalar_one()
        assert app_name == bs.LOCK_CONNECT_ARGS["application_name"]
        bs._assert_lock_held(db, lock)            # 持锁时校验通过
    finally:
        bs._release_backfill_lock(lock)
        db.rollback()
    probe = db.get_bind().engine.connect()
    try:
        assert _lock_is_free(probe) is True
    finally:
        probe.close()


def test_execute_aborts_when_lock_lost_before_commit(db, tmp_path, monkeypatch):
    """锁连接中途被断开（模拟空闲 TCP 被掐/会话被杀）：提交前 pg_locks 校验发现丢锁，
    回滚当前文件并中止整个执行（backfill_lock_lost），后续文件不再处理，锁不滞留。"""
    _import_one(db, tmp_path, "a.xlsx", [_row()])
    _import_one(db, tmp_path, "b.xlsx", [_row(**ROW2)])
    _degrade_to_pre_pr2(db)
    operator = _operator(db)
    dry = bs.run_backfill(db)
    assert dry["totals"]["snapshots_planned"] == 2

    killer = db.get_bind().engine.connect()
    calls: list[int] = []
    real_apply = bs._apply_file

    def _apply_then_kill_lock(session, batch, plan, op, sha):
        calls.append(batch.id)
        out = real_apply(session, batch, plan, op, sha)
        # 等待式终止（PG14+）：返回时持锁后端已退出、锁已释放，避免竞态
        killed = killer.execute(text(
            "SELECT pg_terminate_backend(l.pid, 10000) FROM pg_locks l"
            " WHERE l.locktype = 'advisory' AND l.objsubid = 1"
            " AND ((l.classid::bigint << 32) | l.objid::bigint) = hashtextextended(:seed, 0)"),
            {"seed": bs._LOCK_KEY_SEED}).scalars().all()
        killer.rollback()
        assert killed == [True]
        return out

    monkeypatch.setattr(bs, "_apply_file", _apply_then_kill_lock)
    try:
        with pytest.raises(bs.BackfillError) as ei:
            bs.run_backfill(db, execute=True, operator=operator,
                            confirm_manifest_sha256=dry["manifest_sha256"])
        assert ei.value.code == "backfill_lock_lost"
        assert len(calls) == 1                    # 中止：第二个文件没被处理
        db.rollback()
        assert _snap_count(db) == 0               # 丢锁的文件已回滚，什么都没落盘
        assert all(o.amount_inc_tax is None for o in db.scalars(select(FSalesOrder)))
        assert _lock_is_free(killer) is True      # 锁不滞留
    finally:
        killer.close()


# ---------- 坏档隔离 ----------

def test_archive_missing_hash_mismatch_and_corrupt_are_isolated(db, tmp_path):
    b1 = _import_one(db, tmp_path, "a.xlsx",
                     [_row(**{"数据ID(不可修改)": "RAW-BF-1"})])
    b2 = _import_one(db, tmp_path, "b.xlsx",
                     [_row(**{"数据ID(不可修改)": "RAW-BF-2"})])
    b3 = _import_one(db, tmp_path, "c.xlsx",
                     [_row(**{"数据ID(不可修改)": "RAW-BF-3"})])
    # b1 归档被删；b2 归档被篡改（哈希不符）；b3 完好
    Path(_raw_file(db, b1.id).storage_path).unlink()
    Path(_raw_file(db, b2.id).storage_path).write_bytes(b"tampered")
    _degrade_to_pre_pr2(db)
    operator = _operator(db)

    dry = bs.run_backfill(db)
    by_batch = {f["batch_id"]: f for f in dry["files"]}
    assert by_batch[b1.id]["status"] == "archive_missing"
    assert by_batch[b2.id]["status"] == "archive_hash_mismatch"
    assert by_batch[b3.id]["status"] == "planned"
    for bad in (b1, b2):
        assert by_batch[bad.id]["tax_unresolved"] == 3   # 1 单 × 3 NULL 列

    applied = bs.run_backfill(db, execute=True, operator=operator,
                              confirm_manifest_sha256=dry["manifest_sha256"])
    # S4：坏档不计执行——files[].status 保持其计划状态、files_skipped 单列计数、
    # 不触发 partial（dry-run 报告已如实预告的坏档，对执行而言是已知的零工作
    # 量而非失败；全部坏档/空范围时同样 applied、退出码 0）
    by_status = {f["batch_id"]: f["status"] for f in applied["files"]}
    assert by_status == {b1.id: "archive_missing", b2.id: "archive_hash_mismatch",
                         b3.id: "executed"}
    assert applied["status"] == "applied"
    assert applied["totals"]["files_executed"] == 1
    assert applied["totals"]["files_skipped"] == 2
    assert applied["totals"]["files_failed"] == 0
    assert applied["totals"]["snapshots_inserted"] == 1
    assert applied["totals"]["tax_columns_filled"] == 3
    # 审计口径一致：每文件一条；坏档 action=skip（零写入留痕），好档 action=execute
    audits = {a.entity_id: a.action for a in db.scalars(select(SysAuditLog).where(
        SysAuditLog.entity_type == "sales_reconciliation_backfill"))}
    assert audits == {b1.id: "skip", b2.id: "skip", b3.id: "execute"}

    assert _order(db, "RAW-BF-3").tax_amount == Decimal("13.00")   # 好档照常回填
    for raw_id in ("RAW-BF-1", "RAW-BF-2"):                        # 坏档保持 NULL
        bad_order = _order(db, raw_id)
        assert bad_order.tax_amount is None
        assert bad_order.amount_inc_tax is None
    snaps = db.scalars(select(FSalesReconciliationSnapshot)).all()
    assert [s.raw_order_id for s in snaps] == ["RAW-BF-3"]


def test_hash_valid_but_corrupt_file_is_parse_failed(db, tmp_path):
    batch = _import_one(db, tmp_path, "a.xlsx", [_row()])
    garbage = b"definitely not an xlsx"
    digest = hashlib.sha256(garbage).hexdigest()
    raw = _raw_file(db, batch.id)
    Path(raw.storage_path).write_bytes(garbage)
    raw.file_hash = digest          # 哈希与字节一致：verify 过、openpyxl 解析挂
    batch.file_hash = digest
    db.commit()
    _degrade_to_pre_pr2(db)

    dry = bs.run_backfill(db)
    file0 = dry["files"][0]
    assert file0["status"] == "parse_failed"
    assert file0["tax_unresolved"] == 3
    assert _snap_count(db) == 0


# ---------- 税列口径：冲突不覆盖 / 只认订单自己批次的原件 ----------

def test_tax_all_filled_matching_no_conflict_no_fill(db, tmp_path):
    """三列全填且与原件一致：无冲突无补写（比对覆盖该批次全部订单，不只 NULL 单）。"""
    _import_one(db, tmp_path, "a.xlsx", [_row()])    # 不降级：三件套导入即写
    dry = bs.run_backfill(db)

    file0 = dry["files"][0]
    assert file0["status"] == "planned"
    assert file0["tax_fills_planned"] == 0
    assert file0["tax_conflicts"] == []
    assert file0["tax_unresolved"] == 0
    assert dry["totals"]["tax_conflicts_total"] == 0


def test_tax_all_filled_single_column_differs_reports_conflict_no_write(db, tmp_path):
    """三列全填、仅税金与原件不同：报冲突不改库；dry 与 execute 冲突计数一致。"""
    _import_one(db, tmp_path, "a.xlsx", [_row()])
    db.execute(update(FSalesOrder).where(FSalesOrder.raw_order_id == "RAW-RECON-1")
               .values(tax_amount=Decimal("999.00")))
    db.commit()
    operator = _operator(db)

    dry = bs.run_backfill(db)
    file0 = dry["files"][0]
    assert file0["tax_fills_planned"] == 0           # 无 NULL 可补，但仍要比对
    assert len(file0["tax_conflicts"]) == 1
    conflict = file0["tax_conflicts"][0]
    assert conflict["raw_order_id"] == "RAW-RECON-1"
    assert conflict["order_no"] == "XSDD-20260630-0001"
    assert conflict["column"] == "tax_amount"
    assert conflict["db_value"] == "999.00"
    assert conflict["file_value"] == "13.00"

    applied = bs.run_backfill(db, execute=True, operator=operator,
                              confirm_manifest_sha256=dry["manifest_sha256"])
    assert applied["files"][0]["tax_conflicts"] == 1         # 与 dry-run 同计数
    assert applied["totals"]["tax_conflicts_total"] == dry["totals"]["tax_conflicts_total"]
    assert _order(db).tax_amount == Decimal("999.00")        # 冲突不覆盖


def test_tax_partial_null_plus_conflict_fills_and_reports(db, tmp_path):
    """部分 NULL 部分不同：NULL 补写 + 不同报冲突；重跑幂等（零写入、冲突计数稳定）。"""
    _import_one(db, tmp_path, "a.xlsx", [_row()])
    db.execute(update(FSalesOrder).where(FSalesOrder.raw_order_id == "RAW-RECON-1")
               .values(is_tax_inclusive=None, tax_amount=Decimal("999.00")))
    db.commit()
    operator = _operator(db)

    dry = bs.run_backfill(db)
    file0 = dry["files"][0]
    assert file0["tax_fills_planned"] == 1           # 是否含税 NULL → 补
    assert {c["column"] for c in file0["tax_conflicts"]} == {"tax_amount"}

    applied = bs.run_backfill(db, execute=True, operator=operator,
                              confirm_manifest_sha256=dry["manifest_sha256"])
    assert applied["totals"]["tax_columns_filled"] == 1
    order = _order(db)
    assert order.is_tax_inclusive is True            # NULL 已补
    assert order.tax_amount == Decimal("999.00")     # 冲突保持库内值

    dry2 = bs.run_backfill(db)                       # 重跑：0 补写，冲突如实仍在
    file2 = dry2["files"][0]
    assert file2["tax_fills_planned"] == 0
    assert file2["tax_conflicts"] == file0["tax_conflicts"]
    applied2 = bs.run_backfill(db, execute=True, operator=operator,
                               confirm_manifest_sha256=dry2["manifest_sha256"])
    assert applied2["totals"]["tax_columns_filled"] == 0
    assert applied2["totals"]["tax_conflicts_total"] == 1


def test_manifest_sha_reflects_conflict_details(db, tmp_path):
    """冲突明细纳入 manifest 指纹：库内冲突值变化 → dry-run 指纹变化（计数不变也拦得住）。"""
    _import_one(db, tmp_path, "a.xlsx", [_row()])
    db.execute(update(FSalesOrder).where(FSalesOrder.raw_order_id == "RAW-RECON-1")
               .values(tax_amount=Decimal("999.00")))
    db.commit()
    sha1 = bs.run_backfill(db)["manifest_sha256"]

    # 冲突计数不变（仍 1 条 tax_amount），只有冲突值变了 → 旧口径（只计条数）指纹不变
    db.execute(update(FSalesOrder).where(FSalesOrder.raw_order_id == "RAW-RECON-1")
               .values(tax_amount=Decimal("888.00")))
    db.commit()
    sha2 = bs.run_backfill(db)["manifest_sha256"]
    assert sha2 != sha1


def test_tax_conflict_recorded_not_overwritten(db, tmp_path):
    _import_one(db, tmp_path, "a.xlsx", [_row()])
    _degrade_to_pre_pr2(db)
    # 库内已有不同非空值（模拟他口径写入）→ 记冲突不覆盖；NULL 列照常补
    db.execute(update(FSalesOrder).where(FSalesOrder.raw_order_id == "RAW-RECON-1")
               .values(tax_amount=Decimal("999.00")))
    db.commit()
    operator = _operator(db)

    dry = bs.run_backfill(db)
    file0 = dry["files"][0]
    assert file0["tax_fills_planned"] == 2
    conflicts = file0["tax_conflicts"]
    assert len(conflicts) == 1
    assert conflicts[0]["column"] == "tax_amount"
    assert conflicts[0]["db_value"] == "999.00"
    assert conflicts[0]["file_value"] == "13.00"

    bs.run_backfill(db, execute=True, operator=operator,
                    confirm_manifest_sha256=dry["manifest_sha256"])
    order = _order(db)
    assert order.tax_amount == Decimal("999.00")        # 冲突保持库内值
    assert order.amount_inc_tax == Decimal("113.00")    # NULL 列照填
    assert order.is_tax_inclusive is True


def test_tax_values_come_from_owning_batch_only(db, tmp_path):
    # b1：订单 A 三件套 113/13/含税；b2：同订单另一份文件给出 999/26/不含税
    b1 = _import_one(db, tmp_path, "a.xlsx", [_row()])
    b2 = _import_one(db, tmp_path, "b.xlsx", [
        _row(订单金额=Decimal("999.00"), 不含税金额=Decimal("888.00"),
             税金=Decimal("26.00"), 是否含税="不含税", 已对账金额=Decimal("60.00"))])
    assert b1.id != b2.id
    _degrade_to_pre_pr2(db)
    operator = _operator(db)

    dry = bs.run_backfill(db)
    by_batch = {f["batch_id"]: f for f in dry["files"]}
    assert by_batch[b1.id]["tax_fills_planned"] == 3    # A 属于 b1（import_batch_id）
    assert by_batch[b2.id]["tax_fills_planned"] == 0    # b2 名下无订单

    bs.run_backfill(db, execute=True, operator=operator,
                    confirm_manifest_sha256=dry["manifest_sha256"])
    order = _order(db)
    assert order.amount_inc_tax == Decimal("113.00")    # 只认 b1 原件，不被 b2 顶掉
    assert order.tax_amount == Decimal("13.00")
    assert order.is_tax_inclusive is True


# ---------- 批次范围 ----------

def test_superseded_batches_included(db, tmp_path):
    # superseded 批次的产生方式：同哈希文件以 upsert 模式重导（skip 模式会被去重拒绝）
    path = _write_xlsx(tmp_path / "a.xlsx", [_row()])
    b1 = pipeline.run_import(db, path, "a.xlsx")
    db.commit()
    b2 = pipeline.run_import(db, path, "a.xlsx", mode="upsert")
    db.commit()
    db.refresh(b1)
    assert b1.status == "superseded"
    _degrade_to_pre_pr2(db)
    operator = _operator(db)

    dry = bs.run_backfill(db)
    assert {f["batch_id"] for f in dry["files"]} == {b1.id, b2.id}  # superseded 也在范围
    assert dry["totals"]["snapshots_planned"] == 2   # 每批次各一条观测（内容相同不去重）
    assert dry["totals"]["orders_with_planned_snapshots"] == 1

    bs.run_backfill(db, execute=True, operator=operator,
                    confirm_manifest_sha256=dry["manifest_sha256"])
    snaps = db.scalars(select(FSalesReconciliationSnapshot)).all()
    assert {s.import_batch_id for s in snaps} == {b1.id, b2.id}   # 两批各一条
    assert len({s.content_hash for s in snaps}) == 1              # 内容确实相同
    latest = latest_snapshots_by_raw_order_id(db, ["RAW-RECON-1"])
    assert latest["RAW-RECON-1"].import_batch_id == max(b1.id, b2.id)


def test_backfill_aba_null_times_three_observations_latest_is_a(db, tmp_path):
    """回填路径的 A→B→A（修改时间全空）：三个批次各回放一条观测，最新 = 批次最大的 A。

    dry-run 与 execute 计数一致；旧口径 (raw_order_id, content_hash) 唯一会把
    第三批的 A 当重复跳过、latest_per_order 错误地按 B 汇总。
    """
    _import_one(db, tmp_path, "aba1.xlsx", [
        _row(已对账金额=Decimal("100.00"),
             **{"修改时间(必填)": None, "订单明细.产品名称": "TEST-PN-A"})])
    _import_one(db, tmp_path, "aba2.xlsx", [
        _row(已对账金额=Decimal("60.00"),
             **{"修改时间(必填)": None, "订单明细.产品名称": "TEST-PN-B"})])
    b3 = _import_one(db, tmp_path, "aba3.xlsx", [
        _row(已对账金额=Decimal("100.00"),
             **{"修改时间(必填)": None, "订单明细.产品名称": "TEST-PN-C"})])
    _degrade_to_pre_pr2(db)

    dry = bs.run_backfill(db)
    assert dry["totals"]["snapshots_planned"] == 3
    assert dry["totals"]["orders_with_planned_snapshots"] == 1
    assert dry["totals"]["latest_per_order"]["reconciled_amount"] == "100.00"
    operator = _operator(db)
    applied = bs.run_backfill(db, execute=True, operator=operator,
                              confirm_manifest_sha256=dry["manifest_sha256"])
    assert applied["totals"]["snapshots_inserted"] == dry["totals"]["snapshots_planned"]

    snaps = db.scalars(select(FSalesReconciliationSnapshot).order_by(
        FSalesReconciliationSnapshot.import_batch_id)).all()
    assert [s.reconciled_amount for s in snaps] == [
        Decimal("100.00"), Decimal("60.00"), Decimal("100.00")]
    assert all(s.source_modified_at is None for s in snaps)
    latest = latest_snapshots_by_raw_order_id(db, ["RAW-RECON-1"])
    assert latest["RAW-RECON-1"].import_batch_id == b3.id
    assert latest["RAW-RECON-1"].reconciled_amount == Decimal("100.00")


def test_latest_per_order_timed_beats_null_modified_time(db, tmp_path):
    """latest_per_order 口径：无修改时间的观测让位给任何有值观测（报表层比较器）。

    锁定 _is_newer_observation 的「空值排后」分支——此前该分支零覆盖，
    且 A→B→A 断言用回文序列对选择器漂移不设防（对抗审查发现）。
    """
    _import_one(db, tmp_path, "m1.xlsx", [
        _row(已对账金额=Decimal("100.00"), **{"修改时间(必填)": None})])
    _import_one(db, tmp_path, "m2.xlsx", [
        _row(已对账金额=Decimal("60.00"),
             **{"修改时间(必填)": "2026-07-02 09:30:00"})])
    _degrade_to_pre_pr2(db)

    dry = bs.run_backfill(db)
    assert dry["totals"]["snapshots_planned"] == 2
    assert dry["totals"]["latest_per_order"]["reconciled_amount"] == "60.00"


def test_latest_per_order_newer_time_beats_larger_batch(db, tmp_path):
    """latest_per_order 口径：修改时间较新者优先于批次号较大者（报表层比较器）。"""
    _import_one(db, tmp_path, "t1.xlsx", [
        _row(已对账金额=Decimal("100.00"),
             **{"修改时间(必填)": "2026-07-03 09:30:00"})])
    _import_one(db, tmp_path, "t2.xlsx", [
        _row(已对账金额=Decimal("60.00"),
             **{"修改时间(必填)": "2026-07-02 09:30:00"})])
    _degrade_to_pre_pr2(db)

    dry = bs.run_backfill(db)
    assert dry["totals"]["snapshots_planned"] == 2
    assert dry["totals"]["latest_per_order"]["reconciled_amount"] == "100.00"


def test_import_after_backfill_same_content_appends_observation(db, tmp_path):
    """回填后再常规导入同内容：新批次再记一条观测（source=import），不因内容相同被吞。"""
    b1 = _import_one(db, tmp_path, "a.xlsx", [_row()])
    _degrade_to_pre_pr2(db)
    dry = bs.run_backfill(db)
    operator = _operator(db)
    bs.run_backfill(db, execute=True, operator=operator,
                    confirm_manifest_sha256=dry["manifest_sha256"])
    assert _snap_count(db) == 1

    # 文件字节与 a.xlsx 不同（明细产品名不同，避免同哈希被 skip 模式拒绝）
    b2 = _import_one(db, tmp_path, "c.xlsx",
                     [_row(**{"订单明细.产品名称": "TEST-PN-AFTER"})])
    snaps = db.scalars(select(FSalesReconciliationSnapshot).order_by(
        FSalesReconciliationSnapshot.id)).all()
    assert len(snaps) == 2
    assert {s.source for s in snaps} == {"backfill", "import"}
    assert {s.import_batch_id for s in snaps} == {b1.id, b2.id}
    latest = latest_snapshots_by_raw_order_id(db, ["RAW-RECON-1"])
    assert latest["RAW-RECON-1"].source == "import"
    assert latest["RAW-RECON-1"].import_batch_id == b2.id


def test_batch_id_scoping_and_invalid_scope(db, tmp_path):
    b1 = _import_one(db, tmp_path, "a.xlsx",
                     [_row(**{"数据ID(不可修改)": "RAW-SC-1"})])
    _import_one(db, tmp_path, "b.xlsx",
                [_row(**{"数据ID(不可修改)": "RAW-SC-2"})])
    _degrade_to_pre_pr2(db)

    dry = bs.run_backfill(db, batch_ids=[b1.id])
    assert [f["batch_id"] for f in dry["files"]] == [b1.id]

    with pytest.raises(bs.BackfillError) as ei:
        bs.run_backfill(db, batch_ids=[999999])
    assert ei.value.code == "invalid_batch_scope"


# ---------- CLI main() ----------

def test_main_dry_run_then_execute_and_exit_codes(db, tmp_path, capsys, monkeypatch):
    batch = _import_one(db, tmp_path, "a.xlsx", [_row()])
    _degrade_to_pre_pr2(db)
    operator = _operator(db, "cli-op")
    argv = ["--batch-id", str(batch.id)]

    assert bs.main(argv) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["dry_run"] is True and dry["status"] == "ready"
    assert _snap_count(db) == 0

    assert bs.main(argv + ["--execute", "--operator", operator,
                           "--confirm-manifest-sha256", dry["manifest_sha256"]]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["status"] == "applied"
    assert applied["totals"]["files_executed"] == 1     # 全部成功 → applied、退出码 0
    assert applied["totals"]["snapshots_inserted"] == 1

    # sha 不对 → 退出码 2，拒绝写入
    assert bs.main(argv + ["--execute", "--operator", operator,
                           "--confirm-manifest-sha256", "0" * 64]) == 2
    failed = json.loads(capsys.readouterr().out)
    assert failed["code"] == "manifest_confirmation_mismatch"
    assert _snap_count(db) == 1


def test_main_requires_database_url(capsys, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert bs.main([]) == 2
    failed = json.loads(capsys.readouterr().out)
    assert failed["code"] == "database_url_required"


# ---------- MUST：部分失败 → partial / 退出码 3；空范围语义 ----------

def test_partial_failure_reports_partial_and_exit_3(db, tmp_path, capsys, monkeypatch):
    """单文件执行失败：报告 status=partial、CLI 退出码 3（与 0/2 区分），其余
    文件照常提交；stderr 给出处置指引。修复后按同一 dry-run → 确认 → execute
    流程重跑即幂等补齐（runbook「遇 3 怎么办」的操作路径锁定）。"""
    b1 = _import_one(db, tmp_path, "a.xlsx", [_row()])
    b2 = _import_one(db, tmp_path, "b.xlsx", [_row(**ROW2)])
    _degrade_to_pre_pr2(db)
    operator = _operator(db, "partial-op")
    dry = bs.run_backfill(db)
    assert dry["totals"]["snapshots_planned"] == 2

    real_apply = bs._apply_file

    def _apply_boom_on_second(session, batch, plan, op, sha):
        if batch.id == b2.id:
            raise RuntimeError("boom: simulated per-file failure")
        return real_apply(session, batch, plan, op, sha)

    monkeypatch.setattr(bs, "_apply_file", _apply_boom_on_second)
    assert bs.main(["--execute", "--operator", operator,
                    "--confirm-manifest-sha256", dry["manifest_sha256"]]) == 3
    captured = capsys.readouterr()
    applied = json.loads(captured.out)
    assert applied["status"] == "partial"
    assert applied["totals"]["files_executed"] == 1
    assert applied["totals"]["files_failed"] == 1
    assert applied["totals"]["files_skipped"] == 0
    by_batch = {f["batch_id"]: f for f in applied["files"]}
    assert by_batch[b1.id]["status"] == "executed"
    assert by_batch[b2.id]["status"] == "execute_failed"
    assert "boom" in by_batch[b2.id]["error"]
    assert "partially applied" in captured.err
    assert _order(db, "RAW-RECON-1").tax_amount == Decimal("13.00")   # 好文件已提交
    order2 = _order(db, "RAW-RECON-2")                                # 失败文件零写入
    assert order2.tax_amount is None and order2.amount_inc_tax is None

    monkeypatch.setattr(bs, "_apply_file", real_apply)   # 修复"故障"后重走全流程
    dry2 = bs.run_backfill(db)
    assert dry2["totals"]["snapshots_planned"] == 1      # 只剩 b2 的剩余工作量
    applied2 = bs.run_backfill(db, execute=True, operator=operator,
                               confirm_manifest_sha256=dry2["manifest_sha256"])
    assert applied2["status"] == "applied"
    # 范围内两个文件都重新"执行"（b1 幂等零新增），files_executed 按文件计
    assert applied2["totals"]["files_executed"] == 2
    assert applied2["totals"]["snapshots_inserted"] == 1
    assert _snap_count(db) == 2                          # a + b 各一条，无重复行


def test_execute_with_no_files_in_scope_is_applied_exit_0(db, capsys):
    """空范围（0 个批次）execute 语义：files_executed=0、files_failed=0 →
    status=applied、退出码 0——partial 只由 files_failed>0 触发，「没活干」不是
    失败；范围是否为空以 dry-run 的 files 列表为准（执行前人工核对）。"""
    operator = _operator(db, "empty-scope-op")
    dry = bs.run_backfill(db)
    assert dry["files"] == []

    assert bs.main(["--execute", "--operator", operator,
                    "--confirm-manifest-sha256", dry["manifest_sha256"]]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["status"] == "applied"
    assert applied["files"] == []
    assert applied["totals"] == {
        "files_executed": 0, "files_skipped": 0, "files_failed": 0,
        "snapshots_inserted": 0, "tax_columns_filled": 0, "tax_conflicts_total": 0}
    assert _snap_count(db) == 0


# ---------- S1：锁内逐文件计划指纹核对 ----------

def test_plan_changed_since_dry_run_fails_file_only_zero_write(db, tmp_path, monkeypatch):
    """dry-run 后库中该批次的数据变了：锁内 fresh 计划与核准清单条目不一致 →
    该文件 execute_failed（code=plan_changed_since_dry_run）零写入，其余文件照常
    → partial。篡改经第二条显式连接在锁内重算时提交（无时序 sleep 竞争）。"""
    b1 = _import_one(db, tmp_path, "a.xlsx", [_row(**{"数据ID(不可修改)": "RAW-PC-1"})])
    b2 = _import_one(db, tmp_path, "b.xlsx", [_row(**{"数据ID(不可修改)": "RAW-PC-2"})])
    b3 = _import_one(db, tmp_path, "c.xlsx", [_row(**{"数据ID(不可修改)": "RAW-PC-3"})])
    _degrade_to_pre_pr2(db)
    operator = _operator(db)
    dry = bs.run_backfill(db)
    assert dry["totals"]["snapshots_planned"] == 3

    tamper = db.get_bind().engine.connect()      # 第二条显式连接：模拟并发改库
    real_plan = bs._plan_file
    calls: dict[int, int] = {}

    def _plan_with_tamper(session, batch):
        n = calls.get(batch.id, 0) + 1
        calls[batch.id] = n
        if batch.id == b2.id and n == 2:         # 计划阶段之后的锁内 fresh 重算
            tamper.execute(text(
                "UPDATE f_sales_order SET tax_amount = 999.00"
                " WHERE raw_order_id = 'RAW-PC-2'"))
            tamper.commit()
        return real_plan(session, batch)

    monkeypatch.setattr(bs, "_plan_file", _plan_with_tamper)
    try:
        applied = bs.run_backfill(db, execute=True, operator=operator,
                                  confirm_manifest_sha256=dry["manifest_sha256"])
    finally:
        tamper.close()

    assert applied["status"] == "partial"
    by_batch = {f["batch_id"]: f for f in applied["files"]}
    assert by_batch[b1.id]["status"] == "executed"
    assert by_batch[b2.id]["status"] == "execute_failed"
    assert by_batch[b2.id]["code"] == "plan_changed_since_dry_run"
    assert "tax_fills_plan_sha256" in by_batch[b2.id]["error"]   # 变化字段可追溯
    assert by_batch[b3.id]["status"] == "executed"               # 失败不拖垮其余文件
    assert applied["totals"]["files_executed"] == 2
    assert applied["totals"]["files_failed"] == 1
    # b2 零写入：无快照；三件套没被回填（tax_amount 是篡改值，另两列仍 NULL）
    order2 = _order(db, "RAW-PC-2")
    assert order2.tax_amount == Decimal("999.00")
    assert order2.is_tax_inclusive is None and order2.amount_inc_tax is None
    assert {s.raw_order_id for s in db.scalars(select(FSalesReconciliationSnapshot))} \
        == {"RAW-PC-1", "RAW-PC-3"}
    audits = db.scalars(select(SysAuditLog.entity_id).where(
        SysAuditLog.entity_type == "sales_reconciliation_backfill")).all()
    assert sorted(audits) == sorted([b1.id, b3.id])              # b2 无执行审计


# ---------- S2：税列补写 UPDATE 限定所属批次 ----------

def test_tax_fill_update_scoped_to_owning_batch(db, tmp_path, monkeypatch):
    """补写 UPDATE 必须带 import_batch_id == 本批次。raw_order_id 全局唯一使
    「两行同 raw_order_id」当前不可能，但 loader upsert 会把既有行迁到新批次
    （import_batch_id 随重导刷新）：dry-run 核准 A 批次补写后、UPDATE 执行前
    行被迁到 B 批次 → 无批次谓词会把 A 批次原件的值误写到已属于 B 的行上；
    有谓词则零命中，行保持 NULL 等待其真正批次处理。"""
    b1 = _import_one(db, tmp_path, "a.xlsx", [_row()])           # RAW-RECON-1 → b1
    b2 = _import_one(db, tmp_path, "b.xlsx", [_row(**ROW2)])     # 另一单；作迁移目标批次
    _degrade_to_pre_pr2(db)
    operator = _operator(db)
    dry = bs.run_backfill(db, batch_ids=[b1.id])
    assert dry["totals"]["tax_fills_planned"] == 3

    mover = db.get_bind().engine.connect()
    real_apply = bs._apply_file

    def _apply_after_moving_row(session, batch, plan, op, sha):
        # 指纹核对已过、UPDATE 未跑：此刻把行迁到 b2（并发重导的确定性模拟）
        if batch.id == b1.id:
            mover.execute(text(
                "UPDATE f_sales_order SET import_batch_id = :bid"
                " WHERE raw_order_id = 'RAW-RECON-1'"), {"bid": b2.id})
            mover.commit()
        return real_apply(session, batch, plan, op, sha)

    monkeypatch.setattr(bs, "_apply_file", _apply_after_moving_row)
    try:
        applied = bs.run_backfill(db, execute=True, operator=operator,
                                  confirm_manifest_sha256=dry["manifest_sha256"],
                                  batch_ids=[b1.id])
    finally:
        mover.close()

    assert applied["status"] == "applied"
    assert applied["totals"]["tax_columns_filled"] == 0          # 行已不属 b1：零命中
    order = _order(db, "RAW-RECON-1")
    assert order.import_batch_id == b2.id
    assert (order.is_tax_inclusive is None and order.tax_amount is None
            and order.amount_inc_tax is None)
    # 快照按 (raw_order_id, import_batch_id=b1) 计划，与本行归属无关：照常写入
    assert _snap_count(db) == 1


# ---------- S3：文件级 advisory xact lock（TOCTOU 兜底） ----------

def test_execute_aborts_when_file_xact_lock_held_elsewhere(db, tmp_path):
    """另一数据库会话持有某文件的 xact 键时：执行按 backfill_file_lock_busy
    **中止**（当前文件零写入、此前文件已提交保留、会话级锁照常释放）。选中止
    而非跳过/失败继续：文件级键被占说明互斥前提被破坏（专用锁连接疑似丢失、
    另一实例疑似在跑同一文件），继续只会扩大交错写入面——停下排查才是安全侧。"""
    _import_one(db, tmp_path, "a.xlsx", [_row()])
    b2 = _import_one(db, tmp_path, "b.xlsx", [_row(**ROW2)])
    _degrade_to_pre_pr2(db)
    operator = _operator(db)
    dry = bs.run_backfill(db)
    assert len(dry["files"]) == 2

    other = db.get_bind().engine.connect()
    probe = db.get_bind().engine.connect()
    try:
        assert other.execute(select(
            func.pg_try_advisory_xact_lock(bs._file_lock_key(b2.id)))).scalar_one() is True
        # 不提交不回滚：xact 锁随这条打开的事务一直持有

        with pytest.raises(bs.BackfillError) as ei:
            bs.run_backfill(db, execute=True, operator=operator,
                            confirm_manifest_sha256=dry["manifest_sha256"])
        assert ei.value.code == "backfill_file_lock_busy"

        assert _order(db, "RAW-RECON-1").tax_amount == Decimal("13.00")  # 文件1已提交
        order2 = _order(db, "RAW-RECON-2")                               # 文件2零写入
        assert order2.tax_amount is None and order2.amount_inc_tax is None
        assert _snap_count(db) == 1
        assert _lock_is_free(probe) is True          # 会话级锁已随 finally 释放
    finally:
        other.rollback()                            # 放开 xact 锁（断言失败也不外带）
        other.close()
        probe.close()

    # 干扰解除后同一流程重跑：两个文件都重新执行（文件1 幂等零新增），文件2 补齐
    dry2 = bs.run_backfill(db)
    applied2 = bs.run_backfill(db, execute=True, operator=operator,
                               confirm_manifest_sha256=dry2["manifest_sha256"])
    assert applied2["status"] == "applied"
    assert applied2["totals"]["files_executed"] == 2
    assert applied2["totals"]["snapshots_inserted"] == 1
    assert _order(db, "RAW-RECON-2").tax_amount == Decimal("13.00")


# ---------- S9：操作员校验在每个文件事务开头重新执行 ----------

def test_operator_disabled_midrun_aborts_current_file_rolled_back(db, tmp_path, monkeypatch):
    """FOR SHARE 行锁只保当前事务（计划事务回滚后即释放）：文件1提交后操作员
    被停用 → 文件2事务开头的重验以 operator_not_active 中止——文件2零写入零
    审计，文件1成果保留，换有效账号重跑幂等。"""
    b1 = _import_one(db, tmp_path, "a.xlsx", [_row()])
    b2 = _import_one(db, tmp_path, "b.xlsx", [_row(**ROW2)])
    _degrade_to_pre_pr2(db)
    operator = _operator(db, "midrun-op")
    dry = bs.run_backfill(db)

    disabler = db.get_bind().engine.connect()
    real_acquire = bs._acquire_file_xact_lock

    def _acquire_then_disable(session, batch_id):
        if batch_id == b2.id:
            # 此刻 run_backfill 处于两个文件事务之间（无 FOR SHARE 行锁挡路），
            # 停用立即提交；文件2事务开头的校验随即看到
            disabler.execute(text(
                "UPDATE sys_user SET is_active = false WHERE username = 'midrun-op'"))
            disabler.commit()
        return real_acquire(session, batch_id)

    monkeypatch.setattr(bs, "_acquire_file_xact_lock", _acquire_then_disable)
    try:
        with pytest.raises(bs.BackfillError) as ei:
            bs.run_backfill(db, execute=True, operator=operator,
                            confirm_manifest_sha256=dry["manifest_sha256"])
        assert ei.value.code == "operator_not_active"

        assert _order(db, "RAW-RECON-1").tax_amount == Decimal("13.00")  # 文件1已提交
        order2 = _order(db, "RAW-RECON-2")                               # 文件2零写入
        assert order2.tax_amount is None and order2.amount_inc_tax is None
        assert _snap_count(db) == 1
        audits = db.scalars(select(SysAuditLog.entity_id).where(
            SysAuditLog.entity_type == "sales_reconciliation_backfill")).all()
        assert audits == [b1.id]
    finally:
        disabler.close()


# ---------- S8：放锁/校验/中断分支 ----------

def test_release_lock_unlock_false_invalidates_connection(db, capsys):
    """放锁分支：pg_advisory_unlock 返回 false（连接实际已不持锁）→ 连接被
    invalidate 废弃（绝不能把可能仍持锁的连接还回池）、stderr 告警清楚、不抛
    异常（在 finally 里调用，抛出会顶掉正文原始异常）。"""
    lock = bs._acquire_backfill_lock(db)
    try:
        # 先手动放锁：随后 _release_backfill_lock 里的 unlock 必返回 false
        lock.conn.execute(select(func.pg_advisory_unlock(bs._lock_key())))
        lock.conn.rollback()
    except BaseException:
        bs._release_backfill_lock(lock)
        raise
    bs._release_backfill_lock(lock)

    assert "pg_advisory_unlock 返回 false" in capsys.readouterr().err
    with pytest.raises(ResourceClosedError):       # 已废弃/关闭，不可复用
        lock.conn.execute(text("SELECT 1"))
    probe = db.get_bind().engine.connect()
    try:
        assert _lock_is_free(probe) is True
    finally:
        probe.close()


class _PgLocksExplodingSession:
    """代理 Session：仅持锁校验语句（查 pg_locks）报错，其余原样转发。

    持锁校验查询本身失败无法用真实库状态确定性制造（pg_locks 恒可查），按语句
    文本注入是最小可控手段；__getattr__ 转发保证其余路径走真实 Session。
    """

    def __init__(self, inner):
        self._inner = inner

    def execute(self, stmt, *args, **kwargs):
        if "pg_locks" in str(stmt):
            raise RuntimeError("simulated pg_locks failure")
        return self._inner.execute(stmt, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_lock_check_query_error_treated_as_lock_lost(db, tmp_path):
    """持锁校验查询本身报错 → 无法确认持锁即按丢锁处理：backfill_lock_lost
    中止整个执行，当前文件已回滚零落盘，会话级锁随 finally 释放。"""
    _import_one(db, tmp_path, "a.xlsx", [_row()])
    _degrade_to_pre_pr2(db)
    operator = _operator(db)
    dry = bs.run_backfill(db)

    probe = db.get_bind().engine.connect()
    try:
        with pytest.raises(bs.BackfillError) as ei:
            bs.run_backfill(_PgLocksExplodingSession(db), execute=True,
                            operator=operator,
                            confirm_manifest_sha256=dry["manifest_sha256"])
        assert ei.value.code == "backfill_lock_lost"
        assert "无法校验" in ei.value.message

        db.rollback()
        assert _snap_count(db) == 0               # 当前文件已回滚，零落盘
        assert _order(db).tax_amount is None
        assert _lock_is_free(probe) is True
    finally:
        probe.close()


def test_keyboard_interrupt_rolls_back_releases_lock_and_reraises(db, tmp_path, monkeypatch):
    """执行中 KeyboardInterrupt（BaseException，不吃单文件失败的分寸）：当前
    文件回滚、锁在 finally 释放、异常原样上抛——不吞咽、不转译成报告条目。"""
    _import_one(db, tmp_path, "a.xlsx", [_row()])
    _degrade_to_pre_pr2(db)
    operator = _operator(db)
    dry = bs.run_backfill(db)

    real_apply = bs._apply_file

    def _apply_then_interrupt(session, batch, plan, op, sha):
        real_apply(session, batch, plan, op, sha)   # 已产生未提交写入
        raise KeyboardInterrupt("simulated interrupt")

    monkeypatch.setattr(bs, "_apply_file", _apply_then_interrupt)
    probe = db.get_bind().engine.connect()
    try:
        with pytest.raises(KeyboardInterrupt):
            bs.run_backfill(db, execute=True, operator=operator,
                            confirm_manifest_sha256=dry["manifest_sha256"])

        db.rollback()
        assert _snap_count(db) == 0                 # 当前文件已回滚
        assert _order(db).tax_amount is None
        assert _lock_is_free(probe) is True         # finally 生效，锁不滞留
    finally:
        probe.close()

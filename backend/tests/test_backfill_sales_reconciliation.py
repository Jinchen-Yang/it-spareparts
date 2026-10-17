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
    assert applied["totals"]["files_executed"] == 3      # 坏档也"执行"（零写入+审计）
    assert applied["totals"]["snapshots_inserted"] == 1
    assert applied["totals"]["tax_columns_filled"] == 3

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

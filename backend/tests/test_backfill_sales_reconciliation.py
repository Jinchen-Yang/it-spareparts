"""回填脚本 scripts/backfill_sales_reconciliation（D-40/D-41）契约：只读 dry-run、
execute 双确认护栏、幂等重跑、坏档隔离、税列冲突不覆盖、批次口径。

xlsx 构造复用 test_sales_reconciliation_import 的 helpers（同包跨测试模块导入
是该仓既有惯例，见 test_audit_trail.py）。
"""
import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import delete, func, select, update

from app.etl import pipeline
from app.models.sales import FSalesOrder, FSalesReconciliationSnapshot
from app.models.system import SysAuditLog, SysRawFile, SysUser
from scripts import backfill_sales_reconciliation as bs
from tests.test_sales_reconciliation_import import _row, _write_xlsx


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
    assert dry2["totals"]["snapshots_planned"] == 0    # 内容未变 → 全部已存在
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
    assert dry["totals"]["snapshots_planned"] == 1   # 跨批次同内容去重（= 执行真实写入数）
    assert dry["totals"]["snapshots_duplicate_content_across_files"] == 1

    bs.run_backfill(db, execute=True, operator=operator,
                    confirm_manifest_sha256=dry["manifest_sha256"])
    snaps = db.scalars(select(FSalesReconciliationSnapshot)).all()
    assert len(snaps) == 1                            # 跨重复批次幂等：不因批次多而多写
    assert snaps[0].import_batch_id == b1.id          # 先处理的批次（uploaded_at 早）落账


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

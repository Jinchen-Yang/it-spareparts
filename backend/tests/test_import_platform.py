"""导入平台化：导入人审计(🥇) / 批量后台作业(🥈) / 覆盖写留痕(🥉)。"""
import shutil
from datetime import date
from decimal import Decimal

import pandas as pd
import pytest
from sqlalchemy import func, select

from app.api.imports import _process_import_job
from app.etl import loader, pipeline
from app.models.system import SysAuditLog, SysImportBatch, SysImportJob
from tests import factories as f


def _inv_xlsx(tmp_path, name="inv.xlsx", rows=None):
    rows = rows or [{"产品库存ID": "INV1", "产品名称(PN)": "PN-A", "库存数量": 5, "仓库": "总仓"}]
    p = tmp_path / name
    pd.DataFrame(rows).to_excel(p, index=False)
    return str(p)


def _inv_rows(raw_id, pn, qty, wh="总仓"):
    return [{"产品库存ID": raw_id, "产品名称(PN)": pn, "库存数量": qty, "仓库": wh}]


def _new_job(db, mode="skip", total=1, by="刘青青"):
    job = SysImportJob(created_by=by, mode=mode, total_files=total, status="processing")
    db.add(job)
    db.commit()
    return job.id


# ---------- 🥇 导入人审计 ----------
def test_run_import_records_uploaded_by(db, tmp_path):
    batch = pipeline.run_import(db, _inv_xlsx(tmp_path), "inv.xlsx", uploaded_by="刘青青")
    db.commit()
    assert batch.uploaded_by == "刘青青"
    # 重新查库确认已落库（不是仅内存对象）
    row = db.scalar(select(SysImportBatch).where(SysImportBatch.id == batch.id))
    assert row.uploaded_by == "刘青青"


def test_run_import_uploaded_by_optional(db, tmp_path):
    # 不传 uploaded_by（如 RBAC 关闭、ctx.user_id 为 None）仍能导入，留痕为空
    batch = pipeline.run_import(db, _inv_xlsx(tmp_path, "inv2.xlsx"), "inv2.xlsx")
    db.commit()
    assert batch.uploaded_by is None and batch.status == "success"


# ---------- 🥈 批量后台作业 ----------
def test_import_job_processes_multiple_files(db, tmp_path):
    f1 = _inv_xlsx(tmp_path, "a.xlsx", _inv_rows("INV1", "PN-A", 5))
    f2 = _inv_xlsx(tmp_path, "b.xlsx", _inv_rows("INV2", "PN-B", 3))
    jid = _new_job(db, total=2)
    _process_import_job(jid, [(f1, "a.xlsx"), (f2, "b.xlsx")], "skip", "刘青青")
    db.expire_all()
    j = db.get(SysImportJob, jid)
    assert j.status == "done" and j.done_files == 2 and j.error_files == 0 and j.finished_at is not None
    batches = db.execute(
        select(SysImportBatch).where(SysImportBatch.import_job_id == jid)).scalars().all()
    assert len(batches) == 2 and all(b.uploaded_by == "刘青青" for b in batches)
    # 临时文件被清理
    import os
    assert not os.path.exists(f1) and not os.path.exists(f2)


def test_import_job_handles_duplicate(db, tmp_path):
    # 先成功导入 a.xlsx，再批量含同内容文件 → 重复计 error_files + note，另一个文件正常
    f1 = _inv_xlsx(tmp_path, "a.xlsx", _inv_rows("INV1", "PN-A", 5))
    pipeline.run_import(db, f1, "a.xlsx")
    db.commit()
    dup = str(tmp_path / "a_dup.xlsx")
    shutil.copy(f1, dup)                       # 同字节→同 hash→DuplicateFileError
    f2 = _inv_xlsx(tmp_path, "b.xlsx", _inv_rows("INV2", "PN-B", 3))
    jid = _new_job(db, total=2)
    _process_import_job(jid, [(dup, "a.xlsx"), (f2, "b.xlsx")], "skip", None)
    db.expire_all()
    j = db.get(SysImportJob, jid)
    assert j.status == "partial" and j.done_files == 1 and j.error_files == 1
    assert j.note and "重复" in j.note


def test_upsert_can_reimport_same_file(db, tmp_path):
    """同一份文件可二次导入：skip(非修复)后再 upsert(修复)不被 hash 去重挡掉（甲方反馈）。

    旧成功批次标 superseded 让出 ux_batch_success_hash 偏唯一索引，新批次成功——
    修复模式对同一份文件不再形同虚设。
    """
    p = _inv_xlsx(tmp_path, "same.xlsx", _inv_rows("INV1", "PN-A", 5))
    b1 = pipeline.run_import(db, p, "same.xlsx", mode="skip")
    db.commit()
    assert b1.status == "success"

    dup = str(tmp_path / "same_again.xlsx")
    shutil.copy(p, dup)                            # 同字节 → 同 hash
    b2 = pipeline.run_import(db, dup, "same.xlsx", mode="upsert")   # 不再抛 DuplicateFileError
    db.commit()
    assert b2.status == "success" and b2.id != b1.id

    db.expire_all()
    assert db.get(SysImportBatch, b1.id).status == "superseded"     # 旧批次让位
    n_success = db.scalar(select(func.count()).select_from(SysImportBatch).where(
        SysImportBatch.file_hash == b2.file_hash, SysImportBatch.status == "success"))
    assert n_success == 1                          # 偏唯一索引未被违反


def test_skip_mode_still_blocks_duplicate(db, tmp_path):
    """skip 模式行为不变：同文件二次导入仍抛 DuplicateFileError（幂等保护不回退）。"""
    p = _inv_xlsx(tmp_path, "dup.xlsx", _inv_rows("INV9", "PN-Z", 3))
    pipeline.run_import(db, p, "dup.xlsx", mode="skip")
    db.commit()
    dup = str(tmp_path / "dup2.xlsx")
    shutil.copy(p, dup)
    with pytest.raises(pipeline.DuplicateFileError):
        pipeline.run_import(db, dup, "dup.xlsx", mode="skip")


def test_inactive_empty_pn_not_counted_as_error(db):
    """非生效单的空产品(empty_pn_inactive)不计入 fact_rows_error，只硬错误计数。"""
    from app.etl import transform
    b = SysImportBatch(filename="t.xlsx", file_type="sales", file_hash="hsoft1")
    db.add(b); db.flush()
    res = f.sales_result({"O1": f.sales_head("O1")}, [f.sales_line("O1", "L1", "PN-A")])
    res.errors.append(transform.ErrorRec(2, "empty_pn_inactive", "草稿可忽略", {}))
    res.errors.append(transform.ErrorRec(3, "empty_pn", "真空产品", {}))
    counts = loader.load(db, res, b.id, date(2026, 6, 1), mode="skip")
    db.commit()
    assert counts["fact_rows_error"] == 1   # 软错误不计，只数硬错误


def test_import_job_unrecognized_file_failed_batch(db, tmp_path):
    junk = tmp_path / "junk.xlsx"
    pd.DataFrame([{"随便": "x", "列名": "y"}]).to_excel(junk, index=False)
    jid = _new_job(db, total=1)
    _process_import_job(jid, [(str(junk), "junk.xlsx")], "skip", None)
    db.expire_all()
    j = db.get(SysImportJob, jid)
    assert j.status == "failed" and j.error_files == 1
    fb = db.execute(
        select(SysImportBatch).where(SysImportBatch.import_job_id == jid)).scalars().all()
    assert len(fb) == 1 and fb[0].status == "failed"   # 失败批次带 job_id 留痕


# ---------- 🥉 upsert/库存覆盖写审计 ----------
def _overwrite_logs(db, entity_type):
    return db.execute(
        select(SysAuditLog).where(SysAuditLog.entity_type == entity_type)).scalars().all()


def test_upsert_overwrite_audits_before_after(db):
    b = SysImportBatch(filename="t.xlsx", file_type="purchase", file_hash="ha1")
    db.add(b); db.flush()
    loader.load(db, f.purchase_result({"O1": f.purchase_head("O1")},
                [f.purchase_line("O1", "L1", "PN-A", qty="2", price="100")]),
                b.id, date(2026, 6, 1), mode="skip")
    db.commit()
    # 改价重导（upsert + 审计）→ 被覆盖行写 before/after
    b2 = SysImportBatch(filename="t2.xlsx", file_type="purchase", file_hash="ha2")
    db.add(b2); db.flush()
    loader.load(db, f.purchase_result({"O1": f.purchase_head("O1")},
                [f.purchase_line("O1", "L1", "PN-A", qty="2", price="180")]),
                b2.id, date(2026, 6, 2), mode="upsert", operated_by="刘青青", audit_overwrites=True)
    db.commit()
    logs = _overwrite_logs(db, "import_overwrite")
    assert len(logs) == 1
    lg = logs[0]
    assert lg.operated_by == "刘青青" and lg.action == "overwrite"
    assert Decimal(lg.before_json["unit_price"]) == Decimal("100")
    assert Decimal(lg.after_json["unit_price"]) == Decimal("180")


def test_upsert_no_audit_when_unchanged(db):
    b = SysImportBatch(filename="t.xlsx", file_type="purchase", file_hash="hb1")
    db.add(b); db.flush()
    line = [f.purchase_line("O1", "L1", "PN-A", qty="2", price="100")]
    loader.load(db, f.purchase_result({"O1": f.purchase_head("O1")}, line),
                b.id, date(2026, 6, 1), mode="skip")
    db.commit()
    # 同值重导（仅 import_batch_id 变）→ 不应产生覆盖审计
    b2 = SysImportBatch(filename="t2.xlsx", file_type="purchase", file_hash="hb2")
    db.add(b2); db.flush()
    loader.load(db, f.purchase_result({"O1": f.purchase_head("O1")},
                [f.purchase_line("O1", "L1", "PN-A", qty="2", price="100")]),
                b2.id, date(2026, 6, 2), mode="upsert", operated_by="x", audit_overwrites=True)
    db.commit()
    assert _overwrite_logs(db, "import_overwrite") == []


def test_skip_mode_no_overwrite_audit(db):
    b = SysImportBatch(filename="t.xlsx", file_type="purchase", file_hash="hc1")
    db.add(b); db.flush()
    loader.load(db, f.purchase_result({"O1": f.purchase_head("O1")},
                [f.purchase_line("O1", "L1", "PN-A", price="100")]),
                b.id, date(2026, 6, 1), mode="skip", operated_by="x", audit_overwrites=True)
    db.commit()
    # skip 模式 ON CONFLICT DO NOTHING，不覆盖 → 即便改价重导也无覆盖审计
    b2 = SysImportBatch(filename="t2.xlsx", file_type="purchase", file_hash="hc2")
    db.add(b2); db.flush()
    loader.load(db, f.purchase_result({"O1": f.purchase_head("O1")},
                [f.purchase_line("O1", "L1", "PN-A", price="999")]),
                b2.id, date(2026, 6, 2), mode="skip", operated_by="x", audit_overwrites=True)
    db.commit()
    assert _overwrite_logs(db, "import_overwrite") == []


def test_inventory_overwrite_audits_qty(db):
    b = SysImportBatch(filename="inv.xlsx", file_type="inventory", file_hash="hd1")
    db.add(b); db.flush()
    loader.load(db, f.inventory_result([f.inventory_row("INV1", "PN-A", qty="5")]),
                b.id, date(2026, 6, 1), operated_by="崔丽娜", audit_overwrites=True)
    db.commit()
    assert _overwrite_logs(db, "inventory_overwrite") == []   # 首次写入非覆盖
    # 同 (pn_std,仓库) 再导，数量 5→9 → 覆盖留痕
    b2 = SysImportBatch(filename="inv2.xlsx", file_type="inventory", file_hash="hd2")
    db.add(b2); db.flush()
    loader.load(db, f.inventory_result([f.inventory_row("INV1", "PN-A", qty="9")]),
                b2.id, date(2026, 6, 2), operated_by="崔丽娜", audit_overwrites=True)
    db.commit()
    logs = _overwrite_logs(db, "inventory_overwrite")
    assert len(logs) == 1
    assert Decimal(logs[0].before_json["source_qty"]) == Decimal("5")
    assert Decimal(logs[0].after_json["source_qty"]) == Decimal("9")
    assert "part_id" in logs[0].before_json and "part_id" in logs[0].after_json
    assert logs[0].operated_by == "崔丽娜"


def test_inventory_overwrite_audits_part_id_change(db):
    """数量不变但商品身份(part_id)因别名改判 → 仍留痕（评审补强：与订单行口径一致）。"""
    from sqlalchemy import select as _select, update as _update
    from app.models.dimensions import DimPart, PartAlias
    # 建两个型号 + 库存（PN-A=5 / PN-TARGET 在 B 仓）
    b = SysImportBatch(filename="inv.xlsx", file_type="inventory", file_hash="he1")
    db.add(b); db.flush()
    loader.load(db, f.inventory_result([
        f.inventory_row("S1", "PN-A", qty="5"),
        f.inventory_row("S2", "PN-TARGET", qty="9", warehouse="B仓"),
    ]), b.id, date(2026, 6, 1), operated_by="x", audit_overwrites=True)
    db.commit()
    pid_a = db.scalar(_select(DimPart.id).where(DimPart.pn_std == "PN-A"))
    pid_t = db.scalar(_select(DimPart.id).where(DimPart.pn_std == "PN-TARGET"))
    # 人工接受别名重定向：既有恒等别名 PN-A→PN-A 改判到 PN-TARGET（不走 merge，不 repoint 既有库存行）
    db.execute(_update(PartAlias).where(PartAlias.pn_raw == "PN-A").values(
        pn_std="PN-TARGET", part_id=pid_t, status="active", source="manual"))
    db.commit()
    # 再导 PN-A，数量仍 5 → 解析重定向到 pid_t，覆盖既有行 part_id（pid_a→pid_t），数量未变
    b2 = SysImportBatch(filename="inv2.xlsx", file_type="inventory", file_hash="he2")
    db.add(b2); db.flush()
    loader.load(db, f.inventory_result([f.inventory_row("S1", "PN-A", qty="5")]),
                b2.id, date(2026, 6, 2), operated_by="崔丽娜", audit_overwrites=True)
    db.commit()
    changed = [lg for lg in _overwrite_logs(db, "inventory_overwrite")
               if lg.before_json.get("part_id") != lg.after_json.get("part_id")]
    assert changed, "part_id 改判应留痕（即便数量未变）"
    assert changed[-1].before_json["part_id"] == pid_a and changed[-1].after_json["part_id"] == pid_t
    assert changed[-1].operated_by == "崔丽娜"


def test_excluded_warehouse_rows_skipped():
    """排除仓（坏品仓，甲方 2026-07-03）：整行跳过、计数进报告、不算错误——
    排除判定在空 PN 之前，坏品仓的脏行（如空产品名）也不产生导入错误。"""
    from app.etl import transform as T

    df = pd.DataFrame([
        {"产品库存ID": "I1", "产品名称(PN)": "PN-GOOD", "库存数量": 5, "仓库": "北京成品仓"},
        {"产品库存ID": "I2", "产品名称(PN)": "PN-BAD", "库存数量": 3, "仓库": "北京坏品仓"},
        {"产品库存ID": "I3", "产品名称(PN)": "", "库存数量": 1, "仓库": "上海坏品仓"},
    ])
    res = T.transform(df, "inventory")
    assert res.rows_excluded_warehouse == 2
    assert [r["raw_inventory_id"] for r in res.inventory] == ["I1"]
    assert not res.errors

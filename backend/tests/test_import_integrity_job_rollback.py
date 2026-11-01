"""写入后的 ReaderError 必须经真实批量 worker 回滚，且不能中断后续文件。"""

from decimal import Decimal

import pytest
from openpyxl import Workbook
from sqlalchemy import select

from app.api.imports import _process_import_job
from app.etl import pipeline, reader
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.system import SysImportBatch, SysImportJob, SysRawFile


def _purchase_file(tmp_path, name, rows):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "采购"
    sheet.append([
        "采购单号(必填)", "数据ID(不可修改)", "明细.数据ID(不可修改)",
        "明细.产品名称(必填)", "明细.单价(必填)", "明细.采购数量(必填)",
        "不含税金额",
    ])
    for row in rows:
        sheet.append(row)
    path = tmp_path / name
    workbook.save(path)
    workbook.close()
    return path


@pytest.mark.parametrize("continue_with_valid_file", [False, True], ids=["failed", "partial"])
def test_import_job_rolls_back_post_load_reader_error_and_continues(
    db, tmp_path, monkeypatch, continue_with_valid_file
):
    # 与 test_import_platform 相同的 job 入口：worker 自行创建 SessionLocal，
    # 自行执行真实 run_import/loader 以及 commit/rollback；测试不替它回滚。
    old_path = _purchase_file(tmp_path, "old.xlsx", [
        ["CGDD-OLD", "PO-OLD", "PL-OLD", "PN-OLD", 100, 1, 100],
    ])
    old_batch = pipeline.run_import(db, str(old_path), old_path.name)
    db.commit()
    old_batch_id = old_batch.id

    failing_path = _purchase_file(tmp_path, "first-fails.xlsx", [
        ["CGDD-OLD", "PO-OLD", "PL-OLD", "PN-OLD", 900, 2, 1800],
        ["CGDD-FAILED", "PO-FAILED", "PL-FAILED", "PN-FAILED", 300, 1, 300],
    ])
    failed_hash = pipeline.sha256_file(str(failing_path))
    files = [(str(failing_path), failing_path.name)]
    good_path = None
    if continue_with_valid_file:
        good_path = _purchase_file(tmp_path, "second-succeeds.xlsx", [
            ["CGDD-GOOD", "PO-GOOD", "PL-GOOD", "PN-GOOD", 200, 3, 600],
        ])
        files.append((str(good_path), good_path.name))

    job = SysImportJob(
        created_by="完整性回归", mode="upsert", total_files=len(files), status="processing"
    )
    db.add(job)
    db.commit()
    job_id = job.id

    original_detection = pipeline.data_quality_amount_mismatch.detect_imported_lines
    observed_writes = []

    def fail_after_real_load(session, *, file_type, raw_line_ids, detected_by):
        if "PL-FAILED" not in raw_line_ids:
            return original_detection(
                session, file_type=file_type, raw_line_ids=raw_line_ids,
                detected_by=detected_by,
            )
        # 先 flush 并查询 SQL 结果，证明故障发生时真实事实和原件登记已在事务中，
        # 既覆盖新建回滚，也覆盖已有事实被 upsert 后的恢复。
        session.flush()
        old_line = session.scalar(select(FPurchaseLine).where(
            FPurchaseLine.raw_line_id == "PL-OLD"
        ))
        new_line = session.scalar(select(FPurchaseLine).where(
            FPurchaseLine.raw_line_id == "PL-FAILED"
        ))
        old_order = session.scalar(select(FPurchaseOrder).where(
            FPurchaseOrder.raw_order_id == "PO-OLD"
        ))
        raw_file = session.scalar(select(SysRawFile).where(
            SysRawFile.file_hash == failed_hash
        ))
        observed_writes.append({
            "old_price": old_line.unit_price,
            "old_qty": old_line.qty,
            "old_amount": old_order.amount_ex_tax,
            "new_price": new_line.unit_price,
            "batch_id": new_line.import_batch_id,
            "raw_batch_id": raw_file.batch_id,
        })
        raise reader.ReaderError("模拟写入后校验失败", code="invalid_workbook")

    monkeypatch.setattr(
        pipeline.data_quality_amount_mismatch, "detect_imported_lines", fail_after_real_load
    )

    _process_import_job(job_id, files, "upsert", "完整性回归")

    db.expire_all()
    job = db.get(SysImportJob, job_id)
    assert job.status == ("partial" if continue_with_valid_file else "failed")
    assert job.done_files == int(continue_with_valid_file)
    assert job.error_files == 1
    assert job.finished_at is not None
    assert job.note == (
        "完整性失败：first-fails.xlsx（导入写入阶段失败，整批已回滚：模拟写入后校验失败）"
    )
    assert len(observed_writes) == 1
    seen = observed_writes[0]
    assert seen == {
        "old_price": Decimal("900"), "old_qty": Decimal("2"),
        "old_amount": Decimal("1800"), "new_price": Decimal("300"),
        "batch_id": seen["batch_id"], "raw_batch_id": seen["batch_id"],
    }

    orders = {order.raw_order_id: order for order in db.scalars(select(FPurchaseOrder))}
    lines = {line.raw_line_id: line for line in db.scalars(select(FPurchaseLine))}
    assert set(orders) == ({"PO-OLD", "PO-GOOD"} if continue_with_valid_file else {"PO-OLD"})
    assert set(lines) == ({"PL-OLD", "PL-GOOD"} if continue_with_valid_file else {"PL-OLD"})
    assert orders["PO-OLD"].amount_ex_tax == Decimal("100")
    assert orders["PO-OLD"].import_batch_id == old_batch_id
    assert lines["PL-OLD"].unit_price == Decimal("100")
    assert lines["PL-OLD"].qty == Decimal("1")
    assert lines["PL-OLD"].import_batch_id == old_batch_id

    batches = list(db.scalars(select(SysImportBatch)))
    raw_files = list(db.scalars(select(SysRawFile)))
    assert len(batches) == len(raw_files) == 1 + int(continue_with_valid_file)
    assert all(batch.status == "success" for batch in batches)
    assert all(batch.file_hash != failed_hash for batch in batches)
    assert all(raw.file_hash != failed_hash for raw in raw_files)
    assert all(batch.id != seen["batch_id"] for batch in batches)
    assert db.get(SysImportBatch, old_batch_id).status == "success"
    job_batches = [batch for batch in batches if batch.import_job_id == job_id]
    assert len(job_batches) == int(continue_with_valid_file)
    if continue_with_valid_file:
        good_batch = job_batches[0]
        assert good_batch.filename == "second-succeeds.xlsx"
        assert good_batch.uploaded_by == "完整性回归"
        assert orders["PO-GOOD"].amount_ex_tax == Decimal("600")
        assert orders["PO-GOOD"].import_batch_id == good_batch.id
        assert lines["PL-GOOD"].unit_price == Decimal("200")
        assert lines["PL-GOOD"].qty == Decimal("3")
        assert lines["PL-GOOD"].import_batch_id == good_batch.id
        assert any(raw.batch_id == good_batch.id for raw in raw_files)
        assert not good_path.exists()
    assert not failing_path.exists()

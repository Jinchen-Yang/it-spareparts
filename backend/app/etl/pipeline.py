"""导入编排：校验→hash→锁→batch→read→transform→load→report（§6.1/§6.6）。"""
import hashlib
import os
import shutil
from datetime import date, datetime, timezone

from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.etl import loader, mapping, reader
from app.etl.reader import ReaderError
from app.etl.transform import transform
from app.models.system import SysImportBatch, SysImportError, SysRawFile

_ADVISORY_LOCK_KEY = 0x5350_4152  # 'SPAR' 应用级导入锁


class DuplicateFileError(Exception):
    """同 hash 文件已成功导入。"""

    def __init__(self, batch_id: int):
        self.batch_id = batch_id
        super().__init__("该文件已成功导入")


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _archive(src_path: str, file_hash: str) -> str:
    settings = get_settings()
    os.makedirs(settings.raw_file_dir, exist_ok=True)
    dest = os.path.join(settings.raw_file_dir, f"{file_hash}.xlsx")
    if not os.path.exists(dest):
        shutil.copy2(src_path, dest)
    return dest


def run_import(session: Session, file_path: str, original_name: str,
               uploaded_by: str | None = None, mode: str = "skip",
               import_job_id: int | None = None) -> SysImportBatch:
    """对单个 .xlsx 执行完整导入。返回 batch（含 report_json）。

    校验在 API 层（扩展名/大小）；此处做 hash 去重 + 锁 + 入库。
    import_job_id：批量作业归组（单文件上传为 None）；成功/失败批次都带上，便于作业详情聚合。
    """
    file_hash = sha256_file(file_path)

    # 1) 应用级导入锁先行（同一时间仅一个导入；顺带消除"去重检查在加锁前"的并发竞态）
    session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _ADVISORY_LOCK_KEY})

    # 2) hash 去重：skip 模式拒绝重复成功文件（幂等）；upsert(修复)模式是"显式要求重处理"——
    #    把旧成功批次标记 superseded 后放行，让 loader 按 raw_id ON CONFLICT DO UPDATE 更新已有行。
    #    （否则同 hash 第二条 success 会撞 ux_batch_success_hash 偏唯一索引；不放行则"修复模式"
    #     对同一份文件形同虚设——见甲方反馈：同文件应能先非修复、后修复导入。）
    dup = session.execute(
        select(SysImportBatch.id).where(
            SysImportBatch.file_hash == file_hash, SysImportBatch.status == "success"
        )
    ).first()
    if dup:
        if mode != "upsert":
            raise DuplicateFileError(dup[0])
        session.execute(
            update(SysImportBatch).where(SysImportBatch.id == dup[0])
            .values(status="superseded")
        )

    # 3) 建 batch（先占位，类型稍后回填）
    batch = SysImportBatch(filename=original_name, file_type="unknown",
                           file_hash=file_hash, uploaded_by=uploaded_by,
                           import_job_id=import_job_id, status="processing")
    session.add(batch)
    session.flush()  # 取 batch.id

    try:
        sheets = reader.read_workbook(file_path)
        if not sheets:
            raise ReaderError("无法识别文件类型，请确认是采购/销售/库存/维保出库/报销明细导出文件")

        # §17.5：多可识别页 = 项目追踪工作簿——仅报销明细页入库；备件/采购/销售等页
        # 是系统导出的回填副本（或手工粘贴件），非权威源，吃回去会造成回环污染。
        if len(sheets) > 1:
            ingest = [s for s in sheets if s.file_type == mapping.EXPENSE]
            skipped_sheets = [
                {"sheet": s.sheet_name, "file_type": s.file_type,
                 "note": "多页工作簿仅报销明细页入库；此类数据请用氚云原生导出单独上传"}
                for s in sheets if s.file_type != mapping.EXPENSE
            ]
            if not ingest:
                raise ReaderError(
                    "多页工作簿中没有可入库的报销明细页；备件/采购/销售数据请用氚云原生导出单独上传")
            batch.file_type = "workbook"
        else:
            ingest = sheets
            skipped_sheets = []
            batch.file_type = sheets[0].file_type

        storage_path = _archive(file_path, file_hash)
        session.add(SysRawFile(batch_id=batch.id, filename=original_name,
                               file_hash=file_hash, storage_path=storage_path))

        snapshot = datetime.now(timezone.utc).date()
        totals = {"source_rows_total": 0, "fact_rows_inserted": 0,
                  "fact_rows_skipped": 0, "fact_rows_error": 0, "rows_inactive": 0}
        sheet_reports = []
        multi = len(sheets) > 1
        for s in ingest:
            result = transform(s.df, s.file_type, anchor=s.anchor)

            # 错误行入 sys_import_error（多页时错误详情带上页名定位）
            for e in result.errors:
                detail = f"[{s.sheet_name}] {e.error_detail}" if multi else e.error_detail
                session.add(SysImportError(batch_id=batch.id, row_no=e.row_no,
                                           error_type=e.error_type, error_detail=detail,
                                           raw_row=e.raw_row))

            counts = loader.load(session, result, batch.id, snapshot, mode=mode,
                                 operated_by=uploaded_by, audit_overwrites=True)
            for k in totals:
                totals[k] += counts.get(k, 0)
            sheet_reports.append({
                "sheet": s.sheet_name, "file_type": s.file_type, **counts,
                "rows_skipped_no_data": result.rows_skipped_no_data,
                # 缺价格列留痕：即便用户确认导入了无金额文件，批次详情也能看到此告警
                "missing_price_columns": not mapping.has_price_columns(
                    list(s.df.columns), s.file_type),
                "errors_preview": [
                    {"row_no": e.row_no, "error_type": e.error_type, "detail": e.error_detail}
                    for e in result.errors[:10]
                ]})

        # 单页文件报告结构不变（老前端/老批次兼容）；多页平铺聚合数 + per-sheet 明细
        if multi:
            report = {"file_type": batch.file_type, **totals,
                      "import_mode": mode, "sheets": sheet_reports,
                      "skipped_sheets": skipped_sheets}
        else:
            report = {k: v for k, v in sheet_reports[0].items() if k != "sheet"}
        batch.rows_total = totals["source_rows_total"]
        batch.rows_inserted = totals["fact_rows_inserted"]
        batch.rows_skipped = totals["fact_rows_skipped"]
        batch.rows_error = totals["fact_rows_error"]
        batch.rows_inactive = totals["rows_inactive"]
        batch.report_json = report
        batch.status = "success"
        session.flush()
        return batch
    except ReaderError as exc:
        batch.status = "failed"
        batch.report_json = {"error": str(exc)}
        session.flush()
        raise

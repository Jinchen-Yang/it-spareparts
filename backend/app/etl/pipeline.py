"""导入编排：校验→hash→锁→batch→read→transform→load→report（§6.1/§6.6）。"""
import hashlib
import os
import shutil
from datetime import date, datetime, timezone

from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.etl import loader, reader
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
        df, file_type = reader.read_excel(file_path)
        batch.file_type = file_type

        storage_path = _archive(file_path, file_hash)
        session.add(SysRawFile(batch_id=batch.id, filename=original_name,
                               file_hash=file_hash, storage_path=storage_path))

        result = transform(df, file_type)

        # 错误行入 sys_import_error
        for e in result.errors:
            session.add(SysImportError(batch_id=batch.id, row_no=e.row_no,
                                       error_type=e.error_type, error_detail=e.error_detail,
                                       raw_row=e.raw_row))

        snapshot = datetime.now(timezone.utc).date()
        counts = loader.load(session, result, batch.id, snapshot, mode=mode,
                             operated_by=uploaded_by, audit_overwrites=True)

        report = {"file_type": file_type, **counts,
                  "errors_preview": [
                      {"row_no": e.row_no, "error_type": e.error_type, "detail": e.error_detail}
                      for e in result.errors[:10]
                  ]}
        batch.rows_total = counts["source_rows_total"]
        batch.rows_inserted = counts["fact_rows_inserted"]
        batch.rows_skipped = counts["fact_rows_skipped"]
        batch.rows_error = counts["fact_rows_error"]
        batch.rows_inactive = counts["rows_inactive"]
        batch.report_json = report
        batch.status = "success"
        session.flush()
        return batch
    except ReaderError as exc:
        batch.status = "failed"
        batch.report_json = {"error": str(exc)}
        session.flush()
        raise

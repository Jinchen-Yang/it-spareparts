"""导入编排：校验→hash→锁→batch→read→transform→load→report（§6.1/§6.6）。"""
import hashlib
import logging
import os
import re
import stat
import tempfile
from datetime import datetime, timezone

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from app.config import DATA_CHANGE_ADVISORY_LOCK_KEY, get_settings
from app.etl import loader, mapping, reader, sheet_selection
from app.etl.reader import ReaderError
from app.etl.transform import transform
from app.models.system import SysImportBatch, SysImportError, SysRawFile
from app.services import data_quality_amount_mismatch

_ARCHIVE_HASH_RE = re.compile(r"[0-9a-f]{64}")
_ARCHIVE_ERROR_MESSAGE = "原始文件归档失败"
_log = logging.getLogger(__name__)


class DuplicateFileError(Exception):
    """同 hash 文件已成功导入。"""

    def __init__(self, batch_id: int):
        self.batch_id = batch_id
        super().__init__("该文件已成功导入")


class ArchiveError(RuntimeError):
    """原始文件无法安全归档。"""

    def __init__(self):
        super().__init__(_ARCHIVE_ERROR_MESSAGE)


class _ArchiveDestinationChanged(Exception):
    pass


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def successful_batch_ids_by_hash(
    session: Session,
    file_hashes: set[str],
    *,
    file_type: str | None = None,
) -> dict[str, int]:
    if not file_hashes:
        return {}
    query = select(SysImportBatch.file_hash, SysImportBatch.id).where(
        SysImportBatch.file_hash.in_(file_hashes),
        SysImportBatch.status == "success",
    )
    if file_type is not None:
        query = query.where(SysImportBatch.file_type == file_type)
    rows = session.execute(query).all()
    return {file_hash: batch_id for file_hash, batch_id in rows}


def _archive_digest(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        while chunk := source.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _open_archive_temp(fd: int):
    return os.fdopen(fd, "wb")


def _close_archive_fd(fd: int) -> None:
    os.close(fd)


def _archive_lstat(path: str) -> os.stat_result:
    return os.lstat(path)


def _archive_digest_regular(path: str, expected_stat: os.stat_result) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        opened_stat = os.fstat(fd)
        if not stat.S_ISREG(opened_stat.st_mode):
            raise ArchiveError()
        if (opened_stat.st_dev, opened_stat.st_ino) != (
            expected_stat.st_dev,
            expected_stat.st_ino,
        ):
            raise _ArchiveDestinationChanged()
        digest = hashlib.sha256()
        while chunk := os.read(fd, 1 << 20):
            digest.update(chunk)
        try:
            current_stat = _archive_lstat(path)
        except FileNotFoundError:
            raise _ArchiveDestinationChanged()
        if not stat.S_ISREG(current_stat.st_mode):
            raise ArchiveError()
        if (current_stat.st_dev, current_stat.st_ino) != (
            opened_stat.st_dev,
            opened_stat.st_ino,
        ):
            raise _ArchiveDestinationChanged()
    except Exception:
        try:
            _close_archive_fd(fd)
        except Exception:
            _log.warning("archive destination fd close failed", exc_info=True)
        raise
    _close_archive_fd(fd)
    return digest.hexdigest()


def _copy_archive_chunks(source_path: str, temp_file) -> str:
    digest = hashlib.sha256()
    with open(source_path, "rb") as source:
        while chunk := source.read(1 << 20):
            temp_file.write(chunk)
            digest.update(chunk)
    return digest.hexdigest()


def _flush_archive_temp(temp_file) -> None:
    temp_file.flush()


def _fsync_archive_temp(temp_file) -> None:
    os.fsync(temp_file.fileno())


def _replace_archive_temp(temp_path: str, dest_path: str) -> None:
    os.replace(temp_path, dest_path)


def _remove_archive_temp(temp_path: str) -> None:
    os.remove(temp_path)


def _archive(src_path: str, file_hash: str) -> str:
    if _ARCHIVE_HASH_RE.fullmatch(file_hash) is None:
        raise ArchiveError()

    settings = get_settings()
    dest = os.path.join(settings.raw_file_dir, f"{file_hash}.xlsx")
    try:
        if _archive_digest(src_path) != file_hash:
            raise ArchiveError()
        os.makedirs(settings.raw_file_dir, exist_ok=True)
        for _ in range(4):
            try:
                destination_stat = _archive_lstat(dest)
            except FileNotFoundError:
                break
            if not stat.S_ISREG(destination_stat.st_mode):
                raise ArchiveError()
            try:
                destination_hash = _archive_digest_regular(dest, destination_stat)
            except _ArchiveDestinationChanged:
                continue
            if destination_hash == file_hash:
                return dest
            _log.warning("corrupt raw archive will be repaired")
            break
        else:
            raise ArchiveError()
    except ArchiveError:
        raise
    except Exception as exc:
        raise ArchiveError() from exc

    temp_path: str | None = None
    temp_file = None
    try:
        fd, temp_path = tempfile.mkstemp(dir=settings.raw_file_dir)
        try:
            temp_file = _open_archive_temp(fd)
        except Exception:
            try:
                _close_archive_fd(fd)
            except Exception:
                _log.warning("archive temporary fd close failed", exc_info=True)
            raise
        copied_hash = _copy_archive_chunks(src_path, temp_file)
        if copied_hash != file_hash:
            raise ArchiveError()
        _flush_archive_temp(temp_file)
        _fsync_archive_temp(temp_file)
        temp_file.close()
        temp_file = None
        _replace_archive_temp(temp_path, dest)
        temp_path = None
        return dest
    except Exception as exc:
        if temp_file is not None:
            try:
                temp_file.close()
            except Exception:
                _log.warning("archive temporary file close failed", exc_info=True)
        if temp_path is not None:
            try:
                _remove_archive_temp(temp_path)
            except FileNotFoundError:
                pass
            except Exception:
                _log.warning("archive temporary file cleanup failed", exc_info=True)
        if isinstance(exc, ArchiveError):
            raise
        raise ArchiveError() from exc


def run_import(session: Session, file_path: str, original_name: str,
               uploaded_by: str | None = None, mode: str = "skip",
               import_job_id: int | None = None) -> SysImportBatch:
    """对单个 .xlsx 执行完整导入。返回 batch（含 report_json）。

    校验在 API 层（扩展名/大小）；此处做 hash 去重 + 锁 + 入库。
    import_job_id：批量作业归组（单文件上传为 None）；成功/失败批次都带上，便于作业详情聚合。
    """
    file_hash = sha256_file(file_path)

    # 1) 应用级导入锁先行（同一时间仅一个导入；顺带消除"去重检查在加锁前"的并发竞态）
    session.execute(
        text("SELECT pg_advisory_xact_lock(:k)"),
        {"k": DATA_CHANGE_ADVISORY_LOCK_KEY},
    )

    def new_batch(status: str) -> SysImportBatch:
        return SysImportBatch(
            filename=original_name,
            file_type="unknown",
            file_hash=file_hash,
            uploaded_by=uploaded_by,
            import_job_id=import_job_id,
            status=status,
        )

    # 2) 固定维保回填工作簿必须先于通用 hash 去重分流，否则历史同 hash
    # 成功批次会把“入口错误”误报成“重复文件”。同时，损坏/超限等 ReaderError
    # 仍需建立失败批次，保证批量作业逐文件审计完整。
    try:
        reader.reject_roundtrip_workbook(file_path)
    except ReaderError as exc:
        batch = new_batch("failed")
        batch.report_json = {"error": str(exc)}
        session.add(batch)
        session.flush()
        raise

    # 3) hash 去重：skip 模式拒绝重复成功文件（幂等）；upsert(修复)模式是"显式要求重处理"。
    #    旧成功批次必须等新批次完整通过后再 supersede；否则新文件预检/装载失败会破坏
    #    既有成功审计链。两次状态切换在同一事务 flush，仍满足 success hash 偏唯一索引。
    duplicate_batch_id = successful_batch_ids_by_hash(session, {file_hash}).get(file_hash)
    if duplicate_batch_id is not None:
        if mode != "upsert":
            raise DuplicateFileError(duplicate_batch_id)

    # 4) 建 batch（先占位，类型稍后回填）
    batch = new_batch("processing")
    session.add(batch)
    session.flush()  # 取 batch.id

    try:
        # 先只扫描全部 sheet 的边界和表头；真正的值只加载选中的业务页，
        # 这是 inline-string 大工作簿避免 OOM 的关键边界。
        inspected_sheets = reader.inspect_workbook(file_path, load_data=False)
        selection = sheet_selection.select_workbook_sheets(inspected_sheets)
        if not selection.selected:
            raise ReaderError(
                "无法识别文件类型，请确认是采购/销售/库存/维保出库/报销明细导出文件",
                code="no_recognized_sheet",
            )
        selected_sheets = reader.load_selected_workbook(
            file_path,
            [sheet.sheet_name for sheet in selection.selected],
            inspections=inspected_sheets,
        )

        # §17.5 调度：**有报销页才是项目追踪工作簿**——只吃报销页，其余可识别页
        # （系统导出的备件回填副本/手工粘贴件，非权威源）跳过并报告，防回环污染。
        # 没有报销页 → 老语义：导第一个可识别页（隐藏副本页/杂页不再拖垮整个文件），
        # 其余页在报告中列为 ignored_sheets 提示。
        expense_sheets = [
            sheet for sheet in selected_sheets if sheet.file_type == mapping.EXPENSE
        ]
        if expense_sheets:
            primary = None
            batch.file_type = selection.file_type
            for s in expense_sheets:
                reader.require_clean_columns(s)
        else:
            primary = selected_sheets[0]
            reader.require_clean_columns(primary)
            batch.file_type = selection.file_type

        storage_path = _archive(file_path, file_hash)
        session.add(SysRawFile(batch_id=batch.id, filename=original_name,
                               file_hash=file_hash, storage_path=storage_path))
        snapshot = datetime.now(timezone.utc).date()

        if expense_sheets:
            # 多报销页合并成一次装载：单次合同级替换（修复模式不互删）、计数不虚高
            result = transform(expense_sheets[0].df, mapping.EXPENSE,
                               anchor=expense_sheets[0].anchor)
            if len(expense_sheets) > 1:
                for e in result.errors:
                    e.error_detail = f"[{expense_sheets[0].sheet_name}] {e.error_detail}"
                for s in expense_sheets[1:]:
                    r = transform(s.df, mapping.EXPENSE, anchor=s.anchor)
                    for e in r.errors:
                        e.error_detail = f"[{s.sheet_name}] {e.error_detail}"
                    result.errors.extend(r.errors)
                    result.rows_total += r.rows_total
                    result.rows_inactive += r.rows_inactive
                    result.rows_skipped_no_data += r.rows_skipped_no_data
                    result.lines.extend(r.lines)
                # 跨页同键（完全相同的行/同单号行）＝同一笔费用，保留首次，防 upsert 撞键
                seen_keys: set[str] = set()
                result.lines = [ln for ln in result.lines
                                if not (ln["raw_line_id"] in seen_keys
                                        or seen_keys.add(ln["raw_line_id"]))]
            extra_report = {
                "expense_sheets": [s.sheet_name for s in expense_sheets],
                "skipped_sheets": [f"{s.sheet_name}（{s.file_type}，此类数据请用氚云原生导出单独上传）"
                                   for s in selection.ignored_recognized],
            }
            src_cols = list(expense_sheets[0].df.columns)
        else:
            result = transform(primary.df, primary.file_type)
            extra_report = {}
            if selection.ignored_recognized:
                extra_report["ignored_sheets"] = [
                    f"{s.sheet_name}（{s.file_type}，多页文件只导第一个可识别页）"
                    for s in selection.ignored_recognized
                ]
            src_cols = list(primary.df.columns)

        # 错误行入 sys_import_error（失败批次也保留，便于按行修正）
        for e in result.errors:
            session.add(SysImportError(batch_id=batch.id, row_no=e.row_no,
                                       error_type=e.error_type, error_detail=e.error_detail,
                                       raw_row=e.raw_row))

        # §17.4 修复模式=以本表为准（合同级删除重建）：要求报销页零错误行——
        # 半截行/撞键行意味着"本表"不完整，此时整表替换会静默丢账，必须先修再导
        if expense_sheets and mode == "upsert" and result.errors:
            raise ReaderError(
                f"修复模式（以本表为准）要求报销页无错误行：发现 {len(result.errors)} 行错误"
                "（详见批次错误明细），本次未导入。请修正后重试，或改用「跳过」模式仅补新行。")

        counts = loader.load(session, result, batch.id, snapshot, mode=mode,
                             operated_by=uploaded_by, audit_overwrites=True)
        detection = data_quality_amount_mismatch.detect_imported_lines(
            session,
            file_type=result.file_type,
            raw_line_ids=[line["raw_line_id"] for line in result.lines],
            detected_by=uploaded_by,
        )

        report = {"file_type": batch.file_type, **counts,
                  "data_quality_detection": detection,
                  "rows_skipped_no_data": result.rows_skipped_no_data,
                  # 缺价格列留痕：即便用户确认导入了无金额文件，批次详情也能看到此告警
                  "missing_price_columns": not mapping.has_price_columns(
                      src_cols, result.file_type),
                  **extra_report,
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
        if duplicate_batch_id is not None:
            session.execute(
                update(SysImportBatch).where(SysImportBatch.id == duplicate_batch_id)
                .values(status="superseded")
            )
        batch.status = "success"
        session.flush()
        return batch
    except ReaderError as exc:
        batch.status = "failed"
        batch.report_json = {"error": str(exc)}
        session.flush()
        raise

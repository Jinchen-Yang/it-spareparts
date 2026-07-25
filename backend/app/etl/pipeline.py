"""导入编排：校验→hash→锁→batch→read→transform→load→report（§6.1/§6.6）。"""
import hashlib
import os
import shutil
from datetime import date, datetime, timezone

from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.etl import loader, mapping, reader, sheet_selection
from app.etl.reader import ReaderError
from app.etl.transform import transform
from app.models.system import SysImportBatch, SysImportError, SysRawFile
from app.services import data_quality_amount_mismatch

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
        inspected_sheets = reader.inspect_workbook(file_path)
        selection = sheet_selection.select_workbook_sheets(inspected_sheets)
        if not selection.selected:
            raise ReaderError(
                "无法识别文件类型，请确认是采购/销售/库存/维保出库/报销明细导出文件",
                code="no_recognized_sheet",
            )
        selected_sheets = [
            sheet.parsed for sheet in selection.selected if sheet.parsed is not None
        ]

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
        batch.status = "success"
        session.flush()
        return batch
    except ReaderError as exc:
        batch.status = "failed"
        batch.report_json = {"error": str(exc)}
        session.flush()
        raise

"""导入编排：校验→hash→锁→batch→read→transform→load→report（§6.1/§6.6）。"""
import hashlib
import logging
import os
import re
import stat
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select, text, update, or_
from sqlalchemy.orm import Session

from app.config import DATA_CHANGE_ADVISORY_LOCK_KEY, get_settings
from app.etl import expense_void, loader, mapping, reader, sheet_selection
from app.models.maintenance import FProjectExpense
from app.etl.reader import ReaderError
from app.etl.transform import TransformResult, transform
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


@dataclass
class LoadedWorkbook:
    """inspect → 选表 → 只物化选中页 → 列检查。run_import 与导入前预演共用。"""

    inspected: list
    selection: object
    selected_sheets: list
    expense_sheets: list
    primary: object | None

    @property
    def file_type(self) -> str | None:
        return self.selection.file_type


@dataclass
class TransformedWorkbook:
    result: TransformResult
    extra_report: dict = field(default_factory=dict)
    src_cols: list = field(default_factory=list)


def load_workbook(file_path: str, *, inspected: list | None = None) -> LoadedWorkbook:
    """读法只写一处：预演若自己再写一遍选表/物化，读出的 lines 集合就可能与真实
    导入不同——而 lines 集合正是作废集合的补集。

    inspected：调用方（precheck）已扫过表头时传入，省掉一次整包扫描（客户 3802 行
    文件实测 ≈3.5s）。run_import 不传。
    """
    # 先只扫描全部 sheet 的边界和表头；真正的值只加载选中的业务页，
    # 这是 inline-string 大工作簿避免 OOM 的关键边界。
    inspected_sheets = (inspected if inspected is not None
                        else reader.inspect_workbook(file_path, load_data=False))
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
    primary = None if expense_sheets else selected_sheets[0]
    return LoadedWorkbook(inspected_sheets, selection, selected_sheets,
                          expense_sheets, primary)


def require_clean_workbook(loaded: LoadedWorkbook) -> None:
    """将被取用的页若有重复列名 → 整批拒绝。独立于 load_workbook，好让 run_import
    在它之前把 batch.file_type 落库（失败批次仍记录识别出的类型，与旧行为一致）。"""
    if loaded.expense_sheets:
        for s in loaded.expense_sheets:
            reader.require_clean_columns(s)
    else:
        reader.require_clean_columns(loaded.primary)


def transform_workbook(loaded: LoadedWorkbook) -> TransformedWorkbook:
    """transform + 多报销页合并 + 跨页去重，同样只写一处（同上理由）。"""
    selection, expense_sheets, primary = (
        loaded.selection, loaded.expense_sheets, loaded.primary)
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
                result.expense_anchors.extend(r.expense_anchors)
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
        return TransformedWorkbook(result, extra_report,
                                   list(expense_sheets[0].df.columns))
    result = transform(primary.df, primary.file_type)
    extra_report = {}
    if selection.ignored_recognized:
        extra_report["ignored_sheets"] = [
            f"{s.sheet_name}（{s.file_type}，多页文件只导第一个可识别页）"
            for s in selection.ignored_recognized
        ]
    return TransformedWorkbook(result, extra_report, list(primary.df.columns))


def run_import(session: Session, file_path: str, original_name: str,
               uploaded_by: str | None = None, mode: str = "skip",
               import_job_id: int | None = None,
               auto_assign_maintenance_projects: bool = False, expected_void_fingerprint: str | None = None,
               require_void_preview: bool = False) -> SysImportBatch:
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
        loaded = load_workbook(file_path)
        batch.file_type = loaded.file_type
        require_clean_workbook(loaded)
        selection, expense_sheets = loaded.selection, loaded.expense_sheets

        storage_path = _archive(file_path, file_hash)
        session.add(SysRawFile(batch_id=batch.id, filename=original_name,
                               file_hash=file_hash, storage_path=storage_path))
        snapshot = datetime.now(timezone.utc).date()

        transformed = transform_workbook(loaded)
        result, extra_report, src_cols = (
            transformed.result, transformed.extra_report, transformed.src_cols)

        # 错误行入 sys_import_error（失败批次也保留，便于按行修正）
        for e in result.errors:
            raw_row = e.raw_row
            if e.identity:
                # raw_row 只读本行原始单元格：延续行几乎全空、§17.3 宽松列
                # （单号/序号/报销金额）不在 full_map 里。identity 是 gvh 归一后
                # 的那份，留痕以它为准。
                raw_row = {**(raw_row or {}),
                           "_identity": {k: (v if v is None or isinstance(v, (int, str))
                                             else str(v))
                                         for k, v in e.identity.items()}}
            session.add(SysImportError(batch_id=batch.id, row_no=e.row_no,
                                       error_type=e.error_type, error_detail=e.error_detail,
                                       raw_row=raw_row))

        # §17.4 修复模式=以本表为准（合同级删除重建）：半截行/撞键行意味着"本表"
        # 不完整，此时整表替换会静默丢账，必须先修再导。
        #
        # missing_link 是唯一例外，也是唯一能按类型放行的错误：xsdd 直到
        # transform.py 的归集键那一步才求值，其余错误全部在此之前 continue——
        # 那些行**可能带着完整 XSDD**，error_type 对「这行属不属于某个被重建的
        # 合同」零信息量，故一律仍拦（duplicate_key 更要拦：第一行已进 lines 并
        # 会 UPDATE 掉库里的记录）。missing_link 则按定义即可证明无合同，对重建
        # 范围零贡献。放行的安全性不靠这条门禁，靠 loader 消费的
        # expense_void.classify 两道抑制——门禁判断错了钱也丢不了，反之不成立。
        #
        # 刻意不把门禁对齐到 SOFT_ERROR_TYPES：在途单常带 XSDD，其幂等键与日期
        # 无关，一张已入库单据被退回改「进行中」后重导会静默作废旧行。只对齐计
        # 数与展示（loader/imports 已如此），不对齐作废门禁。
        if expense_sheets and mode == "upsert":
            blocking = expense_void.blocking_errors(result)
            if blocking:
                raise ReaderError(
                    f"修复模式（以本表为准）要求报销页无错误行：发现 {len(blocking)} 行错误"
                    "（详见批次错误明细），本次未导入。请修正后重试，或改用「跳过」模式仅补新行。")
            # 会真的作废的形态（单合同 + 锚定 + 无排除行）必须带着预演承诺进来。
            # 强制放在 HTTP 入口（require_void_preview 由 /upload 与 /upload-batch 传
            # True），不写死在 run_import 内部：既有直接调用 run_import 的路径与用例
            # 不受影响，而两个公开入口都没有绕过预演的后门。
            if (require_void_preview and expected_void_fingerprint is None
                    and expense_void.is_armed(expense_void.plan_inputs(result, mode=mode))):
                raise ReaderError(
                    "修复模式导入单合同项目工作簿报销页必须先完成作废预演（导入页会在预检后"
                    "自动预演并展示将作废的行），本次未导入。",
                    code="void_preview_required")

        maintenance_lock_envelope = None
        assignment_service = None
        warehouse_service = None
        if auto_assign_maintenance_projects and result.file_type == mapping.MAINTENANCE:
            # 身份锁与所有可能既有目标必须早于 WBDD 事实行锁；load 后只能
            # 在此信封内 apply，禁止 order/line → workbook_state 反序。
            from app.services import maintenance_source_assignments as assignment_service
            from app.services import maintenance_warehouse as warehouse_service

            target_project_ids = assignment_service.prelock_import_assignment_targets(
                session,
                order_heads=result.orders.values(),
            )
            maintenance_lock_envelope = loader.MaintenanceImportLockEnvelope(
                target_project_ids=set(target_project_ids)
            )

        counts = loader.load(
            session,
            result,
            batch.id,
            snapshot,
            mode=mode,
            operated_by=uploaded_by,
            audit_overwrites=True,
            maintenance_lock_envelope=maintenance_lock_envelope, expected_void_fingerprint=expected_void_fingerprint)
        sales_project_sync = None
        if auto_assign_maintenance_projects and result.file_type == mapping.SALES:
            from app.services import maintenance_bulk_import

            # 复用 loader 已解析的 TransformResult，不重开 XLSX：_detect 走
            # read_only=False 会实体化每个 worksheet，抵消 load_selected_workbook
            # 的内存边界（真实销售导出有 19 个 sheet），也可能选到与已入库事实
            # 不同的那一张表（Codex P1，2026-09-03）。
            try:
                sales_project_sync = maintenance_bulk_import.sync_uploaded_sales_workbook(
                    session,
                    None,
                    original_name,
                    operated_by=uploaded_by or "system",
                    import_batch_id=batch.id,
                    detected_sheet=maintenance_bulk_import.transformed_sales_sheet(
                        result, source_columns=src_cols),
                )
            except (
                maintenance_bulk_import.BulkImportInvalid,
                maintenance_bulk_import.BulkImportConflict,
            ) as exc:
                # ReaderError is intentionally forbidden here: API callers
                # commit parse-failure batches, which would also commit the
                # already-loaded sales facts.  Integrity failure guarantees a
                # whole-transaction rollback in both single and batch upload.
                raise loader.ImportIntegrityError(
                    f"维保销售订单自动建项失败：{exc}"
                ) from exc
        auto_assignment = None
        if maintenance_lock_envelope is not None and assignment_service is not None:
            try:
                auto_assignment = assignment_service.auto_assign_imported_orders(
                    session,
                    operated_by=uploaded_by or "system",
                    source_order_ids=set(result.orders),
                    prelocked_states=maintenance_lock_envelope.states,
                    prelocked_projects=maintenance_lock_envelope.projects,
                )
            except (
                assignment_service.SourceAssignmentConflict,
                warehouse_service.MaintenanceWarehouseConflict,
            ) as exc:
                raise loader.ImportConcurrencyConflict(
                    "WBDD 自动归属在导入期间发生变化，请重试"
                ) from exc
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
        if auto_assignment is not None:
            report["auto_assignment"] = auto_assignment
        if sales_project_sync is not None:
            report["maintenance_sales_project_sync"] = sales_project_sync
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


def preview_expense_void(session: Session, file_path: str, *, mode: str) -> dict:
    """导入前作废预演（纯读：不加锁、不建批次、不写任何东西）。

    读法与判定与 run_import 逐字节同源（load_workbook / transform_workbook /
    expense_void.classify），本函数没有任何一行自己的作废逻辑；与 loader 的差别只有
    候选行来自无锁投影而非加锁实体、以及不执行写入。返回的 fingerprint 由 HTTP 层
    签进令牌，loader 装载期复核（见 expense_void 模块 docstring）。

    status:
      not_applicable   非修复模式 / 无报销页 / 报销页为空（无合同）——不需要令牌
      will_be_rejected 门禁会整批拒绝（与 pipeline 门禁同一个 blocking_errors）
      suppressed       删除侧被 D-09 抑制（reason 说明原因）——不需要令牌，精确
      ready            会真的作废：给出逐行清单、金额与指纹（令牌由 API 层签发）
    """
    loaded = load_workbook(file_path)
    require_clean_workbook(loaded)
    if mode != "upsert" or not loaded.expense_sheets:
        return {"status": "not_applicable", "file_type": loaded.file_type}
    result = transform_workbook(loaded).result
    inputs = expense_void.plan_inputs(result, mode="upsert")
    base = {
        "file_type": loaded.file_type,
        "rows_incoming": len(inputs.incoming_ids),
        "dropped_no_contract": inputs.dropped_no_contract,
        "contracts": sorted(inputs.contracts),
        "anchored": inputs.anchored,
    }
    blocking = expense_void.blocking_errors(result)
    if blocking:
        return {"status": "will_be_rejected", "blocking_errors": len(blocking),
                "blocking_error_types": sorted(expense_void.iter_error_types(blocking)),
                **base}
    if inputs.suppressed_reason:
        return {"status": "suppressed", "reason": inputs.suppressed_reason, **base}
    if not expense_void.is_armed(inputs):
        return {"status": "not_applicable", **base}

    incoming_ids = sorted(inputs.incoming_ids)
    scope_contracts = expense_void.scope_contracts(inputs)
    predicates = []
    if incoming_ids:
        predicates.append(FProjectExpense.raw_line_id.in_(incoming_ids))
    if scope_contracts:
        predicates.append(FProjectExpense.linked_sales_order_no.in_(scope_contracts))
    existing = {
        row.raw_line_id: row
        for row in session.execute(
            select(
                FProjectExpense.raw_line_id, FProjectExpense.data_status,
                FProjectExpense.amount, FProjectExpense.linked_sales_order_no,
                FProjectExpense.bxd_no, FProjectExpense.line_no,
                FProjectExpense.expense_date, FProjectExpense.person,
                FProjectExpense.reason,
            ).where(or_(*predicates))
        )
    } if predicates else {}
    decision = expense_void.classify(existing, inputs)
    rows = expense_void.void_rows(decision, existing)
    (contract,) = inputs.contracts
    return {
        "status": "ready",
        "contract": contract,
        "fingerprint": expense_void.fingerprint(decision, inputs, existing),
        "void": {
            "rows": len(rows),
            "amount": format(expense_void.void_amount(decision, existing), "f"),
            "already_void_rows": len(decision.already_void_ids),
        },
        "void_rows": rows[:_VOID_PREVIEW_ROW_LIMIT],
        "void_rows_truncated": len(rows) > _VOID_PREVIEW_ROW_LIMIT,
        **base,
    }


_VOID_PREVIEW_ROW_LIMIT = 200

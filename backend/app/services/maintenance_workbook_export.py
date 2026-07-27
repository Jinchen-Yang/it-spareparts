"""维保项目工作簿的单本与批量 ZIP 导出。"""
import csv
import hashlib
import io
import re
import shutil
import threading
import unicodedata
from dataclasses import dataclass
from datetime import date
from tempfile import SpooledTemporaryFile
from zipfile import ZIP_STORED, ZipFile

from openpyxl import Workbook
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app import config
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder, FProjectExpense
from app.services import maintenance_cost, maintenance_workbook_renderer


_INVALID_MEMBER_CHARS = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]+')
_INVALID_TEXT_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")
MAX_WORKBOOKS = 500
MAX_PART_LINES = 250_000
MAX_EXPENSE_LINES = 250_000
MAX_EXCEL_ROWS = 1_048_576
MAX_EXCEL_COLUMNS = 16_384
MAX_ZIP_BYTES = 512 * 1024 * 1024
_MANIFEST_HEADERS = (
    "记录类型", "合同号", "文件名", "命中订单数", "命中最早日期", "命中最晚日期",
    "跳过维保单号", "跳过原始订单ID", "跳过制单日期", "说明",
)
_BULK_EXPORT_LOCK = threading.Lock()


class WorkbookExportRejected(ValueError):
    """可安全展示给调用方的整批拒绝原因。"""


class WorkbookExportBusy(RuntimeError):
    """当前应用实例已有一个批量工作簿构建任务。"""


class _SizeLimitedFile:
    """给 ZipFile 提供可 seek 的硬字节上限，禁止临时文件先超限再事后拒绝。"""

    def __init__(self, raw, max_size: int):
        self._raw = raw
        self._max_size = max_size
        self._extent = raw.tell()

    def write(self, data):
        end = self._raw.tell() + len(data)
        if max(self._extent, end) > self._max_size:
            raise WorkbookExportRejected("批量工作簿 ZIP 超过 512 MiB 上限")
        written = self._raw.write(data)
        self._extent = max(self._extent, self._raw.tell())
        return written

    def seek(self, *args):
        return self._raw.seek(*args)

    def tell(self):
        return self._raw.tell()

    def flush(self):
        return self._raw.flush()


def safe_xlsx_text(value):
    """把外部动态文本转成不会被 Excel 执行或静默截断的单元格值。"""
    if not isinstance(value, str):
        return value
    text = _INVALID_TEXT_CONTROLS.sub("", value)
    if text.lstrip()[:1] in ("=", "+", "-", "@"):
        text = "'" + text
    if len(text) > 32767:
        raise WorkbookExportRejected("文本超过 Excel 单元格上限 32767 个字符")
    return text


@dataclass(frozen=True)
class _ContractMatch:
    contract: str
    order_count: int
    date_from: date | None
    date_to: date | None


def _selection_filters(
    date_from: date | None,
    date_to: date | None,
) -> tuple:
    filters = (FMaintenanceOrder.data_status == config.ACTIVE_STATUS,)
    if date_from is not None and date_to is not None:
        filters += (
            FMaintenanceOrder.order_date >= date_from,
            FMaintenanceOrder.order_date <= date_to,
        )
    return filters


def _valid_contract_filter():
    return (
        FMaintenanceOrder.linked_sales_order_no.is_not(None)
        & (func.btrim(FMaintenanceOrder.linked_sales_order_no) != "")
    )


def _contract_matches(
    db: Session,
    date_from: date | None,
    date_to: date | None,
) -> list[_ContractMatch]:
    filters = _selection_filters(date_from, date_to)
    rows = db.execute(
        select(
            FMaintenanceOrder.linked_sales_order_no,
            func.count(FMaintenanceOrder.id),
            func.min(FMaintenanceOrder.order_date),
            func.max(FMaintenanceOrder.order_date),
        )
        .where(*filters, _valid_contract_filter())
        .group_by(FMaintenanceOrder.linked_sales_order_no)
        .limit(MAX_WORKBOOKS + 1)
    ).all()
    return sorted(
        (
            _ContractMatch(contract, int(count), first_date, last_date)
            for contract, count, first_date, last_date in rows
        ),
        key=lambda match: (
            unicodedata.normalize("NFKC", match.contract).casefold(),
            match.contract,
        ),
    )


def _unlinked_orders(
    db: Session,
    date_from: date | None,
    date_to: date | None,
):
    return db.execute(
        select(
            FMaintenanceOrder.order_no,
            FMaintenanceOrder.raw_order_id,
            FMaintenanceOrder.order_date,
        )
        .where(
            *_selection_filters(date_from, date_to),
            or_(
                FMaintenanceOrder.linked_sales_order_no.is_(None),
                func.btrim(FMaintenanceOrder.linked_sales_order_no) == "",
            ),
        )
        .order_by(
            FMaintenanceOrder.order_date.asc().nullslast(),
            FMaintenanceOrder.order_no.asc().nullslast(),
            FMaintenanceOrder.id,
        )
        .execution_options(stream_results=True, yield_per=1000)
    )


def _preflight_resource_limits(
    db: Session,
    matches: list[_ContractMatch],
) -> None:
    contracts = [match.contract for match in matches]
    part_filters = [
        FMaintenanceOrder.linked_sales_order_no.in_(contracts),
        FMaintenanceOrder.order_date >= config.MAINT_COST_START_DATE,
    ]
    if config.ACTIVE_STATUS_ONLY:
        part_filters.append(FMaintenanceOrder.data_status == config.ACTIVE_STATUS)
    part_count = db.scalar(
        select(func.count(FMaintenanceLine.id))
        .join(FMaintenanceOrder, FMaintenanceOrder.id == FMaintenanceLine.order_id)
        .where(*part_filters)
    ) or 0
    if part_count > MAX_PART_LINES:
        raise WorkbookExportRejected(
            f"项目工作簿备件明细超过批量上限 {MAX_PART_LINES} 行",
        )
    if part_count + 1 > MAX_EXCEL_ROWS:
        raise WorkbookExportRejected("备件明细 Sheet 超过 Excel 行数上限")

    expense_count = db.scalar(
        select(func.count(FProjectExpense.id)).where(
            FProjectExpense.linked_sales_order_no.in_(contracts),
        )
    ) or 0
    if expense_count > MAX_EXPENSE_LINES:
        raise WorkbookExportRejected(
            f"项目工作簿报销明细超过批量上限 {MAX_EXPENSE_LINES} 行",
        )
    if expense_count + 3 > MAX_EXCEL_ROWS:
        raise WorkbookExportRejected("报销明细 Sheet 超过 Excel 行数上限")

    fee_category = func.coalesce(FProjectExpense.fee_category, "(未分类费用)")
    category_count = db.scalar(
        select(func.count(func.distinct(fee_category))).where(
            FProjectExpense.linked_sales_order_no.in_(contracts),
            FProjectExpense.data_status == config.MAINT_EXPENSE_ACTIVE_STATUS,
            FProjectExpense.amount.is_not(None),
            FProjectExpense.expense_date.is_not(None),
        )
    ) or 0
    if category_count + 3 > MAX_EXCEL_COLUMNS:
        raise WorkbookExportRejected("项目预算 Sheet 超过 Excel 列数上限")


def _member_collision_key(name: str) -> str:
    return unicodedata.normalize("NFKC", name).casefold().rstrip(" .")


def _member_name(contract: str, used_names: set[str]) -> str:
    clean = unicodedata.normalize("NFKC", contract)
    clean = _INVALID_MEMBER_CHARS.sub("_", clean)
    while ".." in clean:
        clean = clean.replace("..", "_")
    clean = clean.strip(" .") or "contract"
    clean = clean[:80].rstrip(" ._") or "contract"
    leaf = f"project_workbook_{clean}.xlsx"
    collision_key = _member_collision_key(leaf)
    if collision_key in used_names:
        digest = hashlib.sha256(contract.encode("utf-8")).hexdigest()[:10]
        leaf = f"project_workbook_{clean[:68]}_{digest}.xlsx"
        collision_key = _member_collision_key(leaf)
        suffix = 2
        while collision_key in used_names:
            leaf = f"project_workbook_{clean[:64]}_{digest}_{suffix}.xlsx"
            collision_key = _member_collision_key(leaf)
            suffix += 1
    used_names.add(collision_key)
    return f"项目工作簿/{leaf}"


def _safe_manifest_value(value) -> str:
    if value is None:
        return ""
    text = _INVALID_TEXT_CONTROLS.sub("", str(value))
    if text.lstrip()[:1] in ("=", "+", "-", "@"):
        text = "'" + text
    return text


def _write_manifest_row(writer, values: tuple) -> None:
    writer.writerow(tuple(_safe_manifest_value(value) for value in values))


def build_contract_workbook_file(
    db: Session,
    contract: str,
) -> SpooledTemporaryFile:
    """构建一本项目工作簿；返回的流归调用方所有。"""
    output = SpooledTemporaryFile(max_size=5 * 1024 * 1024, mode="w+b")
    workbook = None
    try:
        data = maintenance_cost.contract_workbook_data(db, contract)
        workbook = Workbook()
        workbook = maintenance_workbook_renderer.render_contract_workbook(
            contract,
            data,
            safe_xlsx_text,
            workbook=workbook,
        )
        workbook.save(output)
        output.seek(0)
        return output
    except BaseException:
        output.close()
        raise
    finally:
        if workbook is not None:
            workbook.close()


def _build_contract_workbooks_zip(
    db: Session,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
) -> SpooledTemporaryFile:
    """按命中维保单选择合同，并把完整项目工作簿打入一个 ZIP。"""
    if (date_from is None) != (date_to is None):
        raise WorkbookExportRejected("date_from 与 date_to 必须同时提供")
    if date_from is not None and date_to is not None and date_from > date_to:
        raise WorkbookExportRejected("date_from 不能晚于 date_to")
    selected_orders = db.scalar(
        select(func.count(FMaintenanceOrder.id)).where(
            *_selection_filters(date_from, date_to),
        )
    ) or 0
    if selected_orders == 0:
        raise WorkbookExportRejected("所选范围内没有已生效维保订单")
    matches = _contract_matches(db, date_from, date_to)
    if not matches:
        raise WorkbookExportRejected("所选范围内的已生效维保订单均未关联合同")
    if len(matches) > MAX_WORKBOOKS:
        raise WorkbookExportRejected(f"命中合同超过批量上限 {MAX_WORKBOOKS} 本")
    _preflight_resource_limits(db, matches)
    output = SpooledTemporaryFile(max_size=16 * 1024 * 1024, mode="w+b")
    try:
        used_member_names: set[str] = set()
        exports = [
            (match, _member_name(match.contract, used_member_names))
            for match in matches
        ]
        limited_output = _SizeLimitedFile(output, MAX_ZIP_BYTES)
        with ZipFile(
            limited_output,
            mode="w",
            compression=ZIP_STORED,
            allowZip64=True,
        ) as archive:
            with archive.open(
                "导出清单.csv",
                mode="w",
                force_zip64=True,
            ) as manifest_bytes:
                manifest_bytes.write(b"\xef\xbb\xbf")
                with io.TextIOWrapper(
                    manifest_bytes,
                    encoding="utf-8",
                    newline="",
                    write_through=True,
                ) as manifest_text:
                    writer = csv.writer(manifest_text)
                    writer.writerow(_MANIFEST_HEADERS)
                    for match, member_name in exports:
                        _write_manifest_row(writer, (
                            "已生成", match.contract, member_name, match.order_count,
                            match.date_from.isoformat() if match.date_from else "",
                            match.date_to.isoformat() if match.date_to else "",
                            "", "", "", "",
                        ))
                    unlinked_orders = _unlinked_orders(db, date_from, date_to)
                    try:
                        for order_no, raw_order_id, order_date in unlinked_orders:
                            _write_manifest_row(writer, (
                                "已跳过", "", "", "", "", "",
                                order_no or "", raw_order_id,
                                order_date.isoformat() if order_date else "",
                                "未关联合同",
                            ))
                    finally:
                        unlinked_orders.close()
            for match, member_name in exports:
                workbook = build_contract_workbook_file(db, match.contract)
                try:
                    with archive.open(member_name, mode="w", force_zip64=True) as member:
                        shutil.copyfileobj(workbook, member, length=1024 * 1024)
                finally:
                    workbook.close()
        output.seek(0, 2)
        if output.tell() > MAX_ZIP_BYTES:
            raise WorkbookExportRejected("批量工作簿 ZIP 超过 512 MiB 上限")
        output.seek(0)
        return output
    except BaseException:
        output.close()
        raise


def build_contract_workbooks_zip(
    db: Session,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
) -> SpooledTemporaryFile:
    """单实例互斥地构建批量 ZIP；锁覆盖选择、预检和完整构建阶段。"""
    if not _BULK_EXPORT_LOCK.acquire(blocking=False):
        raise WorkbookExportBusy("已有批量工作簿导出正在执行，请稍后重试")
    try:
        return _build_contract_workbooks_zip(
            db,
            date_from=date_from,
            date_to=date_to,
        )
    finally:
        _BULK_EXPORT_LOCK.release()

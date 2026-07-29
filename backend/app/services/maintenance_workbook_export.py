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
from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from app import config
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder, FProjectExpense
from app.services import maintenance_cost, maintenance_workbook_renderer


_INVALID_MEMBER_CHARS = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]+')
_INVALID_TEXT_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")
MAX_WORKBOOKS = 500
MAX_SELECTED_ORDERS = 250_000
MAX_PART_LINES = 250_000
MAX_EXPENSE_LINES = 250_000
MAX_PART_LINES_PER_WORKBOOK = 25_000
MAX_EXPENSE_LINES_PER_WORKBOOK = 25_000
MAX_DYNAMIC_TEXT_BYTES_PER_WORKBOOK = 64 * 1024 * 1024
MAX_WORKBOOK_BYTES = 256 * 1024 * 1024
PART_RENDERED_TEXT_OVERHEAD_BYTES = 64
EXPENSE_RENDERED_TEXT_OVERHEAD_BYTES = 32
MAX_EXCEL_ROWS = 1_048_576
MAX_EXCEL_COLUMNS = 16_384
MAX_ZIP_BYTES = 512 * 1024 * 1024
MAX_MEMBER_LEAF_BYTES = 240
_FEE_CATEGORY_HEADER_PREFIX_BYTES = len(
    maintenance_workbook_renderer.FEE_CATEGORY_HEADER_PREFIX.encode("utf-8"),
)
_MANIFEST_HEADERS = (
    "记录类型", "合同号", "文件名", "命中订单数", "命中最早日期", "命中最晚日期",
    "跳过维保单号", "跳过原始订单ID", "跳过制单日期", "说明",
)
_BULK_EXPORT_LOCK = threading.Lock()


class WorkbookExportRejected(ValueError):
    """可安全展示给调用方的整批拒绝原因。"""

    status_code = 422


class WorkbookExportTooLarge(WorkbookExportRejected):
    """标准工作簿导出命中明确资源上限。"""

    status_code = 413


class WorkbookExportNotFound(WorkbookExportRejected):
    """请求的单合同业务对象不存在。"""


class WorkbookExportBusy(RuntimeError):
    """当前应用实例已有一个批量工作簿构建任务。"""


class _SizeLimitedFile:
    """给 ZipFile 提供可 seek 的硬字节上限，禁止临时文件先超限再事后拒绝。"""

    def __init__(
        self,
        raw,
        max_size: int,
        rejection_detail: str = "批量工作簿 ZIP 超过 512 MiB 上限",
    ):
        self._raw = raw
        self._max_size = max_size
        self._rejection_detail = rejection_detail
        self._extent = raw.tell()

    def write(self, data):
        end = self._raw.tell() + len(data)
        if max(self._extent, end) > self._max_size:
            raise WorkbookExportTooLarge(self._rejection_detail)
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
        raise WorkbookExportTooLarge("文本超过 Excel 单元格上限 32767 个字符")
    return text


@dataclass(frozen=True)
class _ContractMatch:
    contract: str
    order_count: int
    date_from: date | None
    date_to: date | None


def _requested_scope_filters(
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


def _selection_filters(
    date_from: date | None,
    date_to: date | None,
) -> tuple:
    return (
        *_requested_scope_filters(date_from, date_to),
        FMaintenanceOrder.order_date >= config.MAINT_COST_START_DATE,
    )


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
            func.count(func.distinct(FMaintenanceOrder.id)),
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


def _octet_length(value):
    """PostgreSQL 端统计将进入 XLSX 的 UTF-8 字节，避免先物化巨型文本。"""
    return func.octet_length(func.coalesce(value, ""))


def _acquire_shared_source_lock(db: Session) -> None:
    """阻止导入/重算在预检与工作簿物化之间提交新事实行。"""
    db.execute(
        text("SELECT pg_advisory_xact_lock_shared(:k)"),
        {"k": config.DATA_CHANGE_ADVISORY_LOCK_KEY},
    )


def _preflight_resource_limits(
    db: Session,
    matches: list[_ContractMatch],
    *,
    date_from: date | None = None,
    date_to: date | None = None,
) -> None:
    contracts = [match.contract for match in matches]
    text_bytes_by_contract = {
        contract: len(contract.encode("utf-8")) * 3 + 2
        for contract in contracts
    }
    part_filters = [
        FMaintenanceOrder.linked_sales_order_no.in_(contracts),
        FMaintenanceOrder.order_date >= config.MAINT_COST_START_DATE,
    ]
    if config.ACTIVE_STATUS_ONLY:
        part_filters.append(FMaintenanceOrder.data_status == config.ACTIVE_STATUS)
    if date_from is not None:
        part_filters.append(FMaintenanceOrder.order_date >= date_from)
    if date_to is not None:
        part_filters.append(FMaintenanceOrder.order_date <= date_to)

    rendered_project = func.coalesce(
        func.nullif(FMaintenanceOrder.project_raw, ""),
        FMaintenanceOrder.project_std,
        "",
    )
    part_text_bytes = (
        _octet_length(FMaintenanceOrder.order_no)
        + _octet_length(FMaintenanceOrder.linked_sales_order_no)
        + _octet_length(rendered_project)
        + _octet_length(FMaintenanceOrder.demand_type)
        + _octet_length(FMaintenanceOrder.warehouse)
        + _octet_length(FMaintenanceOrder.salesperson)
        + _octet_length(FMaintenanceOrder.business_type)
        + _octet_length(FMaintenanceLine.pn_std)
        + _octet_length(FMaintenanceLine.description)
        + _octet_length(FMaintenanceLine.serial_numbers)
        + _octet_length(FMaintenanceLine.cost_source)
        + _octet_length(FMaintenanceLine.confidence)
        + _octet_length(FMaintenanceLine.price_month)
        + _octet_length(FMaintenanceLine.cost_tax_basis)
        + _octet_length(FMaintenanceLine.reference_side)
        + PART_RENDERED_TEXT_OVERHEAD_BYTES
    )
    part_rows = db.execute(
        select(
            FMaintenanceOrder.linked_sales_order_no,
            func.count(),
            func.coalesce(func.sum(part_text_bytes), 0),
        )
        # Renderer semantics are one row per detail plus one placeholder row for
        # every order without details.  The LEFT JOIN has exactly that cardinality;
        # on placeholder rows the line-side text becomes empty while the order
        # text and rendered-label/date overhead remain in the byte budget.
        .select_from(FMaintenanceOrder)
        .outerjoin(FMaintenanceLine, FMaintenanceLine.order_id == FMaintenanceOrder.id)
        .where(*part_filters)
        .group_by(FMaintenanceOrder.linked_sales_order_no)
    ).all()
    part_count = sum(int(count) for _contract, count, _text_bytes in part_rows)
    if part_count > MAX_PART_LINES:
        raise WorkbookExportTooLarge(
            f"项目工作簿备件明细超过批量上限 {MAX_PART_LINES} 行",
        )
    for contract, count, text_bytes in part_rows:
        count = int(count)
        if count > MAX_PART_LINES_PER_WORKBOOK:
            raise WorkbookExportTooLarge(
                "单个项目工作簿备件明细超过上限 "
                f"{MAX_PART_LINES_PER_WORKBOOK} 行（合同：{contract}）",
            )
        if count + 1 > MAX_EXCEL_ROWS:
            raise WorkbookExportTooLarge("备件明细 Sheet 超过 Excel 行数上限")
        text_bytes_by_contract[contract] += int(text_bytes)

    expense_text_bytes = (
        _octet_length(FProjectExpense.person)
        + _octet_length(FProjectExpense.expense_type)
        + _octet_length(FProjectExpense.fee_category)
        + _octet_length(FProjectExpense.reason)
        + _octet_length(FProjectExpense.data_status)
        + _octet_length(FProjectExpense.bxd_no)
        + EXPENSE_RENDERED_TEXT_OVERHEAD_BYTES
    )
    expense_filters = [
        FProjectExpense.linked_sales_order_no.in_(contracts),
        FProjectExpense.expense_date >= config.MAINT_COST_START_DATE,
    ]
    if date_from is not None:
        expense_filters.append(FProjectExpense.expense_date >= date_from)
    if date_to is not None:
        expense_filters.append(FProjectExpense.expense_date <= date_to)
    expense_rows = db.execute(
        select(
            FProjectExpense.linked_sales_order_no,
            func.count(FProjectExpense.id),
            func.coalesce(func.sum(expense_text_bytes), 0),
        ).where(*expense_filters)
        .group_by(FProjectExpense.linked_sales_order_no)
    ).all()
    expense_count = sum(int(count) for _contract, count, _text_bytes in expense_rows)
    if expense_count > MAX_EXPENSE_LINES:
        raise WorkbookExportTooLarge(
            f"项目工作簿报销明细超过批量上限 {MAX_EXPENSE_LINES} 行",
        )
    for contract, count, text_bytes in expense_rows:
        count = int(count)
        if count > MAX_EXPENSE_LINES_PER_WORKBOOK:
            raise WorkbookExportTooLarge(
                "单个项目工作簿报销明细超过上限 "
                f"{MAX_EXPENSE_LINES_PER_WORKBOOK} 行（合同：{contract}）",
            )
        if count + 3 > MAX_EXCEL_ROWS:
            raise WorkbookExportTooLarge("报销明细 Sheet 超过 Excel 行数上限")
        text_bytes_by_contract[contract] += int(text_bytes)

    fee_category = func.coalesce(
        func.nullif(FProjectExpense.fee_category, ""),
        "(未分类费用)",
    )
    distinct_categories = (
        select(
            FProjectExpense.linked_sales_order_no.label("contract"),
            fee_category.label("category"),
        )
        .where(
            *expense_filters,
            FProjectExpense.data_status == config.MAINT_EXPENSE_ACTIVE_STATUS,
            FProjectExpense.amount.is_not(None),
            FProjectExpense.expense_date.is_not(None),
        )
        .distinct()
        .subquery()
    )
    category_rows = db.execute(
        select(
            distinct_categories.c.contract,
            func.count(),
            func.coalesce(
                func.sum(
                    _octet_length(distinct_categories.c.category)
                    + _FEE_CATEGORY_HEADER_PREFIX_BYTES
                    + 1
                ),
                0,
            ),
        )
        .group_by(distinct_categories.c.contract)
    ).all()
    category_count = max(
        (int(count) for _contract, count, _text_bytes in category_rows),
        default=0,
    )
    if category_count + 3 > MAX_EXCEL_COLUMNS:
        raise WorkbookExportTooLarge("项目预算 Sheet 超过 Excel 列数上限")
    for contract, _count, text_bytes in category_rows:
        text_bytes_by_contract[contract] += int(text_bytes)

    for contract, text_bytes in text_bytes_by_contract.items():
        if text_bytes > MAX_DYNAMIC_TEXT_BYTES_PER_WORKBOOK:
            raise WorkbookExportTooLarge(
                "单个项目工作簿动态文本超过安全上限 "
                f"64 MiB（合同：{contract}）",
            )


def _member_collision_key(name: str) -> str:
    return unicodedata.normalize("NFKC", name).casefold().rstrip(" .")


def _utf8_prefix(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _member_leaf(clean: str, suffix: str = "") -> str:
    prefix = "project_workbook_"
    extension = ".xlsx"
    fixed_bytes = len((prefix + suffix + extension).encode("utf-8"))
    stem = _utf8_prefix(
        clean,
        max(MAX_MEMBER_LEAF_BYTES - fixed_bytes, 0),
    ).rstrip(" ._")
    if not stem:
        stem = "contract"
    return f"{prefix}{stem}{suffix}{extension}"


def _member_name(contract: str, used_names: set[str]) -> str:
    clean = unicodedata.normalize("NFKC", contract)
    clean = "".join(
        "_" if unicodedata.category(character) in {"Cc", "Cf"} else character
        for character in clean
    )
    clean = _INVALID_MEMBER_CHARS.sub("_", clean)
    while ".." in clean:
        clean = clean.replace("..", "_")
    clean = clean.strip(" ._") or "contract"
    leaf = _member_leaf(clean)
    collision_key = _member_collision_key(leaf)
    if collision_key in used_names:
        digest = hashlib.sha256(contract.encode("utf-8")).hexdigest()[:10]
        leaf = _member_leaf(clean, f"_{digest}")
        collision_key = _member_collision_key(leaf)
        suffix = 2
        while collision_key in used_names:
            leaf = _member_leaf(clean, f"_{digest}_{suffix}")
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
    resource_limits_preflighted: bool = False,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
) -> SpooledTemporaryFile:
    """构建一本项目工作簿；返回的流归调用方所有。

    单本入口必须在 ORM 物化前独立预检。批量入口已对整批一次性预检，可显式复用
    该结论，避免每本重复执行聚合查询。
    """
    if (date_from is None) != (date_to is None):
        raise WorkbookExportRejected("date_from 与 date_to 必须同时提供")
    if date_from is not None and date_to is not None and date_from > date_to:
        raise WorkbookExportRejected("date_from 不能晚于 date_to")
    if not contract or not contract.strip():
        raise WorkbookExportNotFound("合同不存在：空白合同号")
    if not resource_limits_preflighted:
        _acquire_shared_source_lock(db)
        contract_exists = bool(
            db.scalar(
                select(func.count(FMaintenanceOrder.id)).where(
                    FMaintenanceOrder.linked_sales_order_no == contract,
                )
            )
        )
        if not contract_exists:
            raise WorkbookExportNotFound(f"合同不存在：{contract}")
        selected_orders = int(
            db.scalar(
                select(func.count(FMaintenanceOrder.id)).where(
                    FMaintenanceOrder.linked_sales_order_no == contract,
                    *_selection_filters(date_from, date_to),
                )
            )
            or 0
        )
        if selected_orders == 0:
            historical_orders = int(
                db.scalar(
                    select(func.count(FMaintenanceOrder.id)).where(
                        FMaintenanceOrder.linked_sales_order_no == contract,
                        *_requested_scope_filters(date_from, date_to),
                    )
                )
                or 0
            )
            raise WorkbookExportRejected(
                (
                    "合同只有项目成本起算日前数据，不能生成误导性空账"
                    if historical_orders
                    else "合同存在，但所选范围内没有可导出的维保数据"
                ),
            )
        _preflight_resource_limits(
            db,
            [_ContractMatch(contract, 0, None, None)],
            date_from=date_from,
            date_to=date_to,
        )
    output = SpooledTemporaryFile(max_size=5 * 1024 * 1024, mode="w+b")
    workbook = None
    try:
        # 无日期时保留既有两参数调用契约，避免内部扩展点和运维脚本因新增可选参数失效。
        # 只有显式范围导出才传日期并启用闭区间内容过滤。
        if date_from is None and date_to is None:
            data = maintenance_cost.contract_workbook_data(db, contract)
        else:
            data = maintenance_cost.contract_workbook_data(
                db,
                contract,
                date_from=date_from,
                date_to=date_to,
            )
        workbook = Workbook()
        workbook = maintenance_workbook_renderer.render_contract_workbook(
            contract,
            data,
            safe_xlsx_text,
            workbook=workbook,
        )
        workbook.save(_SizeLimitedFile(
            output,
            MAX_WORKBOOK_BYTES,
            "单本项目工作簿超过 256 MiB 上限",
        ))
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
    """按命中维保单选择合同，并把对应时间范围内容打入一个 ZIP。"""
    if (date_from is None) != (date_to is None):
        raise WorkbookExportRejected("date_from 与 date_to 必须同时提供")
    if date_from is not None and date_to is not None and date_from > date_to:
        raise WorkbookExportRejected("date_from 不能晚于 date_to")
    _acquire_shared_source_lock(db)
    selected_orders = db.scalar(
        select(func.count(FMaintenanceOrder.id)).where(
            *_selection_filters(date_from, date_to),
        )
    ) or 0
    if selected_orders == 0:
        historical_orders = int(
            db.scalar(
                select(func.count(FMaintenanceOrder.id)).where(
                    *_requested_scope_filters(date_from, date_to),
                )
            )
            or 0
        )
        raise WorkbookExportRejected(
            (
                "所选范围只有项目成本起算日前数据，不能生成误导性空账"
                if historical_orders
                else "所选范围内没有已生效维保订单"
            ),
        )
    if selected_orders > MAX_SELECTED_ORDERS:
        raise WorkbookExportTooLarge(
            f"命中维保订单超过批量上限 {MAX_SELECTED_ORDERS} 条",
        )
    matches = _contract_matches(db, date_from, date_to)
    if not matches:
        raise WorkbookExportRejected("所选范围内的已生效维保订单均未关联合同")
    if len(matches) > MAX_WORKBOOKS:
        raise WorkbookExportTooLarge(f"命中合同超过批量上限 {MAX_WORKBOOKS} 本")
    _preflight_resource_limits(
        db,
        matches,
        date_from=date_from,
        date_to=date_to,
    )
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
                if date_from is None:
                    # Preserve the original call shape for the all-time export
                    # path so existing wrappers and instrumentation remain
                    # compatible. Date bounds are only meaningful when a
                    # range was explicitly selected.
                    workbook = build_contract_workbook_file(
                        db,
                        match.contract,
                        True,
                    )
                else:
                    workbook = build_contract_workbook_file(
                        db,
                        match.contract,
                        True,
                        date_from=date_from,
                        date_to=date_to,
                    )
                try:
                    with archive.open(member_name, mode="w") as member:
                        shutil.copyfileobj(workbook, member, length=1024 * 1024)
                finally:
                    workbook.close()
        output.seek(0, 2)
        if output.tell() > MAX_ZIP_BYTES:
            raise WorkbookExportTooLarge("批量工作簿 ZIP 超过 512 MiB 上限")
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

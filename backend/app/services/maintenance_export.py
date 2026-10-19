"""维保订单 XLSX 导出。"""
from contextlib import suppress
from datetime import date
import re
import threading
from tempfile import SpooledTemporaryFile, TemporaryDirectory

from openpyxl.utils.datetime import to_excel
from xlsxwriter import Workbook
from xlsxwriter.worksheet import Worksheet
from sqlalchemy import and_, func, select, text
from sqlalchemy.orm import Bundle, Session

from app import config
from app.models.maintenance import (
    FMaintenanceLine,
    FMaintenanceOrder,
    MaintenanceManualCostOverride,
)
from app.security import UserContext, is_field_hidden, prepare_field_visibility
from app.services import maintenance_cost_quality, maintenance_demands
from app.services.maintenance_export_ordering import wbdd_export_order_by


ORDER_HEADERS = (
    "数据库ID", "原始订单ID", "维保单号", "制单日期", "销售订单", "项目名", "终端客户",
    "需求类型", "业务类型", "销售人员", "出库仓库", "维保开始日期", "维保终止日期", "流程状态",
)
LINE_HEADERS = (
    "数据库ID", "原始明细ID", "订单数据库ID", "原始订单ID", "维保单号", "制单日期", "行号",
    "PN", "原始PN", "产品描述", "需求数量", "退货数量", "发货SN", "单价", "金额",
    "含税单位成本", "未税单位成本", "含税成本金额", "未税成本金额",
    "成本事实层级", "成本来源", "含税口径", "取价月", "追溯月数", "关联采购单", "距采购天数", "置信度",
    "参考侧", "参考池ID", "参考池版本", "参考样本数", "参考起始日", "参考截止日", "最近样本日",
    "异常标记",
)
_INVALID_XML_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")
_COST_ANOMALY_FLAGS = maintenance_cost_quality.COST_DERIVED_ANOMALY_FLAGS
MAX_DATA_ROWS_PER_SHEET = 1_048_575
MAX_DYNAMIC_TEXT_BYTES = 64 * 1024 * 1024
MAX_WORKBOOK_BYTES = 256 * 1024 * 1024
ORDER_RENDERED_TEXT_OVERHEAD_BYTES = 16
LINE_RENDERED_TEXT_OVERHEAD_BYTES = 64
ORDER_EXPORT_ADVISORY_LOCK_KEY = 0x4D4F_584C
_ORDER_EXPORT_LOCK = threading.Lock()


class ExcelCellTooLong(ValueError):
    pass


class ExcelRowLimitExceeded(ValueError):
    pass


class ExcelExportTooLarge(ValueError):
    pass


class ExcelExportEmpty(ValueError):
    pass


class ExcelExportBusy(RuntimeError):
    pass


class _SizeLimitedFile:
    """给 XLSX/ZipFile 提供可 seek 的硬上限，禁止先写爆临时盘再拒绝。"""

    def __init__(self, raw, max_size: int):
        self._raw = raw
        self._max_size = max_size
        self._extent = raw.tell()
        self._aborted = False

    def abort(self):
        # XlsxWriter 的异常回溯可能暂时持有 ZipFile；其延后析构不得继续写入
        # 已失败/关闭的下载文件，也不得再次抛异常掩盖原始限额错误。
        self._aborted = True

    def write(self, data):
        if self._aborted:
            return len(data)
        end = self._raw.tell() + len(data)
        if max(self._extent, end) > self._max_size:
            raise ExcelExportTooLarge("维保订单 XLSX 超过 256 MiB 上限")
        written = self._raw.write(data)
        self._extent = max(self._extent, self._raw.tell())
        return written

    def seek(self, *args):
        if self._aborted:
            return 0
        return self._raw.seek(*args)

    def tell(self):
        if self._aborted:
            return 0
        return self._raw.tell()

    def flush(self):
        if self._aborted:
            return None
        return self._raw.flush()


def _save_workbook(workbook: Workbook, output) -> None:
    """XLSX 压缩输出仍经硬上限文件；失败后隔离 ZIP 的延后析构写入。"""
    try:
        workbook.close()
    except BaseException:
        workbook.filename.abort()
        raise


def _safe_row(values: tuple) -> tuple:
    safe = []
    for value in values:
        if isinstance(value, str):
            value = _INVALID_XML_CONTROL.sub("", value)
            if value[:1] in ("=", "+", "-", "@", "\t", "\r", "\n"):
                value = "'" + value
            if len(value) > 32767:
                raise ExcelCellTooLong("文本超过 Excel 单元格上限 32767 个字符")
        safe.append(value)
    return tuple(safe)


class _ExportWorksheet(Worksheet):
    """保持旧 XLSX 的普通文本语义；本出口不写富文本或 XML 转义占位符。"""

    @staticmethod
    def _escape_control_characters(data):
        # _safe_row 已移除 XML 非法字符。保留合法 CR 和字面 _xHHHH_，
        # 避免 inlineStr 被常用读取器读成 Excel 专有转义字符串。
        return data

    def _escape_data(self, data):
        escaped = super()._escape_data(data)
        return escaped.replace("\r", "&#13;") if isinstance(escaped, str) else escaped

    def _xml_rich_inline_string(self, string, attributes=()):
        # XlsxWriter 会把形如 <r>...</r> 的字符串识别为富文本；源数据仍须按
        # 普通文本转义，不能把看起来像 XML 的描述当成工作簿结构。
        self._xml_inline_string(string, False, attributes)


class _SheetWriter:
    """逐行写入常量内存 worksheet，格式对象只创建一次。"""

    def __init__(self, workbook: Workbook, title: str, formats: dict[int, str]):
        self._sheet = workbook.add_worksheet(title, worksheet_class=_ExportWorksheet)
        self._formats = {
            index: workbook.add_format({"num_format": number_format})
            for index, number_format in formats.items()
        }
        self._row = 0

    def append(self, values: tuple) -> None:
        for column, value in enumerate(_safe_row(values)):
            cell_format = self._formats.get(column) if self._row else None
            # 明确写出带格式的空数值/日期格，保留既有下载件的格式契约。
            # 字段类型已知，直接使用公开类型 API，避免逐格通用分派。
            if value is None or value == "":
                if cell_format is None:
                    continue
                result = self._sheet.write_blank(self._row, column, value, cell_format)
            elif isinstance(value, date):
                # 沿用旧下载件的 Excel 日期序号，含 1900 年闰日兼容边界。
                result = self._sheet.write_number(
                    self._row, column, to_excel(value), cell_format,
                )
            elif isinstance(value, str):
                result = self._sheet.write_string(self._row, column, value, cell_format)
            elif isinstance(value, bool):
                result = self._sheet.write_boolean(self._row, column, value, cell_format)
            else:
                result = self._sheet.write_number(self._row, column, value, cell_format)
            if result == -2:
                raise ExcelCellTooLong("文本超过 Excel 单元格上限 32767 个字符")
            if result == -1:
                raise ExcelRowLimitExceeded("订单明细超过 Excel 单 Sheet 数据行上限 1048575")
        self._row += 1


def _cleanup_workbook(workbook: Workbook) -> None:
    # 成功 close 会关这些句柄；行写入/ZIP 压缩中途失败也必须关闭。
    # 所有 XML 文件均在本次独享 TemporaryDirectory，随后统一删除。
    for worksheet in workbook.worksheets():
        for attribute in ("row_data_fh", "fh"):
            handle = getattr(worksheet, attribute, None)
            if handle is not None:
                with suppress(Exception):
                    handle.close()


def _date_filters(date_from: date | None, date_to: date | None) -> tuple:
    filters = (
        FMaintenanceOrder.data_status == config.ACTIVE_STATUS,
        maintenance_demands.active_demand_condition(),
    )
    if date_from is not None and date_to is not None:
        filters += (
            FMaintenanceOrder.order_date >= date_from,
            FMaintenanceOrder.order_date <= date_to,
        )
    return filters


def _octet_length(value):
    """在 PostgreSQL 端统计即将进入 XLSX 的动态文本 UTF-8 字节。"""
    return func.octet_length(func.coalesce(value, ""))


def _preflight_row_limits(
    db: Session,
    date_from: date | None,
    date_to: date | None,
) -> None:
    filters = _date_filters(date_from, date_to)
    rendered_project = func.coalesce(
        func.nullif(FMaintenanceOrder.project_raw, ""),
        FMaintenanceOrder.project_std,
        "",
    )
    order_text_bytes = (
        _octet_length(FMaintenanceOrder.raw_order_id)
        + _octet_length(FMaintenanceOrder.order_no)
        + _octet_length(FMaintenanceOrder.linked_sales_order_no)
        + _octet_length(rendered_project)
        + _octet_length(FMaintenanceOrder.end_customer)
        + _octet_length(FMaintenanceOrder.demand_type)
        + _octet_length(FMaintenanceOrder.business_type)
        + _octet_length(FMaintenanceOrder.salesperson)
        + _octet_length(FMaintenanceOrder.warehouse)
        + _octet_length(FMaintenanceOrder.data_status)
        + ORDER_RENDERED_TEXT_OVERHEAD_BYTES
    )
    order_count, order_dynamic_text_bytes = db.execute(
        select(
            func.count(FMaintenanceOrder.id),
            func.coalesce(func.sum(order_text_bytes), 0),
        ).where(*filters)
    ).one()
    if order_count == 0:
        raise ExcelExportEmpty("所选范围内没有可导出的维保订单")
    if order_count > MAX_DATA_ROWS_PER_SHEET:
        raise ExcelRowLimitExceeded("维保订单超过 Excel 单 Sheet 数据行上限 1048575")

    line_text_bytes = (
        _octet_length(FMaintenanceLine.raw_line_id)
        + _octet_length(FMaintenanceOrder.raw_order_id)
        + _octet_length(FMaintenanceOrder.order_no)
        + _octet_length(FMaintenanceLine.pn_std)
        + _octet_length(FMaintenanceLine.pn_raw)
        + _octet_length(FMaintenanceLine.description)
        + _octet_length(FMaintenanceLine.serial_numbers)
        + _octet_length(FMaintenanceLine.cost_source)
        + _octet_length(FMaintenanceLine.cost_tax_basis)
        + _octet_length(FMaintenanceLine.price_month)
        + _octet_length(FMaintenanceLine.linked_purchase_order_no)
        + _octet_length(FMaintenanceLine.confidence)
        + _octet_length(FMaintenanceLine.reference_side)
        + _octet_length(func.array_to_string(FMaintenanceLine.anomaly_flags, "、"))
        + LINE_RENDERED_TEXT_OVERHEAD_BYTES
    )
    line_count, line_dynamic_text_bytes = db.execute(
        select(
            func.count(FMaintenanceLine.id),
            func.coalesce(func.sum(line_text_bytes), 0),
        )
        .join(FMaintenanceOrder, FMaintenanceOrder.id == FMaintenanceLine.order_id)
        .where(*filters)
    ).one()
    if line_count > MAX_DATA_ROWS_PER_SHEET:
        raise ExcelRowLimitExceeded("订单明细超过 Excel 单 Sheet 数据行上限 1048575")
    if (
        int(order_dynamic_text_bytes) + int(line_dynamic_text_bytes)
        > MAX_DYNAMIC_TEXT_BYTES
    ):
        raise ExcelExportTooLarge("维保订单 XLSX 动态文本超过 64 MiB 上限")


def _export_statement(date_from: date | None, date_to: date | None):
    """只传输工作簿需要的列；Bundle 返回轻量 Row，不建立宽表 ORM 实体。"""
    order = Bundle("order", *(
        getattr(FMaintenanceOrder, field) for field in (
            "id", "raw_order_id", "order_no", "order_date", "linked_sales_order_no",
            "project_raw", "project_std", "end_customer", "demand_type", "business_type",
            "salesperson", "warehouse", "maint_start", "maint_end", "data_status",
        )
    ))
    line = Bundle("line", *(
        getattr(FMaintenanceLine, field) for field in (
            "id", "raw_line_id", "line_no", "pn_std", "pn_raw", "description",
            "qty", "return_qty", "serial_numbers", "unit_cost", "cost_amount",
            "unit_cost_inc_tax", "unit_cost_ex_tax", "cost_amount_inc_tax",
            "cost_amount_ex_tax", "cost_source", "cost_tax_basis", "price_month",
            "trace_months", "linked_purchase_order_no", "price_distance_days",
            "confidence", "reference_side", "reference_pool_group_id",
            "reference_pool_version", "reference_sample_count", "reference_from_date",
            "reference_to_date", "reference_latest_date", "anomaly_flags",
        )
    ))
    override = Bundle(
        "override", MaintenanceManualCostOverride.active,
        MaintenanceManualCostOverride.unit_cost_inc_tax,
        MaintenanceManualCostOverride.unit_cost_ex_tax,
    )
    return (
        select(order, line, override)
        .select_from(FMaintenanceOrder)
        .outerjoin(FMaintenanceLine, FMaintenanceLine.order_id == FMaintenanceOrder.id)
        .outerjoin(
            MaintenanceManualCostOverride,
            and_(
                MaintenanceManualCostOverride.line_id == FMaintenanceLine.id,
                MaintenanceManualCostOverride.active.is_(True),
            ),
        )
        .where(*_date_filters(date_from, date_to))
        .order_by(*wbdd_export_order_by())
        .execution_options(stream_results=True, yield_per=1000)
    )


def _build_workbook(
    db: Session,
    user_ctx: UserContext,
    date_from: date | None = None,
    date_to: date | None = None,
) -> SpooledTemporaryFile:
    _preflight_row_limits(db, date_from, date_to)
    mask_fields = prepare_field_visibility(user_ctx)
    hide_cost_anomalies = is_field_hidden(user_ctx, "unit_cost")
    workbook = None
    output = None
    temporary = None
    completed = False
    try:
        temporary = TemporaryDirectory(prefix="itsp-order-export-")
        output = SpooledTemporaryFile(max_size=5 * 1024 * 1024, mode="w+b")
        workbook = Workbook(_SizeLimitedFile(output, MAX_WORKBOOK_BYTES), {
            "constant_memory": True,
            "tmpdir": temporary.name,
            "strings_to_formulas": False,
            "strings_to_urls": False,
            "use_zip64": True,
        })
        orders = _SheetWriter(workbook, "维保订单", {
            3: "yyyy-mm-dd", 11: "yyyy-mm-dd", 12: "yyyy-mm-dd",
        })
        lines = _SheetWriter(workbook, "订单明细", {
            5: "yyyy-mm-dd", 10: "0.00", 11: "0.00",
            13: "#,##0.00", 14: "#,##0.00",
            15: "#,##0.00", 16: "#,##0.00",
            17: "#,##0.00", 18: "#,##0.00",
            31: "yyyy-mm-dd", 32: "yyyy-mm-dd", 33: "yyyy-mm-dd",
        })
        orders.append(ORDER_HEADERS)
        lines.append(LINE_HEADERS)

        statement = _export_statement(date_from, date_to)
        previous_order_id = None
        order_count = 0
        line_count = 0
        for order, line, override in db.execute(statement):
            if order.id != previous_order_id:
                order_count += 1
                if order_count > MAX_DATA_ROWS_PER_SHEET:
                    raise ExcelRowLimitExceeded("维保订单超过 Excel 单 Sheet 数据行上限 1048575")
                visible_order = mask_fields({
                    "end_customer": order.end_customer,
                })
                orders.append((
                    order.id, order.raw_order_id, order.order_no, order.order_date,
                    order.linked_sales_order_no, order.project_raw or order.project_std,
                    visible_order["end_customer"], order.demand_type, order.business_type, order.salesperson,
                    order.warehouse, order.maint_start, order.maint_end, order.data_status,
                ))
                previous_order_id = order.id
            if line.id is not None:
                line_count += 1
                if line_count > MAX_DATA_ROWS_PER_SHEET:
                    raise ExcelRowLimitExceeded("订单明细超过 Excel 单 Sheet 数据行上限 1048575")
                cost_fact = maintenance_cost_quality.resolved_line_cost_fields(
                    source=line.cost_source,
                    tax_basis=line.cost_tax_basis,
                    legacy_unit_cost=line.unit_cost,
                    legacy_amount=line.cost_amount,
                    unit_cost_inc_tax=line.unit_cost_inc_tax,
                    unit_cost_ex_tax=line.unit_cost_ex_tax,
                    cost_amount_inc_tax=line.cost_amount_inc_tax,
                    cost_amount_ex_tax=line.cost_amount_ex_tax,
                    anomaly_flags=line.anomaly_flags,
                    confidence=line.confidence,
                    qty=line.qty,
                    return_qty=line.return_qty,
                    manual_unit_cost_inc_tax=override.unit_cost_inc_tax,
                    manual_unit_cost_ex_tax=override.unit_cost_ex_tax,
                    manual_active=override.active is True,
                )
                cost_tier = cost_fact["tier"]
                has_known_cost = cost_tier != "missing"
                manual_fallback = cost_fact["manual_fallback"]
                cost = mask_fields({
                    "unit_cost": cost_fact["unit_cost"] if has_known_cost else None,
                    "cost_amount": cost_fact["cost_amount"] if has_known_cost else None,
                    "unit_cost_inc_tax": (
                        cost_fact["unit_cost_inc_tax"] if has_known_cost else None
                    ),
                    "unit_cost_ex_tax": (
                        cost_fact["unit_cost_ex_tax"] if has_known_cost else None
                    ),
                    "cost_amount_inc_tax": (
                        cost_fact["cost_amount_inc_tax"] if has_known_cost else None
                    ),
                    "cost_amount_ex_tax": (
                        cost_fact["cost_amount_ex_tax"] if has_known_cost else None
                    ),
                    "cost_tier": cost_tier,
                    "cost_source": cost_fact["source"],
                    "cost_tax_basis": cost_fact["tax_basis"],
                    "price_month": None if manual_fallback else line.price_month,
                    "trace_months": None if manual_fallback else line.trace_months,
                    "linked_purchase_order_no": (
                        None if manual_fallback else line.linked_purchase_order_no
                    ),
                    "price_distance_days": (
                        None if manual_fallback else line.price_distance_days
                    ),
                    "confidence": cost_fact["confidence"],
                    "reference_side": None if manual_fallback else line.reference_side,
                    "reference_pool_group_id": (
                        None if manual_fallback else line.reference_pool_group_id
                    ),
                    "reference_pool_version": (
                        None if manual_fallback else line.reference_pool_version
                    ),
                    "reference_sample_count": (
                        None if manual_fallback else line.reference_sample_count
                    ),
                    "reference_from_date": (
                        None if manual_fallback else line.reference_from_date
                    ),
                    "reference_to_date": (
                        None if manual_fallback else line.reference_to_date
                    ),
                    "reference_latest_date": (
                        None if manual_fallback else line.reference_latest_date
                    ),
                })
                anomaly_flags = cost_fact["anomaly_flags"]
                if hide_cost_anomalies:
                    anomaly_flags = [flag for flag in anomaly_flags if flag not in _COST_ANOMALY_FLAGS]
                lines.append((
                    line.id, line.raw_line_id, order.id, order.raw_order_id, order.order_no,
                    order.order_date, line.line_no, line.pn_std, line.pn_raw, line.description,
                    line.qty, line.return_qty, line.serial_numbers, cost["unit_cost"], cost["cost_amount"],
                    cost["unit_cost_inc_tax"], cost["unit_cost_ex_tax"],
                    cost["cost_amount_inc_tax"], cost["cost_amount_ex_tax"],
                    {"actual": "实际采购参考", "estimated": "估算参考", "missing": "成本缺失"}
                    .get(cost["cost_tier"], cost["cost_tier"]),
                    cost["cost_source"], cost["cost_tax_basis"], cost["price_month"], cost["trace_months"],
                    cost["linked_purchase_order_no"], cost["price_distance_days"], cost["confidence"],
                    cost["reference_side"], cost["reference_pool_group_id"],
                    cost["reference_pool_version"], cost["reference_sample_count"],
                    cost["reference_from_date"], cost["reference_to_date"],
                    cost["reference_latest_date"],
                    "、".join(anomaly_flags),
                ))

        _save_workbook(workbook, output)
        output.seek(0)
        completed = True
        return output
    finally:
        if workbook is not None:
            _cleanup_workbook(workbook)
        if temporary is not None:
            temporary.cleanup()
        if not completed and output is not None:
            output.close()


def _acquire_shared_source_lock(db: Session) -> None:
    """冻结导出事实快照，覆盖资源预检到工作簿完整物化的整个事务。"""
    db.execute(
        text("SELECT pg_advisory_xact_lock_shared(:k)"),
        {"k": config.DATA_CHANGE_ADVISORY_LOCK_KEY},
    )


def build_workbook(
    db: Session,
    user_ctx: UserContext,
    date_from: date | None = None,
    date_to: date | None = None,
) -> SpooledTemporaryFile:
    """跨进程非阻塞构建逐单工作簿，避免并发任务叠加临时磁盘和内存。"""
    if not _ORDER_EXPORT_LOCK.acquire(blocking=False):
        raise ExcelExportBusy("已有逐单维保导出正在执行，请稍后重试")
    try:
        acquired = db.scalar(
            text("SELECT pg_try_advisory_xact_lock(:k)"),
            {"k": ORDER_EXPORT_ADVISORY_LOCK_KEY},
        )
        if not acquired:
            raise ExcelExportBusy("已有逐单维保导出正在执行，请稍后重试")
        _acquire_shared_source_lock(db)
        return _build_workbook(db, user_ctx, date_from, date_to)
    finally:
        _ORDER_EXPORT_LOCK.release()

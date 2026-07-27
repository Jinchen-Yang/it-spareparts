"""维保订单 XLSX 导出。"""
from contextlib import suppress
from datetime import date
import re
from tempfile import SpooledTemporaryFile

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.worksheet._writer import ALL_TEMP_FILES
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder
from app.security import UserContext, apply_field_visibility, is_field_hidden


ORDER_HEADERS = (
    "数据库ID", "原始订单ID", "维保单号", "制单日期", "销售订单", "项目名", "终端客户",
    "需求类型", "业务类型", "销售人员", "出库仓库", "维保开始日期", "维保终止日期", "流程状态",
)
LINE_HEADERS = (
    "数据库ID", "原始明细ID", "订单数据库ID", "原始订单ID", "维保单号", "制单日期", "行号",
    "PN", "原始PN", "产品描述", "需求数量", "退货数量", "发货SN", "单价", "金额",
    "成本来源", "含税口径", "取价月", "追溯月数", "关联采购单", "距采购天数", "置信度",
    "异常标记",
)
_INVALID_XML_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")
_COST_ANOMALY_FLAGS = {"no_cost", "cost_overflow"}
MAX_DATA_ROWS_PER_SHEET = 1_048_575


class ExcelCellTooLong(ValueError):
    pass


class ExcelRowLimitExceeded(ValueError):
    pass


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


def _formatted_row(worksheet, values: tuple, formats: dict[int, str]) -> tuple:
    cells = []
    for index, value in enumerate(_safe_row(values)):
        cell = WriteOnlyCell(worksheet, value=value)
        if index in formats:
            cell.number_format = formats[index]
        cells.append(cell)
    return tuple(cells)


def _cleanup_workbook(workbook: Workbook) -> None:
    for worksheet in workbook.worksheets:
        with suppress(Exception):
            if not worksheet.closed:
                worksheet.close()
        writer = getattr(worksheet, "_writer", None)
        if writer is not None and writer.out in ALL_TEMP_FILES:
            with suppress(Exception):
                writer.close()
            with suppress(Exception):
                writer.cleanup()


def build_workbook(
    db: Session,
    user_ctx: UserContext,
    date_from: date | None = None,
    date_to: date | None = None,
) -> SpooledTemporaryFile:
    workbook = Workbook(write_only=True)
    output = None
    completed = False
    try:
        orders = workbook.create_sheet("维保订单")
        lines = workbook.create_sheet("订单明细")
        orders.append(ORDER_HEADERS)
        lines.append(LINE_HEADERS)

        statement = (
            select(FMaintenanceOrder, FMaintenanceLine)
            .outerjoin(FMaintenanceLine, FMaintenanceLine.order_id == FMaintenanceOrder.id)
            .where(FMaintenanceOrder.data_status == config.ACTIVE_STATUS)
            .order_by(FMaintenanceOrder.id, FMaintenanceLine.id)
            .execution_options(stream_results=True, yield_per=1000)
        )
        if date_from is not None and date_to is not None:
            statement = statement.where(
                FMaintenanceOrder.order_date >= date_from,
                FMaintenanceOrder.order_date <= date_to,
            )
        previous_order_id = None
        order_count = 0
        line_count = 0
        for order, line in db.execute(statement):
            if order.id != previous_order_id:
                order_count += 1
                if order_count > MAX_DATA_ROWS_PER_SHEET:
                    raise ExcelRowLimitExceeded("维保订单超过 Excel 单 Sheet 数据行上限 1048575")
                visible_order = apply_field_visibility({
                    "end_customer": order.end_customer,
                }, user_ctx)
                orders.append(_formatted_row(orders, (
                    order.id, order.raw_order_id, order.order_no, order.order_date,
                    order.linked_sales_order_no, order.project_raw or order.project_std,
                    visible_order["end_customer"], order.demand_type, order.business_type, order.salesperson,
                    order.warehouse, order.maint_start, order.maint_end, order.data_status,
                ), {3: "yyyy-mm-dd", 11: "yyyy-mm-dd", 12: "yyyy-mm-dd"}))
                previous_order_id = order.id
            if line is not None:
                line_count += 1
                if line_count > MAX_DATA_ROWS_PER_SHEET:
                    raise ExcelRowLimitExceeded("订单明细超过 Excel 单 Sheet 数据行上限 1048575")
                cost = apply_field_visibility({
                    "unit_cost": line.unit_cost,
                    "cost_amount": line.cost_amount,
                    "cost_source": line.cost_source,
                    "cost_tax_basis": line.cost_tax_basis,
                    "price_month": line.price_month,
                    "trace_months": line.trace_months,
                    "linked_purchase_order_no": line.linked_purchase_order_no,
                    "price_distance_days": line.price_distance_days,
                    "confidence": line.confidence,
                }, user_ctx)
                anomaly_flags = line.anomaly_flags or []
                if is_field_hidden(user_ctx, "unit_cost"):
                    anomaly_flags = [flag for flag in anomaly_flags if flag not in _COST_ANOMALY_FLAGS]
                lines.append(_formatted_row(lines, (
                    line.id, line.raw_line_id, order.id, order.raw_order_id, order.order_no,
                    order.order_date, line.line_no, line.pn_std, line.pn_raw, line.description,
                    line.qty, line.return_qty, line.serial_numbers, cost["unit_cost"], cost["cost_amount"],
                    cost["cost_source"], cost["cost_tax_basis"], cost["price_month"], cost["trace_months"],
                    cost["linked_purchase_order_no"], cost["price_distance_days"], cost["confidence"],
                    "、".join(anomaly_flags),
                ), {
                    5: "yyyy-mm-dd", 10: "0.00", 11: "0.00",
                    13: "#,##0.00", 14: "#,##0.00",
                }))

        output = SpooledTemporaryFile(max_size=5 * 1024 * 1024, mode="w+b")
        workbook.save(output)
        output.seek(0)
        completed = True
        return output
    finally:
        if not completed:
            if output is not None:
                output.close()
            _cleanup_workbook(workbook)
        workbook.close()

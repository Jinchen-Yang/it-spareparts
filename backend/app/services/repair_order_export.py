"""Excel download for repaired quantities with recorded dispositions."""

from io import BytesIO

from sqlalchemy import select
from sqlalchemy.orm import Session
from xlsxwriter import Workbook

from app.models.maintenance_repair_orders import MaintenanceRepairOrder as Order
from app.models.maintenance_repair_orders import MaintenanceRepairOrderLine as Line

_DESTINATIONS = {"company": "主公司", "shop": "电商", "retry": "二次维修"}
_HEADERS = ("维修单号", "任务条", "PN", "描述", "去向", "数量", "标签")
_LABEL = "修好 · 已登记去向"


def export_fixed_dispositions(db: Session) -> bytes:
    """Use disposition quantities so partially arranged results export accurately."""
    query = (
        select(Order.id, Line.position, Line.pn, Line.description, Line.dispositions)
        .join(Line, Line.order_id == Order.id)
        .where(Line.fixed > 0)
        .order_by(Order.created_at.desc(), Order.id.desc(), Line.position)
        .execution_options(yield_per=500)
    )
    output = BytesIO()
    with Workbook(output, {"constant_memory": True}) as workbook:
        title_style = workbook.add_format({"bold": True})

        def add_sheet(page: int):
            sheet = workbook.add_worksheet(f"修好件去向 {page}")
            sheet.write_row(0, 0, _HEADERS, title_style)
            sheet.freeze_panes(1, 0)
            for column, width in enumerate((23, 9, 24, 42, 14, 9, 24)):
                sheet.set_column(column, column, width)
            return sheet

        page = 1
        sheet = add_sheet(page)
        row_number = 1
        for order_id, position, pn, description, dispositions in db.execute(query):
            for entry in dispositions:
                if entry["result"] != "fixed" or entry["qty"] <= 0:
                    continue
                if row_number == 1_048_576:
                    sheet.autofilter(0, 0, row_number - 1, len(_HEADERS) - 1)
                    page += 1
                    sheet = add_sheet(page)
                    row_number = 1
                sheet.write_string(row_number, 0, order_id)
                sheet.write_number(row_number, 1, position + 1)
                sheet.write_string(row_number, 2, pn)
                sheet.write_string(row_number, 3, description)
                sheet.write_string(row_number, 4, _DESTINATIONS[entry["kind"]])
                sheet.write_number(row_number, 5, entry["qty"])
                sheet.write_string(row_number, 6, _LABEL)
                row_number += 1
        sheet.autofilter(0, 0, row_number - 1, len(_HEADERS) - 1)
    return output.getvalue()

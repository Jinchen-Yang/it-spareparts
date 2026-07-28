"""项目成本工作簿渲染器。

本模块只负责把已经查询好的合同工作簿数据渲染成 openpyxl Workbook。
查询、权限、批量选择、临时文件与 ZIP 生命周期由上层导出服务负责。
"""
from decimal import Decimal

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from app import config


SOURCE_LABELS = {
    "direct": "实际·专属采购",
    "window": "实际·±7天最近价",
    "month_avg": "实际·当月均价",
    "trace_avg": "预估·追溯均价",
    "sales_ref": "预估·销售参考",
    "none": "成本缺失",
}
CONFIDENCE_LABELS = {"high": "高", "medium": "中", "low": "低"}
_HDR_FILL = PatternFill("solid", fgColor="35506B")
_HDR_FONT = Font(bold=True, color="FFFFFF", size=11)
_TITLE_FONT = Font(bold=True, size=15)
_SUB_FONT = Font(color="8C8C8C", size=10)
_KV_FILL = PatternFill("solid", fgColor="EFEBE3")
_ALT_FILL = PatternFill("solid", fgColor="F7F4EE")
_DOC_FILL = PatternFill("solid", fgColor="FDF3D7")
_TOTAL_FILL = PatternFill("solid", fgColor="E8E2D6")
_THIN = Side(style="thin", color="D8D2C6")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
_MONEY = "#,##0.00"
_CENTER = Alignment(horizontal="center", vertical="center")
_STATUS_STYLE = {
    "incomplete_cost": ("成本不完整，需补数据", "8C8C8C"),
    "red": ("预算已用完或超预算", "C0524A"),
    "yellow": ("预算余量≤20%", "B8860B"),
    "green": ("预算余量>20%", "3F7A45"),
    "no_budget": ("无预算", "8C8C8C"),
}
_QUALITY_LABELS = {
    "actual_only": "完整：仅实际采购参考",
    "contains_estimate": "完整：含估算参考",
    "incomplete": "成本不完整，需补数据",
}
_TIER_LABELS = {
    "actual": "实际采购参考",
    "estimated": "估算参考",
    "missing": "成本缺失",
}


def _hdr_row(ws, row: int, ncols: int) -> None:
    for column in range(1, ncols + 1):
        cell = ws.cell(row=row, column=column)
        cell.fill, cell.font = _HDR_FILL, _HDR_FONT
        cell.border, cell.alignment = _BORDER, _CENTER


def _col_widths(ws, widths: list[float]) -> None:
    for index, width in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(index)].width = width


def render_contract_workbook(
    contract: str,
    data: dict,
    safe_text,
    *,
    workbook: Workbook | None = None,
) -> Workbook:
    """把完整合同数据渲染成四个 Sheet 的财务工作簿。"""
    owns_workbook = workbook is None
    workbook = workbook or Workbook()
    try:
        return _populate_contract_workbook(contract, data, safe_text, workbook)
    except BaseException:
        if owns_workbook:
            workbook.close()
        raise


def _populate_contract_workbook(
    contract: str,
    data: dict,
    safe_text,
    workbook: Workbook,
) -> Workbook:
    safe_contract = safe_text(contract)
    budget = data["budget"]
    sales_order = data["sales_order"]
    cost_summary = data["cost_summary"]
    decision = data["decision"]
    spent_parts = float(cost_summary["known_cost_total"])
    spent_expenses = float(data["expense_total"])
    spent = float(decision["known_spend_total"])
    remaining = (
        float(decision["remaining"])
        if decision["remaining"] is not None else None
    )
    status = decision["decision_status"]
    status_label, status_color = _STATUS_STYLE[status]
    quality_label = _QUALITY_LABELS[cost_summary["cost_quality"]]

    budget_sheet = workbook.active
    budget_sheet.title = "项目预算"
    budget_sheet.merge_cells("A1:F1")
    budget_sheet["A1"] = safe_text(f"维保项目成本工作簿 · {contract}")
    budget_sheet["A1"].font = _TITLE_FONT
    budget_sheet.row_dimensions[1].height = 26
    budget_sheet.merge_cells("A2:F2")
    budget_sheet["A2"] = (
        "实际、估算、缺失分层同源 · 已知金额为含税/不含税混合原值参考 · "
        "成本缺失时不计算预算余量或红黄绿 · "
        "导出自 IT 备件智能管理系统"
    )
    budget_sheet["A2"].font = _SUB_FONT

    key_values = [
        ("合同（销售订单）", safe_contract, None,
         "成本完整性", quality_label, None),
        ("合同金额（含税参考）",
         float(budget) if budget is not None else "（销售表未找到）",
         _MONEY if budget is not None else None,
         "预算消耗参考状态", status_label, None),
        ("实际采购参考（含税）", float(cost_summary["actual_cost_inc"]), _MONEY,
         "实际参考行", cost_summary["actual_lines"], None),
        ("实际采购参考（不含税）", float(cost_summary["actual_cost_ex"]), _MONEY,
         "税率",
         (
             float(sales_order.tax_rate)
             if sales_order is not None and sales_order.tax_rate is not None
             else "—"
         ), None),
        ("估算参考（含税）", float(cost_summary["estimated_cost_inc"]), _MONEY,
         "估算参考行", cost_summary["estimated_lines"], None),
        ("估算参考（不含税）", float(cost_summary["estimated_cost_ex"]), _MONEY,
         "缺失成本行", cost_summary["missing_cost_lines"], None),
        ("已知备件成本参考（混合原值）", spent_parts, _MONEY,
         "生效报销费用", spent_expenses, _MONEY),
        ("已知支出参考（备件+报销）", spent, _MONEY,
         "剩余预算", remaining if remaining is not None else "—",
         _MONEY if remaining is not None else None),
    ]
    row = 4
    for left_label, left_value, left_format, right_label, right_value, right_format in key_values:
        for label, value, number_format, label_column in (
            (left_label, left_value, left_format, 1),
            (right_label, right_value, right_format, 3),
        ):
            label_cell = budget_sheet.cell(row=row, column=label_column, value=label)
            value_cell = budget_sheet.cell(row=row, column=label_column + 1, value=value)
            label_cell.fill, label_cell.font, label_cell.border = (
                _KV_FILL,
                Font(bold=True),
                _BORDER,
            )
            value_cell.border = _BORDER
            if number_format:
                value_cell.number_format = number_format
            if label == "剩余预算" and remaining is not None:
                value_cell.font = Font(bold=True, color=status_color)
            if label == "预算消耗参考状态":
                value_cell.fill = PatternFill("solid", fgColor=status_color)
                value_cell.font = Font(bold=True, color="FFFFFF")
                value_cell.alignment = _CENTER
        row += 1

    row += 1
    categories = sorted({
        category
        for month in data["monthly"].values()
        for category in month
        if category != "备件消耗"
    })
    columns = ["月份", "已知备件成本参考（混合原值·兼容）", *categories, "当月合计"]
    for index, heading in enumerate(columns, 1):
        budget_sheet.cell(row=row, column=index, value=safe_text(heading))
    _hdr_row(budget_sheet, row, len(columns))
    band = False
    totals = [0.0] * (len(columns) - 2)
    for year_month in sorted(data["monthly"]):
        row += 1
        band = not band
        month = data["monthly"][year_month]
        values = [float(month.get("备件消耗", 0))]
        values.extend(float(month.get(category, 0)) for category in categories)
        for index, value in enumerate(values):
            totals[index] += value
        row_values = [safe_text(year_month), *values, round(sum(values), 2)]
        for index, value in enumerate(row_values, 1):
            cell = budget_sheet.cell(row=row, column=index, value=value)
            cell.border = _BORDER
            if band:
                cell.fill = _ALT_FILL
            if index > 1:
                cell.number_format = _MONEY
    row += 1
    total_row = ["合计", *[round(total, 2) for total in totals], round(sum(totals), 2)]
    for index, value in enumerate(total_row, 1):
        cell = budget_sheet.cell(row=row, column=index, value=value)
        cell.font, cell.fill, cell.border = Font(bold=True), _TOTAL_FILL, _BORDER
        if index > 1:
            cell.number_format = _MONEY
    _col_widths(
        budget_sheet,
        [24, 16] + [14] * (len(categories) + 1),
    )

    parts_sheet = workbook.create_sheet("备件明细-氚云")
    part_headers = [
        "数据标题(WBDD单号)", "制单日期", "销售订单", "项目名", "需求类型",
        "出库仓库", "销售人员", "业务类型", "序号", "需供货产品", "产品描述",
        "需求数量", "已知成本参考", "单价", "合计", "发货SN", "行成本单价",
        "行成本金额", "成本来源", "置信度", "取价月", "距采购天数", "含税口径",
        "成本事实层级",
    ]
    parts_sheet.append(part_headers)
    _hdr_row(parts_sheet, 1, len(part_headers))
    parts_sheet.freeze_panes = "A2"
    previous_order, band = None, False
    for line, order in data["lines"]:
        first = order.order_no != previous_order
        if first:
            band = not band
        previous_order = order.order_no
        document_cost = data["doc_total"].get(order.order_no)
        tier = data["line_cost_tiers"][line.id]
        known_line = tier != "missing"
        parts_sheet.append([
            safe_text(order.order_no),
            order.order_date.isoformat() if order.order_date else None,
            safe_text(order.linked_sales_order_no),
            safe_text(order.project_raw or order.project_std),
            safe_text(order.demand_type),
            safe_text(order.warehouse),
            safe_text(order.salesperson),
            safe_text(order.business_type),
            line.line_no,
            safe_text(line.pn_std),
            safe_text(line.description),
            float(line.qty) if line.qty is not None else None,
            float(document_cost) if first and document_cost is not None else None,
            None,
            None,
            safe_text(line.serial_numbers),
            float(line.unit_cost) if known_line and line.unit_cost is not None else None,
            float(line.cost_amount) if known_line and line.cost_amount is not None else None,
            safe_text(SOURCE_LABELS.get(line.cost_source, line.cost_source)),
            safe_text(CONFIDENCE_LABELS.get(line.confidence, line.confidence or "")),
            safe_text(line.price_month),
            line.price_distance_days,
            safe_text(line.cost_tax_basis),
            safe_text(_TIER_LABELS[tier]),
        ])
        rendered_row = parts_sheet.max_row
        for column in range(1, len(part_headers) + 1):
            cell = parts_sheet.cell(row=rendered_row, column=column)
            cell.border = _BORDER
            if band:
                cell.fill = _ALT_FILL
        for column in (13, 14, 15, 17, 18):
            parts_sheet.cell(row=rendered_row, column=column).number_format = _MONEY
        if parts_sheet.cell(row=rendered_row, column=13).value is not None:
            document_cost_cell = parts_sheet.cell(row=rendered_row, column=13)
            document_cost_cell.fill = _DOC_FILL
            document_cost_cell.font = Font(bold=True)
        if line.confidence == "low":
            parts_sheet.cell(row=rendered_row, column=20).font = Font(
                color="B8860B",
                bold=True,
            )
    parts_sheet.auto_filter.ref = (
        f"A1:{get_column_letter(len(part_headers))}{parts_sheet.max_row}"
    )
    _col_widths(
        parts_sheet,
        [
            20, 11, 16, 26, 10, 12, 9, 10, 6, 20, 36, 9, 13, 9, 9, 18, 11,
            12, 16, 8, 9, 11, 9, 12,
        ],
    )

    expense_sheet = workbook.create_sheet("报销明细")
    expense_sheet.cell(row=1, column=1, value="销售订单").font = Font(bold=True)
    expense_sheet.cell(row=1, column=1).fill = _KV_FILL
    expense_sheet.cell(row=1, column=2, value=safe_contract).font = Font(bold=True)
    for column in (1, 2):
        expense_sheet.cell(row=1, column=column).border = _BORDER
    expense_headers = [
        "报销日期", "报销人员", "报销类别", "费用分类", "支出事由",
        "报销金额", "流程状态", "单号", "序号",
    ]
    amount_column = 6
    for column, heading in enumerate(expense_headers, 1):
        expense_sheet.cell(row=2, column=column, value=heading)
    _hdr_row(expense_sheet, 2, len(expense_headers))
    expense_sheet.freeze_panes = "A3"
    for index, expense in enumerate(data["expenses"]):
        expense_sheet.append([
            expense.expense_date.isoformat() if expense.expense_date else None,
            safe_text(expense.person),
            safe_text(expense.expense_type),
            safe_text(expense.fee_category),
            safe_text(expense.reason),
            float(expense.amount) if expense.amount is not None else None,
            safe_text(expense.data_status),
            safe_text(expense.bxd_no),
            expense.line_no,
        ])
        rendered_row = expense_sheet.max_row
        inactive = expense.data_status != config.MAINT_EXPENSE_ACTIVE_STATUS
        for column in range(1, len(expense_headers) + 1):
            cell = expense_sheet.cell(row=rendered_row, column=column)
            cell.border = _BORDER
            if index % 2:
                cell.fill = _ALT_FILL
            if inactive:
                cell.font = Font(color="A0A0A0")
        expense_sheet.cell(
            row=rendered_row,
            column=amount_column,
        ).number_format = _MONEY
    if data["expenses"]:
        expense_sheet.auto_filter.ref = f"A2:I{expense_sheet.max_row}"
        total_row_index = expense_sheet.max_row + 1
        expense_sheet.cell(
            row=total_row_index,
            column=amount_column - 1,
            value="合计（仅已结束）",
        ).font = Font(bold=True)
        total_cell = expense_sheet.cell(
            row=total_row_index,
            column=amount_column,
            value=spent_expenses,
        )
        total_cell.font, total_cell.fill = Font(bold=True), _TOTAL_FILL
        total_cell.number_format = _MONEY
    _col_widths(expense_sheet, [12, 10, 12, 14, 42, 13, 10, 18, 6])

    instructions_sheet = workbook.create_sheet("填写说明")
    instructions_sheet.sheet_view.showGridLines = False
    notes = [
        (
            "这本工作簿怎么用",
            "这是系统导出的「项目追踪工作簿」：报销明细页由你续填，其余页由系统生成。"
            "填好后把整本工作簿拖回系统「数据导入」页——系统只吃报销明细页，其它页自动跳过。",
        ),
        (
            "报销明细页",
            "必填仅两列：报销日期、报销金额，且**每行都要填日期**（不允许留空表示"
            "「同上」——有金额没日期的行会报错打回）。行内没有销售订单列时，按第 1 行锚"
            "（销售订单=本合同）归集；流程状态留空视为「已结束」（计入项目已花）；"
            "单号/序号选填，有则参与防重（同一文件内单号+序号不能重复）。",
        ),
        (
            "导入模式",
            "「跳过」=增量，只进新行；「修复」=以本表为准——本合同在系统里的报销行"
            "会被本表整体替换（改了金额/删了行都以表为准），因此修复模式要求本页没有任何"
            "错误行，有错先修再导。",
        ),
        (
            "备件明细页",
            "系统按取价瀑布自动回填（产品成本=单据级总额填首行，行级取价在附加列），"
            "此页导入时忽略——备件出库数据请一直用氚云「维保需求单」导出上传，样式不变。",
        ),
        (
            "空白表单",
            "新项目可直接导出本工作簿当作空白表单分发：报销页只有表头和锚行，填完传回即可。",
        ),
    ]
    instructions_sheet.column_dimensions["A"].width = 16
    instructions_sheet.column_dimensions["B"].width = 96
    title = instructions_sheet.cell(row=1, column=1, value="项目追踪工作簿 · 填写说明")
    title.font = _TITLE_FONT
    for row, (heading, text) in enumerate(notes, 3):
        heading_cell = instructions_sheet.cell(row=row, column=1, value=heading)
        heading_cell.font = Font(bold=True)
        heading_cell.alignment = Alignment(vertical="top")
        text_cell = instructions_sheet.cell(row=row, column=2, value=text)
        text_cell.alignment = Alignment(wrap_text=True, vertical="top")
        instructions_sheet.row_dimensions[row].height = 42
    return workbook

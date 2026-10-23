"""Workbook projections of already loaded rows; no query or write ownership.

The caller loads current objects. Display projections and identity projections
stay distinct so a readable issue alias cannot change replay or deletion guards.
"""
from __future__ import annotations

from decimal import Decimal


def _v2_part_row_values(line, order, override) -> dict[str, object]:
    """03 一行的全部展示值（按表头名）。build/parse 共用，保证哈希口径一致。"""
    has_cost = line.unit_cost_ex_tax is not None
    return {
        "操作": "",
        "维保单号": order.order_no or "",
        "制单日期": order.order_date,
        "XSDD": order.linked_sales_order_no or "",
        "需求类型": order.demand_type or "",
        "仓库": order.warehouse or "",
        "销售人员": order.salesperson or "",
        "业务类型": order.business_type or "",
        "PN": line.pn_std or line.pn_raw or "",
        "描述": line.description or "",
        "需求数量": line.qty,
        "SN": line.serial_numbers or "",
        "退货数量": line.return_qty,
        "成本来源": line.cost_source or "",
        "置信度": line.confidence or "none",
        "系统未税单位成本": line.unit_cost_ex_tax,
        "系统含税单位成本": line.unit_cost_inc_tax,
        "人工未税单位成本": override.unit_cost_ex_tax if override else None,
        "人工成本原因": override.reason if override else None,
        "成本缺失类型": (
            "out_of_scope" if line.cost_source is None and not has_cost
            else ("none" if not has_cost else "")
        ),
        "可补价": "是" if line.cost_source in (None, "none") and not has_cost else "否",
        "实体ID": line.id,
        "备件主键": line.part_id,
        "只读哈希": "",  # 占位，下方按锁定列回填
        "备注": line.line_note or "",
        # 三来源口径（v1.36）：workbook_manual→工作簿；page_manual→页面人工登记
        # （新建手工需求行，不得谎报 WBDD）；其余（wbdd）→ WBDD 原口径。
        "来源": (
            "工作簿" if line.edited_source == "workbook_manual"
            else "页面人工登记" if line.edited_source == "page_manual"
            else "WBDD"
        ),
    }


def _v2_expense_row_values(expense, project_display: str) -> dict[str, object]:
    """04 一行的展示值（按表头名）。build/parse 共用，保证基线口径一致。"""
    return {
        "操作": "",
        "费用单号": expense.bxd_no or "",
        "明细序号": expense.line_no,
        "报销日期": expense.expense_date,
        "报销人员": expense.person or "",
        "报销类别": expense.expense_type or "",
        "费用分类": expense.fee_category or "",
        "支出事由": expense.reason or "",
        "维保销售订单（归集键）": expense.linked_sales_order_no or "",
        "项目名称": project_display,
        "销售人员": "—",
        "流程状态": expense.data_status or "",
        "原始报销金额": expense.amount,
        "金额口径": expense.tax_basis or "ex",
        "未税金额": expense.amount_ex_tax,
        "含税金额（系统）": expense.amount_inc_tax,
        "备注": expense.remark or "",
        "实体ID": expense.raw_line_id,
    }


def _v2_receipt_row_values(snapshot, contract_no: str) -> dict[str, object]:
    """05 一行的展示值（按表头名）。build/parse 共用。"""
    month = snapshot.report_month
    month_text = (
        month.isoformat()[:7] if hasattr(month, "isoformat") else str(month or "")
    )
    return {
        "合同编号": contract_no,
        "报告月份": month_text,
        "累计实收金额（含税）": snapshot.cumulative_amount,
        "状态": snapshot.status,
        "回款凭证号": snapshot.receipt_reference or "",
        "备注": snapshot.remark or "",
        "实体ID": snapshot.collection_id,
    }


def _v2_site_row_values(line, issue, details=None) -> dict[str, object]:
    """06 一行的展示值（按表头名）。build/parse 共用。"""
    requirement = (details or {}).get("return_requirement")
    state = requirement["requirement_status"] if requirement else (
        "exempt" if line.no_return is True else "required" if line.no_return is False else "pending_category")
    return {
        "领用单号": issue.display_no_effective,
        "领用日期": issue.issue_date,
        "PN": line.pn,
        "SN": line.serial_number or "",
        "领用数量": line.quantity,
        "是否应返还": (
            "" if line.no_return is None else ("否" if line.no_return else "是")
        ),
        "备注": line.remark or "",
        "应返数量": (Decimal(requirement["required_quantity"]) if requirement and state != "pending_category"
                      else line.quantity if state == "required" else 0 if state == "exempt" else "—"),
        "返还状态": {"required": "应返；收货见项目返还台账", "exempt": "免返；收货记录保留",
                     "pending_category": "按规则判断（品类待确认）"}[state],
        "返还单号": "按项目/需求单汇总，不逐行分配",
        "实体ID": line.issue_line_id,
        "关联需求单号": line.demand_order_no or "",
        "领用地址": issue.site_location or "",
        "地址操作": "",
    }


def _v2_site_identity_values(line, issue, details=None) -> dict[str, object]:
    """06 一行的**身份口径**值：领用单号恒取 issue_no（hash 身份）。

    决策 0006：导出单元格展示可读号，但 replay 比对、99_元数据 base_hashes
    与删行守卫必须钉在不可变身份上——否则旧版（hash 号）导出的在途文件一上传
    就会因展示列变化被误判「服务端该行已变」。display_no 分配后永不变更，
    身份口径下新旧文件同一把尺子。
    """
    values = _v2_site_row_values(line, issue, details)
    values["领用单号"] = issue.issue_no
    return values

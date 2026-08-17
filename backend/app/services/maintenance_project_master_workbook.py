"""项目总表（表 6 六 sheet）与全项目备件行级表（页面重设计 R2）。

页面定稿的核心原则（REQUIREMENTS #40）：**一张项目总表改所有数据**——缺价补录、
报销、回款全部走「下载 Excel → 改 → 上传覆盖」，系统内不再手工散改。
下载/上传共 3 处，**在哪下载就在哪上传**（#38）：

1. 主页全局：全项目备件行级表（本模块 `build_global_lines` / `apply_global_lines`）；
2. 项目面板：本项目总表六 sheet（`build_project_master` / `apply_project_master`）；
3. 各 tab：单 sheet（同两个函数，传 `sheets=` 子集）。

本模块**扩展** AB-3 的 `maintenance_expense_collection_workbook`，不另起炉灶：
`04_报销订单` / `05_项目经理回款单` 的解析与写库直接复用那边已验收的实现，
本模块只新增 `01/02`（只读）、`03_备件订单`（缺价补录）、`06_现场领用与返还`
（行级不返还标记）。

零迁移：`03` 的未税单位成本落 `maintenance_manual_cost_override`（该表的既有用途
就是「自动取价瀑布仍缺失时的人工成本证据」，line_id 唯一，天然可覆盖）；
`06` 的不返还标记落 `maintenance_site_issue_line.no_return`。
"""
from __future__ import annotations

import io
import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from uuid import uuid4

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.business_time import business_today
from app.models.maintenance import (
    FMaintenanceLine,
    FMaintenanceOrder,
    FProjectExpense,
    MaintenanceManualCostOverride,
)
from app.models.maintenance_project import (
    MaintenanceProject,
    MaintenanceProjectContract,
)
from app.models.maintenance_project_operations import (
    MaintenanceCollectionSnapshot,
    MaintenanceSiteIssue,
    MaintenanceSiteIssueLine,
)
from app.models.maintenance_manager import MaintenanceCollectionMilestone
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.services import maintenance_expense_collection_workbook as ec
from app.services.maintenance_collection_milestones import write_collection_milestone
from app.services.maintenance_expense_collection_workbook import (
    TAX_RATE,
    WorkbookError,
)

PROTOCOL_VERSION = "project-master-v1"
V2_PROTOCOL_ID = "ITDATA_MAINT_PROJECT_MASTER/2.0"
V2_TEMPLATE_VERSION = "2.0.0"

SHEET_BASICS = "01_项目基础信息"
SHEET_OVERVIEW = "02_概览数据"
SHEET_PARTS = "03_备件订单"
SHEET_EXPENSE = ec.SHEET_EXPENSE          # 04_报销订单（AB-3 已实现）
SHEET_COLLECTION = ec.SHEET_COLLECTION    # 05_项目经理回款单（AB-3 已实现）
SHEET_SITE = "06_现场领用与返还"
_META_SHEET = "99_元数据"

ALL_SHEETS = (SHEET_BASICS, SHEET_OVERVIEW, SHEET_PARTS, SHEET_EXPENSE,
              SHEET_COLLECTION, SHEET_SITE)
# 可回填的四张：01/02 是只读呈现（表 6 规格明写「全只读」）
EDITABLE_SHEETS = (SHEET_PARTS, SHEET_EXPENSE, SHEET_COLLECTION, SHEET_SITE)

_EDITABLE = PatternFill("solid", fgColor="FFF3C4")
_READONLY = PatternFill("solid", fgColor="EFEFEF")

# 全项目备件行级表（主页全局下载）
GLOBAL_SHEET = "备件行级表"
_RANGE_PRESETS = ("today", "yesterday", "this_week", "this_month", "custom")

_PARTS_HEADERS = ["维保单号", "制单日期", "合同编号", "项目名称", "PN", "产品描述",
                  "需求数量", "退货数量", "发货SN", "出库仓库", "成本来源(系统)",
                  "未税单位成本", "含税单位成本(系统计算)", "变更原因"]
_SITE_HEADERS = ["现场领用单号", "领用日期", "PN", "备件SN", "领用数量",
                 "是否应返还(行级)", "备注"]
_GLOBAL_HEADERS = ["项目名称", "合同编号", "维保单号", "制单日期", "PN", "产品描述",
                   "需求数量", "退货数量", "成本来源(系统)", "未税单位成本",
                   "含税单位成本(系统计算)", "变更原因"]

V2_SHEET_OVERVIEW = "01_项目概览"
V2_SHEET_PLAN = "02_回款计划"
V2_SHEET_PARTS = "03_备件明细"
V2_SHEET_EXPENSE = "04_费用报销"
V2_SHEET_RECEIPTS = "05_实收回款"
V2_SHEET_SITE = "06_领用返还"
V2_SHEET_DICTIONARY = "98_字段说明"
V2_SHEET_META = "99_元数据"
V2_ALL_SHEETS = (V2_SHEET_OVERVIEW, V2_SHEET_PLAN, V2_SHEET_PARTS,
                 V2_SHEET_EXPENSE, V2_SHEET_RECEIPTS, V2_SHEET_SITE,
                 V2_SHEET_DICTIONARY, V2_SHEET_META)
V2_EDITABLE = PatternFill("solid", fgColor="FFE699")
V2_READONLY = PatternFill("solid", fgColor="D9E2F3")
V2_HEADER = PatternFill("solid", fgColor="1F4E78")
V2_TITLE = PatternFill("solid", fgColor="17365D")
V2_PART_HEADERS = [
    "维保单号", "制单日期", "XSDD", "需求类型", "仓库", "销售人员", "业务类型",
    "PN", "描述", "需求数量", "SN", "退货数量", "成本来源", "置信度",
    "系统未税单位成本", "系统含税单位成本", "人工未税单位成本", "人工成本原因",
    "成本缺失类型", "可补价", "实体ID", "备件主键", "只读哈希",
    "备注", "来源",
]
V2_PLAN_HEADERS = ["操作", "合同编号", "期次", "计划回款日期", "日期精度", "计划回款金额（含税）",
                   "累计计划金额", "最新累计实收", "到款状态", "提醒状态", "负责人", "备注", "实体ID", "基础版本"]
V2_EXPENSE_HEADERS = ["费用单号", "明细序号", "报销日期", "报销人员", "报销类别", "费用分类",
                      "支出事由", "维保销售订单（归集键）", "项目名称", "销售人员", "流程状态",
                      "原始报销金额", "金额口径", "未税金额", "含税金额（系统）", "备注", "实体ID"]
V2_RECEIPT_HEADERS = ["合同编号", "报告月份", "累计实收金额（含税）", "状态", "回款凭证号", "备注", "实体ID"]
V2_SITE_HEADERS = ["领用单号", "领用日期", "PN", "SN", "领用数量", "是否应返还", "备注",
                   "应返数量", "返还状态", "返还单号", "实体ID"]


# ------------------------------------------------------------------ 计划

@dataclass(frozen=True)
class CostRefill:
    line_id: int
    unit_cost_ex_tax: Decimal
    unit_cost_inc_tax: Decimal
    reason: str | None


@dataclass(frozen=True)
class SiteReturnFlag:
    issue_line_id: str
    no_return: bool | None
    # 2026-08-17 全面放开：可回传覆盖的领用事实
    issue_no: str | None = None
    issue_date: date | None = None
    pn: str | None = None
    serial_number: str | None = None
    quantity: Decimal | None = None
    remark: str | None = None


@dataclass(frozen=True)
class MasterPlan:
    project_id: str | None
    cost_refills: tuple[CostRefill, ...] = ()
    site_flags: tuple[SiteReturnFlag, ...] = ()
    inner: ec.WorkbookPlan | None = None      # 04/05 复用 AB-3 的计划
    sheets: tuple[str, ...] = field(default=ALL_SHEETS)

    @property
    def summary(self) -> dict:
        out = {"cost_refills": len(self.cost_refills),
               "site_return_flags": len(self.site_flags),
               "expense_updates": 0, "collection_creates": 0,
               "collection_voids": 0}
        if self.inner is not None:
            out.update(self.inner.summary)
        return out


# ------------------------------------------------------------------ 取数

def resolve_range(preset: str, date_from: date | None,
                  date_to: date | None) -> tuple[date, date]:
    """今天/昨天/本周/本月/自定义（#38）。周一为周首。"""
    if preset not in _RANGE_PRESETS:
        raise WorkbookError("invalid_range", f"时间预设必须是 {'/'.join(_RANGE_PRESETS)}")
    today = business_today()
    if preset == "today":
        return today, today
    if preset == "yesterday":
        return today - timedelta(days=1), today - timedelta(days=1)
    if preset == "this_week":
        return today - timedelta(days=today.weekday()), today
    if preset == "this_month":
        return today.replace(day=1), today
    if date_from is None or date_to is None:
        raise WorkbookError("invalid_range", "自定义区间必须同时给 from 与 to")
    if date_from > date_to:
        raise WorkbookError("invalid_range", "起始日期不能晚于结束日期")
    return date_from, date_to


def _assigned_lines(db: Session, *, project_id: str | None,
                    window: tuple[date, date] | None):
    """需求单明细 + 其项目归属（未归属单不进这两张表——它们还没有项目口径）。"""
    stmt = (
        select(FMaintenanceLine, FMaintenanceOrder,
               MaintenanceSourceOrderAssignment.project_id)
        .join(FMaintenanceOrder, FMaintenanceOrder.id == FMaintenanceLine.order_id)
        .join(MaintenanceSourceOrderAssignment,
              (MaintenanceSourceOrderAssignment.source_order_id
               == FMaintenanceOrder.raw_order_id)
              & MaintenanceSourceOrderAssignment.is_active.is_(True))
        .order_by(FMaintenanceOrder.order_date, FMaintenanceOrder.order_no,
                  FMaintenanceLine.line_no)
    )
    if project_id is not None:
        stmt = stmt.where(MaintenanceSourceOrderAssignment.project_id == project_id)
    if window is not None:
        stmt = stmt.where(FMaintenanceOrder.order_date >= window[0],
                          FMaintenanceOrder.order_date <= window[1])
    return db.execute(stmt).all()


def _style(ws, headers: list[str], fills: list[PatternFill]) -> None:
    ws.append(headers)
    for idx, fill in enumerate(fills, start=1):
        cell = ws.cell(row=1, column=idx)
        cell.fill = fill
        cell.font = Font(bold=True)


def _hide_key_column(ws, headers: list[str]) -> None:
    """行标识写进隐藏列：可编辑列不能当主键（改了单号就对不上行）。"""
    ws.column_dimensions[
        ws.cell(row=1, column=len(headers) + 1).column_letter].hidden = True


def _num(value) -> float | str:
    return float(value) if value is not None else ""


# ------------------------------------------------------------------ 导出

def _sheet_basics(wb, db: Session, project: MaintenanceProject,
                  contracts: list[MaintenanceProjectContract]) -> None:
    ws = wb.create_sheet(SHEET_BASICS)
    _style(ws, ["字段", "值"], [_READONLY, _READONLY])
    for label, value in (
        ("项目编号", project.project_code),
        ("项目名称", project.display_name),
        ("业务类型", project.business_type or ""),
        ("项目经理(CMO)", project.cmo_name or ""),
        ("销售人员", project.salesperson or ""),
        ("项目状态", project.lifecycle_status),
        ("硬盘不返还默认值(项目级)", "是" if project.no_return_default else "否"),
        ("合同编号(XSDD)", "、".join(c.contract_no for c in contracts)),
        ("前置库种类数 / 件数 / 金额(含税)", "尚未接入"),
    ):
        ws.append([label, value])


def _sheet_overview(wb, db: Session, project: MaintenanceProject,
                    contracts: list[MaintenanceProjectContract],
                    lines) -> None:
    ws = wb.create_sheet(SHEET_OVERVIEW)
    _style(ws, ["合同编号", "合同额(含税)", "原始合同状态", "是否计入总额",
                "生效日期", "失效日期"],
           [_READONLY] * 6)
    for c in contracts:
        ws.append([c.contract_no, _num(c.amount_inc_tax), c.contract_status or "",
                   "是" if c.included_in_total else "否",
                   c.effective_from.isoformat() if c.effective_from else "",
                   c.effective_to.isoformat() if c.effective_to else ""])
    ws.append([])
    ws.append(["关键指标", "值"])
    total = sum((c.amount_inc_tax or Decimal(0))
                for c in contracts if c.included_in_total)
    known = sum((ln.cost_amount_inc_tax or Decimal(0)) for ln, _o, _p in lines)
    missing = sum(1 for ln, _o, _p in lines if ln.cost_amount_inc_tax is None)
    collected = db.execute(
        select(func.coalesce(func.sum(MaintenanceCollectionSnapshot.cumulative_amount), 0))
        .where(MaintenanceCollectionSnapshot.project_id == project.project_id,
               MaintenanceCollectionSnapshot.status == "confirmed")
    ).scalar_one()
    # 缺数据的行照常展示，缺失只提示、不按 0 计（表 6 §02 口径）
    ws.append(["合同总额(含税)", _num(total)])
    ws.append(["累计回款(含税)", _num(collected)])
    ws.append(["项目已计成本(含税)", _num(known)])
    ws.append(["成本率(进度条口径)",
               f"{known / total * 100:.1f}%" if total else "—（无合同额，算不出）"])
    ws.append(["缺失成本行数", missing])


def _sheet_parts(wb, db: Session, lines, *, project_name_by_id=None) -> None:
    ws = wb.create_sheet(SHEET_PARTS)
    # 2026-08-17 全面放开：未税单价+含税单价+变更原因 三列黄底可改
    _style(ws, _PARTS_HEADERS,
           [_READONLY] * 11 + [_EDITABLE, _EDITABLE, _EDITABLE])
    for ln, order, _pid in lines:
        ws.append([
            order.order_no, order.order_date.isoformat() if order.order_date else "",
            order.linked_sales_order_no or "", order.project_raw or "",
            ln.pn_std or ln.pn_raw or "", ln.description or "",
            _num(ln.qty), _num(ln.return_qty), ln.serial_numbers or "",
            order.warehouse or "", ln.cost_source or "",
            _num(ln.unit_cost_ex_tax), _num(ln.unit_cost_inc_tax), "",
        ])
        ws.cell(row=ws.max_row, column=len(_PARTS_HEADERS) + 1, value=ln.id)
    _hide_key_column(ws, _PARTS_HEADERS)


def _sheet_site(wb, db: Session, project_id: str) -> None:
    ws = wb.create_sheet(SHEET_SITE)
    # 2026-08-17 全面放开：所有列黄底可改
    _style(ws, _SITE_HEADERS, [_EDITABLE] * 7)
    rows = db.execute(
        select(MaintenanceSiteIssueLine, MaintenanceSiteIssue)
        .join(MaintenanceSiteIssue,
              MaintenanceSiteIssue.issue_id == MaintenanceSiteIssueLine.issue_id)
        .where(MaintenanceSiteIssue.project_id == project_id)
        .order_by(MaintenanceSiteIssue.issue_date, MaintenanceSiteIssueLine.line_no)
    ).all()
    for line, issue in rows:
        ws.append([
            issue.issue_no, issue.issue_date.isoformat() if issue.issue_date else "",
            line.pn, line.serial_number or "", _num(line.quantity),
            "" if line.no_return is None else ("否" if line.no_return else "是"),
            "",
        ])
        ws.cell(row=ws.max_row, column=len(_SITE_HEADERS) + 1, value=line.issue_line_id)
    _hide_key_column(ws, _SITE_HEADERS)


def build_project_master(db: Session, *, project_id: str,
                         sheets: tuple[str, ...] = ALL_SHEETS) -> bytes | None:
    """项目总表：默认六 sheet；传 sheets 子集即「各 tab 单 sheet 下载」。"""
    project = db.get(MaintenanceProject, project_id)
    if project is None:
        return None
    unknown = [name for name in sheets if name not in ALL_SHEETS]
    if unknown:
        raise WorkbookError("unknown_sheet", f"未知 sheet：{'、'.join(unknown)}")
    contracts = ec._contracts(db, project_id)
    lines = _assigned_lines(db, project_id=project_id, window=None)

    wb = Workbook()
    wb.remove(wb.active)
    for name in sheets:
        if name == SHEET_BASICS:
            _sheet_basics(wb, db, project, contracts)
        elif name == SHEET_OVERVIEW:
            _sheet_overview(wb, db, project, contracts, lines)
        elif name == SHEET_PARTS:
            _sheet_parts(wb, db, lines)
        elif name == SHEET_EXPENSE:
            ec._build_expense_sheet(wb, db, contracts)
        elif name == SHEET_COLLECTION:
            ec._build_collection_sheet(wb, db, project_id, contracts)
        elif name == SHEET_SITE:
            _sheet_site(wb, db, project_id)

    meta = wb.create_sheet(_META_SHEET)
    meta.append(["协议版本", PROTOCOL_VERSION])
    meta.append(["项目ID", project_id])
    meta.append(["导出ID", str(uuid4())])
    meta.append(["导出时间", datetime.now(timezone.utc).isoformat()])
    meta.append(["包含 sheet", "、".join(sheets)])
    meta.sheet_state = "hidden"
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def build_global_lines(db: Session, *, preset: str, date_from: date | None = None,
                       date_to: date | None = None) -> bytes:
    """主页全局下载：**所有项目**的备件行级表（系统自动回填价之后）。"""
    window = resolve_range(preset, date_from, date_to)
    lines = _assigned_lines(db, project_id=None, window=window)
    names = dict(db.execute(
        select(MaintenanceProject.project_id, MaintenanceProject.display_name)).all())

    wb = Workbook()
    wb.remove(wb.active)
    ws = wb.create_sheet(GLOBAL_SHEET)
    _style(ws, _GLOBAL_HEADERS, [_READONLY] * 9 + [_EDITABLE, _READONLY, _EDITABLE])
    for ln, order, pid in lines:
        ws.append([
            names.get(pid, ""), order.linked_sales_order_no or "",
            order.order_no, order.order_date.isoformat() if order.order_date else "",
            ln.pn_std or ln.pn_raw or "", ln.description or "",
            _num(ln.qty), _num(ln.return_qty), ln.cost_source or "",
            _num(ln.unit_cost_ex_tax), _num(ln.unit_cost_inc_tax), "",
        ])
        ws.cell(row=ws.max_row, column=len(_GLOBAL_HEADERS) + 1, value=ln.id)
    _hide_key_column(ws, _GLOBAL_HEADERS)

    meta = wb.create_sheet(_META_SHEET)
    meta.append(["协议版本", PROTOCOL_VERSION])
    meta.append(["时间预设", preset])
    meta.append(["区间", f"{window[0].isoformat()}~{window[1].isoformat()}"])
    meta.append(["导出ID", str(uuid4())])
    meta.sheet_state = "hidden"
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


# ------------------------------------------------------------------ 解析

def _decimal(value, *, label: str, row_no: int) -> Decimal:
    try:
        parsed = Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, AttributeError, ValueError):
        raise WorkbookError("invalid_amount", f"第 {row_no} 行{label}不是合法数字")
    if parsed < 0:
        raise WorkbookError("invalid_amount", f"第 {row_no} 行{label}不能为负")
    return parsed.quantize(Decimal("0.01"))


def _parse_cost_refills(db: Session, ws, *, headers: list[str],
                        cost_col: int, reason_col: int) -> list[CostRefill]:
    """缺价补录：只认隐藏列里的 line_id，未填金额的行原样不动。
    2026-08-17 全面放开：含税单价也可直接填写。"""
    key_col = len(headers) + 1
    out: list[CostRefill] = []
    for row_no, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if not row or all(ec._text(v) == "" for v in row):
            continue
        raw_key = ec._text(row[key_col - 1]) if len(row) >= key_col else ""
        if not raw_key:
            raise WorkbookError(
                "line_not_recognized",
                f"第 {row_no} 行不是导出的备件行——需求单只能由氚云导入，本表只补价")
        raw_ex = row[cost_col - 1] if len(row) >= cost_col else None
        # 含税单价列（紧跟未税之后）
        inc_col = cost_col + 1
        raw_inc = row[inc_col - 1] if len(row) >= inc_col else None
        has_ex = ec._text(raw_ex) != ""
        has_inc = ec._text(raw_inc) != ""
        if not has_ex and not has_inc:
            continue
        line = db.get(FMaintenanceLine, int(raw_key))
        if line is None:
            raise WorkbookError("line_not_found",
                                f"第 {row_no} 行的备件行已不存在，请重新下载")
        if has_ex:
            ex_tax = _decimal(raw_ex, label="未税单位成本", row_no=row_no)
            inc_tax = (ex_tax * (Decimal("1") + TAX_RATE)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP)
        else:
            # 只填含税 → 推算未税
            inc_val = _decimal(raw_inc, label="含税单位成本", row_no=row_no)
            inc_tax = inc_val
            ex_tax = (inc_val / (Decimal("1") + TAX_RATE)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP)
        out.append(CostRefill(
            line_id=line.id,
            unit_cost_ex_tax=ex_tax,
            unit_cost_inc_tax=inc_tax,
            reason=ec._text(row[reason_col - 1]) or None
            if len(row) >= reason_col else None,
        ))
    return out


def _parse_site_flags(db: Session, ws) -> list[SiteReturnFlag]:
    """2026-08-17 全面放开：06 所有列可改后回传覆盖。"""
    key_col = len(_SITE_HEADERS) + 1
    out: list[SiteReturnFlag] = []
    for row_no, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if not row or all(ec._text(v) == "" for v in row):
            continue
        raw_key = ec._text(row[key_col - 1]) if len(row) >= key_col else ""
        if not raw_key:
            raise WorkbookError("line_not_recognized",
                                f"第 {row_no} 行不是导出的领用行")

        issue_no = ec._text(row[0]) if len(row) > 0 and ec._text(row[0]) else None
        raw_date = ec._text(row[1]) if len(row) > 1 else ""
        parsed_date = None
        if raw_date:
            from datetime import datetime as dt_cls
            try:
                parsed_date = dt_cls.strptime(raw_date, "%Y-%m-%d").date()
            except (ValueError, TypeError):
                try:
                    parsed_date = dt_cls.strptime(raw_date, "%Y/%m/%d").date()
                except (ValueError, TypeError):
                    parsed_date = None
        pn = ec._text(row[2]) if len(row) > 2 and ec._text(row[2]) else None
        sn = ec._text(row[3]) if len(row) > 3 and ec._text(row[3]) else None
        raw_qty = row[4] if len(row) > 4 else None
        qty = _decimal(raw_qty, label="领用数量", row_no=row_no) if ec._text(raw_qty) != "" else None
        raw_flag = ec._text(row[5]) if len(row) > 5 else ""
        no_return: bool | None = None
        if raw_flag != "":
            if raw_flag not in ("是", "否"):
                raise WorkbookError("invalid_flag",
                                    f"第 {row_no} 行「是否应返还」只能填 是 / 否")
            no_return = raw_flag == "否"
        remark = ec._text(row[6]) if len(row) > 6 and ec._text(row[6]) else None
        # 至少有一个字段变化才记录
        if no_return is None and remark is None and parsed_date is None and pn is None and sn is None and qty is None and issue_no is None:
            continue
        out.append(SiteReturnFlag(
            issue_line_id=raw_key,
            no_return=no_return,
            issue_no=issue_no,
            issue_date=parsed_date,
            pn=pn,
            serial_number=sn,
            quantity=qty,
            remark=remark,
        ))
    return out


def validate(db: Session, *, project_id: str | None, data: bytes) -> MasterPlan:
    """解析并产出无副作用计划；任何一行不合法即整份拒绝（与 AB-3 同语义）。"""
    try:
        wb = load_workbook(io.BytesIO(data), data_only=True)
    except Exception as exc:                                    # noqa: BLE001
        raise WorkbookError("invalid_file",
                            f"无法读取 .xlsx：{type(exc).__name__}") from exc
    present = [name for name in ALL_SHEETS if name in wb.sheetnames]
    if not present:
        raise WorkbookError(
            "missing_sheet",
            f"文件里没有任何可识别的工作表（期望其一：{'、'.join(ALL_SHEETS)}）")

    refills: list[CostRefill] = []
    flags: list[SiteReturnFlag] = []
    if SHEET_PARTS in wb.sheetnames:
        refills = _parse_cost_refills(db, wb[SHEET_PARTS], headers=_PARTS_HEADERS,
                                      cost_col=12, reason_col=14)
    if SHEET_SITE in wb.sheetnames:
        flags = _parse_site_flags(db, wb[SHEET_SITE])

    inner = None
    if project_id is not None and (SHEET_EXPENSE in wb.sheetnames
                                   or SHEET_COLLECTION in wb.sheetnames):
        # 04/05 直接复用 AB-3 已验收的解析；缺哪张就跳过哪张
        inner = ec.validate_partial(db, project_id=project_id, workbook=wb)
    return MasterPlan(project_id=project_id, cost_refills=tuple(refills),
                      site_flags=tuple(flags), inner=inner,
                      sheets=tuple(present))


def validate_global(db: Session, *, data: bytes) -> MasterPlan:
    """主页全局备件行级表回传：只有补价一件事。"""
    try:
        wb = load_workbook(io.BytesIO(data), data_only=True)
    except Exception as exc:                                    # noqa: BLE001
        raise WorkbookError("invalid_file",
                            f"无法读取 .xlsx：{type(exc).__name__}") from exc
    if GLOBAL_SHEET not in wb.sheetnames:
        raise WorkbookError("missing_sheet", f"缺少工作表：{GLOBAL_SHEET}")
    refills = _parse_cost_refills(db, wb[GLOBAL_SHEET], headers=_GLOBAL_HEADERS,
                                  cost_col=10, reason_col=12)
    return MasterPlan(project_id=None, cost_refills=tuple(refills),
                      sheets=(GLOBAL_SHEET,))


# ------------------------------------------------------------------ 应用

def apply(db: Session, plan: MasterPlan, *, operated_by: str,
          import_batch_id: str) -> dict:
    """整份事务应用；上传即覆盖。"""
    for refill in plan.cost_refills:
        existing = db.execute(
            select(MaintenanceManualCostOverride)
            .where(MaintenanceManualCostOverride.line_id == refill.line_id)
        ).scalar_one_or_none()
        if existing is None:
            db.add(MaintenanceManualCostOverride(
                line_id=refill.line_id,
                unit_cost_ex_tax=refill.unit_cost_ex_tax,
                unit_cost_inc_tax=refill.unit_cost_inc_tax,
                reason=refill.reason, active=True, version=1,
                updated_by=operated_by))
        else:
            existing.unit_cost_ex_tax = refill.unit_cost_ex_tax
            existing.unit_cost_inc_tax = refill.unit_cost_inc_tax
            existing.reason = refill.reason
            existing.active = True
            existing.version += 1
            existing.updated_by = operated_by

    for flag in plan.site_flags:
        line = db.get(MaintenanceSiteIssueLine, flag.issue_line_id)
        if line is None:
            raise WorkbookError("line_not_found",
                                f"领用行 {flag.issue_line_id} 已不存在，请重新下载")
        line.no_return = flag.no_return
        # 2026-08-17 全面放开：领用行级字段覆盖
        if flag.pn is not None:
            line.pn = flag.pn
        if flag.serial_number is not None:
            line.serial_number = flag.serial_number
        if flag.quantity is not None:
            line.quantity = flag.quantity
        if flag.remark is not None:
            line.remark = flag.remark
        # 现场领用单头字段（issue_no/issue_date）需要通过关联的单头更新
        if flag.issue_no is not None or flag.issue_date is not None:
            issue = db.get(MaintenanceSiteIssue, line.issue_id)
            if issue is not None:
                if flag.issue_no is not None:
                    issue.issue_no = flag.issue_no
                if flag.issue_date is not None:
                    issue.issue_date = flag.issue_date

    if plan.inner is not None:
        ec.apply(db, plan.inner, operated_by=operated_by,
                 import_batch_id=import_batch_id, commit=False)
    db.commit()
    return {"applied_by": operated_by, "import_batch_id": import_batch_id,
            "sheets": list(plan.sheets), **plan.summary}


# ====================================================================== V2

@dataclass(frozen=True)
class V2MilestoneChange:
    operation: str
    contract_no: str
    sequence: int
    planned_date: date | None
    date_precision: str
    planned_amount: Decimal | None
    entity_id: str | None
    base_version: int | None


@dataclass(frozen=True)
class MasterV2Plan:
    project_id: str
    sheets: tuple[str, ...]
    cost_refills: tuple[CostRefill, ...] = ()
    site_flags: tuple[SiteReturnFlag, ...] = ()
    expense_updates: tuple[ec.ExpenseUpdate, ...] = ()
    receipt_ops: tuple[ec.CollectionOp, ...] = ()
    milestone_changes: tuple[V2MilestoneChange, ...] = ()

    @property
    def summary(self) -> dict:
        return {
            "cost_overrides": len(self.cost_refills),
            "expense_updates": len(self.expense_updates),
            "plan_creates": sum(x.operation == "CREATE" for x in self.milestone_changes),
            "plan_updates": sum(x.operation == "UPDATE" for x in self.milestone_changes),
            "plan_voids": sum(x.operation == "VOID" for x in self.milestone_changes),
            "collection_updates": len(self.receipt_ops),
            "site_updates": len(self.site_flags),
        }


def _v2_hash(values: list[object]) -> str:
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, default=str, separators=(",", ":")).encode()).hexdigest()


def _v2_finalize(ws, headers: list[str], *, hidden_from: int | None = None) -> None:
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{ws.cell(ws.max_row or 1, len(headers)).coordinate}"
    for index, header in enumerate(headers, 1):
        cell = ws.cell(1, index)
        cell.fill = V2_HEADER
        cell.font = Font(bold=True, color="FFFFFF")
        width = min(60, max(12, len(str(header)) * 2 + 2))
        ws.column_dimensions[cell.column_letter].width = width
    if hidden_from is not None:
        for index in range(hidden_from, len(headers) + 1):
            ws.column_dimensions[ws.cell(1, index).column_letter].hidden = True
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = cell.alignment.copy(wrap_text=True, vertical="top")
        ws.row_dimensions[row[0].row].height = min(72, max(18, 18 + 12 * sum("\n" in str(c.value or "") for c in row)))


def _v2_header(ws, headers: list[str], editable: set[int] = set()) -> None:
    ws.append(headers)
    for idx in range(1, len(headers) + 1):
        cell = ws.cell(1, idx)
        cell.fill = V2_EDITABLE if idx in editable else V2_HEADER
        cell.font = Font(bold=True, color="FFFFFF" if idx not in editable else "4F3B00")


def _v2_build_overview(wb, project, contracts, db, lines) -> None:
    ws = wb.create_sheet(V2_SHEET_OVERVIEW)
    ws.append(["项目总览", None])
    ws.merge_cells("A1:B1")
    ws["A1"].fill = V2_TITLE
    ws["A1"].font = Font(bold=True, color="FFFFFF", size=14)
    total_contract = sum((c.amount_inc_tax or Decimal(0)) for c in contracts if c.included_in_total)
    cost = sum((line.cost_amount_inc_tax or Decimal(0)) for line, _order, _pid in lines)
    values = [
        ("项目编号", project.project_code), ("项目名称", project.display_name),
        ("生命周期", project.lifecycle_status), ("服务期", f"{project.period_from or '—'} ~ {project.period_to or '—'}"),
        ("负责人账号", project.project_manager_id or "未关联账号"),
        ("销售人员", project.salesperson or "—"), ("CMO", project.cmo_name or "—"),
        ("合同编号", "、".join(c.contract_no for c in contracts) or "—"),
        ("合同总额（含税）", str(total_contract) if total_contract else "—"),
        ("备件成本（含税）", str(cost) if cost else "—"),
        ("成本率", f"{(cost / total_contract * 100).quantize(Decimal('0.1'))}%" if total_contract else "—"),
        ("缺成本行数", sum(line.cost_amount_inc_tax is None for line, _order, _pid in lines)),
    ]
    for item in values:
        ws.append(list(item))
    ws.column_dimensions["A"].width = 24
    ws.column_dimensions["B"].width = 54
    ws.freeze_panes = "A2"


def _v2_build_plan(wb, db, project_id: str, contracts) -> None:
    ws = wb.create_sheet(V2_SHEET_PLAN)
    _v2_header(ws, V2_PLAN_HEADERS, editable={1, 2, 3, 4, 5, 6, 12})
    contract_by_id = {c.project_contract_id: c.contract_no for c in contracts}
    milestones = list(db.scalars(select(MaintenanceCollectionMilestone).where(
        MaintenanceCollectionMilestone.project_id == project_id,
        MaintenanceCollectionMilestone.is_active.is_(True),
    ).order_by(MaintenanceCollectionMilestone.project_contract_id, MaintenanceCollectionMilestone.sequence)))
    cumulative: dict[str, Decimal] = {}
    for milestone in milestones:
        contract_no = contract_by_id.get(milestone.project_contract_id, "")
        cumulative[contract_no] = cumulative.get(contract_no, Decimal(0)) + (milestone.planned_amount or Decimal(0))
        ws.append([
            "", contract_no, milestone.sequence, milestone.planned_date,
            milestone.date_precision, milestone.planned_amount,
            cumulative[contract_no], "—", "未上报", milestone.follow_up_status,
            project_id, milestone.follow_up_note or "", milestone.milestone_id, milestone.version,
        ])
    for _ in range(4):
        ws.append([""] * len(V2_PLAN_HEADERS))
    _v2_finalize(ws, V2_PLAN_HEADERS, hidden_from=13)


def _v2_build_parts(wb, db, project_id: str, lines) -> None:
    ws = wb.create_sheet(V2_SHEET_PARTS)
    # 1-based：17=人工未税单位成本 18=人工成本原因 24=来源（删除"行事实版本"后列号）
    editable = {17, 18, 24}
    _v2_header(ws, V2_PART_HEADERS, editable=editable)
    line_ids = [line.id for line, _order, _pid in lines]
    overrides = {
        item.line_id: item for item in db.scalars(select(MaintenanceManualCostOverride).where(
            MaintenanceManualCostOverride.line_id.in_(line_ids)
        ))
    } if line_ids else {}
    for line, order, _pid in lines:
        override = overrides.get(line.id)
        readonly = [order.order_no, order.order_date, order.linked_sales_order_no or "", order.demand_type or "",
                    order.warehouse or "", order.salesperson or "", order.business_type or "",
                    line.pn_std or line.pn_raw or "", line.description or "", line.qty, line.serial_numbers or "",
                    line.return_qty, line.cost_source or "", line.confidence or "none",
                    line.unit_cost_ex_tax, line.unit_cost_inc_tax, line.id]
        ws.append(readonly[:14] + [readonly[14], readonly[15],
                                   override.unit_cost_ex_tax if override else "",
                                   override.reason if override else "",
                                   "out_of_scope" if line.cost_source is None and line.unit_cost_ex_tax is None else ("none" if line.unit_cost_ex_tax is None else ""),
                                   "是" if line.cost_source in (None, "none") else "否",
                                   line.id, line.part_id,
                                   _v2_hash(readonly), line.line_note or "", "WBDD"])
    _v2_finalize(ws, V2_PART_HEADERS, hidden_from=20)


def _v2_build_expense(wb, db, project_id: str, contracts) -> None:
    ws = wb.create_sheet(V2_SHEET_EXPENSE)
    _v2_header(ws, V2_EXPENSE_HEADERS, editable={1, 3, 4, 5, 6, 7, 8, 11, 13, 16})
    expenses = ec._expenses(db, [c.contract_no for c in contracts])
    for expense in expenses:
        ws.append([expense.bxd_no or "", expense.line_no, expense.expense_date, expense.person or "",
                   expense.expense_type or "", expense.fee_category or "", expense.reason or "",
                   expense.linked_sales_order_no or "", project_id, "—", expense.data_status or "",
                   expense.amount, expense.tax_basis or "ex", expense.amount_ex_tax,
                   expense.amount_inc_tax, expense.remark or "", expense.raw_line_id])
    _v2_finalize(ws, V2_EXPENSE_HEADERS, hidden_from=17)


def _v2_build_receipts(wb, db, project_id: str, contracts) -> None:
    ws = wb.create_sheet(V2_SHEET_RECEIPTS)
    _v2_header(ws, V2_RECEIPT_HEADERS)
    contract_by_id = {c.project_contract_id: c.contract_no for c in contracts}
    rows = list(db.scalars(select(MaintenanceCollectionSnapshot).where(
        MaintenanceCollectionSnapshot.project_id == project_id,
        MaintenanceCollectionSnapshot.status == "confirmed",
    ).order_by(MaintenanceCollectionSnapshot.report_month)))
    for row in rows:
        ws.append([contract_by_id.get(row.project_contract_id, ""), row.report_month,
                   row.cumulative_amount, row.status, row.receipt_reference or "", row.remark or "", row.collection_id])
    _v2_finalize(ws, V2_RECEIPT_HEADERS, hidden_from=7)


def _v2_build_site(wb, db, project_id: str) -> None:
    ws = wb.create_sheet(V2_SHEET_SITE)
    _v2_header(ws, V2_SITE_HEADERS, editable={1, 2, 3, 4, 5, 6, 7})
    rows = db.execute(select(MaintenanceSiteIssueLine, MaintenanceSiteIssue).join(
        MaintenanceSiteIssue, MaintenanceSiteIssue.issue_id == MaintenanceSiteIssueLine.issue_id
    ).where(MaintenanceSiteIssue.project_id == project_id).order_by(MaintenanceSiteIssue.issue_date, MaintenanceSiteIssueLine.line_no)).all()
    for line, issue in rows:
        ws.append([issue.issue_no, issue.issue_date, line.pn, line.serial_number or "", line.quantity,
                   "" if line.no_return is None else ("否" if line.no_return else "是"), line.remark or "",
                   "—", "待确认品类", "—", line.issue_line_id])
    _v2_finalize(ws, V2_SITE_HEADERS, hidden_from=11)


def _v2_build_dictionary(wb) -> None:
    ws = wb.create_sheet(V2_SHEET_DICTIONARY)
    headers = ["字段", "业务含义", "来源/表字段", "编辑性", "格式/允许值", "空值意义", "覆盖影响"]
    _v2_header(ws, headers)
    rows = [
        ("part_id", "备件主键", "f_maintenance_line.part_id → dim_part.id", "只读", "整数", "异常需治理", "关系身份不随 PN 文本改变"),
        ("人工未税单位成本", "缺成本时的人工补价", "maintenance_manual_cost_override.unit_cost_ex_tax", "可编辑", "非负金额", "不创建 override", "覆盖该行成本人工证据"),
        ("计划回款金额", "合同期次计划金额", "maintenance_collection_milestone.planned_amount", "可编辑", "正数", "节点 incomplete", "更新计划节点并触发提醒复核"),
        ("最新累计实收", "已确认回款快照累计额", "maintenance_collection_snapshot.cumulative_amount", "只读", "金额", "未上报，不等于0", "仅影响展示和到款状态"),
        ("实体ID", "稳定写回身份", "各事实表主键", "只读", "UUID/字符串", "非法文件", "缺失或跨项目拒绝"),
    ]
    for row in rows:
        ws.append(row)
    _v2_finalize(ws, headers)


def build_project_master_v2(db: Session, *, project_id: str) -> bytes | None:
    project = db.get(MaintenanceProject, project_id)
    if project is None:
        return None
    contracts = ec._contracts(db, project_id)
    lines = _assigned_lines(db, project_id=project_id, window=None)
    wb = Workbook()
    wb.remove(wb.active)
    _v2_build_overview(wb, project, contracts, db, lines)
    _v2_build_plan(wb, db, project_id, contracts)
    _v2_build_parts(wb, db, project_id, lines)
    _v2_build_expense(wb, db, project_id, contracts)
    _v2_build_receipts(wb, db, project_id, contracts)
    _v2_build_site(wb, db, project_id)
    _v2_build_dictionary(wb)
    meta = wb.create_sheet(V2_SHEET_META)
    for key, value in (("protocol_id", V2_PROTOCOL_ID), ("template_version", V2_TEMPLATE_VERSION),
                       ("project_id", project_id), ("export_id", str(uuid4())),
                       ("exported_at", datetime.now(timezone.utc).isoformat()),
                       ("included_sheets", ",".join(V2_ALL_SHEETS[:-1]))):
        meta.append([key, value])
    meta.sheet_state = "hidden"
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _v2_date(value, *, row_no: int, label: str) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m"):
        try:
            parsed = datetime.strptime(raw, fmt).date()
            return parsed.replace(day=1) if fmt == "%Y-%m" else parsed
        except ValueError:
            continue
    raise WorkbookError("invalid_date", f"第 {row_no} 行{label}日期格式无效")


def _v2_decimal(value, *, row_no: int, label: str, required: bool = False) -> Decimal | None:
    if value in (None, ""):
        if required:
            raise WorkbookError("missing_amount", f"第 {row_no} 行{label}不能为空")
        return None
    try:
        parsed = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        raise WorkbookError("invalid_amount", f"第 {row_no} 行{label}不是合法数字")
    if parsed < 0:
        raise WorkbookError("invalid_amount", f"第 {row_no} 行{label}不能为负")
    return parsed.quantize(Decimal("0.01"))


def _v2_meta(wb) -> dict[str, str]:
    if V2_SHEET_META not in wb.sheetnames:
        raise WorkbookError("template_version_mismatch", "工作簿缺少 V2 元数据，请重新下载当前项目总表")
    return {
        str(row[0].value).strip(): str(row[1].value or "").strip()
        for row in wb[V2_SHEET_META].iter_rows(min_col=1, max_col=2)
        if row[0].value
    }


def _v2_verify_meta(wb, project_id: str) -> dict[str, str]:
    meta = _v2_meta(wb)
    if meta.get("protocol_id") != V2_PROTOCOL_ID or meta.get("template_version") != V2_TEMPLATE_VERSION:
        raise WorkbookError("template_version_mismatch", "工作簿版本已更新，请重新下载当前项目总表后再上传。")
    if meta.get("project_id") != project_id:
        raise WorkbookError("project_mismatch", "工作簿所属项目与上传入口不一致")
    missing = [name for name in V2_ALL_SHEETS[:-1] if name not in wb.sheetnames]
    if missing:
        raise WorkbookError("missing_sheet", f"V2 工作簿缺少工作表：{'、'.join(missing)}")
    return meta


def _v2_parse_parts(db: Session, ws) -> list[CostRefill]:
    headers = [str(cell.value or "") for cell in ws[1]]
    index = {name: i for i, name in enumerate(headers)}
    required = {"系统未税单位成本", "人工未税单位成本", "人工成本原因", "实体ID", "只读哈希"}
    if not required.issubset(index):
        raise WorkbookError("template_version_mismatch", "03_备件明细列定义不是当前 V2 版本")
    out: list[CostRefill] = []
    for row_no, row in enumerate(ws.iter_rows(min_row=2, values_only=True), 2):
        if not row or all(value in (None, "") for value in row):
            continue
        line_id = row[index["实体ID"]]
        if line_id in (None, ""):
            raise WorkbookError("line_not_recognized", f"03_备件明细第 {row_no} 行缺少实体ID")
        line = db.get(FMaintenanceLine, int(line_id))
        if line is None:
            raise WorkbookError("line_not_found", f"第 {row_no} 行备件事实已不存在，请重新下载")
        readonly = list(row[:16]) + [line.id]
        if _v2_hash(readonly) != str(row[index["只读哈希"]] or ""):
            raise WorkbookError("readonly_cell_modified", f"第 {row_no} 行只读事实已修改或已过期，请重新下载")
        raw_manual = row[index["人工未税单位成本"]]
        reason = str(row[index["人工成本原因"]] or "").strip() or None
        if raw_manual in (None, ""):
            continue
        amount = _v2_decimal(raw_manual, row_no=row_no, label="人工未税单位成本", required=True)
        inc = (amount * (Decimal("1") + TAX_RATE)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        out.append(CostRefill(line_id=line.id, unit_cost_ex_tax=amount, unit_cost_inc_tax=inc, reason=reason))
    return out


def _v2_parse_site(db: Session, ws) -> list[SiteReturnFlag]:
    headers = [str(cell.value or "") for cell in ws[1]]
    index = {name: i for i, name in enumerate(headers)}
    out: list[SiteReturnFlag] = []
    for row_no, row in enumerate(ws.iter_rows(min_row=2, values_only=True), 2):
        if not row or all(value in (None, "") for value in row):
            continue
        raw_id = row[index["实体ID"]]
        if raw_id in (None, ""):
            raise WorkbookError("line_not_recognized", f"06_领用返还第 {row_no} 行缺少实体ID")
        flag = str(row[index["是否应返还"]] or "").strip()
        if flag and flag not in {"是", "否"}:
            raise WorkbookError("invalid_flag", f"第 {row_no} 行是否应返还只能填 是 / 否")
        out.append(SiteReturnFlag(
            issue_line_id=str(raw_id),
            no_return=(flag == "否") if flag else None,
            issue_no=str(row[index["领用单号"]] or "").strip() or None,
            issue_date=_v2_date(row[index["领用日期"]], row_no=row_no, label="领用") if row[index["领用日期"]] not in (None, "") else None,
            pn=str(row[index["PN"]] or "").strip() or None,
            serial_number=str(row[index["SN"]] or "").strip() or None,
            quantity=_v2_decimal(row[index["领用数量"]], row_no=row_no, label="领用数量") if row[index["领用数量"]] not in (None, "") else None,
            remark=str(row[index["备注"]] or "").strip() or None,
        ))
    return out


def _v2_parse_plan(db: Session, project_id: str, ws) -> list[V2MilestoneChange]:
    contracts = {c.contract_no: c for c in ec._contracts(db, project_id)}
    headers = [str(cell.value or "") for cell in ws[1]]
    index = {name: i for i, name in enumerate(headers)}
    out: list[V2MilestoneChange] = []
    for row_no, row in enumerate(ws.iter_rows(min_row=2, values_only=True), 2):
        if not row or all(value in (None, "") for value in row):
            continue
        operation = str(row[index["操作"]] or "").strip().upper()
        if not operation:
            continue
        if operation not in {"CREATE", "UPDATE", "VOID"}:
            raise WorkbookError("invalid_operation", f"第 {row_no} 行操作必须是 CREATE、UPDATE 或 VOID")
        contract_no = str(row[index["合同编号"]] or "").strip()
        contract = contracts.get(contract_no)
        if contract is None:
            raise WorkbookError("contract_not_found", f"第 {row_no} 行合同编号不属于本项目")
        sequence = int(row[index["期次"]])
        if not 1 <= sequence <= 24:
            raise WorkbookError("invalid_sequence", f"第 {row_no} 行期次必须为 1-24")
        precision = str(row[index["日期精度"]] or "day").strip()
        if precision not in {"day", "month"}:
            raise WorkbookError("invalid_date_precision", f"第 {row_no} 行日期精度只能是 day/month")
        amount = _v2_decimal(row[index["计划回款金额（含税）"]], row_no=row_no, label="计划回款金额", required=operation != "VOID")
        planned_date = _v2_date(row[index["计划回款日期"]], row_no=row_no, label="计划回款")
        if operation != "VOID" and planned_date is None and amount is None:
            raise WorkbookError("incomplete_milestone", f"第 {row_no} 行计划日期和金额不能同时为空")
        out.append(V2MilestoneChange(
            operation=operation, contract_no=contract_no, sequence=sequence,
            planned_date=planned_date, date_precision=precision, planned_amount=amount,
            entity_id=str(row[index["实体ID"]] or "").strip() or None,
            base_version=int(row[index["基础版本"]]) if row[index["基础版本"]] not in (None, "") else None,
        ))
    return out


def _v2_parse_expenses(db: Session, ws) -> list[ec.ExpenseUpdate]:
    headers = [str(cell.value or "") for cell in ws[1]]
    index = {name: i for i, name in enumerate(headers)}
    out: list[ec.ExpenseUpdate] = []
    for row_no, row in enumerate(ws.iter_rows(min_row=2, values_only=True), 2):
        if not row or all(value in (None, "") for value in row):
            continue
        raw_id = str(row[index["实体ID"]] or "").strip()
        expense = db.scalar(select(FProjectExpense).where(FProjectExpense.raw_line_id == raw_id))
        if expense is None:
            raise WorkbookError("expense_not_found", f"第 {row_no} 行报销事实已不存在，请重新下载")
        amount = _v2_decimal(row[index["未税金额"]], row_no=row_no, label="未税金额")
        inc = (amount * (Decimal("1") + TAX_RATE)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if amount is not None else None
        out.append(ec.ExpenseUpdate(
            raw_line_id=raw_id,
            expense_date=_v2_date(row[index["报销日期"]], row_no=row_no, label="报销") if row[index["报销日期"]] not in (None, "") else None,
            person=str(row[index["报销人员"]] or "").strip() or None,
            expense_type=str(row[index["报销类别"]] or "").strip() or None,
            fee_category=str(row[index["费用分类"]] or "").strip() or None,
            reason=str(row[index["支出事由"]] or "").strip() or None,
            contract_no=str(row[index["维保销售订单（归集键）"]] or "").strip() or None,
            amount_ex_tax=amount,
            amount_inc_tax=inc,
            data_status=str(row[index["流程状态"]] or "").strip() or None,
            remark=str(row[index["备注"]] or "").strip() or None,
        ))
    return out


def _v2_parse_receipts(db: Session, project_id: str, ws) -> list[ec.CollectionOp]:
    contracts = {c.contract_no: c for c in ec._contracts(db, project_id)}
    headers = [str(cell.value or "") for cell in ws[1]]
    index = {name: i for i, name in enumerate(headers)}
    out: list[ec.CollectionOp] = []
    for row_no, row in enumerate(ws.iter_rows(min_row=2, values_only=True), 2):
        if not row or all(value in (None, "") for value in row):
            continue
        contract_no = str(row[index["合同编号"]] or "").strip()
        contract = contracts.get(contract_no)
        if contract is None:
            raise WorkbookError("contract_not_found", f"第 {row_no} 行合同编号不属于本项目")
        month = _v2_date(row[index["报告月份"]], row_no=row_no, label="报告月份")
        if month is None:
            raise WorkbookError("invalid_month", f"第 {row_no} 行报告月份不能为空")
        amount = _v2_decimal(row[index["累计实收金额（含税）"]], row_no=row_no, label="累计实收金额", required=True)
        existing = db.scalar(select(MaintenanceCollectionSnapshot).where(
            MaintenanceCollectionSnapshot.project_contract_id == contract.project_contract_id,
            MaintenanceCollectionSnapshot.report_month == month,
        ))
        out.append(ec.CollectionOp(
            operation="UPDATE" if existing is not None else "CREATE",
            project_contract_id=contract.project_contract_id, contract_no=contract_no,
            report_month=month, cumulative_amount=amount,
            receipt_reference=str(row[index["回款凭证号"]] or "").strip() or None,
            remark=str(row[index["备注"]] or "").strip() or None,
            collection_status=str(row[index["状态"]] or "confirmed").strip() or "confirmed",
        ))
    return out


def validate_project_master_v2(db: Session, *, project_id: str, data: bytes) -> MasterV2Plan:
    try:
        wb = load_workbook(io.BytesIO(data), data_only=True)
    except Exception as exc:
        raise WorkbookError("invalid_file", f"无法读取 .xlsx：{type(exc).__name__}") from exc
    _v2_verify_meta(wb, project_id)
    return MasterV2Plan(
        project_id=project_id,
        sheets=tuple(name for name in V2_ALL_SHEETS if name in wb.sheetnames),
        cost_refills=tuple(_v2_parse_parts(db, wb[V2_SHEET_PARTS])),
        site_flags=tuple(_v2_parse_site(db, wb[V2_SHEET_SITE])),
        expense_updates=tuple(_v2_parse_expenses(db, wb[V2_SHEET_EXPENSE])),
        receipt_ops=tuple(_v2_parse_receipts(db, project_id, wb[V2_SHEET_RECEIPTS])),
        milestone_changes=tuple(_v2_parse_plan(db, project_id, wb[V2_SHEET_PLAN])),
    )


def apply_project_master_v2(db: Session, plan: MasterV2Plan, *, operated_by: str, import_batch_id: str) -> dict:
    # All mutations deliberately happen on this one Session transaction.
    for refill in plan.cost_refills:
        existing = db.scalar(select(MaintenanceManualCostOverride).where(MaintenanceManualCostOverride.line_id == refill.line_id))
        if existing is None:
            db.add(MaintenanceManualCostOverride(line_id=refill.line_id, unit_cost_ex_tax=refill.unit_cost_ex_tax,
                                                 unit_cost_inc_tax=refill.unit_cost_inc_tax, reason=refill.reason,
                                                 active=True, version=1, updated_by=operated_by))
        else:
            existing.unit_cost_ex_tax = refill.unit_cost_ex_tax
            existing.unit_cost_inc_tax = refill.unit_cost_inc_tax
            existing.reason = refill.reason
            existing.active = True
            existing.version += 1
            existing.updated_by = operated_by
    for flag in plan.site_flags:
        line = db.get(MaintenanceSiteIssueLine, flag.issue_line_id)
        if line is None:
            raise WorkbookError("line_not_found", f"领用行 {flag.issue_line_id} 已不存在，请重新下载")
        line.no_return = flag.no_return
        if flag.pn is not None: line.pn = flag.pn
        if flag.serial_number is not None: line.serial_number = flag.serial_number
        if flag.quantity is not None: line.quantity = flag.quantity
        if flag.remark is not None: line.remark = flag.remark
        if flag.issue_no is not None or flag.issue_date is not None:
            issue = db.get(MaintenanceSiteIssue, line.issue_id)
            if issue is not None:
                if flag.issue_no is not None: issue.issue_no = flag.issue_no
                if flag.issue_date is not None: issue.issue_date = flag.issue_date
    if plan.expense_updates or plan.receipt_ops:
        ec.apply(db, ec.WorkbookPlan(plan.project_id, plan.expense_updates, plan.receipt_ops),
                 operated_by=operated_by, import_batch_id=import_batch_id, commit=False)
    contracts = {c.contract_no: c for c in ec._contracts(db, plan.project_id)}
    for change in plan.milestone_changes:
        contract = contracts[change.contract_no]
        existing = db.scalar(select(MaintenanceCollectionMilestone).where(
            MaintenanceCollectionMilestone.project_contract_id == contract.project_contract_id,
            MaintenanceCollectionMilestone.sequence == change.sequence,
        ).with_for_update())
        if change.operation == "VOID":
            if existing is None:
                raise WorkbookError("void_target_missing", f"合同 {change.contract_no} 第 {change.sequence} 期不存在")
            if change.base_version is not None and existing.version != change.base_version:
                raise WorkbookError("stale_row", f"合同 {change.contract_no} 第 {change.sequence} 期已被更新")
            existing.is_active = False
            existing.version += 1
            continue
        if existing is not None and change.base_version is not None and existing.version != change.base_version:
            raise WorkbookError("stale_row", f"合同 {change.contract_no} 第 {change.sequence} 期已被更新")
        milestone = write_collection_milestone(
            db, project_id=plan.project_id, project_contract_id=contract.project_contract_id,
            sequence=change.sequence, planned_date=change.planned_date,
            planned_amount=change.planned_amount, completeness_state=("complete" if change.planned_date and change.planned_amount else "date_only" if change.planned_date else "amount_only"),
            source="project_master_v2", date_precision=change.date_precision, operator=operated_by,
        )
        milestone.is_active = True
    db.commit()
    return {"applied_by": operated_by, "import_batch_id": import_batch_id,
            "protocol_id": V2_PROTOCOL_ID, "template_version": V2_TEMPLATE_VERSION,
            "project_id": plan.project_id, "sheets": list(plan.sheets), **plan.summary,
            "warnings": []}

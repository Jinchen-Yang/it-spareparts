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
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from uuid import uuid4

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.business_time import business_today
from app.models.dimensions import DimPart, PartAlias
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
    MaintenanceProjectOperationAudit,
    MaintenanceSiteIssue,
    MaintenanceSiteIssueLine,
)
from app.models.system import SysImportBatch
from app.models.maintenance_manager import MaintenanceCollectionMilestone
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.models.sales import FSalesOrder
from app.services import maintenance_expense_collection_workbook as ec
from app.services.maintenance_boss_board import _card_contracts
from app.services.maintenance_collection_milestones import write_collection_milestone
from app.services.maintenance_expense_collection_workbook import (
    TAX_RATE,
    WorkbookError,
)

PROTOCOL_VERSION = "project-master-v1"
V2_PROTOCOL_ID = "ITDATA_MAINT_PROJECT_MASTER/2.0"
# 2026-08-19（#264/#267）：03 全字段可编辑 + 04 作废/缺行=作废——新增「操作」列、
# 放开行级数据列、只读哈希收窄。旧 2.0.0 工作簿因哈希列集合变更一律拒绝并
# 提示重新下载。
V2_TEMPLATE_VERSION = "2.3.0"

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

V2_SHEET_USAGE = "00_使用说明"
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
                 V2_SHEET_DICTIONARY, V2_SHEET_USAGE, V2_SHEET_META)
V2_EDITABLE = PatternFill("solid", fgColor="FFE699")
V2_READONLY = PatternFill("solid", fgColor="D9E2F3")
V2_HEADER = PatternFill("solid", fgColor="1F4E78")
V2_TITLE = PatternFill("solid", fgColor="17365D")
V2_PART_HEADERS = [
    "操作", "维保单号", "制单日期", "XSDD", "需求类型", "仓库", "销售人员", "业务类型",
    "PN", "描述", "需求数量", "SN", "退货数量", "成本来源", "置信度",
    "系统未税单位成本", "系统含税单位成本", "人工未税单位成本", "人工成本原因",
    "成本缺失类型", "可补价", "实体ID", "备件主键", "只读哈希",
    "备注", "来源",
]
# 2026-08-19 全字段可编辑（1-based 列号）：操作列 + 行级数据列（PN/描述/数量/SN/
# 退货数量）+ 人工成本两列 + 备注。维保单号/制单日期/XSDD/头级字段/系统成本/来源仍锁。
V2_PART_EDITABLE = {1, 9, 10, 11, 12, 13, 18, 19, 25}
# 参与只读哈希的列（按表头名定位，避免插列后错位）：行级身份与系统事实。
# 可编辑列一律不进哈希，改黄底列不再触发 readonly_cell_modified。
V2_PART_HASH_COLUMNS = [
    "维保单号", "制单日期", "XSDD", "需求类型", "仓库", "销售人员", "业务类型",
    "成本来源", "置信度", "系统未税单位成本", "系统含税单位成本", "来源", "实体ID",
]
V2_PLAN_HEADERS = ["操作", "合同编号", "期次", "计划回款日期", "日期精度", "计划回款金额（含税）",
                   "累计计划金额", "最新累计实收", "到款状态", "提醒状态", "负责人", "备注", "实体ID", "基础版本"]
V2_EXPENSE_HEADERS = ["操作", "费用单号", "明细序号", "报销日期", "报销人员", "报销类别", "费用分类",
                      "支出事由", "维保销售订单（归集键）", "项目名称", "销售人员", "流程状态",
                      "原始报销金额", "金额口径", "未税金额", "含税金额（系统）", "备注", "实体ID"]
V2_RECEIPT_HEADERS = ["合同编号", "报告月份", "累计实收金额（含税）", "状态", "回款凭证号", "备注", "实体ID"]
V2_SITE_HEADERS = ["领用单号", "领用日期", "PN", "SN", "领用数量", "是否应返还", "备注",
                   "应返数量", "返还状态", "返还单号", "实体ID"]


# ------------------------------------------------------------------ 计划

@dataclass(frozen=True)
class CostRefill:
    line_id: int | None
    unit_cost_ex_tax: Decimal | None
    unit_cost_inc_tax: Decimal | None
    reason: str | None
    note: str | None = None
    # 2026-08-19 全字段可编辑（03，#264/#267）：UPDATE/VOID 作用于既有行；
    # CREATE 新增行时 line_id 为 None，order_no+pn+qty 必填。
    operation: str = "UPDATE"
    pn: str | None = None
    description: str | None = None
    qty: Decimal | None = None
    return_qty: Decimal | None = None
    serial_numbers: str | None = None
    order_no: str | None = None
    is_create: bool = False
    order_id: int | None = None
    part_id: int | None = None


@dataclass(frozen=True)
class SiteReturnFlag:
    issue_line_id: str
    no_return: bool | None
    is_create: bool = False
    # 2026-08-23：06 缺行=作废（与 03/04 同语义）——删除导出中存在的领用行
    is_void: bool = False
    issue_id: str | None = None
    line_no: int | None = None
    part_id: int | None = None
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

def project_sales_order_nos(db: Session, project_id: str) -> list[str]:
    """项目可用于费用归集的 XSDD：合同台账优先，并兼容已挂靠 WBDD 的销售订单。"""
    values = set(db.scalars(select(MaintenanceProjectContract.contract_no).where(
        MaintenanceProjectContract.project_id == project_id,
    )).all())
    values.update(db.scalars(
        select(FMaintenanceOrder.linked_sales_order_no)
        .join(
            MaintenanceSourceOrderAssignment,
            MaintenanceSourceOrderAssignment.source_order_id
            == FMaintenanceOrder.raw_order_id,
        )
        .where(
            MaintenanceSourceOrderAssignment.project_id == project_id,
            MaintenanceSourceOrderAssignment.is_active.is_(True),
            FMaintenanceOrder.linked_sales_order_no.is_not(None),
        )
    ).all())
    return sorted(value for value in values if value)


def _exact_part_for_pn(db: Session, pn: str) -> DimPart | None:
    normalized = pn.strip().upper()
    part = db.scalar(select(DimPart).where(
        DimPart.pn_std == normalized,
        DimPart.status != "merged",
    ))
    if part is not None:
        return part
    return db.scalar(
        select(DimPart)
        .join(PartAlias, PartAlias.part_id == DimPart.id)
        .where(
            PartAlias.pn_raw == pn.strip(),
            PartAlias.status == "active",
            DimPart.status != "merged",
        )
    )

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
    """需求单明细 + 其项目归属（未归属单不进这两张表——它们还没有项目口径）。

    2026-08-19（#267 读侧修复 1）：墓碑整单作废与行级软作废都在此过滤——
    项目总表 03、02 概览、主页全局行级表三处共用本入口。
    2026-08-21（客户反馈）：order_date 倒序——最新日期排前面；行键/行级哈希
    与行序解耦，Excel 回传不受行序影响。
    """
    from app.services import maintenance_demands

    stmt = (
        select(FMaintenanceLine, FMaintenanceOrder,
               MaintenanceSourceOrderAssignment.project_id)
        .join(FMaintenanceOrder, FMaintenanceOrder.id == FMaintenanceLine.order_id)
        .join(MaintenanceSourceOrderAssignment,
              (MaintenanceSourceOrderAssignment.source_order_id
               == FMaintenanceOrder.raw_order_id)
              & MaintenanceSourceOrderAssignment.is_active.is_(True))
        .where(FMaintenanceLine.is_active.is_(True),
               maintenance_demands.active_demand_condition())
        .order_by(FMaintenanceOrder.order_date.desc(), FMaintenanceOrder.order_no,
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
        # 备注与成本独立——只改备注/原因不写覆盖表
        if refill.unit_cost_ex_tax is not None or refill.reason is not None:
            _merge_manual_cost_to_line(db, refill, operated_by=operated_by)
        if refill.note is not None:
            line = db.get(FMaintenanceLine, refill.line_id)
            if line is not None:
                line.line_note = refill.note

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
    will_void_rows: list[dict] = []
    # 04 报销作废（#264/#267 契约）：显式 VOID 操作列 + 缺行=作废。
    expense_voids: tuple[str, ...] = ()
    will_void_rows: tuple[dict, ...] = ()

    @property
    def summary(self) -> dict:
        updates = [x for x in self.cost_refills if not x.is_create and x.operation != "VOID"]
        return {
            # E2E #5：改数量不再被误报为 cost_overrides——行更新与成本覆盖分开计
            "line_updates": len(updates),
            "qty_updates": sum(x.qty is not None or x.return_qty is not None for x in updates),
            "cost_overrides": sum(
                x.unit_cost_ex_tax is not None or x.reason is not None for x in updates),
            "line_creates": sum(x.is_create for x in self.cost_refills),
            "line_voids": sum(x.operation == "VOID" for x in self.cost_refills),
            "expense_creates": sum(getattr(x, "is_create", False) for x in self.expense_updates),
            "expense_updates": sum(not getattr(x, "is_create", False) for x in self.expense_updates),
            "expense_voids": len(self.expense_voids),
            "plan_creates": sum(x.operation == "CREATE" for x in self.milestone_changes),
            "plan_updates": sum(x.operation == "UPDATE" for x in self.milestone_changes),
            "plan_voids": sum(x.operation == "VOID" for x in self.milestone_changes),
            "collection_updates": len(self.receipt_ops),
            "site_creates": sum(x.is_create for x in self.site_flags),
            "site_voids": sum(x.is_void for x in self.site_flags),
            "site_updates": sum(not x.is_create and not x.is_void
                                for x in self.site_flags),
        }


def _v2_hash_value(value) -> str:
    """哈希输入归一化：Excel 往返后 Decimal 变 float、date 变 datetime，
    直接 json 序列化会导致导出侧与解析侧哈希不一致（checkpoint 潜伏 bug）。"""
    if value is None:
        return ""
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float, Decimal)):
        return format(Decimal(str(value)).normalize(), "f")
    return str(value)


def _v2_hash(values: list[object]) -> str:
    return hashlib.sha256(json.dumps(
        [_v2_hash_value(v) for v in values],
        ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _v2_finalize(ws, headers: list[str], *, hidden_from: int | None = None,
                 editable: set[int] | None = None,
                 operation_col: int | None = None,
                 operation_choices: tuple[str, ...] = ("UPDATE", "VOID", "CREATE")) -> None:
    """收尾样式：黄底=可编辑（表头+数据区整列），操作列做成下拉只能选不能乱填。"""
    editable = editable or set()
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{ws.cell(ws.max_row or 1, len(headers)).coordinate}"
    for index, header in enumerate(headers, 1):
        cell = ws.cell(1, index)
        if index in editable:
            cell.fill = V2_EDITABLE
            cell.font = Font(bold=True, color="4F3B00")
        else:
            cell.fill = V2_HEADER
            cell.font = Font(bold=True, color="FFFFFF")
        width = min(60, max(12, len(str(header)) * 2 + 2))
        ws.column_dimensions[cell.column_letter].width = width
    if hidden_from is not None:
        for index in range(hidden_from, len(headers) + 1):
            ws.column_dimensions[ws.cell(1, index).column_letter].hidden = True
    # 数据区可编辑列整列黄底（含空白新增行，向下多刷 20 行备用）
    if editable:
        last_row = (ws.max_row or 1) + 20
        for index in editable:
            letter = ws.cell(1, index).column_letter
            for r in range(2, last_row + 1):
                ws[f"{letter}{r}"].fill = V2_EDITABLE
    # 操作列：Excel 数据验证下拉（允许留空），杜绝自由输入拼错
    if operation_col is not None:
        dv = DataValidation(
            type="list",
            formula1='"' + ",".join(operation_choices) + '"',
            allow_blank=True,
            showDropDown=False,  # False=显示下拉箭头（openpyxl 语义反转）
        )
        dv.error = "操作列只能从下拉选择：{}".format("/".join(operation_choices))
        dv.errorTitle = "操作列取值无效"
        ws.add_data_validation(dv)
        letter = ws.cell(1, operation_col).column_letter
        dv.add(f"{letter}2:{letter}{(ws.max_row or 1) + 20}")
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


def _project_order_salesperson(db: Session, project_id: str) -> str | None:
    """项目销售人员（2026-08-20 用户口径）：本项目挂靠需求单上的销售众数。

    台账 salesperson 优先（project.salesperson），缺省回落到订单侧自动回填。
    """
    from app.services import maintenance_demands

    rows = db.execute(
        select(FMaintenanceOrder.salesperson, func.count())
        .join(MaintenanceSourceOrderAssignment,
              (MaintenanceSourceOrderAssignment.source_order_id
               == FMaintenanceOrder.raw_order_id)
              & MaintenanceSourceOrderAssignment.is_active.is_(True))
        .where(
            MaintenanceSourceOrderAssignment.project_id == project_id,
            FMaintenanceOrder.salesperson.is_not(None),
            FMaintenanceOrder.salesperson != "",
            maintenance_demands.active_demand_condition(),
        )
        .group_by(FMaintenanceOrder.salesperson)
        .order_by(func.count().desc())
        .limit(1)
    ).all()
    return rows[0][0] if rows else None


def _account_display_name(db: Session, username: str | None) -> str | None:
    """账号 → 显示人名（找不到回退账号本身）。"""
    if not username:
        return None
    from app.models.system import SysUser

    row = db.execute(
        select(SysUser.display_name).where(SysUser.username == username)
    ).scalar_one_or_none()
    return row or username


_EXAMPLE_FONT = Font(color="999999", italic=True)
_EXAMPLE_FILL = PatternFill("solid", fgColor="F5F5F5")


def _v2_append_example_row(ws, headers: list[str], values: dict[str, object]) -> None:
    """每个数据 sheet 底部一行灰色「示例」：告诉用户怎么填，上传时系统忽略。

    标记约定：操作列填「示例」（02/03/04）；无操作列的 sheet（05/06）在备注列
    填「【示例】…」。解析侧 _is_example_row 统一识别跳过。
    """
    row = [values.get(name, "") for name in headers]
    ws.append(row)
    r = ws.max_row
    for c in range(1, len(headers) + 1):
        cell = ws.cell(r, c)
        cell.font = _EXAMPLE_FONT
        cell.fill = _EXAMPLE_FILL


def _is_example_row(row) -> bool:
    """上传侧识别示例行：任一单元格为「示例」或以「【示例】」开头。"""
    for value in row or ():
        text = str(value or "").strip()
        if text == "示例" or text.startswith("【示例】"):
            return True
    return False


def _v2_build_overview(wb, project, contracts, db, lines) -> None:
    ws = wb.create_sheet(V2_SHEET_OVERVIEW)
    ws.append(["项目总览", None])
    ws.merge_cells("A1:B1")
    ws["A1"].fill = V2_TITLE
    ws["A1"].font = Font(bold=True, color="FFFFFF", size=14)
    total_contract = sum((c.amount_inc_tax or Decimal(0)) for c in contracts if c.included_in_total)
    # 台账合同表缺位（生产仅 7 条）→ 与看板同口径：XSDD 销售回退（#51：
    # 台账优先 → 项目挂靠单据的 distinct XSDD 去销售表取金额）
    card = _card_contracts(db, [project.project_id]).get(project.project_id)
    if not total_contract and card and card.get("amount_inc_tax") is not None:
        total_contract = card["amount_inc_tax"]
    contract_shared = bool(card and card.get("contract_shared"))
    contract_incomplete = bool(card and card.get("contract_incomplete"))
    # 2026-08-19：备件成本合并人工覆盖——主表无成本但有 override 的行按
    # override 含税金额×数量计入（与看板/面板口径一致）
    line_ids = [line.id for line, _order, _pid in lines]
    override_map = {
        item.line_id: item for item in db.scalars(
            select(MaintenanceManualCostOverride).where(
                MaintenanceManualCostOverride.line_id.in_(line_ids)
            )
        )
    } if line_ids else {}

    def _line_cost(line) -> Decimal:
        base = line.cost_amount_inc_tax
        if base is not None:
            return base
        ov = override_map.get(line.id)
        if ov is not None and ov.unit_cost_inc_tax is not None:
            return ov.unit_cost_inc_tax * (line.qty or Decimal(0))
        return Decimal(0)

    cost = sum(_line_cost(line) for line, _order, _pid in lines)
    values = [
        ("项目编号", project.project_code), ("项目名称", project.display_name),
        ("生命周期", project.lifecycle_status), ("服务期", f"{project.period_from or '—'} ~ {project.period_to or '—'}"),
        # 2026-08-20 用户口径：负责人＝项目经理（显示人名）；销售人员＝台账优先、
        # 缺省自动回填为挂靠需求单的销售众数；CMO＝台账来源（无台账则缺）。
        ("项目经理（负责人）",
         _account_display_name(db, project.project_manager_id) or "未关联账号"),
        ("销售人员", project.salesperson or _project_order_salesperson(db, project.project_id) or "—"),
        ("CMO", project.cmo_name or "—"),
        ("合同编号", "、".join(c.contract_no for c in contracts) or "—"),
        ("合同总额（含税）", str(total_contract) if total_contract else "—"),
        ("合同额口径", "XSDD 销售回退（台账未导入）" if (contract_shared or contract_incomplete) or (total_contract and not contracts) else "台账合同"),
        ("备件成本（含税）", str(cost) if cost else "—"),
        ("成本率", f"{(cost / total_contract * 100).quantize(Decimal('0.1'))}%" if total_contract else "—"),
        ("缺成本行数", sum(
            line.cost_amount_inc_tax is None and line.id not in override_map
            for line, _order, _pid in lines
        )),
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
    sample_contract = ((contracts[0].contract_no if contracts else None)
                       or next(iter(project_sales_order_nos(db, project_id)), ""))
    _v2_append_example_row(ws, V2_PLAN_HEADERS, {
        "操作": "示例",
        "合同编号": sample_contract or "（先在需求单挂靠后自动获得 XSDD）",
        "期次": 1,
        "计划回款日期": "2026-09-30",
        "日期精度": "day",
        "计划回款金额（含税）": 50000,
        "备注": "新增一条计划：操作选 CREATE，按合同节点填期次/日期/金额",
    })
    _v2_finalize(ws, V2_PLAN_HEADERS, hidden_from=13,
                 editable={1, 2, 3, 4, 5, 6, 12}, operation_col=1)


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
        "来源": "工作簿" if line.edited_source == "workbook_manual" else "WBDD",
    }


def _v2_build_parts(wb, db, project_id: str, lines) -> None:
    ws = wb.create_sheet(V2_SHEET_PARTS)
    _v2_header(ws, V2_PART_HEADERS, editable=V2_PART_EDITABLE)
    line_ids = [line.id for line, _order, _pid in lines]
    overrides = {
        item.line_id: item for item in db.scalars(select(MaintenanceManualCostOverride).where(
            MaintenanceManualCostOverride.line_id.in_(line_ids)
        ))
    } if line_ids else {}
    for line, order, _pid in lines:
        values = _v2_part_row_values(line, order, overrides.get(line.id))
        digest = _v2_hash([values[name] for name in V2_PART_HASH_COLUMNS])
        values["只读哈希"] = digest
        ws.append([values[name] for name in V2_PART_HEADERS])
    # 空白新增行（无实体ID）：用户填操作=CREATE 的新明细
    for _ in range(5):
        ws.append([""] * len(V2_PART_HEADERS))
    sample_line = lines[0] if lines else None
    _v2_append_example_row(ws, V2_PART_HEADERS, {
        "操作": "示例",
        "维保单号": (sample_line[1].order_no if sample_line else "（本项目已有需求单号）"),
        "PN": ((sample_line[0].pn_std or sample_line[0].pn_raw or "") if sample_line
               else "（标准PN）"),
        "描述": "新增一行备件",
        "需求数量": 2,
        "退货数量": 0,
        "人工未税单位成本": 88.5,
        "人工成本原因": "采购价依据",
        "备注": "新增示例：操作选 CREATE，必须挂本项目已有单号",
    })
    _v2_finalize(ws, V2_PART_HEADERS,
                 editable=V2_PART_EDITABLE, operation_col=1)
    # 仅隐藏技术列：实体ID(22)/备件主键(23)/只读哈希(24)。备注(25)/来源(26) 可见。
    for col in (22, 23, 24):
        ws.column_dimensions[ws.cell(1, col).column_letter].hidden = True


def _v2_build_expense(wb, db, project_id: str, contracts, project=None) -> None:
    ws = wb.create_sheet(V2_SHEET_EXPENSE)
    _v2_header(ws, V2_EXPENSE_HEADERS, editable={1, 4, 5, 6, 7, 8, 9, 12, 14, 17})
    expenses = ec._expenses(db, project_sales_order_nos(db, project_id))
    for expense in expenses:
        ws.append(["", expense.bxd_no or "", expense.line_no, expense.expense_date, expense.person or "",
                   expense.expense_type or "", expense.fee_category or "", expense.reason or "",
                   expense.linked_sales_order_no or "",
                   project.display_name if getattr(project, "display_name", None) else project_id,
                   "—", expense.data_status or "",
                   expense.amount, expense.tax_basis or "ex", expense.amount_ex_tax,
                   expense.amount_inc_tax, expense.remark or "", expense.raw_line_id])
    _v2_append_example_row(ws, V2_EXPENSE_HEADERS, {
        "操作": "示例",
        "费用单号": "BXD-20260901-0001",
        "明细序号": 1,
        "报销日期": "2026-09-01",
        "报销人员": "张三",
        "报销类别": "维保费用",
        "费用分类": "交通费",
        "支出事由": "现场交通",
        "维保销售订单（归集键）": "（填本项目XSDD合同号）",
        "原始报销金额": 226,
        "未税金额": 200,
        "备注": "手工新增示例：实体ID留空，费用单号+明细序号必填",
    })
    _v2_finalize(ws, V2_EXPENSE_HEADERS, hidden_from=18,
                 editable={1, 4, 5, 6, 7, 8, 9, 12, 14, 17}, operation_col=1)


def _v2_build_receipts(wb, db, project_id: str, contracts) -> None:
    ws = wb.create_sheet(V2_SHEET_RECEIPTS)
    _v2_header(ws, V2_RECEIPT_HEADERS)
    contract_by_id = {c.project_contract_id: c.contract_no for c in contracts}
    rows = list(db.scalars(select(MaintenanceCollectionSnapshot).where(
        MaintenanceCollectionSnapshot.project_id == project_id,
        MaintenanceCollectionSnapshot.status == "confirmed",
    ).order_by(MaintenanceCollectionSnapshot.report_month)))
    for row in rows:
        # 报告月份导出为 YYYY-MM（用户看到 2026-09-01 00:00:00 很怪；解析侧
        # 对 YYYY-MM 回 parse 为当月一号，往返一致）
        month_text = (row.report_month.isoformat()[:7]
                      if hasattr(row.report_month, "isoformat") else str(row.report_month or ""))
        ws.append([contract_by_id.get(row.project_contract_id, ""), month_text,
                   row.cumulative_amount, row.status, row.receipt_reference or "", row.remark or "", row.collection_id])
    _v2_append_example_row(ws, V2_RECEIPT_HEADERS, {
        "合同编号": next(iter(contract_by_id.values()), "") or "（本项目合同号）",
        "报告月份": "2026-09",
        "累计实收金额（含税）": 50000,
        "状态": "confirmed",
        "回款凭证号": "PJ-202609-001",
        "备注": "【示例】实收=每月累计快照：同一合同同月重复上传即覆盖更新，凭证号选填",
    })
    _v2_finalize(ws, V2_RECEIPT_HEADERS, hidden_from=7)


def _v2_build_site(wb, db, project_id: str) -> None:
    ws = wb.create_sheet(V2_SHEET_SITE)
    _v2_header(ws, V2_SITE_HEADERS, editable={1, 2, 3, 4, 5, 6, 7})
    rows = db.execute(select(MaintenanceSiteIssueLine, MaintenanceSiteIssue).join(
        MaintenanceSiteIssue, MaintenanceSiteIssue.issue_id == MaintenanceSiteIssueLine.issue_id
    ).where(MaintenanceSiteIssue.project_id == project_id,
            MaintenanceSiteIssueLine.is_active.is_(True)
    ).order_by(MaintenanceSiteIssue.issue_date, MaintenanceSiteIssueLine.line_no)).all()
    for line, issue in rows:
        ws.append([issue.issue_no, issue.issue_date, line.pn, line.serial_number or "", line.quantity,
                   "" if line.no_return is None else ("否" if line.no_return else "是"), line.remark or "",
                   "—", "待确认品类", "—", line.issue_line_id])
    _v2_append_example_row(ws, V2_SITE_HEADERS, {
        "领用单号": "CKD-20260901-0001",
        "领用日期": "2026-09-01",
        "PN": (rows[0][0].pn if rows else "（标准PN）"),
        "SN": "SN-001",
        "领用数量": 1,
        "是否应返还": "是",
        "备注": "【示例】手工新增领用：实体ID留空，单号/日期/PN/数量必填",
    })
    _v2_finalize(ws, V2_SITE_HEADERS, hidden_from=11,
                 editable={1, 2, 3, 4, 5, 6, 7})


def _v2_build_usage(wb) -> None:
    """00_使用说明：怎么改、怎么删、哪些能改（黄底）、防呆规则。"""
    ws = wb.create_sheet(V2_SHEET_USAGE)
    rows = [
        ("如何使用本项目总表", ""),
        ("", ""),
        ("【总原则】", "黄底单元格 = 可以修改；其他颜色/无底色 = 系统只读，改了会被拒绝并要求重新下载。"),
        ("", "每个数据行的第 1 列是「操作」下拉框：留空=更新该行 / UPDATE=更新 / VOID=作废该行 / CREATE=新增行（只用于表尾空白行）。"),
        ("", ""),
        ("【03 备件明细】", ""),
        ("改数量/退货数量", "直接改黄底单元格，回传后系统按 数量-退货数量×单价 重算金额。"),
        ("改 PN", "填新的标准 PN（必须能匹配备件主数据），回传后行会整体换绑新备件。"),
        ("新增一行", "在表尾空白行：操作列选 CREATE，填 维保单号（必须是本项目已有需求单）+ PN + 数量。"),
        ("删除一行", "两种等价方式：① 操作列选 VOID；② 直接把该行整行删掉。回传后该行作废，不再进入任何统计与导出。"),
        ("整单删除", "把该单的所有行都删掉/标 VOID，回传后整张需求单自动作废（可由管理员在系统内恢复）。"),
        ("【04 费用报销】", ""),
        ("删除一行", "直接删行或操作列选 VOID。作废的行从此不再导出、不计金额。"),
        ("新增一行", "表尾填 费用单号+明细序号+报销日期+金额+归集键（XSDD），操作列留空或 CREATE。"),
        ("【02 回款计划】", "计划=打算什么时候收多少钱：一行一个合同期次。操作选 CREATE 新增，填合同号、期次（第几期）、计划回款日期、金额；改已有行选 UPDATE（带基础版本防冲突）；作废选 VOID。"),
        ("【05 实收回款】", "实收=每月实际收到的累计数：同一合同同一月份只保留一行，报告月份填 YYYY-MM，金额填「截至该月累计实收」（不是当月增量）。同月重复上传=覆盖更新，凭证号选填。"),
        ("【06 领用返还】", "按黄底提示编辑；手工新增领用行填单号/日期/PN/数量，实体ID留空。"),
        ("删除领用行", "2026-08-23 起：直接把该行整行删掉再上传 = 该领用行作废（退出成本与返还计算）；一张单的行全删 = 整单作废。"),
        ("【灰色示例行】", "每个数据页最后一行灰色斜体是填写示例，系统上传时自动忽略，不会入库——照着它的格式填，填完可保留或删除该行。"),
        ("", ""),
        ("【安全规则】", ""),
        ("行数防呆", "上传的数据行少于导出时的一半 → 整本拒绝（防止筛选后误传删掉大片数据）。"),
        ("缺行判定", "「删行=作废」只对下载时存在的行生效；下载之后系统里新导入的行不受影响。"),
        ("原样回传", "基于「本次下载」的文件什么都不改直接传回去 = 零变更（幂等）。注意：已经应用过一次的旧文件再传，其中已作废的行会被拒绝（提示重新下载），这是正常的保护。"),
        ("改错了怎么办", "行级作废需管理员在系统内恢复；整单作废同。恢复入口：维保需求单页面 → 搜索（勾选「含已作废」）→ 恢复。"),
    ]
    for k, v in rows:
        ws.append([k, v])
    ws.column_dimensions["A"].width = 26
    ws.column_dimensions["B"].width = 100
    ws["A1"].fill = V2_TITLE
    ws["A1"].font = Font(bold=True, color="FFFFFF", size=14)
    ws.merge_cells("A1:B1")
    for r in range(1, ws.max_row + 1):
        a = ws.cell(r, 1)
        if a.value and str(a.value).startswith("【"):
            a.font = Font(bold=True)
        ws.cell(r, 2).alignment = ws.cell(r, 2).alignment.copy(wrap_text=True, vertical="top")


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


def build_project_master_v2(
    db: Session,
    *,
    project_id: str,
    sheets: tuple[str, ...] | None = None,
) -> bytes | None:
    project = db.get(MaintenanceProject, project_id)
    if project is None:
        return None
    contracts = ec._contracts(db, project_id)
    wanted = tuple(sheets or V2_ALL_SHEETS[:-2])
    unknown = [name for name in wanted if name not in V2_ALL_SHEETS[:-2]]
    if unknown:
        raise WorkbookError("unsupported_sheet", f"不支持的 V2 工作表：{'、'.join(unknown)}")
    lines = (_assigned_lines(db, project_id=project_id, window=None)
             if V2_SHEET_OVERVIEW in wanted or V2_SHEET_PARTS in wanted else [])
    wb = Workbook()
    wb.remove(wb.active)
    if V2_SHEET_OVERVIEW in wanted:
        _v2_build_overview(wb, project, contracts, db, lines)
    if V2_SHEET_PLAN in wanted:
        _v2_build_plan(wb, db, project_id, contracts)
    if V2_SHEET_PARTS in wanted:
        _v2_build_parts(wb, db, project_id, lines)
    if V2_SHEET_EXPENSE in wanted:
        _v2_build_expense(wb, db, project_id, contracts, project=project)
    if V2_SHEET_RECEIPTS in wanted:
        _v2_build_receipts(wb, db, project_id, contracts)
    if V2_SHEET_SITE in wanted:
        _v2_build_site(wb, db, project_id)
    _v2_build_dictionary(wb)
    _v2_build_usage(wb)
    meta_rows = [
        ("protocol_id", V2_PROTOCOL_ID), ("template_version", V2_TEMPLATE_VERSION),
        ("project_id", project_id), ("export_id", str(uuid4())),
        ("exported_at", datetime.now(timezone.utc).isoformat()),
        ("included_sheets", ",".join(wanted)),
    ]
    if V2_SHEET_PLAN in wanted:
        from app.models.maintenance_manager import MaintenanceCollectionMilestone

        plan_ids = db.scalars(select(MaintenanceCollectionMilestone.milestone_id).where(
            MaintenanceCollectionMilestone.project_id == project_id,
            MaintenanceCollectionMilestone.is_active.is_(True),
        )).all()
        meta_rows.append(("plan_row_ids", _encode_row_ids(plan_ids)))
    if V2_SHEET_PARTS in wanted:
        meta_rows.append(("parts_row_ids",
                          _encode_row_ids([ln.id for ln, _o, _p in lines])))
    if V2_SHEET_EXPENSE in wanted:
        meta_rows.append(("expense_row_ids",
                          _encode_row_ids(_expected_expense_ids(db, project_id))))
    if V2_SHEET_SITE in wanted:
        site_line_ids = db.scalars(
            select(MaintenanceSiteIssueLine.issue_line_id)
            .join(MaintenanceSiteIssue,
                  MaintenanceSiteIssue.issue_id == MaintenanceSiteIssueLine.issue_id)
            .where(MaintenanceSiteIssue.project_id == project_id,
                   MaintenanceSiteIssueLine.is_active.is_(True))
            .order_by(MaintenanceSiteIssue.issue_date, MaintenanceSiteIssueLine.line_no)
        ).all()
        meta_rows.append(("site_row_ids", _encode_row_ids(site_line_ids)))
    meta = wb.create_sheet(V2_SHEET_META)
    for key, value in meta_rows:
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
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S", "%Y-%m"):
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


def _cell(row, index: dict[str, int], name: str):
    return row[index[name]] if name in index and index[name] < len(row) else None


def _exact_part_for_pn(db: Session, pn: str) -> DimPart | None:
    normalized = pn.strip().upper()
    part = db.scalar(select(DimPart).where(
        DimPart.pn_std == normalized,
        DimPart.status != "merged",
    ))
    if part is not None:
        return part
    return db.scalar(
        select(DimPart)
        .join(PartAlias, PartAlias.part_id == DimPart.id)
        .where(
            PartAlias.pn_raw == pn.strip(),
            PartAlias.status == "active",
            DimPart.status != "merged",
        )
    )


def _merge_manual_cost_to_line(
    db: Session, refill: "CostRefill", *, operated_by: str,
) -> None:
    """人工成本合并到主表——写 override 表（审计/回滚）并同步 f_maintenance_line：
    unit_cost_ex_tax/inc_tax + cost_source='manual' + cost_amount 字段。
    面板/看板/概览读主表即得人工值，无需各处 merge。

    仅当主表当前无自动成本（cost_source IN (NULL,'none')）时才合并到主表——已有
    direct/window 等自动证据的行不被人工值覆盖；'manual' 行允许修正人工价。
    override 表始终写入审计。
    """
    if refill.unit_cost_ex_tax is not None:
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
        line = db.get(FMaintenanceLine, refill.line_id)
        if line is not None and line.cost_source in (None, "none"):
            _recompute_line_amounts(line, unit_cost_ex_tax=refill.unit_cost_ex_tax,
                                    unit_cost_inc_tax=refill.unit_cost_inc_tax)
            line.cost_source = "manual"
    elif refill.reason is not None:
        existing = db.execute(
            select(MaintenanceManualCostOverride)
            .where(MaintenanceManualCostOverride.line_id == refill.line_id)
        ).scalar_one_or_none()
        if existing is not None:
            existing.reason = refill.reason
            existing.version += 1
            existing.updated_by = operated_by


def _recompute_line_amounts(line: FMaintenanceLine, *,
                            unit_cost_ex_tax: Decimal | None = None,
                            unit_cost_inc_tax: Decimal | None = None) -> None:
    """数量/退货数量/单价变更后重算两套成本金额（有效数量×单价）。
    无成本的行（none/NULL）保持 NULL——不知道≠0（铁律 5）。"""
    if unit_cost_ex_tax is not None:
        line.unit_cost_ex_tax = unit_cost_ex_tax
    if unit_cost_inc_tax is not None:
        line.unit_cost_inc_tax = unit_cost_inc_tax
    effective_qty = max((line.qty or Decimal(0)) - (line.return_qty or Decimal(0)),
                        Decimal(0))
    if line.unit_cost_ex_tax is not None:
        line.cost_amount_ex_tax = (line.unit_cost_ex_tax * effective_qty).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP)
    if line.unit_cost_inc_tax is not None:
        line.cost_amount_inc_tax = (line.unit_cost_inc_tax * effective_qty).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP)


def _write_audit(db: Session, *, project_id: str, entity_type: str,
                 entity_id: str, action: str, operated_by: str,
                 reason: str, before: dict | None = None,
                 after: dict | None = None) -> None:
    """行级审计（行级即可，不做字段级全量 diff）。"""
    db.add(MaintenanceProjectOperationAudit(
        project_id=project_id, entity_type=entity_type, entity_id=str(entity_id),
        action=action, before_json=before, after_json=after,
        reason=reason or "工作簿编辑", operated_by=operated_by,
    ))


def _cascade_void_site_lines(db: Session, line: FMaintenanceLine) -> int:
    """03 行作废时，按 source_line_id 文本匹配级联作废 06 领用返还行
    （06↔03 无 FK，best-effort，匹配不到不报错）。返回级联条数。"""
    if not line.raw_line_id:
        return 0
    site_lines = db.scalars(
        select(MaintenanceSiteIssueLine)
        .join(MaintenanceSiteIssue,
              MaintenanceSiteIssue.issue_id == MaintenanceSiteIssueLine.issue_id)
        .where(MaintenanceSiteIssueLine.source_line_id == line.raw_line_id,
               MaintenanceSiteIssueLine.is_active.is_(True))
    ).all()
    for sl in site_lines:
        sl.is_active = False
    return len(site_lines)


def _v2_meta(wb) -> dict[str, str]:
    if V2_SHEET_META not in wb.sheetnames:
        raise WorkbookError("template_version_mismatch", "工作簿缺少 V2 元数据，请重新下载当前项目总表")
    return {
        str(row[0].value).strip(): str(row[1].value or "").strip()
        for row in wb[V2_SHEET_META].iter_rows(min_col=1, max_col=2)
        if row[0].value
    }


def _v2_verify_meta(db: Session, wb, project_id: str) -> dict[str, str]:
    meta = _v2_meta(wb)
    if meta.get("protocol_id") != V2_PROTOCOL_ID or meta.get("template_version") != V2_TEMPLATE_VERSION:
        raise WorkbookError("template_version_mismatch", "工作簿版本已更新，请重新下载当前项目总表后再上传。")
    if meta.get("project_id") != project_id:
        # 2026-08-23：带上工作簿真正所属的项目名——批量处理多项目时极易
        # 拿错文件，只说「不一致」用户不知道这表是谁的
        from app.models.maintenance_project import MaintenanceProject

        other = db.get(MaintenanceProject, meta.get("project_id"))
        other_name = other.display_name if other is not None else "未知项目"
        raise WorkbookError(
            "project_mismatch",
            f"这份工作簿属于项目「{other_name}」，请到该项目的面板上传；"
            f"或重新从当前项目下载后再改。")
    included = tuple(
        name.strip() for name in meta.get("included_sheets", "").split(",")
        if name.strip()
    )
    if not included:
        raise WorkbookError("template_version_mismatch", "V2 工作簿缺少 included_sheets 元数据")
    unknown = [name for name in included if name not in V2_ALL_SHEETS[:-1]]
    if unknown:
        raise WorkbookError("template_version_mismatch", "V2 工作簿 included_sheets 无效")
    missing = [name for name in included if name not in wb.sheetnames]
    if missing:
        raise WorkbookError("missing_sheet", f"V2 工作簿缺少工作表：{'、'.join(missing)}")
    return meta


def _v2_parse_parts(db: Session, project_id: str, ws) -> tuple[list[CostRefill], int, set[int]]:
    headers = [str(cell.value or "") for cell in ws[1]]
    index = {name: i for i, name in enumerate(headers)}
    required = {"操作", "维保单号", "PN", "需求数量", "人工未税单位成本",
                "人工成本原因", "实体ID", "只读哈希", "备注"}
    if not required.issubset(index):
        raise WorkbookError("template_version_mismatch",
                            "03_备件明细列定义不是当前 V2.1 版本，请重新下载当前项目总表")
    out: list[CostRefill] = []
    uploaded_entity_rows = 0
    uploaded_entity_ids: set[int] = set()
    for row_no, row in enumerate(ws.iter_rows(min_row=2, values_only=True), 2):
        if not row or all(value in (None, "") for value in row):
            continue
        if _is_example_row(row):
            continue
        raw_id = _cell(row, index, "实体ID")
        operation = str(_cell(row, index, "操作") or "").strip().upper()
        has_entity = raw_id not in (None, "")
        if has_entity:
            uploaded_entity_rows += 1
            uploaded_entity_ids.add(int(raw_id))

        if not has_entity:
            # 新增行：操作可空或 CREATE；空白占位行（单号/PN/数量全空）跳过
            order_no = str(_cell(row, index, "维保单号") or "").strip()
            pn_raw = str(_cell(row, index, "PN") or "").strip()
            qty_raw = _cell(row, index, "需求数量")
            if not order_no and not pn_raw and qty_raw in (None, ""):
                continue
            if operation and operation != "CREATE":
                raise WorkbookError("invalid_operation",
                                    f"第 {row_no} 行无实体ID，只能填 CREATE 或留空")
            if not order_no:
                raise WorkbookError("missing_field", f"第 {row_no} 行新增明细必须填写维保单号")
            # 维保单号必须已存在且归属本项目
            order = db.scalar(
                select(FMaintenanceOrder)
                .join(MaintenanceSourceOrderAssignment,
                      (MaintenanceSourceOrderAssignment.source_order_id
                       == FMaintenanceOrder.raw_order_id)
                      & MaintenanceSourceOrderAssignment.is_active.is_(True))
                .where(FMaintenanceOrder.order_no == order_no,
                       MaintenanceSourceOrderAssignment.project_id == project_id)
                .limit(1)
            )
            if order is None:
                raise WorkbookError(
                    "order_not_in_project",
                    f"第 {row_no} 行维保单号 {order_no!r} 不在本项目，"
                    "新增明细只能挂到本项目已有需求单")
            if not pn_raw:
                raise WorkbookError("missing_field", f"第 {row_no} 行新增明细必须填写 PN")
            part = _exact_part_for_pn(db, pn_raw)
            if part is None:
                raise WorkbookError("part_not_found",
                                    f"第 {row_no} 行 PN {pn_raw!r} 未匹配备件主数据")
            qty = _v2_decimal(qty_raw, row_no=row_no, label="需求数量", required=True)
            if qty is None or qty <= 0:
                raise WorkbookError("invalid_amount",
                                    f"第 {row_no} 行需求数量必须大于 0")
            return_qty = _v2_decimal(_cell(row, index, "退货数量"),
                                     row_no=row_no, label="退货数量") or Decimal(0)
            if return_qty > qty:
                raise WorkbookError("invalid_amount",
                                    f"第 {row_no} 行退货数量不能大于需求数量")
            raw_manual = _cell(row, index, "人工未税单位成本")
            amount = inc = None
            if raw_manual not in (None, ""):
                amount = _v2_decimal(raw_manual, row_no=row_no,
                                     label="人工未税单位成本", required=True)
                inc = (amount * (Decimal("1") + TAX_RATE)).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP)
            out.append(CostRefill(
                line_id=None, operation="CREATE", is_create=True,
                order_no=order_no, order_id=order.id, part_id=part.id, pn=part.pn_std,
                description=str(_cell(row, index, "描述") or "").strip() or None,
                qty=qty, return_qty=return_qty,
                serial_numbers=str(_cell(row, index, "SN") or "").strip() or None,
                unit_cost_ex_tax=amount, unit_cost_inc_tax=inc,
                reason=(str(_cell(row, index, "人工成本原因") or "").strip() or None),
                note=(str(_cell(row, index, "备注") or "").strip() or None),
            ))
            continue

        line = db.get(FMaintenanceLine, int(raw_id))
        if line is None or not line.is_active:
            raise WorkbookError("line_not_found",
                                f"第 {row_no} 行备件事实已不存在或已作废，请重新下载")
        # 行必须归属本项目（P1，Codex review #272）：防止拿别项目工作簿的
        # 实体ID（或构造行）越权 UPDATE/VOID 本项目之外的事实。
        owner_project = db.scalar(
            select(MaintenanceSourceOrderAssignment.project_id)
            .join(FMaintenanceOrder,
                  FMaintenanceOrder.id == line.order_id)
            .where(
                MaintenanceSourceOrderAssignment.source_order_id
                == FMaintenanceOrder.raw_order_id,
                MaintenanceSourceOrderAssignment.is_active.is_(True),
            )
            .limit(1)
        )
        if owner_project != project_id:
            raise WorkbookError("line_not_in_project",
                                f"第 {row_no} 行备件事实不属于本项目，请重新下载")
        # 只读事实哈希校验（只覆盖锁定列；可编辑列改动不触发）
        readonly_values = [_cell(row, index, name) for name in V2_PART_HASH_COLUMNS]
        if _v2_hash(readonly_values) != str(_cell(row, index, "只读哈希") or ""):
            raise WorkbookError(
                "readonly_cell_modified",
                f"第 {row_no} 行只读事实（单号/日期/XSDD/系统成本等）已修改或已过期，"
                "请重新下载；如需改数量/PN/备注请改黄底列")
        if operation == "VOID":
            out.append(CostRefill(line_id=line.id, operation="VOID",
                                  unit_cost_ex_tax=None, unit_cost_inc_tax=None,
                                  reason=None))
            continue
        if operation and operation != "UPDATE":
            raise WorkbookError("invalid_operation",
                                f"第 {row_no} 行操作只能是 UPDATE、VOID 或留空")

        # UPDATE：逐字段与现值比对，只有变化的字段下发——原样上传不重写、
        # 不标 workbook_manual、不重算金额、不写假审计（幂等）。
        pn = None
        part_id = None
        pn_cell = str(_cell(row, index, "PN") or "").strip()
        if pn_cell and pn_cell != (line.pn_std or line.pn_raw or ""):
            part = _exact_part_for_pn(db, pn_cell)
            if part is None:
                raise WorkbookError("part_not_found",
                                    f"第 {row_no} 行 PN {pn_cell!r} 未匹配备件主数据")
            pn = part.pn_std
            part_id = part.id
        desc_cell = str(_cell(row, index, "描述") or "").strip()
        description = desc_cell if desc_cell != (line.description or "") else None
        qty_cell = _cell(row, index, "需求数量")
        qty_parsed = _v2_decimal(qty_cell, row_no=row_no, label="需求数量") if qty_cell not in (None, "") else None
        if qty_parsed is not None and qty_parsed <= 0:
            raise WorkbookError("invalid_amount", f"第 {row_no} 行需求数量必须大于 0")
        qty = qty_parsed if qty_parsed is not None and qty_parsed != (line.qty or Decimal(0)) else None
        return_parsed = (
            _v2_decimal(_cell(row, index, "退货数量"), row_no=row_no, label="退货数量")
            if _cell(row, index, "退货数量") not in (None, "") else None)
        return_qty = (return_parsed if return_parsed is not None
                      and return_parsed != (line.return_qty or Decimal(0)) else None)
        # 用「变更后生效值」校验退货不超需求（只改退货数量时也要校验）。
        effective_qty = qty if qty is not None else (line.qty or Decimal(0))
        effective_return = return_qty if return_qty is not None else (line.return_qty or Decimal(0))
        if effective_return > effective_qty:
            raise WorkbookError("invalid_amount",
                                f"第 {row_no} 行退货数量不能大于需求数量")
        sn_cell = str(_cell(row, index, "SN") or "").strip()
        serial_numbers = sn_cell if sn_cell != (line.serial_numbers or "") else None
        note_cell = str(_cell(row, index, "备注") or "").strip()
        note = note_cell if note_cell != (line.line_note or "") else None

        # 人工成本两列导出时对已有 override 预填；与现值比对防止原样上传重写。
        override = db.scalar(
            select(MaintenanceManualCostOverride).where(
                MaintenanceManualCostOverride.line_id == line.id))
        raw_manual = _cell(row, index, "人工未税单位成本")
        amount = inc = None
        if raw_manual not in (None, ""):
            parsed_amount = _v2_decimal(raw_manual, row_no=row_no,
                                        label="人工未税单位成本", required=True)
            if override is None or parsed_amount != (override.unit_cost_ex_tax or Decimal(0)):
                amount = parsed_amount
                inc = (amount * (Decimal("1") + TAX_RATE)).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP)
        reason_cell = str(_cell(row, index, "人工成本原因") or "").strip() or None
        reason = (reason_cell if reason_cell != (override.reason if override else None)
                  else None)

        if (pn is None and description is None and qty is None
                and return_qty is None and serial_numbers is None and note is None
                and amount is None and reason is None):
            continue
        out.append(CostRefill(
            line_id=line.id, operation="UPDATE",
            pn=pn, part_id=part_id,
            description=description, qty=qty, return_qty=return_qty,
            serial_numbers=serial_numbers,
            unit_cost_ex_tax=amount, unit_cost_inc_tax=inc,
            reason=reason, note=note,
        ))
    return out, uploaded_entity_rows, uploaded_entity_ids


def _v2_parse_site(db: Session, project_id: str, ws) -> list[SiteReturnFlag]:
    headers = [str(cell.value or "") for cell in ws[1]]
    index = {name: i for i, name in enumerate(headers)}
    out: list[SiteReturnFlag] = []
    new_counts: dict[str, int] = {}
    for row_no, row in enumerate(ws.iter_rows(min_row=2, values_only=True), 2):
        if not row or all(value in (None, "") for value in row):
            continue
        if _is_example_row(row):
            continue
        raw_id = _cell(row, index, "实体ID")
        issue_no = str(_cell(row, index, "领用单号") or "").strip() or None
        issue_date = (_v2_date(_cell(row, index, "领用日期"), row_no=row_no, label="领用")
                      if _cell(row, index, "领用日期") not in (None, "") else None)
        pn = str(_cell(row, index, "PN") or "").strip() or None
        serial_number = str(_cell(row, index, "SN") or "").strip() or None
        quantity = (_v2_decimal(_cell(row, index, "领用数量"), row_no=row_no, label="领用数量")
                    if _cell(row, index, "领用数量") not in (None, "") else None)
        flag = str(_cell(row, index, "是否应返还") or "").strip()
        if flag and flag not in {"是", "否"}:
            raise WorkbookError("invalid_flag", f"第 {row_no} 行是否应返还只能填 是 / 否")
        is_create = raw_id in (None, "")
        issue_id = None
        line_no = None
        part_id = None
        if is_create:
            if not issue_no or issue_date is None or not pn or quantity is None:
                raise WorkbookError(
                    "missing_site_identity",
                    f"06_领用返还第 {row_no} 行手工新增必须填写领用单号、日期、PN 和数量",
                )
            if quantity <= 0:
                raise WorkbookError("invalid_amount", f"第 {row_no} 行领用数量必须大于 0")
            part = _exact_part_for_pn(db, pn)
            if part is None:
                raise WorkbookError(
                    "part_not_found",
                    f"06_领用返还第 {row_no} 行 PN {pn!r} 未匹配备件主数据",
                )
            part_id = part.id
            pn = part.pn_std
            document_date = re.search(r"(?:^|-)20\d{6}(?:-|$)", issue_no)
            if document_date:
                raw_document_date = document_date.group(0).strip("-")
                issue_date = datetime.strptime(raw_document_date, "%Y%m%d").date()
            identity = "|".join([project_id, issue_no, pn, serial_number or ""])
            raw_id = f"manual-site:{hashlib.sha1(identity.encode('utf-8')).hexdigest()}"
            existing_line = db.get(MaintenanceSiteIssueLine, raw_id)
            if existing_line is not None:
                is_create = False
                issue_id = existing_line.issue_id
                line_no = existing_line.line_no
                part_id = existing_line.part_id
            else:
                base = db.scalar(
                    select(func.max(MaintenanceSiteIssueLine.line_no))
                    .join(MaintenanceSiteIssue,
                          MaintenanceSiteIssue.issue_id == MaintenanceSiteIssueLine.issue_id)
                    .where(
                        MaintenanceSiteIssue.project_id == project_id,
                        MaintenanceSiteIssue.issue_no == issue_no,
                    )
                ) or 0
                new_counts[issue_no] = new_counts.get(issue_no, 0) + 1
                line_no = int(base) + new_counts[issue_no]
        else:
            raw_id = str(raw_id)
            existing_line = db.get(MaintenanceSiteIssueLine, raw_id)
            if existing_line is None:
                raise WorkbookError("line_not_found", f"第 {row_no} 行领用事实已不存在，请重新下载")
            issue = db.get(MaintenanceSiteIssue, existing_line.issue_id)
            if issue is None or issue.project_id != project_id:
                raise WorkbookError("project_mismatch", f"第 {row_no} 行领用事实不属于本项目")
            issue_id = existing_line.issue_id
            line_no = existing_line.line_no
            part_id = existing_line.part_id
        out.append(SiteReturnFlag(
            issue_line_id=str(raw_id), is_create=is_create,
            issue_id=issue_id, line_no=line_no, part_id=part_id,
            no_return=(flag == "否") if flag else None,
            issue_no=issue_no,
            issue_date=issue_date,
            pn=pn,
            serial_number=serial_number,
            quantity=quantity,
            remark=str(_cell(row, index, "备注") or "").strip() or None,
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
        if _is_example_row(row):
            continue
        operation = str(row[index["操作"]] or "").strip().upper()
        raw_entity = str(row[index["实体ID"]] or "").strip() or None
        if not operation and not raw_entity:
            continue
        if operation and operation not in {"CREATE", "UPDATE", "VOID"}:
            raise WorkbookError("invalid_operation", f"第 {row_no} 行操作必须是 CREATE、UPDATE 或 VOID")
        # 留空操作 + 已有实体行 = 与 03/04 同语义：改了才更新（diff 幂等），
        # 原样回传零变更（2026-08-20 用户三连问：改/删都要同步）。
        contract_no = str(row[index["合同编号"]] or "").strip()
        contract = _resolve_plan_contract(db, project_id, contract_no, row_no)
        sequence = int(row[index["期次"]])
        if not 1 <= sequence <= 24:
            raise WorkbookError("invalid_sequence", f"第 {row_no} 行期次必须为 1-24")
        precision = str(row[index["日期精度"]] or "day").strip()
        if precision not in {"day", "month"}:
            raise WorkbookError("invalid_date_precision", f"第 {row_no} 行日期精度只能是 day/month")
        amount = _v2_decimal(row[index["计划回款金额（含税）"]], row_no=row_no, label="计划回款金额", required=operation == "CREATE")
        planned_date = _v2_date(row[index["计划回款日期"]], row_no=row_no, label="计划回款")
        if operation not in ("VOID",) and not raw_entity and planned_date is None and amount is None:
            raise WorkbookError("incomplete_milestone", f"第 {row_no} 行计划日期和金额不能同时为空")
        note = str(row[index["备注"]] or "").strip() if "备注" in index else None
        if raw_entity:
            milestone = db.get(MaintenanceCollectionMilestone, raw_entity)
            if milestone is None or not milestone.is_active or milestone.project_id != project_id:
                raise WorkbookError("milestone_not_found",
                                    f"第 {row_no} 行回款计划已不存在或已作废，请重新下载")
            if operation == "VOID":
                out.append(V2MilestoneChange(
                    operation="VOID", contract_no=contract_no, sequence=sequence,
                    planned_date=planned_date, date_precision=precision, planned_amount=amount,
                    entity_id=raw_entity,
                    base_version=int(row[index["基础版本"]]) if row[index["基础版本"]] not in (None, "") else None))
                continue
            # 留空或 UPDATE：空单元格=保留现值；与现值 diff，未变化不下发
            # （原样回传零变更；writer 对 None 会当清空，故传生效值）。
            eff_date = planned_date if planned_date is not None else milestone.planned_date
            eff_amount = amount if amount is not None else milestone.planned_amount
            changed = (
                eff_date != milestone.planned_date
                or eff_amount != milestone.planned_amount
                or precision != milestone.date_precision
                or (note is not None and note != (milestone.follow_up_note or ""))
            )
            if not changed:
                continue
            out.append(V2MilestoneChange(
                operation="UPDATE", contract_no=contract_no, sequence=sequence,
                planned_date=eff_date, date_precision=precision,
                planned_amount=eff_amount,
                entity_id=raw_entity,
                base_version=int(row[index["基础版本"]]) if row[index["基础版本"]] not in (None, "") else None,
            ))
            continue
        if operation == "VOID":
            raise WorkbookError("invalid_operation", f"第 {row_no} 行无实体ID不能 VOID")
        out.append(V2MilestoneChange(
            operation="CREATE", contract_no=contract_no, sequence=sequence,
            planned_date=planned_date, date_precision=precision, planned_amount=amount,
            entity_id=None,
            base_version=int(row[index["基础版本"]]) if row[index["基础版本"]] not in (None, "") else None,
        ))
    return out


def _v2_parse_expenses(db: Session, project_id: str, ws) -> tuple[list[ec.ExpenseUpdate], list[str]]:
    """04 解析（V2.1）：操作列（空/CREATE/UPDATE/VOID）+ 空白实体ID 手工新增
    （27c95fa 既有语义，实体ID 空 + 费用单号/明细序号必填 → CREATE）。

    返回 (updates, void_raw_line_ids)；缺行=作废的对账在 validate 里做。
    """
    headers = [str(cell.value or "") for cell in ws[1]]
    index = {name: i for i, name in enumerate(headers)}
    if "操作" not in index:
        raise WorkbookError("template_version_mismatch",
                            "04_费用报销列定义不是当前 V2.1 版本，请重新下载当前项目总表")
    contract_nos = set(project_sales_order_nos(db, project_id))
    out: list[ec.ExpenseUpdate] = []
    voids: list[str] = []
    for row_no, row in enumerate(ws.iter_rows(min_row=2, values_only=True), 2):
        if not row or all(value in (None, "") for value in row):
            continue
        if _is_example_row(row):
            continue
        operation = str(_cell(row, index, "操作") or "").strip().upper()
        if operation and operation not in {"VOID", "UPDATE", "CREATE"}:
            raise WorkbookError("invalid_operation",
                                f"第 {row_no} 行操作只能是 VOID、UPDATE、CREATE 或留空")
        raw_id = str(_cell(row, index, "实体ID") or "").strip()
        bxd_no = str(_cell(row, index, "费用单号") or "").strip() or None
        raw_line_no = _cell(row, index, "明细序号")
        line_no = None
        if raw_line_no not in (None, ""):
            try:
                line_no = int(raw_line_no)
            except (TypeError, ValueError):
                raise WorkbookError("invalid_line_no", f"第 {row_no} 行明细序号必须是整数")
            if line_no < 1:
                raise WorkbookError("invalid_line_no", f"第 {row_no} 行明细序号必须大于 0")
        contract_no = str(_cell(row, index, "维保销售订单（归集键）") or "").strip() or None
        if contract_no not in contract_nos:
            raise WorkbookError(
                "contract_not_found",
                f"第 {row_no} 行维保销售订单 {contract_no or ''!r} 不属于本项目",
            )
        amount = _v2_decimal(_cell(row, index, "未税金额"), row_no=row_no, label="未税金额")
        inc = (amount * (Decimal("1") + TAX_RATE)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if amount is not None else None
        expense_date = (_v2_date(_cell(row, index, "报销日期"), row_no=row_no, label="报销")
                        if _cell(row, index, "报销日期") not in (None, "") else None)
        is_create = False
        expense = None
        if raw_id:
            expense = db.scalar(select(FProjectExpense).where(FProjectExpense.raw_line_id == raw_id))
            if expense is None:
                raise WorkbookError("expense_not_found", f"第 {row_no} 行报销事实已不存在，请重新下载")
            if operation == "VOID":
                voids.append(raw_id)
                continue
        else:
            if not bxd_no or line_no is None:
                raise WorkbookError(
                    "missing_expense_identity",
                    f"第 {row_no} 行手工新增报销必须填写费用单号和明细序号",
                )
            if expense_date is None:
                raise WorkbookError("missing_expense_date", f"第 {row_no} 行手工新增报销必须填写报销日期")
            if amount is None:
                raise WorkbookError("missing_amount", f"第 {row_no} 行手工新增报销必须填写未税金额")
            scope = hashlib.sha1(contract_no.encode("utf-8")).hexdigest()[:8]
            raw_id = f"{bxd_no[:40]}#{line_no}@{scope}"
            expense = db.scalar(select(FProjectExpense).where(FProjectExpense.raw_line_id == raw_id))
            is_create = expense is None

        if not is_create and expense.amount_ex_tax is not None and amount == expense.amount_ex_tax:
            amount = inc = None
        out.append(ec.ExpenseUpdate(
            raw_line_id=raw_id, is_create=is_create,
            bxd_no=bxd_no, line_no=line_no,
            expense_date=expense_date,
            person=str(_cell(row, index, "报销人员") or "").strip() or None,
            expense_type=str(_cell(row, index, "报销类别") or "").strip() or None,
            fee_category=str(_cell(row, index, "费用分类") or "").strip() or None,
            reason=str(_cell(row, index, "支出事由") or "").strip() or None,
            contract_no=contract_no,
            amount_ex_tax=amount,
            amount_inc_tax=inc,
            data_status=str(_cell(row, index, "流程状态") or "").strip() or None,
            remark=str(_cell(row, index, "备注") or "").strip() or None,
        ))
    return out, voids


def _ensure_contract_for_xsdd(db: Session, project_id: str, contract_no: str):
    """按 XSDD 取合同；台账没有则从销售表自动建（幂等复用），对齐 05 既有语义。"""
    contract = db.scalar(select(MaintenanceProjectContract).where(
        MaintenanceProjectContract.project_id == project_id,
        MaintenanceProjectContract.contract_no == contract_no,
    ))
    if contract is not None:
        return contract
    sale = db.execute(
        select(FSalesOrder.amount_ex_tax, FSalesOrder.tax_rate)
        .where(FSalesOrder.order_no == contract_no,
               FSalesOrder.amount_ex_tax.is_not(None))
        .limit(1)
    ).one_or_none()
    amount_ex_tax = Decimal(str(sale[0])) if sale and sale[0] is not None else None
    inc_tax = (amount_ex_tax * (Decimal("1") + TAX_RATE)).quantize(Decimal("0.01")) \
        if amount_ex_tax is not None else None
    project = db.get(MaintenanceProject, project_id)
    contract = MaintenanceProjectContract(
        project_contract_id=str(uuid4()),
        project_id=project_id,
        contract_id=f"xsdd-{contract_no}",
        contract_no=contract_no,
        contract_amount=amount_ex_tax,
        amount_inc_tax=inc_tax,
        contract_status="正常",
        status_mapping_state="mapped",
        status_mapping_version="workbook-v2-xsdd",
        included_in_total=True,
        effective_from=project.period_from if project else None,
        source="sales_fallback",
        version=1,
    )
    db.add(contract)
    db.flush()
    return contract


def _xsdd_contract_for_project(db: Session, project_id: str, row_no: int):
    """合同编号留空 → 按项目唯一挂靠 XSDD 自动回填（多义/缺失整本拒绝）。"""
    xsdd_rows = db.scalars(
        select(FMaintenanceOrder.linked_sales_order_no)
        .join(MaintenanceSourceOrderAssignment,
              (MaintenanceSourceOrderAssignment.source_order_id
               == FMaintenanceOrder.raw_order_id)
              & MaintenanceSourceOrderAssignment.is_active.is_(True))
        .where(
            MaintenanceSourceOrderAssignment.project_id == project_id,
            FMaintenanceOrder.linked_sales_order_no.is_not(None),
        )
        .group_by(FMaintenanceOrder.linked_sales_order_no)
    ).all()
    xsdd_nos = sorted({str(o) for o in xsdd_rows if o})
    if not xsdd_nos:
        raise WorkbookError(
            "contract_not_found",
            f"第 {row_no} 行合同编号留空，但该项目未挂靠任何 XSDD 单据，无法自动回填")
    if len(xsdd_nos) > 1:
        raise WorkbookError(
            "contract_not_found",
            f"第 {row_no} 行合同编号留空，但该项目挂靠多个 XSDD（{'、'.join(xsdd_nos)}），请明确填写")
    return _ensure_contract_for_xsdd(db, project_id, xsdd_nos[0])


def _resolve_plan_contract(db: Session, project_id: str, contract_no: str, row_no: int):
    """02 回款计划的合同解析：台账优先 → 本项目 XSDD 自动建（无台账也能填）→
    明确报错并列出可用合同（2026-08-20 用户踩坑：台账未导入时全部被拒）。"""
    ledger = {c.contract_no: c for c in ec._contracts(db, project_id)}
    if contract_no in ledger:
        return ledger[contract_no]
    xsdd = set(project_sales_order_nos(db, project_id))
    if contract_no in xsdd:
        return _ensure_contract_for_xsdd(db, project_id, contract_no)
    available = sorted(set(ledger) | xsdd)
    raise WorkbookError(
        "contract_not_found",
        f"第 {row_no} 行合同编号 {contract_no or ''!r} 不属于本项目"
        + (f"（可用：{'、'.join(available[:8])}）" if available else "（本项目还没有合同，请先填挂靠单据上的 XSDD 号）"))


def _v2_parse_receipts(db: Session, project_id: str, ws) -> list[ec.CollectionOp]:
    contracts = {c.contract_no: c for c in ec._contracts(db, project_id)}
    headers = [str(cell.value or "") for cell in ws[1]]
    index = {name: i for i, name in enumerate(headers)}
    out: list[ec.CollectionOp] = []
    for row_no, row in enumerate(ws.iter_rows(min_row=2, values_only=True), 2):
        if not row or all(value in (None, "") for value in row):
            continue
        if _is_example_row(row):
            continue
        contract_no = str(row[index["合同编号"]] or "").strip()
        contract = contracts.get(contract_no)
        if contract is None:
            # 合同编号留空 → 按项目唯一挂靠 XSDD 自动回填（多义/缺失整本拒绝）
            contract = _xsdd_contract_for_project(db, project_id, row_no)
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


def _encode_row_ids(ids) -> str:
    """导出时行ID全集（P1，Codex review #272）：缺行=作废只允许命中
    「导出时存在、上传时消失」的行；导出之后新导入的行天然豁免，
    应用后原样重传（幂等重试）也不受影响。"""
    return ",".join(str(i) for i in sorted(ids))


def _decode_row_ids(value: str | None) -> set[str]:
    if not value:
        return set()
    return {part for part in value.split(",") if part}


def _expected_expense_ids(db: Session, project_id: str) -> list[str]:
    # 与 _v2_build_expense 同口径（含挂靠 XSDD），保证缺行=作废的对账集合一致
    return [e.raw_line_id for e in ec._expenses(db, project_sales_order_nos(db, project_id))]


# 上传数据行（带实体ID）低于导出行数的该比例 → 疑似筛选/复制粘贴事故，整本拒绝。


def validate_project_master_v2(db: Session, *, project_id: str, data: bytes) -> MasterV2Plan:
    try:
        wb = load_workbook(io.BytesIO(data), data_only=True)
    except Exception as exc:
        raise WorkbookError("invalid_file", f"无法读取 .xlsx：{type(exc).__name__}") from exc
    meta = _v2_verify_meta(db, wb, project_id)
    included_meta = tuple(
        name.strip() for name in meta["included_sheets"].split(",") if name.strip()
    )
    included = tuple(name for name in included_meta if name in V2_ALL_SHEETS[:-2])

    cost_refills: tuple[CostRefill, ...] = ()
    site_flags: tuple[SiteReturnFlag, ...] = ()
    expense_updates: list[ec.ExpenseUpdate] = []
    expense_voids: list[str] = []
    receipt_ops: tuple[ec.CollectionOp, ...] = ()
    milestone_changes: tuple[V2MilestoneChange, ...] = ()
    will_void_rows: list[dict] = []
    uploaded_line_rows = 0
    uploaded_line_ids: set[int] = set()
    if V2_SHEET_PARTS in included:
        parsed_refills, uploaded_line_rows, uploaded_line_ids = _v2_parse_parts(
            db, project_id, wb[V2_SHEET_PARTS])
        cost_refills = tuple(parsed_refills)
    if V2_SHEET_SITE in included:
        site_flags = list(_v2_parse_site(db, project_id, wb[V2_SHEET_SITE]))
        # 2026-08-23：06 缺行=作废（用户口径：Excel 删行覆盖上传，没有的默认作废）
        export_site_ids = _decode_row_ids(meta.get("site_row_ids"))
        uploaded_site_ids = {f.issue_line_id for f in site_flags if not f.is_create}
        missing_site_ids = [sid for sid in export_site_ids
                            if sid not in uploaded_site_ids]
        for sid in missing_site_ids:
            line_row = db.get(MaintenanceSiteIssueLine, sid)
            if line_row is not None and line_row.is_active:
                site_flags.append(SiteReturnFlag(
                    issue_line_id=sid, no_return=None, is_void=True))
        site_flags = tuple(site_flags)
    if V2_SHEET_EXPENSE in included:
        expense_updates, expense_voids = _v2_parse_expenses(
            db, project_id, wb[V2_SHEET_EXPENSE])
    if V2_SHEET_RECEIPTS in included:
        receipt_ops = tuple(_v2_parse_receipts(db, project_id, wb[V2_SHEET_RECEIPTS]))
    if V2_SHEET_PLAN in included:
        milestone_changes = tuple(_v2_parse_plan(db, project_id, wb[V2_SHEET_PLAN]))
        # 02 缺行=作废（2026-08-20 用户三连问）：只命中导出时存在的里程碑
        export_plan_ids = _decode_row_ids(meta.get("plan_row_ids"))
        voided_plan_ids = {c.entity_id for c in milestone_changes if c.operation == "VOID"}
        extra_voids = []
        for mid in export_plan_ids:
            if mid in voided_plan_ids:
                continue
            found = any(
                c.entity_id == mid and c.operation != "CREATE"
                for c in milestone_changes
            )
            if not found:
                extra_voids.append(mid)
        if extra_voids:
            from app.models.maintenance_manager import MaintenanceCollectionMilestone

            for mid in extra_voids:
                ms = db.get(MaintenanceCollectionMilestone, mid)
                contract_no = next(
                    (c.contract_no for c in ec._contracts(db, project_id)
                     if c.project_contract_id == ms.project_contract_id), "")
                milestone_changes = milestone_changes + (V2MilestoneChange(
                    operation="VOID", contract_no=contract_no, sequence=ms.sequence,
                    planned_date=None, date_precision="day", planned_amount=None,
                    entity_id=mid, base_version=None),)
                will_void_rows.append({"sheet": "02_回款计划", "entity_id": mid,
                                       "label": f"第{ms.sequence}期", "reason": "上传文件缺行"})

    # ---- 缺行=作废（04，#264 契约）+ 行数骤减防呆（03/04 共用一道） ----
    for refill in cost_refills:
        if refill.operation == "VOID":
            will_void_rows.append({"sheet": "03_备件明细", "entity_id": str(refill.line_id),
                                   "label": f"{refill.order_no or ''}"})

    # 03 缺行=作废（用户 2026-08-20 拍板，与 04 同语义）：只命中「导出时存在、
    # 上传时消失」的行；导出后新导入的行不在导出全集里，天然豁免。
    if V2_SHEET_PARTS in included:
        export_parts_ids = _decode_row_ids(meta.get("parts_row_ids"))
        voided_line_ids = {r.line_id for r in cost_refills if r.operation == "VOID"}
        for line_id_str in export_parts_ids - {str(i) for i in uploaded_line_ids}:
            line_id = int(line_id_str)
            if line_id in voided_line_ids:
                continue
            cost_refills = cost_refills + (CostRefill(
                line_id=line_id, operation="VOID",
                unit_cost_ex_tax=None, unit_cost_inc_tax=None, reason=None),)
            will_void_rows.append({"sheet": "03_备件明细", "entity_id": line_id_str,
                                   "label": "", "reason": "上传文件缺行"})

    if V2_SHEET_EXPENSE in included:
        uploaded_expense_ids = ({u.raw_line_id for u in expense_updates}
                                | set(expense_voids))
        # 缺行=作废只针对导出时存在的行（P1，Codex review #272）：
        # 导出后新导入的行不在导出全集里，天然豁免误杀。
        export_expense_ids = _decode_row_ids(meta.get("expense_row_ids"))
        missing_expense_ids = [rid for rid in export_expense_ids
                               if rid not in uploaded_expense_ids]
        for rid in missing_expense_ids:
            expense_voids.append(rid)
            will_void_rows.append({"sheet": "04_费用报销", "entity_id": rid, "label": ""})
        # 2026-08-22 用户拍板：撤销行损失防呆（原 50% 批量损失拦截）——
        # 线上进入大批量作废期，需要能全量增删改。缺行=作废的语义与审计不变；
        # 误传风险由前端的 will_void_rows 确认弹窗兜底。

    if V2_SHEET_PARTS in included:
        export_parts_ids = _decode_row_ids(meta.get("parts_row_ids"))

    return MasterV2Plan(
        project_id=project_id,
        sheets=included,
        cost_refills=cost_refills,
        site_flags=site_flags,
        expense_updates=tuple(expense_updates),
        receipt_ops=receipt_ops,
        milestone_changes=milestone_changes,
        expense_voids=tuple(expense_voids),
        will_void_rows=tuple(will_void_rows),
    )


def apply_project_master_v2(db: Session, plan: MasterV2Plan, *, operated_by: str, import_batch_id: str) -> dict:
    # All mutations deliberately happen on this one Session transaction.
    audit_reason = f"项目总表应用 {import_batch_id[:8]}"
    # 手工新增行需要一个 import_batch（NOT NULL FK）
    manual_batch: SysImportBatch | None = None
    if any(r.is_create for r in plan.cost_refills):
        manual_batch = SysImportBatch(
            filename="manual-maintenance-line-workbook.xlsx",
            file_type="maintenance",
            file_hash=hashlib.sha256(
                f"manual-line:{import_batch_id}".encode("utf-8")).hexdigest(),
            uploaded_by=operated_by,
            rows_total=sum(r.is_create for r in plan.cost_refills),
            rows_inserted=sum(r.is_create for r in plan.cost_refills),
            status="success",
            report_json={"source": "workbook_manual_create",
                         "project_id": plan.project_id},
        )
        db.add(manual_batch)
        db.flush()

    for refill in plan.cost_refills:
        if refill.is_create:
            order = db.get(FMaintenanceOrder, refill.order_id)
            base_line_no = db.scalar(
                select(func.coalesce(func.max(FMaintenanceLine.line_no), 0))
                .where(FMaintenanceLine.order_id == refill.order_id)) or 0
            new_line = FMaintenanceLine(
                raw_line_id=f"manual-line:{uuid4()}",
                order_id=refill.order_id,
                line_no=int(base_line_no) + 1,
                part_id=refill.part_id,
                pn_std=refill.pn, pn_raw=refill.pn,
                description=refill.description,
                qty=refill.qty, return_qty=refill.return_qty or Decimal(0),
                serial_numbers=refill.serial_numbers,
                line_note=refill.note,
                edited_source="workbook_manual",
                is_active=True,
                import_batch_id=manual_batch.id,
            )
            if refill.unit_cost_ex_tax is not None:
                new_line.cost_source = "manual"
                new_line.cost_tax_basis = "ex"
                _recompute_line_amounts(new_line, unit_cost_ex_tax=refill.unit_cost_ex_tax,
                                        unit_cost_inc_tax=refill.unit_cost_inc_tax)
            db.add(new_line)
            db.flush()
            if refill.unit_cost_ex_tax is not None:
                db.add(MaintenanceManualCostOverride(
                    line_id=new_line.id,
                    unit_cost_ex_tax=refill.unit_cost_ex_tax,
                    unit_cost_inc_tax=refill.unit_cost_inc_tax,
                    reason=refill.reason, active=True, version=1,
                    updated_by=operated_by))
            _write_audit(db, project_id=plan.project_id,
                         entity_type="maintenance_line", entity_id=new_line.id,
                         action="CREATE", operated_by=operated_by, reason=audit_reason,
                         after={"pn": refill.pn, "qty": str(refill.qty),
                                "order_no": refill.order_no})
            continue

        line = db.get(FMaintenanceLine, refill.line_id)
        if line is None:
            raise WorkbookError("line_not_found",
                                f"备件行 {refill.line_id} 已不存在，请重新下载")
        if refill.operation == "VOID":
            if line.is_active:
                line.is_active = False
                line.voided_at = datetime.now(timezone.utc)
                line.voided_by = operated_by
                line.void_reason = audit_reason
                cascaded = _cascade_void_site_lines(db, line)
                _write_audit(db, project_id=plan.project_id,
                             entity_type="maintenance_line", entity_id=line.id,
                             action="VOID", operated_by=operated_by, reason=audit_reason,
                             after={"cascaded_site_lines": cascaded})
            continue

        # UPDATE：行级数据字段
        before: dict[str, object] = {}
        if refill.pn is not None:
            before["pn"] = line.pn_std or line.pn_raw
            line.pn_std = refill.pn
            line.pn_raw = refill.pn
            # 跨表身份同步换 part_id（P1，Codex review #272）：库存/成本/池
            # 等 join 都走 part_id，只改文本不改主键会腐化下游聚合。
            if refill.part_id is not None:
                before["part_id"] = line.part_id
                line.part_id = refill.part_id
        if refill.description is not None:
            before["description"] = line.description
            line.description = refill.description
        if refill.qty is not None:
            before["qty"] = str(line.qty) if line.qty is not None else None
            line.qty = refill.qty
        if refill.return_qty is not None:
            before["return_qty"] = str(line.return_qty) if line.return_qty is not None else None
            line.return_qty = refill.return_qty
        if refill.serial_numbers is not None:
            before["sn"] = line.serial_numbers
            line.serial_numbers = refill.serial_numbers
        if refill.note is not None:
            before["note"] = line.line_note
            line.line_note = refill.note
        # 数量类字段有变化 → 重算金额（无成本行保持 NULL）
        if "qty" in before or "return_qty" in before:
            _recompute_line_amounts(line)
        # 人工成本（写 override + 合并主表，沿用既有语义）
        cost_changed = False
        if refill.unit_cost_ex_tax is not None or refill.reason is not None:
            _merge_manual_cost_to_line(db, refill, operated_by=operated_by)
            cost_changed = True
        line.edited_source = "workbook_manual"
        if before or cost_changed:
            after = {"pn": line.pn_std, "qty": str(line.qty),
                     "return_qty": str(line.return_qty)}
            if refill.unit_cost_ex_tax is not None:
                after["cost"] = str(refill.unit_cost_ex_tax)
            if refill.reason is not None:
                after["cost_reason"] = refill.reason
            _write_audit(db, project_id=plan.project_id,
                         entity_type="maintenance_line", entity_id=line.id,
                         action="UPDATE", operated_by=operated_by, reason=audit_reason,
                         before=before or None, after=after)
    # 行级作废的整单级联：被 VOID 的行所属需求单若活动行归零 → 整单墓碑
    # （需求单搜索消失、氚云重传不复活；恢复走 demands 页面既有入口）。
    db.flush()  # Session autoflush=False：先把 is_active=False 刷库，级联计数才准
    voided_order_ids = {
        line.order_id
        for line in (
            db.get(FMaintenanceLine, r.line_id)
            for r in plan.cost_refills if r.operation == "VOID"
        )
        if line is not None
    }
    if voided_order_ids:
        from app.services import maintenance_demands as _demands

        raw_ids = db.scalars(
            select(FMaintenanceOrder.raw_order_id).where(
                FMaintenanceOrder.id.in_(voided_order_ids))
        ).all()
        cascaded = _demands.cascade_tombstone_orders(
            db, source_order_ids=list(raw_ids),
            operated_by=operated_by,
            reason=f"{audit_reason}（行全部作废级联）",
        )
        if cascaded:
            audit_after = {"cascaded_orders": cascaded}
            _write_audit(db, project_id=plan.project_id,
                         entity_type="maintenance_order", entity_id=",".join(cascaded),
                         action="VOID", operated_by=operated_by, reason=audit_reason,
                         after=audit_after)
    for flag in plan.site_flags:
        line = db.get(MaintenanceSiteIssueLine, flag.issue_line_id)
        # 2026-08-23：缺行=作废——领用行软作废，退出成本与返还义务计算；
        # 一张单全部行都作废时，单据状态同步置 void（与系统内作废同观感）
        if flag.is_void:
            if line is not None and line.is_active:
                line.is_active = False
                db.flush()
                issue_doc = db.get(MaintenanceSiteIssue, line.issue_id)
                if issue_doc is not None:
                    remaining = db.scalar(
                        select(func.count()).select_from(MaintenanceSiteIssueLine).where(
                            MaintenanceSiteIssueLine.issue_id == issue_doc.issue_id,
                            MaintenanceSiteIssueLine.is_active.is_(True)))
                    if not remaining:
                        issue_doc.normalized_status = "void"
                        issue_doc.version += 1
                        db.flush()
                _write_audit(db, project_id=plan.project_id,
                             entity_type="site_issue_line", entity_id=line.issue_line_id,
                             action="VOID", operated_by=operated_by, reason=audit_reason,
                             after={"issue_no": (db.get(MaintenanceSiteIssue, line.issue_id).issue_no
                                                 if db.get(MaintenanceSiteIssue, line.issue_id) else None)})
            continue
        if line is None and flag.is_create:
            issue = db.scalar(select(MaintenanceSiteIssue).where(
                MaintenanceSiteIssue.project_id == plan.project_id,
                MaintenanceSiteIssue.issue_no == flag.issue_no,
            ))
            if issue is None:
                issue = MaintenanceSiteIssue(
                    issue_id=str(uuid4()),
                    project_id=plan.project_id,
                    issue_no=flag.issue_no,
                    issue_date=flag.issue_date,
                    raw_status="已确认",
                    status_mapping_state="mapped",
                    normalized_status="confirmed",
                    status_mapping_version="workbook-manual-v1",
                    source="workbook",
                    import_batch_id=import_batch_id,
                    created_by=operated_by,
                    version=1,
                )
                db.add(issue)
                db.flush()
            line = MaintenanceSiteIssueLine(
                issue_line_id=flag.issue_line_id,
                issue_id=issue.issue_id,
                line_no=flag.line_no,
                part_id=flag.part_id,
                pn=flag.pn,
                quantity=flag.quantity,
                serial_number=flag.serial_number,
                remark=flag.remark,
                no_return=flag.no_return,
                is_active=True,
                algorithm_version="workbook-manual-v1",
            )
            db.add(line)
            continue
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
    # 04 作废（显式 VOID + 缺行=作废）：软删标记，读侧从此不导出（#264 契约）。
    for raw_line_id in plan.expense_voids:
        expense = db.scalar(select(FProjectExpense).where(FProjectExpense.raw_line_id == raw_line_id))
        if expense is None or expense.data_status == "已作废":
            continue
        expense.data_status = "已作废"
        _write_audit(db, project_id=plan.project_id,
                     entity_type="project_expense", entity_id=raw_line_id,
                     action="VOID", operated_by=operated_by, reason=audit_reason,
                     before={"data_status": None})
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
            _write_audit(db, project_id=plan.project_id,
                         entity_type="collection_milestone",
                         entity_id=existing.milestone_id, action="VOID",
                         operated_by=operated_by, reason=audit_reason,
                         after={"contract_no": change.contract_no,
                                "sequence": change.sequence})
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
            "will_void_rows": [dict(r) for r in plan.will_void_rows],
            "warnings": []}

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
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.services import maintenance_expense_collection_workbook as ec
from app.services.maintenance_expense_collection_workbook import (
    TAX_RATE,
    WorkbookError,
)

PROTOCOL_VERSION = "project-master-v1"

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

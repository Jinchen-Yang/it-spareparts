"""报销/回款往返工作簿（增补包 AB-3，F3 改判并入 v1）。

一个 .xlsx、两张 sheet，**下载 → 本地改 → 上传覆盖**：

- ``04_报销订单``：黄底可编辑「未税金额」；正式金额列 = ``amount_inc_tax``
  （REQUIREMENTS #8），由未税 × (1+税率) 系统算出，不接受人工直填含税。
- ``05_项目经理回款单``：**月度累计快照**（REQUIREMENTS #30），表尾追加；
  黄底可编辑「操作 / 报告月份 / 累计回款金额 / 回款凭证号 / 备注」。
  操作 CREATE=新增或覆盖该合同该月快照；VOID=作废历史快照（缺行 ≠ 删除）。

v1 明确不做（AB-3 裁剪）：氚云 BXD 逐条对账、凭证附件、进老板看板三槽。
回款**计划**不在本表——唯一事实源是台账 ``02_回款计划``（REQUIREMENTS #31），
本表只记已收到的累计额，不重复录计划。

与冻结的「项目工作簿 v3 导出」的关系：两张 sheet 的列定义原样取自 v3 模板
（`maintenance_project_workbook_v3.py`），但本模块是**独立的两表工作簿**，
不导出 v3 的其余四表，因此不解冻 v3。
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from uuid import uuid4

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.business_time import business_today
from app.models.maintenance import FProjectExpense
from app.models.maintenance_project import (
    MaintenanceProject,
    MaintenanceProjectContract,
)
from app.models.maintenance_project_operations import MaintenanceCollectionSnapshot

PROTOCOL_VERSION = "expense-collection-v1"
SHEET_EXPENSE = "04_报销订单"
SHEET_COLLECTION = "05_项目经理回款单"
_META_SHEET = "99_元数据"

# 与 f_project_expense 的 CHECK 约束一致（税率固定 13%，双口径保留原值）
TAX_RATE = Decimal("0.13")

_EXPENSE_HEADERS = ["报销单号", "报销日期", "报销人员", "报销类别", "费用分类",
                    "支出事由", "合同编号", "未税金额", "含税金额(系统计算)",
                    "流程状态"]
_COLLECTION_HEADERS = ["操作", "合同编号", "报告月份", "累计回款金额(含税)",
                       "回款凭证号", "状态(系统)", "备注"]

_EDITABLE = PatternFill("solid", fgColor="FFF3C4")   # 黄底=可编辑
_READONLY = PatternFill("solid", fgColor="EFEFEF")


class WorkbookError(ValueError):
    """回传给 API 层映射 422 的用户可读校验错误。"""

    def __init__(self, code: str, message: str, issues: list[dict] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.issues = issues or []


@dataclass(frozen=True)
class ExpenseUpdate:
    raw_line_id: str
    amount_ex_tax: Decimal
    amount_inc_tax: Decimal


@dataclass(frozen=True)
class CollectionOp:
    operation: str                       # CREATE | VOID
    project_contract_id: str
    contract_no: str
    report_month: date
    cumulative_amount: Decimal | None    # VOID 时为 None
    receipt_reference: str | None
    remark: str | None


@dataclass(frozen=True)
class WorkbookPlan:
    project_id: str
    expense_updates: tuple[ExpenseUpdate, ...]
    collection_ops: tuple[CollectionOp, ...]

    @property
    def summary(self) -> dict:
        creates = sum(1 for op in self.collection_ops if op.operation == "CREATE")
        return {
            "expense_updates": len(self.expense_updates),
            "collection_creates": creates,
            "collection_voids": len(self.collection_ops) - creates,
        }


# ------------------------------------------------------------------ 导出

def _contracts(db: Session, project_id: str) -> list[MaintenanceProjectContract]:
    return list(db.execute(
        select(MaintenanceProjectContract)
        .where(MaintenanceProjectContract.project_id == project_id)
        .order_by(MaintenanceProjectContract.effective_from)
    ).scalars())


def _expenses(db: Session, contract_nos: list[str]) -> list[FProjectExpense]:
    """报销行经 XSDD/合同编号归集到项目（与 v3 同源，口径不另立）。"""
    if not contract_nos:
        return []
    return list(db.execute(
        select(FProjectExpense)
        .where(FProjectExpense.linked_sales_order_no.in_(contract_nos))
        .order_by(FProjectExpense.expense_date, FProjectExpense.raw_line_id)
    ).scalars())


def _latest_snapshots(db: Session, project_id: str
                      ) -> dict[str, MaintenanceCollectionSnapshot]:
    """每份合同取最新 confirmed 快照；未来月份不进导出（与 v3 同口径）。"""
    rows = db.execute(
        select(MaintenanceCollectionSnapshot)
        .where(MaintenanceCollectionSnapshot.project_id == project_id,
               MaintenanceCollectionSnapshot.status == "confirmed",
               MaintenanceCollectionSnapshot.report_month <= business_today())
        .order_by(MaintenanceCollectionSnapshot.report_month)
    ).scalars()
    latest: dict[str, MaintenanceCollectionSnapshot] = {}
    for snapshot in rows:
        latest[snapshot.project_contract_id] = snapshot
    return latest


def _style_header(ws, headers: list[str], fills: list[PatternFill]) -> None:
    ws.append(headers)
    for idx, fill in enumerate(fills, start=1):
        cell = ws.cell(row=1, column=idx)
        cell.fill = fill
        cell.font = Font(bold=True)


def build_workbook(db: Session, *, project_id: str) -> bytes | None:
    """导出两表工作簿；项目不存在返回 None（由 API 映射 404）。"""
    project = db.get(MaintenanceProject, project_id)
    if project is None:
        return None
    contracts = _contracts(db, project_id)
    contract_no_by_id = {c.project_contract_id: c.contract_no for c in contracts}
    expenses = _expenses(db, [c.contract_no for c in contracts])
    latest = _latest_snapshots(db, project_id)

    wb = Workbook()
    wb.remove(wb.active)

    ws = wb.create_sheet(SHEET_EXPENSE)
    _style_header(ws, _EXPENSE_HEADERS,
                  [_READONLY] * 7 + [_EDITABLE, _READONLY, _READONLY])
    for expense in expenses:
        ws.append([
            expense.bxd_no or "",
            expense.expense_date.isoformat() if expense.expense_date else "",
            expense.person or "",
            expense.expense_type or "",
            expense.fee_category or "",
            (expense.reason or "")[:120],
            expense.linked_sales_order_no or "",
            float(expense.amount_ex_tax) if expense.amount_ex_tax is not None else "",
            float(expense.amount_inc_tax) if expense.amount_inc_tax is not None else "",
            expense.data_status or "",
        ])
        # 行标识写进隐藏元数据列，避免用可编辑列做主键（改了单号就对不上行）
        ws.cell(row=ws.max_row, column=len(_EXPENSE_HEADERS) + 1,
                value=expense.raw_line_id)
    ws.column_dimensions[ws.cell(row=1, column=len(_EXPENSE_HEADERS) + 1)
                         .column_letter].hidden = True

    ws = wb.create_sheet(SHEET_COLLECTION)
    _style_header(ws, _COLLECTION_HEADERS,
                  [_EDITABLE, _READONLY, _EDITABLE, _EDITABLE, _EDITABLE,
                   _READONLY, _EDITABLE])
    for snapshot in latest.values():
        ws.append([
            "",                                    # 操作留空=本行不动
            contract_no_by_id.get(snapshot.project_contract_id, ""),
            snapshot.report_month.strftime("%Y-%m"),
            float(snapshot.cumulative_amount),
            snapshot.receipt_reference or "",
            snapshot.status,
            snapshot.remark or "",
        ])
    # 表尾留空行供追加新月份（月度累计快照按月追加，不是改历史行）
    for _ in range(8):
        ws.append([""] * len(_COLLECTION_HEADERS))

    meta = wb.create_sheet(_META_SHEET)
    meta.append(["协议版本", PROTOCOL_VERSION])
    meta.append(["项目ID", project_id])
    meta.append(["导出ID", str(uuid4())])
    meta.append(["导出时间", datetime.now(timezone.utc).isoformat()])
    meta.append(["口径", "正式金额=含税(系统按未税×1.13 计算)；回款=月度累计快照"])
    meta.sheet_state = "hidden"

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


# ------------------------------------------------------------------ 解析

def _text(value) -> str:
    return "" if value is None else str(value).strip()


def _decimal(value, *, label: str, row_no: int) -> Decimal:
    try:
        parsed = Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, AttributeError, ValueError):
        raise WorkbookError("invalid_amount", f"第 {row_no} 行{label}不是合法数字")
    if parsed < 0:
        raise WorkbookError("invalid_amount", f"第 {row_no} 行{label}不能为负")
    return parsed.quantize(Decimal("0.01"))


def _month(value, *, row_no: int) -> date:
    raw = _text(value)
    if isinstance(value, datetime):
        return date(value.year, value.month, 1)
    if isinstance(value, date):
        return date(value.year, value.month, 1)
    for fmt in ("%Y-%m", "%Y/%m", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        return date(parsed.year, parsed.month, 1)
    raise WorkbookError("invalid_month",
                        f"第 {row_no} 行报告月份必须是 YYYY-MM（收到 {raw!r}）")


def validate(db: Session, *, project_id: str, data: bytes) -> WorkbookPlan:
    """解析工作簿并产出**无副作用**的应用计划；任何一行不合法即整份拒绝。

    整份拒绝而非跳过：这是一份人工编辑的表，静默丢掉一行意味着操作者以为改了、
    实际没改——比报错难发现得多。
    """
    try:
        wb = load_workbook(io.BytesIO(data), data_only=True)
    except Exception as exc:                                    # noqa: BLE001
        raise WorkbookError("invalid_file",
                            f"无法读取 .xlsx：{type(exc).__name__}") from exc
    missing = [name for name in (SHEET_EXPENSE, SHEET_COLLECTION)
               if name not in wb.sheetnames]
    if missing:
        raise WorkbookError("missing_sheet", f"缺少工作表：{'、'.join(missing)}")

    expense_updates = _parse_expenses(db, wb[SHEET_EXPENSE], project_id=project_id)
    collection_ops = _parse_collections(db, wb[SHEET_COLLECTION],
                                        project_id=project_id)
    return WorkbookPlan(project_id=project_id,
                        expense_updates=tuple(expense_updates),
                        collection_ops=tuple(collection_ops))


def _parse_expenses(db: Session, ws, *, project_id: str) -> list[ExpenseUpdate]:
    id_col = len(_EXPENSE_HEADERS) + 1
    updates: list[ExpenseUpdate] = []
    for row_no, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if not row or all(_text(v) == "" for v in row):
            continue
        raw_line_id = _text(row[id_col - 1]) if len(row) >= id_col else ""
        if not raw_line_id:
            # 报销行只能改金额，不能凭空新增：新报销单走氚云导入（铁律 1）
            raise WorkbookError(
                "expense_row_not_recognized",
                f"第 {row_no} 行不是导出的报销行——报销单只能在源系统新增，"
                "本表只改未税金额")
        expense = db.execute(
            select(FProjectExpense)
            .where(FProjectExpense.raw_line_id == raw_line_id)
        ).scalar_one_or_none()
        if expense is None:
            raise WorkbookError("expense_not_found",
                                f"第 {row_no} 行的报销行已不存在，请重新下载")
        raw_amount = row[7] if len(row) > 7 else None
        if _text(raw_amount) == "":
            continue                                   # 未填=不改这一行
        ex_tax = _decimal(raw_amount, label="未税金额", row_no=row_no)
        if expense.amount_ex_tax is not None and ex_tax == expense.amount_ex_tax:
            continue                                   # 无变化不写库
        updates.append(ExpenseUpdate(
            raw_line_id=raw_line_id,
            amount_ex_tax=ex_tax,
            # 正式金额列由系统算，不接受人工直填含税（REQUIREMENTS #8）
            # 与 CHECK 里的 round(amount_ex_tax * 1.13, 2) 同形：Postgres 的
            # round 是四舍五入，Decimal 默认 ROUND_HALF_EVEN 会在 .005 上差一分。
            amount_inc_tax=(ex_tax * (Decimal("1") + TAX_RATE))
            .quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        ))
    return updates


def _parse_collections(db: Session, ws, *, project_id: str) -> list[CollectionOp]:
    contracts = {c.contract_no: c for c in _contracts(db, project_id)}
    ops: list[CollectionOp] = []
    seen: set[tuple[str, date]] = set()
    for row_no, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if not row or all(_text(v) == "" for v in row):
            continue
        operation = _text(row[0]).upper()
        if operation == "":
            continue                                   # 操作留空=本行不动
        if operation not in ("CREATE", "VOID"):
            raise WorkbookError("invalid_operation",
                                f"第 {row_no} 行操作必须是 CREATE 或 VOID")
        contract_no = _text(row[1])
        contract = contracts.get(contract_no)
        if contract is None:
            raise WorkbookError(
                "contract_not_found",
                f"第 {row_no} 行合同编号 {contract_no!r} 不属于本项目")
        report_month = _month(row[2] if len(row) > 2 else None, row_no=row_no)
        key = (contract.project_contract_id, report_month)
        if key in seen:
            raise WorkbookError(
                "duplicate_month",
                f"第 {row_no} 行与前面重复：同一合同同一月份只能有一条累计快照")
        seen.add(key)
        amount = None
        if operation == "CREATE":
            if _text(row[3] if len(row) > 3 else None) == "":
                raise WorkbookError("missing_amount",
                                    f"第 {row_no} 行 CREATE 必须填累计回款金额")
            amount = _decimal(row[3], label="累计回款金额", row_no=row_no)
        ops.append(CollectionOp(
            operation=operation,
            project_contract_id=contract.project_contract_id,
            contract_no=contract_no,
            report_month=report_month,
            cumulative_amount=amount,
            receipt_reference=_text(row[4] if len(row) > 4 else None) or None,
            remark=_text(row[6] if len(row) > 6 else None) or None,
        ))
    return ops


# ------------------------------------------------------------------ 应用

def apply(db: Session, plan: WorkbookPlan, *, operated_by: str,
          import_batch_id: str) -> dict:
    """整份事务应用。上传即覆盖——同合同同月份的 CREATE 覆盖既有累计额。"""
    for update in plan.expense_updates:
        expense = db.execute(
            select(FProjectExpense)
            .where(FProjectExpense.raw_line_id == update.raw_line_id)
        ).scalar_one()
        # 三个字段必须一起改：f_project_expense 有两条 CHECK 锁死
        # 「amount = 该 basis 对应的那一列」与「含税 = round(未税×1.13, 2)」。
        # amount 保留操作者实际填入的原值（这里就是未税），审计可追。
        expense.amount = update.amount_ex_tax
        expense.amount_ex_tax = update.amount_ex_tax
        expense.amount_inc_tax = update.amount_inc_tax
        expense.tax_basis = "ex"            # 人工填的是未税，含税为系统计算值

    for op in plan.collection_ops:
        existing = db.execute(
            select(MaintenanceCollectionSnapshot)
            .where(MaintenanceCollectionSnapshot.project_contract_id
                   == op.project_contract_id,
                   MaintenanceCollectionSnapshot.report_month == op.report_month)
        ).scalar_one_or_none()
        if op.operation == "VOID":
            if existing is None:
                raise WorkbookError(
                    "void_target_missing",
                    f"{op.contract_no} {op.report_month:%Y-%m} 没有可作废的快照")
            existing.status = "void"
            existing.version += 1
            continue
        if existing is None:
            db.add(MaintenanceCollectionSnapshot(
                collection_id=str(uuid4()),
                project_id=plan.project_id,
                project_contract_id=op.project_contract_id,
                report_month=op.report_month,
                cumulative_amount=op.cumulative_amount,
                status="confirmed",
                receipt_reference=op.receipt_reference,
                remark=op.remark,
                source="workbook",
                import_batch_id=import_batch_id,
                version=1,
            ))
        else:
            existing.cumulative_amount = op.cumulative_amount
            existing.status = "confirmed"
            existing.receipt_reference = op.receipt_reference
            existing.remark = op.remark
            existing.source = "workbook"
            existing.import_batch_id = import_batch_id
            existing.version += 1
    db.commit()
    return {"applied_by": operated_by, "import_batch_id": import_batch_id,
            **plan.summary}

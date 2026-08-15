"""维保台账工作簿：解析（preview）与 apply（B2）。

台账 = 商务线唯一事实源。支持两种结构：
- 旧结构（业务现行文件）：`维保项目清单`（16 固定列 + 24 组横向「回款时间N/回款金额」对）
  + `项目成本`；
- 新模板 v1（docs/maintenance/templates/）：`01_项目与合同` + `02_回款计划` + `03_项目成本`。

preview 只解析落 raw 行（零 canonical 写入）；apply 才同步 project / contract /
milestone。报销归集行仅保留 raw（与氚云 BXD 对账后再进 canonical，见 C4）。
"""
from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from uuid import uuid4

from openpyxl import load_workbook
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.business_time import business_today
from app.models.maintenance_ledger import (
    LEDGER_SOURCE,
    LEDGER_TEMPLATE_SOURCE,
    MaintenanceLedgerContractRow,
    MaintenanceLedgerExpenseRow,
    MaintenanceLedgerImportBatch,
    MaintenanceLedgerPlanRow,
)
from app.models.maintenance_manager import MaintenanceCollectionMilestone
from app.models.maintenance_project import (
    MaintenanceProject,
    MaintenanceProjectAuditLog,
    MaintenanceProjectContract,
)
from app.models.sales import FSalesOrder
from app.services.date_loose import (
    parse_amount_loose,
    parse_date_loose,
    parse_project_name,
)

MAX_PREVIEW_BYTES = 16 * 1024 * 1024
MAX_PROJECT_CODE_LEN = 64

_CONTRACT_HEADERS = [
    "订单编号", "订单日期", "销售人员", "业务类型", "项目名称",
    "维保起始日期", "维保终止日期", "CMO", "项目经理", "订单金额",
    "已收尾款", "待收尾款", "验收材料", "验收材料是否完成及上传附件",
    "巡检时间", "巡检是否完成及上传附件",
]
_EXPENSE_HEADERS = [
    "费用单号", "报销人员", "报销类别", "支出事由", "维保销售订单",
    "项目名称", "销售订单", "销售人员", "费用分类", "报销金额", "备注",
]
_PLAN_HEADERS = ["订单编号", "计划期次", "计划回款时间", "计划回款金额"]

_XSDD_RE = re.compile(r"(XSDD-\d{8}-\d{4})")
_BXD_RE = re.compile(r"(BXD-\d{8}-\d{4})")


class LedgerParseError(RuntimeError):
    """台账文件不可解析（结构/大小/加密等）。"""


class LedgerBatchError(RuntimeError):
    """台账批次状态错误（重复 apply / 不存在等）。"""


@dataclass
class ContractRowData:
    row_no: int
    values: dict[str, str]
    issues: list[str] = field(default_factory=list)


@dataclass
class PlanRowData:
    row_no: int
    order_no_raw: str
    sequence: int
    time_raw: str | None
    amount_raw: str | None
    issues: list[str] = field(default_factory=list)


@dataclass
class ExpenseRowData:
    row_no: int
    values: dict[str, str]
    issues: list[str] = field(default_factory=list)


def _header_index(headers: list, name: str) -> int | None:
    for idx, value in enumerate(headers, 1):
        if value == name:
            return idx
    return None


def _cell(row: tuple, index: int | None) -> str | None:
    if index is None or index > len(row):
        return None
    value = row[index - 1]
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clean_order_no(raw: str | None) -> str | None:
    if not raw:
        return None
    match = _XSDD_RE.search(raw)
    return match.group(1) if match else None


def _clean_bxd_no(raw: str | None) -> str | None:
    if not raw:
        return None
    match = _BXD_RE.search(raw)
    return match.group(1) if match else None


def _non_empty_row(row: tuple) -> bool:
    return bool(row) and any(v is not None and str(v).strip() != "" for v in row)


def _parse_old_ledger(workbook):
    try:
        contract_sheet = workbook["维保项目清单"]
    except KeyError as exc:
        raise LedgerParseError("缺少「维保项目清单」Sheet") from exc

    rows = list(contract_sheet.iter_rows(values_only=True))
    if not rows:
        raise LedgerParseError("「维保项目清单」为空")
    headers = [str(v).strip() if v is not None else "" for v in rows[0]]
    indexes = {name: _header_index(headers, name) for name in _CONTRACT_HEADERS}
    if indexes["订单编号"] is None:
        raise LedgerParseError("「维保项目清单」缺少「订单编号」列")

    time_cols: list[tuple[int, int]] = []
    amount_cols: list[int] = []
    for idx, name in enumerate(headers, 1):
        match = re.fullmatch(r"回款时间(\d{1,2})", name)
        if match:
            time_cols.append((int(match.group(1)), idx))
        elif name == "回款金额":
            amount_cols.append(idx)
    time_cols.sort()
    pairs = list(zip(time_cols, amount_cols))[:24]

    contract_rows: list[ContractRowData] = []
    plan_rows: list[PlanRowData] = []
    for row_no, row in enumerate(rows[1:], 2):
        if not _non_empty_row(row):
            continue
        values = {name: _cell(row, indexes[name]) for name in _CONTRACT_HEADERS}
        issues: list[str] = []
        order_no = _clean_order_no(values["订单编号"])
        if not order_no:
            issues.append("订单编号缺失或格式异常")
        for (seq, time_idx), amount_idx in pairs:
            time_raw = _cell(row, time_idx)
            amount_raw = _cell(row, amount_idx)
            if not time_raw and not amount_raw:
                continue
            plan_rows.append(
                PlanRowData(
                    row_no=row_no,
                    order_no_raw=order_no or "",
                    sequence=seq,
                    time_raw=time_raw,
                    amount_raw=amount_raw,
                    issues=list(issues),
                )
            )
        contract_rows.append(ContractRowData(row_no=row_no, values=values, issues=issues))

    expense_rows: list[ExpenseRowData] = []
    if "项目成本" in workbook.sheetnames:
        expense_rows.extend(_parse_expense_sheet(workbook["项目成本"]))
    return contract_rows, plan_rows, expense_rows


def _parse_expense_sheet(sheet) -> list[ExpenseRowData]:
    rows = list(sheet.iter_rows(values_only=True))
    if not rows:
        return []
    headers = [str(v).strip() if v is not None else "" for v in rows[0]]
    indexes = {name: _header_index(headers, name) for name in _EXPENSE_HEADERS}
    result: list[ExpenseRowData] = []
    for row_no, row in enumerate(rows[1:], 2):
        if not _non_empty_row(row):
            continue
        values = {name: _cell(row, indexes[name]) for name in _EXPENSE_HEADERS}
        issues: list[str] = []
        if not values["费用单号"] and not values["维保销售订单"]:
            issues.append("费用单号与维保销售订单均缺失")
        result.append(ExpenseRowData(row_no=row_no, values=values, issues=issues))
    return result


def _parse_new_ledger(workbook):
    try:
        contract_sheet = workbook["01_项目与合同"]
    except KeyError as exc:
        raise LedgerParseError("缺少「01_项目与合同」Sheet") from exc

    rows = list(contract_sheet.iter_rows(values_only=True))
    if not rows:
        raise LedgerParseError("「01_项目与合同」为空")
    headers = [str(v).strip() if v is not None else "" for v in rows[0]]
    indexes = {name: _header_index(headers, name) for name in _CONTRACT_HEADERS}
    if indexes["订单编号"] is None:
        raise LedgerParseError("「01_项目与合同」缺少「订单编号」列")

    contract_rows: list[ContractRowData] = []
    for row_no, row in enumerate(rows[1:], 2):
        if not _non_empty_row(row):
            continue
        values = {name: _cell(row, indexes[name]) for name in _CONTRACT_HEADERS}
        issues: list[str] = []
        if not _clean_order_no(values["订单编号"]):
            issues.append("订单编号缺失或格式异常")
        contract_rows.append(ContractRowData(row_no=row_no, values=values, issues=issues))

    plan_rows: list[PlanRowData] = []
    if "02_回款计划" in workbook.sheetnames:
        plan_data = list(workbook["02_回款计划"].iter_rows(values_only=True))
        if plan_data:
            plan_headers = [str(v).strip() if v is not None else "" for v in plan_data[0]]
            plan_indexes = {name: _header_index(plan_headers, name) for name in _PLAN_HEADERS}
            for row_no, row in enumerate(plan_data[1:], 2):
                if not _non_empty_row(row):
                    continue
                order_no_raw = _cell(row, plan_indexes["订单编号"]) or ""
                seq_raw = _cell(row, plan_indexes["计划期次"])
                time_raw = _cell(row, plan_indexes["计划回款时间"])
                amount_raw = _cell(row, plan_indexes["计划回款金额"])
                try:
                    sequence = int(str(seq_raw).strip()) if seq_raw else 0
                except ValueError:
                    sequence = 0
                issues: list[str] = []
                if not _clean_order_no(order_no_raw):
                    issues.append("订单编号缺失或格式异常")
                if not 1 <= sequence <= 24:
                    issues.append("计划期次无效（应为 1-24）")
                plan_rows.append(
                    PlanRowData(
                        row_no=row_no,
                        order_no_raw=order_no_raw,
                        sequence=sequence,
                        time_raw=time_raw,
                        amount_raw=amount_raw,
                        issues=issues,
                    )
                )

    expense_rows: list[ExpenseRowData] = []
    if "03_项目成本" in workbook.sheetnames:
        expense_rows.extend(_parse_expense_sheet(workbook["03_项目成本"]))
    return contract_rows, plan_rows, expense_rows


def parse_ledger_workbook(data: bytes, filename: str) -> dict:
    """解析台账工作簿字节流。返回 {source_kind, file_hash, contract_rows, plan_rows, expense_rows}。"""
    if len(data) > MAX_PREVIEW_BYTES:
        raise LedgerParseError("台账文件超过大小上限")
    try:
        workbook = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    except Exception as exc:  # noqa: BLE001 - 文件损坏时统一业务错误
        raise LedgerParseError(f"无法读取 Excel 文件：{type(exc).__name__}") from exc
    try:
        sheet_names = set(workbook.sheetnames)
        if "维保项目清单" in sheet_names:
            source_kind = LEDGER_SOURCE
            contract_rows, plan_rows, expense_rows = _parse_old_ledger(workbook)
        elif "01_项目与合同" in sheet_names:
            source_kind = LEDGER_TEMPLATE_SOURCE
            contract_rows, plan_rows, expense_rows = _parse_new_ledger(workbook)
        else:
            raise LedgerParseError(
                "无法识别台账结构：需要「维保项目清单」或「01_项目与合同」Sheet"
            )
    finally:
        workbook.close()
    return {
        "source_kind": source_kind,
        "file_hash": hashlib.sha256(data).hexdigest(),
        "filename": filename,
        "contract_rows": contract_rows,
        "plan_rows": plan_rows,
        "expense_rows": expense_rows,
    }


def store_preview(db: Session, parsed: dict, operated_by: str) -> str:
    """落 raw 行并返回 batch_id。零 canonical 写入。"""
    batch = MaintenanceLedgerImportBatch(
        batch_id=str(uuid4()),
        file_hash=parsed["file_hash"],
        filename=parsed["filename"][:255],
        source_kind=parsed["source_kind"],
        uploaded_by=operated_by,
        contract_rows=len(parsed["contract_rows"]),
        plan_rows=len(parsed["plan_rows"]),
        expense_rows=len(parsed["expense_rows"]),
        status="pending",
    )
    db.add(batch)
    db.flush()
    issue_rows = 0
    for data in parsed["contract_rows"]:
        order_date, _ = parse_date_loose(data.values["订单日期"])
        start, _ = parse_date_loose(data.values["维保起始日期"])
        end, _ = parse_date_loose(data.values["维保终止日期"])
        parsed_project = parse_project_name(data.values["项目名称"] or "")
        if start is None:
            start = parsed_project["period_from"]
        if end is None:
            end = parsed_project["period_to"]
        if not data.values["项目名称"]:
            data.issues.append("项目名称缺失")
        if data.values["维保起始日期"] and start is None:
            data.issues.append("维保起始日期无法解析")
        if data.values["订单金额"] and parse_amount_loose(data.values["订单金额"]) is None:
            data.issues.append("订单金额无法解析")
        if start is None and end is None:
            data.issues.append("维保期限缺失（台账日期与项目名称内均未找到）")
        if data.issues:
            issue_rows += 1
        db.add(
            MaintenanceLedgerContractRow(
                row_id=str(uuid4()),
                batch_id=batch.batch_id,
                row_no=data.row_no,
                order_no_raw=data.values["订单编号"],
                order_date_raw=data.values["订单日期"],
                salesperson_raw=data.values["销售人员"],
                business_type_raw=data.values["业务类型"],
                project_name_raw=data.values["项目名称"],
                maint_start_raw=data.values["维保起始日期"],
                maint_end_raw=data.values["维保终止日期"],
                cmo_raw=data.values["CMO"],
                manager_raw=data.values["项目经理"],
                amount_raw=data.values["订单金额"],
                collected_raw=data.values["已收尾款"],
                receivable_raw=data.values["待收尾款"],
                acceptance_material_raw=data.values["验收材料"],
                acceptance_done_raw=data.values["验收材料是否完成及上传附件"],
                inspection_time_raw=data.values["巡检时间"],
                inspection_done_raw=data.values["巡检是否完成及上传附件"],
                order_no=_clean_order_no(data.values["订单编号"]),
                order_date=order_date,
                business_type=data.values["业务类型"] or None,
                project_name=parsed_project["identity"] or None,
                project_period_from=start,
                project_period_to=end,
                cmo=data.values["CMO"] or None,
                manager=data.values["项目经理"] or None,
                amount_inc_tax=parse_amount_loose(data.values["订单金额"]),
                collected_amount=parse_amount_loose(data.values["已收尾款"]),
                receivable_amount=parse_amount_loose(data.values["待收尾款"]),
                issues=data.issues,
            )
        )
    for data in parsed["plan_rows"]:
        if data.issues:
            issue_rows += 1
        planned_date, precision = parse_date_loose(data.time_raw)
        if data.time_raw and planned_date is None:
            data.issues.append("计划回款时间无法解析")
        db.add(
            MaintenanceLedgerPlanRow(
                row_id=str(uuid4()),
                batch_id=batch.batch_id,
                row_no=data.row_no,
                order_no_raw=data.order_no_raw,
                sequence=data.sequence if 1 <= data.sequence <= 24 else 0,
                time_raw=data.time_raw,
                amount_raw=data.amount_raw,
                order_no=_clean_order_no(data.order_no_raw),
                planned_date=planned_date,
                date_precision=precision,
                planned_amount=parse_amount_loose(data.amount_raw),
                issues=data.issues,
            )
        )
    for data in parsed["expense_rows"]:
        if data.issues:
            issue_rows += 1
        db.add(
            MaintenanceLedgerExpenseRow(
                row_id=str(uuid4()),
                batch_id=batch.batch_id,
                row_no=data.row_no,
                bxd_no_raw=data.values["费用单号"],
                person_raw=data.values["报销人员"],
                expense_type_raw=data.values["报销类别"],
                reason_raw=data.values["支出事由"],
                sales_order_raw=data.values["维保销售订单"],
                project_name_raw=data.values["项目名称"],
                sales_order_dup_raw=data.values["销售订单"],
                salesperson_raw=data.values["销售人员"],
                fee_category_raw=data.values["费用分类"],
                amount_raw=data.values["报销金额"],
                remark_raw=data.values["备注"],
                bxd_no=_clean_bxd_no(data.values["费用单号"]),
                sales_order=_clean_order_no(data.values["维保销售订单"]),
                amount=parse_amount_loose(data.values["报销金额"]),
                issues=data.issues,
            )
        )
    batch.issue_rows = issue_rows
    batch.report_json = {
        "contract_rows": batch.contract_rows,
        "plan_rows": batch.plan_rows,
        "expense_rows": batch.expense_rows,
        "issue_rows": issue_rows,
    }
    db.commit()
    return batch.batch_id


def _lifecycle_status(period_from: date | None, period_to: date | None, today: date) -> str:
    if period_from is None and period_to is None:
        return "missing"
    if period_to is not None and period_to < today:
        return "ended"
    if period_from is not None and period_from <= today and (
        period_to is None or today <= period_to
    ):
        return "ongoing"
    return "missing"


def _upsert_project(
    db: Session,
    row: MaintenanceLedgerContractRow,
    operated_by: str,
    summary: dict,
    today: date,
) -> MaintenanceProject:
    code = (row.project_name or row.project_name_raw or "未命名项目")[:MAX_PROJECT_CODE_LEN]
    project = db.execute(
        select(MaintenanceProject).where(MaintenanceProject.project_code == code)
    ).scalar_one_or_none()
    lifecycle = _lifecycle_status(row.project_period_from, row.project_period_to, today)
    display_name = row.project_name_raw or code
    if project is None:
        project = MaintenanceProject(
            project_id=str(uuid4()),
            project_code=code,
            display_name=display_name,
            project_manager_id=(row.manager or "")[:64] or None,
            business_type=row.business_type or None,
            cmo_name=(row.cmo or "")[:128] or None,
            salesperson=(row.salesperson_raw or "")[:64] or None,
            lifecycle_status=lifecycle,
            is_active=True,
            version=1,
        )
        db.add(project)
        db.flush()
        db.add(
            MaintenanceProjectAuditLog(
                project_id=project.project_id,
                entity_type="project",
                entity_id=project.project_id,
                action="ledger_create",
                after_json={"display_name": display_name, "code": code},
                reason="台账导入创建项目",
                operated_by=operated_by,
            )
        )
        summary["projects_created"] += 1
        return project
    changed = False
    before = {
        "display_name": project.display_name,
        "lifecycle_status": project.lifecycle_status,
        "business_type": project.business_type,
        "cmo_name": project.cmo_name,
        "salesperson": project.salesperson,
    }
    after = dict(before)
    if project.display_name != display_name:
        project.display_name = display_name
        after["display_name"] = display_name
        changed = True
    if project.lifecycle_status == "missing" and lifecycle != "missing":
        project.lifecycle_status = lifecycle
        after["lifecycle_status"] = lifecycle
        changed = True
    if row.business_type and project.business_type != row.business_type:
        project.business_type = row.business_type
        after["business_type"] = row.business_type
        changed = True
    if row.cmo and project.cmo_name != row.cmo[:128]:
        project.cmo_name = row.cmo[:128]
        after["cmo_name"] = row.cmo[:128]
        changed = True
    if row.salesperson_raw and project.salesperson != row.salesperson_raw[:64]:
        project.salesperson = row.salesperson_raw[:64]
        after["salesperson"] = row.salesperson_raw[:64]
        changed = True
    if changed:
        project.version += 1
        db.add(
            MaintenanceProjectAuditLog(
                project_id=project.project_id,
                entity_type="project",
                entity_id=project.project_id,
                action="ledger_update",
                before_json=before,
                after_json=after,
                reason="台账导入更新项目",
                operated_by=operated_by,
            )
        )
        summary["projects_updated"] += 1
    return project


def _upsert_contract(
    db: Session,
    row: MaintenanceLedgerContractRow,
    project: MaintenanceProject,
    operated_by: str,
    summary: dict,
) -> MaintenanceProjectContract | None:
    effective_from = row.project_period_from or row.order_date
    sales_order = db.execute(
        select(FSalesOrder).where(FSalesOrder.order_no == row.order_no)
    ).scalar_one_or_none()
    if effective_from is None and sales_order is not None:
        effective_from = sales_order.order_date
    if effective_from is None:
        summary["skipped_rows"] += 1
        return None
    contract = db.execute(
        select(MaintenanceProjectContract).where(
            MaintenanceProjectContract.project_id == project.project_id,
            MaintenanceProjectContract.contract_no == row.order_no,
        )
    ).scalar_one_or_none()
    if contract is None:
        contract = MaintenanceProjectContract(
            project_contract_id=str(uuid4()),
            project_id=project.project_id,
            contract_id=row.order_no,
            contract_no=row.order_no,
            contract_amount=(sales_order.amount_ex_tax if sales_order is not None else None),
            amount_inc_tax=row.amount_inc_tax,
            contract_status=None,
            status_mapping_state="mapped",
            status_mapping_version=LEDGER_SOURCE,
            included_in_total=True,
            effective_from=effective_from,
            effective_to=row.project_period_to,
            source=LEDGER_SOURCE,
            version=1,
        )
        db.add(contract)
        db.flush()
        db.add(
            MaintenanceProjectAuditLog(
                project_id=project.project_id,
                entity_type="project_contract",
                entity_id=contract.contract_no,
                action="ledger_create",
                after_json={
                    "contract_no": row.order_no,
                    "amount_inc_tax": (
                        str(row.amount_inc_tax) if row.amount_inc_tax is not None else None
                    ),
                    "effective_from": str(effective_from),
                },
                reason="台账导入创建合同",
                operated_by=operated_by,
            )
        )
        summary["contracts_created"] += 1
        return contract
    changed = False
    if row.amount_inc_tax is not None and contract.amount_inc_tax != row.amount_inc_tax:
        contract.amount_inc_tax = row.amount_inc_tax
        changed = True
    if sales_order is not None and contract.contract_amount != sales_order.amount_ex_tax:
        contract.contract_amount = sales_order.amount_ex_tax
        changed = True
    if contract.effective_to != row.project_period_to:
        contract.effective_to = row.project_period_to
        changed = True
    if changed:
        contract.version += 1
        summary["contracts_updated"] += 1
    return contract


def _upsert_milestone(
    db: Session,
    row: MaintenanceLedgerPlanRow,
    contract: MaintenanceProjectContract,
    ledger_batch_id: str,
    summary: dict,
) -> None:
    milestone = db.execute(
        select(MaintenanceCollectionMilestone).where(
            MaintenanceCollectionMilestone.project_contract_id
            == contract.project_contract_id,
            MaintenanceCollectionMilestone.sequence == row.sequence,
        )
    ).scalar_one_or_none()
    if row.planned_date is None and row.planned_amount is None:
        return
    completeness = "complete"
    if row.planned_date is None:
        completeness = "amount_only"
    elif row.planned_amount is None:
        completeness = "date_only"
    precision = row.date_precision if row.date_precision in ("day", "month") else "day"
    if milestone is None:
        db.add(
            MaintenanceCollectionMilestone(
                milestone_id=str(uuid4()),
                project_id=contract.project_id,
                project_contract_id=contract.project_contract_id,
                sequence=row.sequence,
                planned_date=row.planned_date,
                planned_amount=row.planned_amount,
                completeness_state=completeness,
                source=LEDGER_SOURCE,
                ledger_batch_id=ledger_batch_id,
                date_precision=precision,
                follow_up_status="pending",
                follow_up_review_required=False,
            )
        )
        summary["milestones_created"] += 1
        return
    changed = False
    if milestone.planned_date != row.planned_date:
        milestone.planned_date = row.planned_date
        changed = True
    if milestone.planned_amount != row.planned_amount:
        milestone.planned_amount = row.planned_amount
        changed = True
    if milestone.completeness_state != completeness:
        milestone.completeness_state = completeness
        changed = True
    if milestone.date_precision != precision:
        milestone.date_precision = precision
        changed = True
    if changed:
        milestone.version += 1
        summary["milestones_updated"] += 1


def apply_batch(db: Session, batch_id: str, operated_by: str) -> dict:
    """把 raw 行同步进 canonical 表。幂等：同值不重复写；变更字段更新并升版本。"""
    batch = db.get(MaintenanceLedgerImportBatch, batch_id)
    if batch is None:
        raise LedgerBatchError("台账批次不存在")
    if batch.status == "applied":
        raise LedgerBatchError("台账批次已应用，不能重复应用")
    today = business_today()
    summary = {
        "projects_created": 0,
        "projects_updated": 0,
        "contracts_created": 0,
        "contracts_updated": 0,
        "milestones_created": 0,
        "milestones_updated": 0,
        "skipped_rows": 0,
    }
    contract_rows = (
        db.execute(
            select(MaintenanceLedgerContractRow)
            .where(MaintenanceLedgerContractRow.batch_id == batch_id)
            .order_by(MaintenanceLedgerContractRow.row_no)
        )
        .scalars()
        .all()
    )
    plan_rows = (
        db.execute(
            select(MaintenanceLedgerPlanRow)
            .where(MaintenanceLedgerPlanRow.batch_id == batch_id)
            .order_by(MaintenanceLedgerPlanRow.row_no)
        )
        .scalars()
        .all()
    )
    contract_by_order: dict[str, MaintenanceProjectContract] = {}
    for row in contract_rows:
        if row.order_no is None:
            summary["skipped_rows"] += 1
            continue
        project = _upsert_project(db, row, operated_by, summary, today)
        contract = _upsert_contract(db, row, project, operated_by, summary)
        if contract is not None:
            contract_by_order[row.order_no] = contract
    for row in plan_rows:
        contract = contract_by_order.get(row.order_no)
        if contract is None or not 1 <= row.sequence <= 24:
            summary["skipped_rows"] += 1
            continue
        _upsert_milestone(db, row, contract, batch_id, summary)
    batch.status = "applied"
    batch.applied_by = operated_by
    batch.applied_at = datetime.now(timezone.utc)
    db.commit()
    return summary

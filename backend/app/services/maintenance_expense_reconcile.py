"""报销对账只读视图（C4，round-5 修复版）：台账归集 vs 氚云 BXD raw vs 正式费用事实。

口径（import-field-contract §168-176,261-280 + Codex round-5 Blocker 7）：
- 正式计入金额列与审批完成原值待业务确认：确认前**不输出 matched/mismatch
  经营结论**，只输出三源证据与 ``amounts_aligned``（原始比对事实）；
  多源齐备但口径未定的行 status = ``unresolved``（附 unresolved_basis 说明）；
- 单源存在时输出 *_only 证据行（无结论）；
- 同单号多行先聚合成单号级金额再比较；任一金额缺失 → 该源金额为 null，
  不按 0 计（缺金额不伪造对齐）；
- 有效事实过滤：台账=已应用批次且无 issue、BXD=已应用批次且头/明细无 issue、
  正式费用=生效口径 + 成功导入批次（sys_import_batch.status='success'）；
- 每条带来源计数与批次引用（可下钻审计）；limit/offset 分页。
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import MAINT_EXPENSE_ACTIVE_STATUS
from app.models.maintenance import FProjectExpense
from app.models.maintenance_doc_import import (
    MaintenanceDocHeadRow,
    MaintenanceDocImportBatch,
    MaintenanceDocLineRow,
)
from app.models.maintenance_ledger import (
    MaintenanceLedgerExpenseRow,
    MaintenanceLedgerImportBatch,
)
from app.models.system import SysImportBatch


def _qty(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _status(ledger, bxd, formal) -> str:
    present = sum(1 for value in (ledger, bxd, formal) if value is not None)
    if present <= 1:
        if ledger is not None:
            return "ledger_only"
        if bxd is not None:
            return "bxd_only"
        return "formal_only"
    return "unresolved"


def expense_reconcile_rows(
    db: Session, *, limit: int | None = None, offset: int = 0
) -> list[dict]:
    """三源全集逐费用单号对账（证据视角，不出经营结论）。"""
    # 台账：已应用批次、无 issue 行，按单号聚合；任一金额缺失 → 该源金额 null
    ledger_facts: dict[str, dict] = defaultdict(
        lambda: {"amount": None, "has_null": False, "row_count": 0, "batch_ids": set()}
    )
    ledger_rows = db.execute(
        select(
            MaintenanceLedgerExpenseRow.bxd_no,
            MaintenanceLedgerExpenseRow.amount,
            MaintenanceLedgerExpenseRow.batch_id,
            MaintenanceLedgerExpenseRow.project_name_raw,
            func.cardinality(MaintenanceLedgerExpenseRow.issues),
        )
        .join(
            MaintenanceLedgerImportBatch,
            MaintenanceLedgerImportBatch.batch_id
            == MaintenanceLedgerExpenseRow.batch_id,
        )
        .where(
            MaintenanceLedgerExpenseRow.bxd_no.is_not(None),
            MaintenanceLedgerImportBatch.status == "applied",
        )
    ).all()
    ledger_projects: dict[str, str] = {}
    for bxd_no, amount, batch_id, project_name, issue_count in ledger_rows:
        if issue_count:
            continue
        facts = ledger_facts[bxd_no]
        facts["row_count"] += 1
        facts["batch_ids"].add(batch_id)
        if amount is None:
            facts["has_null"] = True
        elif not facts["has_null"]:
            facts["amount"] = (facts["amount"] or Decimal("0")) + amount
        if project_name:
            ledger_projects[bxd_no] = project_name

    # BXD：已应用批次、头/明细均无 issue，按单号聚合明细金额（原值口径）
    bxd_facts: dict[str, dict] = defaultdict(
        lambda: {"amount": None, "has_null": False, "line_count": 0, "batch_ids": set()}
    )
    bxd_rows = db.execute(
        select(
            MaintenanceDocHeadRow.head_no,
            MaintenanceDocLineRow.amount,
            MaintenanceDocHeadRow.batch_id,
            func.cardinality(MaintenanceDocHeadRow.issues),
            func.cardinality(MaintenanceDocLineRow.issues),
        )
        .join(
            MaintenanceDocLineRow,
            MaintenanceDocLineRow.head_row_id == MaintenanceDocHeadRow.row_id,
        )
        .join(
            MaintenanceDocImportBatch,
            MaintenanceDocImportBatch.batch_id == MaintenanceDocHeadRow.batch_id,
        )
        .where(
            MaintenanceDocImportBatch.doc_type == "bxd_expense",
            MaintenanceDocImportBatch.status == "applied",
            MaintenanceDocHeadRow.head_no.is_not(None),
        )
    ).all()
    for head_no, amount, batch_id, head_issues, line_issues in bxd_rows:
        if head_issues or line_issues:
            continue
        facts = bxd_facts[head_no]
        facts["line_count"] += 1
        facts["batch_ids"].add(batch_id)
        if amount is None:
            facts["has_null"] = True
        elif not facts["has_null"]:
            facts["amount"] = (facts["amount"] or Decimal("0")) + amount

    # 正式费用：生效口径 + 成功导入批次，含税计算值；缺金额不按 0
    formal_facts: dict[str, dict] = defaultdict(
        lambda: {"amount": None, "has_null": False, "row_count": 0}
    )
    formal_rows = db.execute(
        select(
            FProjectExpense.bxd_no,
            FProjectExpense.amount_inc_tax,
            SysImportBatch.status,
        )
        .join(
            SysImportBatch, SysImportBatch.id == FProjectExpense.import_batch_id
        )
        .where(
            FProjectExpense.bxd_no.is_not(None),
            FProjectExpense.data_status == MAINT_EXPENSE_ACTIVE_STATUS,
        )
    ).all()
    for bxd_no, amount_inc_tax, batch_status in formal_rows:
        if batch_status != "success":
            continue
        facts = formal_facts[bxd_no]
        facts["row_count"] += 1
        if amount_inc_tax is None:
            facts["has_null"] = True
        elif not facts["has_null"]:
            facts["amount"] = (facts["amount"] or Decimal("0")) + amount_inc_tax

    keys = sorted(set(ledger_facts) | set(bxd_facts) | set(formal_facts))
    if offset:
        keys = keys[offset:]
    if limit is not None:
        keys = keys[:limit]
    result: list[dict] = []
    for bxd_no in keys:
        ledger_fact = ledger_facts.get(bxd_no)
        bxd_fact = bxd_facts.get(bxd_no)
        formal_fact = formal_facts.get(bxd_no)
        ledger_amount = (
            None if ledger_fact is None or ledger_fact["has_null"]
            else ledger_fact["amount"]
        )
        bxd_amount = (
            None if bxd_fact is None or bxd_fact["has_null"]
            else bxd_fact["amount"]
        )
        formal_amount = (
            None if formal_fact is None or formal_fact["has_null"]
            else formal_fact["amount"]
        )
        present = [
            value for value in (ledger_amount, bxd_amount, formal_amount)
            if value is not None
        ]
        aligned = len(present) >= 2 and all(
            value == present[0] for value in present[1:]
        )
        result.append(
            {
                "bxd_no": bxd_no,
                "status": _status(ledger_amount, bxd_amount, formal_amount),
                "amounts_aligned": aligned,
                "unresolved_basis": (
                    "正式计入金额列与审批原值待业务确认；"
                    "多源金额齐备前不输出对账结论"
                    if _status(ledger_amount, bxd_amount, formal_amount)
                    == "unresolved"
                    else None
                ),
                # 口径：台账=含税归集、BXD=氚云原值、正式=含税计算值
                "ledger_amount": _qty(ledger_amount),
                "bxd_amount": _qty(bxd_amount),
                "formal_amount": _qty(formal_amount),
                "ledger_row_count": ledger_fact["row_count"] if ledger_fact else 0,
                "bxd_line_count": bxd_fact["line_count"] if bxd_fact else 0,
                "formal_row_count": formal_fact["row_count"] if formal_fact else 0,
                "ledger_batch_ids": sorted(ledger_fact["batch_ids"])
                if ledger_fact
                else [],
                "bxd_batch_ids": sorted(bxd_fact["batch_ids"]) if bxd_fact else [],
                "ledger_project_name": ledger_projects.get(bxd_no),
            }
        )
    return result

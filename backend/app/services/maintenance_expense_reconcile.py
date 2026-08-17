"""报销对账只读视图（C4，round-6 修复版）：台账归集 vs 氚云 BXD raw vs 正式费用事实。

口径（业务 2026-08-16 确认）：
- 正式计入金额列 = amount_inc_tax（含税计算值；amount 仅作审计原值）；
- 结论口径：台账（含税归集）↔ 正式（含税计算）→ matched/mismatch；
  BXD（氚云原值）作为第三源证据，仅报告 ``bxd_aligned``，不混层级；
- 来源存在与金额是否为空是两个独立字段（缺金额不伪造存在，也不伪造对齐）；
- 同文件重传去重：每个 (单号, file_hash) 只取最近 applied 批次；
- 有效事实过滤：台账=已应用且无 issue、BXD=已应用且头/明细无 issue、
  正式=生效口径 + 成功导入批次；limit/offset 分页。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
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


def _latest_batch_ids(rows: list[tuple], file_hash_pos: int, at_pos: int) -> dict:
    """(row_key, file_hash) → 最近 applied 批次集合（同文件重传只计最新）。"""
    by_file: dict[tuple, dict[str, datetime]] = defaultdict(dict)
    for row in rows:
        key = (row[0], row[file_hash_pos])
        batch_id = row[-1]
        applied_at = row[at_pos] or datetime.min
        if batch_id not in by_file[key]:
            by_file[key][batch_id] = applied_at
    latest: set[str] = set()
    for entries in by_file.values():
        max_at = max(entries.values())
        latest.update(batch_id for batch_id, at in entries.items() if at == max_at)
    return latest


def expense_reconcile_rows(
    db: Session, *, limit: int | None = None, offset: int = 0
) -> list[dict]:
    """三源全集逐费用单号对账（台账↔正式为结论，BXD 为证据）。"""
    # 台账：已应用批次、无 issue 行；同文件重传取最新批次
    ledger_rows = db.execute(
        select(
            MaintenanceLedgerExpenseRow.bxd_no,
            MaintenanceLedgerExpenseRow.amount,
            MaintenanceLedgerImportBatch.file_hash,
            MaintenanceLedgerImportBatch.applied_at,
            MaintenanceLedgerImportBatch.batch_id,
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
    latest_ledger = _latest_batch_ids(
        [(r[0], r[2], r[3], r[4]) for r in ledger_rows], 1, 2
    )
    ledger_facts: dict[str, dict] = defaultdict(
        lambda: {"present": False, "amount": None, "has_null": False, "row_count": 0}
    )
    ledger_projects: dict[str, str] = {}
    for row in ledger_rows:
        (
            bxd_no, amount, file_hash, applied_at, batch_id, project_name, issues,
        ) = row
        if issues or batch_id not in latest_ledger:
            continue
        facts = ledger_facts[bxd_no]
        facts["present"] = True
        facts["row_count"] += 1
        if amount is None:
            facts["has_null"] = True
        elif not facts["has_null"]:
            facts["amount"] = (facts["amount"] or Decimal("0")) + amount
        if project_name:
            ledger_projects[bxd_no] = project_name

    # BXD：已应用批次、头/明细无 issue；同文件重传取最新批次
    bxd_rows = db.execute(
        select(
            MaintenanceDocHeadRow.head_no,
            MaintenanceDocLineRow.amount,
            MaintenanceDocImportBatch.file_hash,
            MaintenanceDocImportBatch.applied_at,
            MaintenanceDocImportBatch.batch_id,
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
    latest_bxd = _latest_batch_ids(
        [(r[0], r[2], r[3], r[4]) for r in bxd_rows], 1, 2
    )
    bxd_facts: dict[str, dict] = defaultdict(
        lambda: {"present": False, "amount": None, "has_null": False, "line_count": 0}
    )
    for row in bxd_rows:
        head_no, amount, file_hash, applied_at, batch_id, head_issues, line_issues = row
        if head_issues or line_issues or batch_id not in latest_bxd:
            continue
        facts = bxd_facts[head_no]
        facts["present"] = True
        facts["line_count"] += 1
        if amount is None:
            facts["has_null"] = True
        elif not facts["has_null"]:
            facts["amount"] = (facts["amount"] or Decimal("0")) + amount

    # 正式费用：生效口径 + 成功导入批次；含税计算值（业务确认口径）
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
    formal_facts: dict[str, dict] = defaultdict(
        lambda: {"present": False, "amount": None, "has_null": False, "row_count": 0}
    )
    for bxd_no, amount_inc_tax, batch_status in formal_rows:
        if batch_status != "success":
            continue
        facts = formal_facts[bxd_no]
        facts["present"] = True
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
        ledger_present = ledger_fact is not None and ledger_fact["present"]
        bxd_present = bxd_fact is not None and bxd_fact["present"]
        formal_present = formal_fact is not None and formal_fact["present"]
        ledger_amount = (
            None if not ledger_present or ledger_fact["has_null"]
            else ledger_fact["amount"]
        )
        bxd_amount = (
            None if not bxd_present or bxd_fact["has_null"]
            else bxd_fact["amount"]
        )
        formal_amount = (
            None if not formal_present or formal_fact["has_null"]
            else formal_fact["amount"]
        )
        if ledger_present and formal_present:
            if ledger_amount is None or formal_amount is None:
                status = "unresolved"  # 来源在但金额缺，不出结论
            else:
                status = "matched" if ledger_amount == formal_amount else "mismatch"
        elif ledger_present:
            status = "ledger_only"
        elif formal_present:
            status = "formal_only"
        else:
            status = "bxd_only"
        bxd_aligned = (
            bxd_amount is not None
            and formal_amount is not None
            and bxd_amount == formal_amount
        )
        result.append(
            {
                "bxd_no": bxd_no,
                "status": status,
                # 结论口径：台账含税归集 ↔ 正式含税计算（业务 2026-08-16 确认）
                "conclusion_basis": (
                    "ledger_inc_tax == formal_amount_inc_tax"
                    if status in ("matched", "mismatch")
                    else None
                ),
                "bxd_aligned": bxd_aligned,
                "ledger_amount": _qty(ledger_amount),
                "bxd_amount": _qty(bxd_amount),
                "formal_amount": _qty(formal_amount),
                "ledger_present": ledger_present,
                "bxd_present": bxd_present,
                "formal_present": formal_present,
                "ledger_row_count": ledger_fact["row_count"] if ledger_fact else 0,
                "bxd_line_count": bxd_fact["line_count"] if bxd_fact else 0,
                "formal_row_count": formal_fact["row_count"] if formal_fact else 0,
                "ledger_project_name": ledger_projects.get(bxd_no),
            }
        )
    return result

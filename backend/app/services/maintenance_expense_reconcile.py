"""报销对账只读视图（C4，round-4 修复版）：台账归集 vs 氚云 BXD raw vs 正式费用事实。

口径（PRD §19 + import-field-contract §168-175 + Codex round-4 Blocker 3/10）：
- 三源全集按费用单号并集对账，任意一源存在即输出（不再从台账起步）；
- 同单号多行先聚合成单号级金额再比较（BXD 明细 400+600 vs 1000 → mismatch）；
- 有效事实过滤：台账=已应用批次、BXD=已应用批次、正式费用=生效口径
  （流程状态 MAINT_EXPENSE_ACTIVE_STATUS）；无 issue 行才参与；
- 金额口径标注在响应里，不混用不伪装：
  ledger_amount=台账含税归集、formal_amount=正式行含税计算值、
  bxd_amount=氚云导出原值；
- 每条带来源计数与批次引用（可下钻审计），差异进入清单不静默合并。
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


def _qty(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _classify(
    ledger: Decimal | None, bxd: Decimal | None, formal: Decimal | None
) -> str:
    present = [v for v in (ledger, bxd, formal) if v is not None]
    if len(present) <= 1:
        if ledger is not None:
            return "ledger_only"
        if bxd is not None:
            return "bxd_only"
        return "formal_only"
    if len(present) == 3:
        # 三源齐备：任何不一致都是 mismatch（即使其中两源一致）
        if ledger == bxd == formal:
            return "matched"
        return "mismatch"
    # 两源齐备：一致为 partial_match（缺一源），不一致为 mismatch
    pair = next((a, b) for a, b in ((ledger, bxd), (ledger, formal), (bxd, formal)) if a is not None and b is not None)
    return "partial_match" if pair[0] == pair[1] else "mismatch"


def expense_reconcile_rows(db: Session) -> list[dict]:
    """三源全集逐费用单号对账。"""
    # 台账：已应用批次、无 issue 行，按单号聚合
    ledger_facts: dict[str, dict] = defaultdict(
        lambda: {"amount": Decimal("0"), "row_count": 0, "batch_ids": set()}
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
        if amount is not None:
            facts["amount"] += amount
        facts["row_count"] += 1
        facts["batch_ids"].add(batch_id)
        if project_name:
            ledger_projects[bxd_no] = project_name

    # BXD：已应用批次，按单号聚合明细金额（原值口径）
    bxd_facts: dict[str, dict] = defaultdict(
        lambda: {"amount": Decimal("0"), "line_count": 0, "batch_ids": set()}
    )
    bxd_rows = db.execute(
        select(
            MaintenanceDocHeadRow.head_no,
            MaintenanceDocLineRow.amount,
            MaintenanceDocHeadRow.batch_id,
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
    for head_no, amount, batch_id in bxd_rows:
        facts = bxd_facts[head_no]
        if amount is not None:
            facts["amount"] += amount
        facts["line_count"] += 1
        facts["batch_ids"].add(batch_id)

    # 正式费用：生效口径（流程状态=MAINT_EXPENSE_ACTIVE_STATUS），含税计算值
    formal_facts: dict[str, dict] = defaultdict(
        lambda: {"amount": Decimal("0"), "row_count": 0, "statuses": set()}
    )
    formal_rows = db.execute(
        select(FProjectExpense.bxd_no, FProjectExpense.amount_inc_tax, FProjectExpense.data_status)
        .where(
            FProjectExpense.bxd_no.is_not(None),
            FProjectExpense.data_status == MAINT_EXPENSE_ACTIVE_STATUS,
        )
    ).all()
    for bxd_no, amount_inc_tax, data_status in formal_rows:
        facts = formal_facts[bxd_no]
        if amount_inc_tax is not None:
            facts["amount"] += amount_inc_tax
        facts["row_count"] += 1
        if data_status:
            facts["statuses"].add(data_status)

    keys = set(ledger_facts) | set(bxd_facts) | set(formal_facts)
    result: list[dict] = []
    for bxd_no in keys:
        ledger_fact = ledger_facts.get(bxd_no)
        bxd_fact = bxd_facts.get(bxd_no)
        formal_fact = formal_facts.get(bxd_no)
        ledger_amount = ledger_fact["amount"] if ledger_fact else None
        bxd_amount = bxd_fact["amount"] if bxd_fact else None
        formal_amount = formal_fact["amount"] if formal_fact else None
        result.append(
            {
                "bxd_no": bxd_no,
                "status": _classify(ledger_amount, bxd_amount, formal_amount),
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
    result.sort(key=lambda item: item["bxd_no"] or "")
    return result

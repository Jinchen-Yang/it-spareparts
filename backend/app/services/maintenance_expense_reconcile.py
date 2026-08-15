"""报销对账只读视图（C4）：台账归集 vs 氚云 BXD raw vs 正式费用事实。

口径：三份数据按费用单号逐条对账；两边都有时以氚云导出为金额事实，
台账归集为归属线索；差异进入清单，不静默合并。
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.maintenance import FProjectExpense
from app.models.maintenance_doc_import import (
    MaintenanceDocHeadRow,
    MaintenanceDocImportBatch,
    MaintenanceDocLineRow,
)
from app.models.maintenance_ledger import MaintenanceLedgerExpenseRow


def expense_reconcile_rows(db: Session) -> list[dict]:
    """逐费用单号对账：返回每条的状态（matched/mismatch/ledger_only/bxd_only）。"""
    ledger_rows = db.execute(
        select(MaintenanceLedgerExpenseRow).order_by(MaintenanceLedgerExpenseRow.row_no)
    ).scalars().all()
    bxd_heads = {
        row.head_no: row
        for row in db.execute(
            select(MaintenanceDocHeadRow)
            .join(
                MaintenanceDocImportBatch,
                MaintenanceDocImportBatch.batch_id == MaintenanceDocHeadRow.batch_id,
            )
            .where(MaintenanceDocImportBatch.doc_type == "bxd_expense")
        ).scalars()
    }
    formal = {
        row.bxd_no: row
        for row in db.execute(select(FProjectExpense)).scalars()
        if row.bxd_no
    }
    result: list[dict] = []
    seen_bxd: set[str] = set()
    for row in ledger_rows:
        if row.bxd_no is None:
            continue
        seen_bxd.add(row.bxd_no)
        bxd_head = bxd_heads.get(row.bxd_no)
        bxd_amount = None
        if bxd_head is not None:
            line = db.execute(
                select(MaintenanceDocLineRow)
                .where(MaintenanceDocLineRow.head_row_id == bxd_head.row_id)
                .order_by(MaintenanceDocLineRow.row_no)
            ).scalars().first()
            bxd_amount = line.amount if line is not None else None
        formal_row = formal.get(row.bxd_no)
        formal_amount = formal_row.amount if formal_row is not None else None
        ledger_amount = row.amount
        if bxd_head is None and formal_row is None:
            status = "ledger_only"
        elif _equal(ledger_amount, bxd_amount) and _equal(ledger_amount, formal_amount):
            status = "matched"
        elif _equal(ledger_amount, bxd_amount) or _equal(ledger_amount, formal_amount):
            status = "partial_match"
        else:
            status = "mismatch"
        result.append(
            {
                "bxd_no": row.bxd_no,
                "status": status,
                "ledger_amount": (
                    float(ledger_amount) if ledger_amount is not None else None
                ),
                "bxd_amount": (
                    float(bxd_amount) if bxd_amount is not None else None
                ),
                "formal_amount": (
                    float(formal_amount) if formal_amount is not None else None
                ),
                "ledger_project_name": row.project_name_raw,
            }
        )
    for bxd_no, head in bxd_heads.items():
        if bxd_no in seen_bxd:
            continue
        line = db.execute(
            select(MaintenanceDocLineRow)
            .where(MaintenanceDocLineRow.head_row_id == head.row_id)
            .order_by(MaintenanceDocLineRow.row_no)
        ).scalars().first()
        result.append(
            {
                "bxd_no": bxd_no,
                "status": "bxd_only",
                "ledger_amount": None,
                "bxd_amount": float(line.amount) if line and line.amount is not None else None,
                "formal_amount": None,
                "ledger_project_name": None,
            }
        )
    result.sort(key=lambda item: item["bxd_no"] or "")
    return result


def _equal(left: Decimal | None, right: Decimal | None) -> bool:
    if left is None or right is None:
        return False
    return left == right

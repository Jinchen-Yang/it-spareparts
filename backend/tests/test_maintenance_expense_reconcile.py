"""报销对账只读服务测试（C4）。"""

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.maintenance import FProjectExpense
from app.models.maintenance_doc_import import (
    MaintenanceDocHeadRow,
    MaintenanceDocLineRow,
)
from app.models.maintenance_ledger import MaintenanceLedgerExpenseRow
from app.services import maintenance_expense_reconcile as reconcile


@pytest.fixture()
def seeded(db):
    from app.models.maintenance_doc_import import MaintenanceDocImportBatch
    from app.models.maintenance_ledger import MaintenanceLedgerImportBatch

    db.add(
        MaintenanceLedgerImportBatch(
            batch_id="legacy-batch-1",
            file_hash="h1",
            filename="台账.xlsx",
            idempotency_key="legacy-key-1",
            source_kind="project_manager_xls_v1",
            uploaded_by="合成管理员",
        )
    )
    db.add(
        MaintenanceDocImportBatch(
            batch_id="legacy-batch-2",
            doc_type="bxd_expense",
            file_hash="h2",
            filename="报销.xlsx",
            idempotency_key="legacy-key-2",
            uploaded_by="合成管理员",
        )
    )
    db.flush()
    ledger_row = MaintenanceLedgerExpenseRow(
        row_id="ler-1",
        batch_id="legacy-batch-1",
        row_no=2,
        bxd_no_raw="BXD-20260425-0002",
        bxd_no="BXD-20260425-0002",
        project_name_raw="某项目",
        amount=Decimal("1068.50"),
        issues=[],
    )
    db.add(ledger_row)
    head = MaintenanceDocHeadRow(
        row_id="bhd-1",
        batch_id="legacy-batch-2",
        row_no=3,
        raw_json={"费用单号": "BXD-20260425-0002"},
        head_no="BXD-20260425-0002",
        issues=[],
    )
    db.add(head)
    db.flush()
    db.add(
        MaintenanceDocLineRow(
            row_id="bln-1",
            batch_id="legacy-batch-2",
            head_row_id="bhd-1",
            row_no=4,
            raw_json={"报销明细.报销金额": "1068.5"},
            line_key="BXD-LID-1",
            amount=Decimal("1068.50"),
            issues=[],
        )
    )
    # 三单一致 → matched
    from app.models.system import SysImportBatch

    import_batch = SysImportBatch(
        filename="expense.xlsx", file_type="expense", file_hash="h3", status="success"
    )
    db.add(import_batch)
    db.flush()
    db.add(
        FProjectExpense(
            raw_line_id="fpe-1",
            bxd_no="BXD-20260425-0002",
            line_no=1,
            data_status="已结束",
            expense_date=None,
            fee_category="差旅费",
            amount=Decimal("1068.50"),
            amount_ex_tax=Decimal("945.58"),
            amount_inc_tax=Decimal("1068.50"),
            tax_basis="inc",
            import_batch_id=import_batch.id,
        )
    )
    # 台账有、BXD 无 → ledger_only
    db.add(
        MaintenanceLedgerExpenseRow(
            row_id="ler-2",
            batch_id="legacy-batch-1",
            row_no=3,
            bxd_no_raw="BXD-20260420-0007",
            bxd_no="BXD-20260420-0007",
            project_name_raw="另一项目",
            amount=Decimal("1369.92"),
            issues=[],
        )
    )
    db.commit()


@pytest.fixture()
def seeded_cleanup(seeded):
    yield


def _cleanup_after(db):
    from sqlalchemy import text

    db.execute(text("DELETE FROM f_project_expense WHERE raw_line_id = 'fpe-1'"))
    db.execute(text("DELETE FROM maintenance_doc_line_row WHERE batch_id = 'legacy-batch-2'"))
    db.execute(text("DELETE FROM maintenance_doc_head_row WHERE batch_id = 'legacy-batch-2'"))
    db.execute(text("DELETE FROM maintenance_doc_import_batch WHERE batch_id = 'legacy-batch-2'"))
    db.execute(text("DELETE FROM maintenance_ledger_expense_row WHERE batch_id = 'legacy-batch-1'"))
    db.execute(text("DELETE FROM maintenance_ledger_import_batch WHERE batch_id = 'legacy-batch-1'"))
    db.commit()


def test_expense_reconcile_matched_and_ledger_only(db, seeded):
    try:
        rows = reconcile.expense_reconcile_rows(db)
        by_no = {row["bxd_no"]: row for row in rows}
        assert by_no["BXD-20260425-0002"]["status"] == "matched"
        assert by_no["BXD-20260425-0002"]["bxd_amount"] == 1068.5
        assert by_no["BXD-20260420-0007"]["status"] == "ledger_only"
    finally:
        _cleanup_after(db)


def test_expense_reconcile_mismatch_detected(db, seeded):
    # 改台账金额制造差异
    try:
        row = db.execute(
            select(MaintenanceLedgerExpenseRow).where(
                MaintenanceLedgerExpenseRow.bxd_no == "BXD-20260425-0002"
            )
        ).scalar_one()
        row.amount = Decimal("999.00")
        db.commit()
        rows = reconcile.expense_reconcile_rows(db)
        by_no = {item["bxd_no"]: item for item in rows}
        assert by_no["BXD-20260425-0002"]["status"] == "mismatch"
    finally:
        _cleanup_after(db)

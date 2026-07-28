from datetime import date
from decimal import Decimal

from app.etl import loader
from app.models.maintenance import FProjectExpense
from app.models.system import SysImportBatch
from app.services import maintenance_margin_evidence
from tests import factories as f


def _batch(db) -> SysImportBatch:
    batch = SysImportBatch(
        filename="margin-evidence.xlsx",
        file_type="sales",
        file_hash="margin-evidence",
    )
    db.add(batch)
    db.flush()
    return batch


def test_revenue_evidence_accepts_identical_duplicates_and_rejects_conflicts(db):
    batch = _batch(db)
    loader.load(
        db,
        f.sales_result(
            {
                "S-A1": f.sales_head(
                    "S-A1",
                    order_no="XS-A",
                    amount_ex_tax=Decimal("1000"),
                    tax_rate=Decimal("0.06"),
                ),
                "S-A2": f.sales_head(
                    "S-A2",
                    order_no="XS-A",
                    amount_ex_tax=Decimal("1000.00"),
                    tax_rate=Decimal("0.0600"),
                ),
                "S-B1": f.sales_head(
                    "S-B1",
                    order_no="XS-B",
                    amount_ex_tax=Decimal("1000"),
                    tax_rate=Decimal("0.06"),
                ),
                "S-B2": f.sales_head(
                    "S-B2",
                    order_no="XS-B",
                    amount_ex_tax=Decimal("1200"),
                    tax_rate=Decimal("0.13"),
                ),
                "S-C": f.sales_head(
                    "S-C",
                    order_no="XS-C",
                    amount_ex_tax=Decimal("900"),
                    tax_rate=None,
                ),
                "S-D1": f.sales_head(
                    "S-D1",
                    order_no="XS-D",
                    amount_ex_tax=Decimal("700"),
                    tax_rate=None,
                ),
                "S-D2": f.sales_head(
                    "S-D2",
                    order_no="XS-D",
                    amount_ex_tax=Decimal("700"),
                    tax_rate=Decimal("0.06"),
                ),
                "S-INACTIVE": f.sales_head(
                    "S-INACTIVE",
                    order_no="XS-C",
                    amount_ex_tax=Decimal("9999"),
                    tax_rate=Decimal("0.13"),
                    data_status="已取消",
                ),
            },
            [],
        ),
        batch.id,
        date(2026, 7, 28),
    )
    db.commit()

    evidence = maintenance_margin_evidence.load_contract_revenue_evidence(
        db,
        ["XS-A", "XS-B", "XS-C", "XS-D", "XS-MISSING"],
    )

    assert evidence["XS-A"] == maintenance_margin_evidence.RevenueEvidence(
        revenue_ex=Decimal("1000.00"),
        tax_rate=Decimal("0.0600"),
        tax_rate_ambiguous=False,
        ambiguous_inc=False,
        ambiguous_ex=False,
        record_count=2,
        legacy_contract_amount_inc=Decimal("1060.00"),
    )
    assert evidence["XS-B"].ambiguous_inc is True
    assert evidence["XS-B"].ambiguous_ex is True
    assert evidence["XS-B"].tax_rate_ambiguous is True
    assert evidence["XS-B"].revenue_ex is None
    assert evidence["XS-B"].tax_rate is None
    assert evidence["XS-B"].legacy_contract_amount_inc == Decimal("1356.00")
    assert evidence["XS-C"].revenue_ex == Decimal("900.00")
    assert evidence["XS-C"].tax_rate is None
    assert evidence["XS-D"].revenue_ex == Decimal("700.00")
    assert evidence["XS-D"].tax_rate is None
    assert evidence["XS-D"].tax_rate_ambiguous is True
    assert evidence["XS-D"].ambiguous_inc is True
    assert evidence["XS-D"].ambiguous_ex is False
    assert "XS-MISSING" not in evidence


def test_amount_conflict_keeps_a_unique_tax_rate_as_separate_evidence():
    evidence = maintenance_margin_evidence.summarize_revenue_candidates([
        (Decimal("1000"), Decimal("0.06")),
        (Decimal("1200"), Decimal("0.06")),
    ])

    assert evidence is not None
    assert evidence.ambiguous_inc is True
    assert evidence.ambiguous_ex is True
    assert evidence.tax_rate_ambiguous is False
    assert evidence.tax_rate == Decimal("0.06")


def test_expense_evidence_uses_absolute_gate_and_fails_closed_on_null(db):
    batch = _batch(db)
    db.add_all([
        FProjectExpense(
            raw_line_id="EXP-A1",
            linked_sales_order_no="XS-A",
            data_status="已结束",
            amount=Decimal("50"),
            import_batch_id=batch.id,
        ),
        FProjectExpense(
            raw_line_id="EXP-A2",
            linked_sales_order_no="XS-A",
            data_status="已结束",
            amount=Decimal("-50"),
            import_batch_id=batch.id,
        ),
        FProjectExpense(
            raw_line_id="EXP-B1",
            linked_sales_order_no="XS-B",
            data_status="已结束",
            amount=None,
            import_batch_id=batch.id,
        ),
        FProjectExpense(
            raw_line_id="EXP-C-INACTIVE",
            linked_sales_order_no="XS-C",
            data_status="已取消",
            amount=Decimal("88"),
            import_batch_id=batch.id,
        ),
    ])
    db.commit()

    evidence = maintenance_margin_evidence.load_untyped_expense_evidence(
        db,
        ["XS-A", "XS-B", "XS-C"],
    )

    assert evidence["XS-A"] == maintenance_margin_evidence.ExpenseEvidence(
        legacy_raw_total=Decimal("0.00"),
        unknown_tax_total=Decimal("100.00"),
        record_count=2,
    )
    assert evidence["XS-B"].legacy_raw_total == Decimal("0")
    assert evidence["XS-B"].unknown_tax_total is None
    assert "XS-C" not in evidence


def test_negative_formal_revenue_keeps_legacy_budget_zero_floor():
    evidence = maintenance_margin_evidence.summarize_revenue_candidates([
        (Decimal("-100"), Decimal("0.13")),
    ])

    assert evidence is not None
    assert evidence.revenue_ex == Decimal("-100")
    assert evidence.legacy_contract_amount_inc == Decimal("0")

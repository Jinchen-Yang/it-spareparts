from datetime import date, datetime, timezone
from decimal import Decimal

from app.etl import loader
from app.models.maintenance import FProjectExpense
from app.models.system import SysImportBatch
from app.services import maintenance_margin_evidence
from tests import factories as f


def _batch(
    db,
    *,
    suffix: str = "",
    uploaded_at: datetime | None = None,
) -> SysImportBatch:
    batch = SysImportBatch(
        filename=f"margin-evidence{suffix}.xlsx",
        file_type="sales",
        file_hash=f"margin-evidence{suffix}",
        status="success",
        uploaded_at=uploaded_at,
    )
    db.add(batch)
    db.flush()
    return batch


def test_revenue_evidence_selects_latest_effective_version(db):
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
        tax_rate=Decimal("0.13"),
        tax_rate_ambiguous=False,
        ambiguous_inc=False,
        ambiguous_ex=False,
        record_count=2,
        legacy_contract_amount_inc=Decimal("1130.00"),
    )
    assert evidence["XS-B"].ambiguous_inc is False
    assert evidence["XS-B"].ambiguous_ex is False
    assert evidence["XS-B"].tax_rate_ambiguous is False
    assert evidence["XS-B"].revenue_ex == Decimal("1200.00")
    assert evidence["XS-B"].tax_rate == Decimal("0.13")
    assert evidence["XS-B"].legacy_contract_amount_inc == Decimal("1356.00")
    assert evidence["XS-C"].revenue_ex == Decimal("900.00")
    assert evidence["XS-C"].tax_rate == Decimal("0.13")
    assert evidence["XS-D"].revenue_ex == Decimal("700.00")
    assert evidence["XS-D"].tax_rate == Decimal("0.13")
    assert evidence["XS-D"].tax_rate_ambiguous is False
    assert evidence["XS-D"].ambiguous_inc is False
    assert evidence["XS-D"].ambiguous_ex is False
    assert "XS-MISSING" not in evidence


def test_compatibility_summary_also_uses_fixed_tax_rate():
    evidence = maintenance_margin_evidence.summarize_revenue_candidates([
        (Decimal("1000"), Decimal("0.06")),
        (Decimal("1200"), Decimal("0.06")),
    ])

    assert evidence is not None
    assert evidence.ambiguous_inc is True
    assert evidence.ambiguous_ex is True
    assert evidence.tax_rate_ambiguous is False
    assert evidence.tax_rate == Decimal("0.13")


def test_latest_revenue_uses_batch_id_to_break_timestamp_ties(db):
    tied_at = datetime(2026, 7, 28, 8, 0, tzinfo=timezone.utc)
    older_batch = _batch(db, suffix="-older", uploaded_at=tied_at)
    loader.load(
        db,
        f.sales_result(
            {"OLD": f.sales_head(
                "OLD",
                order_no="XS-TIE",
                amount_ex_tax=Decimal("1000"),
                tax_rate=Decimal("0"),
            )},
            [],
        ),
        older_batch.id,
        date(2026, 7, 28),
    )
    newer_batch = _batch(db, suffix="-newer", uploaded_at=tied_at)
    loader.load(
        db,
        f.sales_result(
            {"NEW": f.sales_head(
                "NEW",
                order_no="XS-TIE",
                amount_ex_tax=Decimal("1200"),
                tax_rate=Decimal("0.06"),
            )},
            [],
        ),
        newer_batch.id,
        date(2026, 7, 28),
    )
    db.commit()

    evidence = maintenance_margin_evidence.load_contract_revenue_evidence(
        db,
        ["XS-TIE"],
    )["XS-TIE"]
    assert evidence.revenue_ex == Decimal("1200.00")
    assert evidence.legacy_contract_amount_inc == Decimal("1356.00")
    assert evidence.record_count == 2


def test_expense_evidence_aggregates_dual_amounts_and_fails_closed_on_null(db):
    batch = _batch(db)
    db.add_all([
        FProjectExpense(
            raw_line_id="EXP-A1",
            linked_sales_order_no="XS-A",
            data_status="已结束",
            amount=Decimal("50"),
            amount_ex_tax=Decimal("50"),
            amount_inc_tax=Decimal("56.50"),
            import_batch_id=batch.id,
        ),
        FProjectExpense(
            raw_line_id="EXP-A2",
            linked_sales_order_no="XS-A",
            data_status="已结束",
            amount=Decimal("-50"),
            amount_ex_tax=Decimal("-50"),
            amount_inc_tax=Decimal("-56.50"),
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
            amount_ex_tax=Decimal("88"),
            amount_inc_tax=Decimal("99.44"),
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
        expense_inc=Decimal("0.00"),
        expense_ex=Decimal("0.00"),
        record_count=2,
    )
    assert evidence["XS-B"].legacy_raw_total == Decimal("0")
    assert evidence["XS-B"].expense_inc is None
    assert evidence["XS-B"].expense_ex is None
    assert "XS-C" not in evidence


def test_negative_formal_revenue_keeps_legacy_budget_zero_floor():
    evidence = maintenance_margin_evidence.summarize_revenue_candidates([
        (Decimal("-100"), Decimal("0.13")),
    ])

    assert evidence is not None
    assert evidence.revenue_ex == Decimal("-100")
    assert evidence.legacy_contract_amount_inc == Decimal("0")

"""D-28: preview projection, with no accounting writes or receipt allocation."""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from app.services.maintenance_collection_progress import build_collection_progress


class ReadOnlyDb:
    def __init__(self, milestones=(), snapshots=()):
        self.results = [list(milestones), list(snapshots)]
        self.statements = []

    def scalars(self, statement):
        self.statements.append(statement)
        return self.results.pop(0)


def milestone(sequence, amount="100.00", *, complete=True, contract="contract-a"):
    return SimpleNamespace(
        milestone_id=f"{contract}-{sequence}",
        project_contract_id=contract,
        sequence=sequence,
        planned_amount=Decimal(amount) if amount is not None else None,
        planned_date=date(2026, sequence, 1),
        completeness_state="complete" if complete else "date_only",
        date_precision="month",
    )


def snapshot(amount="0.00", month="2026-01-01", *, contract="contract-a"):
    return SimpleNamespace(
        project_contract_id=contract,
        report_month=date.fromisoformat(month),
        cumulative_amount=Decimal(amount),
    )


def row(amount="150.00", month="2026-02-01", *, contract="contract-a", **extra):
    return {
        "row_key": f"{contract}-{month}",
        "matched_contract_id": contract,
        "matched_project_id": f"project-{contract}",
        "match_state": "matched",
        "row_status": "ready",
        "action": "upsert_collection_snapshot",
        "canonical": {"report_month": month, "sales_order_no": "20260101-0001"},
        "after": {"cumulative_amount": amount},
        "errors": [],
        **extra,
    }


def project(db, rows):
    return build_collection_progress(db, rows, as_of=date(2026, 2, 15))


def test_partial_receipt_and_one_payment_covering_multiple_periods():
    plans = [milestone(1), milestone(2), milestone(3)]
    db = ReadOnlyDb(plans, [snapshot("40.00")])
    result = project(db, [row("250.00")])[0]
    assert result["contract_id"] == "contract-a"
    assert result["before"]["cumulative_received"] == "40.00"
    assert result["before"]["milestones"][0]["payment_state"] == "partial"
    assert result["before"]["milestones"][0]["received_for_period"] == "40.00"
    after = result["candidates"][0]["after"]["milestones"]
    assert [item["payment_state"] for item in after] == ["paid", "paid", "partial"]
    assert [item["received_for_period"] for item in after] == ["100.00", "100.00", "50.00"]
    assert [item["remaining_amount"] for item in after] == ["0.00", "0.00", "50.00"]


def test_no_confirmed_snapshot_is_not_reported_not_zero_received():
    result = project(ReadOnlyDb([milestone(1)]), [row("20.00")])[0]
    before = result["before"]
    assert before["report_month"] is None
    assert before["cumulative_received"] is None
    assert before["milestones"][0]["payment_state"] == "not_reported"
    assert before["milestones"][0]["received_for_period"] is None
    assert before["milestones"][0]["remaining_amount"] is None
    assert result["candidates"][0]["after"]["milestones"][0]["payment_state"] == "partial"


def test_no_plan_keeps_receipts_visible_with_empty_periods():
    result = project(ReadOnlyDb(), [row("23.45")])[0]
    assert result["before"]["milestones"] == []
    assert result["candidates"][0]["after"]["cumulative_received"] == "23.45"
    assert result["candidates"][0]["after"]["milestones"] == []


@pytest.mark.parametrize("plans", [
    [milestone(1, None, complete=False), milestone(2)],
    [milestone(1, complete=False), milestone(2)],
    [milestone(2)],
])
def test_missing_or_incomplete_prior_plan_never_becomes_zero_target(plans):
    result = project(ReadOnlyDb(plans), [row("1000.00")])[0]
    states = result["candidates"][0]["after"]["milestones"]
    assert all(item["payment_state"] == "incomplete" for item in states)
    assert all(item["received_for_period"] is None for item in states)
    assert all(item["remaining_amount"] is None for item in states)


def test_older_month_does_not_replace_latest_confirmed_progress():
    db = ReadOnlyDb([milestone(1), milestone(2)], [snapshot("150.00", "2026-03-01")])
    result = project(db, [row("50.00", "2026-01-01")])[0]
    candidate = result["candidates"][0]
    assert candidate["report_month"] == "2026-01-01"
    assert candidate["after"] == result["before"]
    assert candidate["after"]["report_month"] == "2026-03-01"


def test_same_month_explicit_overwrite_can_change_progress():
    db = ReadOnlyDb([milestone(1), milestone(2)], [snapshot("150.00", "2026-02-01")])
    result = project(db, [row("75.00", action="update_collection_snapshot")])[0]
    assert result["before"]["milestones"][0]["payment_state"] == "paid"
    assert result["candidates"][0]["after"]["milestones"][0]["payment_state"] == "partial"


def test_candidates_remain_separate_for_partial_selection_never_sum_cumulatives():
    db = ReadOnlyDb([milestone(1), milestone(2)])
    result = project(db, [row("50.00", "2026-01-01"), row("150.00", "2026-02-01")])[0]
    assert result["before"]["cumulative_received"] is None
    assert [item["after"]["cumulative_received"] for item in result["candidates"]] == ["50.00", "150.00"]
    assert result["candidates"][0]["after"]["milestones"][0]["payment_state"] == "partial"
    assert result["candidates"][1]["after"]["milestones"][1]["payment_state"] == "partial"


@pytest.mark.parametrize("extra", [
    {"row_status": "blocked"}, {"action": "skip"}, {"match_state": "invalid"},
    {"errors": [{"code": "conflict"}]},
])
def test_blocked_known_and_invalid_rows_never_offer_after_candidate(extra):
    result = project(ReadOnlyDb(), [row(**extra)])[0]
    assert result["candidates"] == []


def test_ledger_only_candidate_preserves_amount_and_does_not_mutate_plan():
    plan = milestone(1)
    before_plan = vars(plan).copy()
    db = ReadOnlyDb([plan], [snapshot("100.00", "2026-02-01")])
    result = project(db, [row("100.00", action="record_receipts")])[0]
    assert result["candidates"][0]["after"] == result["before"]
    assert vars(plan) == before_plan


def test_decimal_amounts_and_overpayment_are_capped_per_period():
    db = ReadOnlyDb([milestone(1, "0.10"), milestone(2, "0.20")])
    states = project(db, [row("100.30")])[0]["candidates"][0]["after"]["milestones"]
    assert [item["received_for_period"] for item in states] == ["0.10", "0.20"]
    assert [item["remaining_amount"] for item in states] == ["0.00", "0.00"]


def test_queries_are_batched_scoped_and_only_load_active_plans_confirmed_snapshots():
    db = ReadOnlyDb([milestone(1), milestone(1, contract="contract-b")])
    result = project(db, [row(), row(contract="contract-b"), {"canonical": {}}])
    assert len(result) == 2
    assert len(db.statements) == 2
    compiled = [statement.compile(dialect=postgresql.dialect()) for statement in db.statements]
    for statement in compiled:
        assert sorted(next(value for value in statement.params.values() if isinstance(value, list))) == ["contract-a", "contract-b"]
    assert "is_active IS true" in str(compiled[0])
    assert "DISTINCT ON" in str(compiled[1])
    assert "confirmed" in compiled[1].params.values()
    assert "report_month DESC" in str(compiled[1])


def test_empty_or_unmatched_preview_does_not_query_any_contract():
    db = ReadOnlyDb()
    assert project(db, []) == []
    assert project(db, [{"matched_contract_id": None}]) == []
    assert db.statements == []


def test_database_projection_only_reads_scoped_active_plans_and_latest_confirmed(db):
    from uuid import uuid4

    from app.models.maintenance_manager import MaintenanceCollectionMilestone
    from app.models.maintenance_project_operations import MaintenanceCollectionSnapshot
    from tests.test_maintenance_receipt_ledger_import import _project_with_contract

    first_project, first_contract = _project_with_contract(db)
    other_project, other_contract = _project_with_contract(
        db, contract_no="XSDD-20260101-0002",
    )
    for project_row, contract, sequence, active in [
        (first_project, first_contract, 1, True),
        (first_project, first_contract, 2, False),
        (other_project, other_contract, 1, True),
    ]:
        db.add(MaintenanceCollectionMilestone(
            milestone_id=str(uuid4()), project_id=project_row.project_id,
            project_contract_id=contract.project_contract_id,
            sequence=sequence, planned_date=date(2026, sequence, 1),
            planned_amount=Decimal("100.00"), completeness_state="complete",
            source="direct_api", is_active=active,
        ))
    for month, amount, status in [
        ("2026-01-01", "30.00", "confirmed"),
        ("2026-02-01", "60.00", "confirmed"),
        ("2026-03-01", "1000.00", "unconfirmed"),
        ("2026-04-01", "2000.00", "void"),
    ]:
        db.add(MaintenanceCollectionSnapshot(
            collection_id=str(uuid4()), project_id=first_project.project_id,
            project_contract_id=first_contract.project_contract_id,
            report_month=date.fromisoformat(month),
            cumulative_amount=Decimal(amount), status=status,
        ))
    db.flush()
    result = project(db, [row(
        "50.00", "2026-01-01", contract=first_contract.project_contract_id,
        matched_project_id=first_project.project_id,
    )])
    assert len(result) == 1
    assert result[0]["contract_id"] == first_contract.project_contract_id
    assert result[0]["before"]["report_month"] == "2026-02-01"
    assert result[0]["before"]["cumulative_received"] == "60.00"
    assert len(result[0]["before"]["milestones"]) == 1
    assert result[0]["before"]["milestones"][0]["payment_state"] == "partial"
    assert result[0]["candidates"][0]["after"] == result[0]["before"]
    assert not db.new
    assert not db.dirty

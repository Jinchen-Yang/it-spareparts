"""Independent R1/R2 adversarial checks on PostgreSQL.

Ledger/fallback details characterize extraction compatibility, not new policy.
Fact validation and transactional audit checks exercise the recent facts contract.
"""

from datetime import date, timedelta
from decimal import Decimal

import psycopg
import pytest
from sqlalchemy import event, func, select
from sqlalchemy.exc import OperationalError

from app.models.maintenance_project_operations import (
    MaintenanceProjectOperationAudit,
    MaintenanceSiteIssueReturnEvent,
)
from app.services import maintenance_contract_reference as reference
from app.services import maintenance_site_issue_facts as facts
from tests.test_maintenance_contract_reference import TODAY, _contract, _fallback, _project
from tests.test_maintenance_site_issue_facts import _demand_prices, _facts


@pytest.fixture(autouse=True)
def _fixed_business_day(monkeypatch):
    monkeypatch.setattr(reference, "business_today", lambda: TODAY)


@pytest.mark.parametrize("off_page_state", ["expired", "future", "excluded"])
def test_noncurrent_or_excluded_offpage_owner_does_not_poison_ledger(db, off_page_state):
    """Global sharing must not accidentally remove its temporal/inclusion filter."""
    requested, other = _project(db), _project(db)
    _contract(db, requested, contract_id="same-contract", contract_no="SHARED-NO")
    options = {
        "expired": {"effective_to": TODAY},
        "future": {"effective_from": TODAY + timedelta(days=1)},
        "excluded": {"included": False},
    }[off_page_state]
    _contract(db, other, contract_id="same-contract", contract_no="SHARED-NO", **options)
    result = reference.current_contract_references(db, [requested])
    assert set(result) == {requested}
    assert result[requested]["amount_inc_tax"] == Decimal("100.00")
    assert not result[requested]["contract_shared"]
    assert not result[requested]["contract_incomplete"]


def test_mixed_sources_are_partition_invariant_despite_duplicate_request_ids(db):
    """A ledger owner still participates in a fallback owner's global sharing."""
    ledger, fallback, private = [_project(db) for _ in range(3)]
    _contract(db, ledger, contract_no="AUTHORITATIVE", amount="17.00")
    _fallback(db, [(ledger, "JOINT-SALES"), (fallback, "JOINT-SALES"),
                   (private, "PRIVATE-SALES")])
    batched = reference.current_contract_references(db, [private, fallback, ledger, fallback])
    singleton_union = {
        pid: reference.current_contract_references(db, [pid])[pid]
        for pid in (ledger, fallback, private)
    }
    assert batched == singleton_union
    assert set(batched) == {ledger, fallback, private}
    assert batched[ledger]["contract_nos"] == ["AUTHORITATIVE"]
    assert batched[ledger]["amount_inc_tax"] == Decimal("17.00")
    assert not batched[ledger]["contract_shared"]
    assert batched[fallback]["contract_shared"] and batched[fallback]["contract_incomplete"]
    assert batched[private]["amount_inc_tax"] == Decimal("113.00")
    assert not batched[private]["contract_shared"]


def test_sales_evidence_query_failure_cannot_return_partial_ledger_success(db):
    """One failing batched evidence read must escape, not produce partial totals."""
    ledger = _project(db)
    _contract(db, ledger, amount="23.00")
    fallback_ids = [_project(db) for _ in range(8)]
    _fallback(db, [(pid, f"FAIL-SALES-{i}") for i, pid in enumerate(fallback_ids)])
    db.commit()
    attempts, commits = [], []
    failure = OperationalError("synthetic sales evidence query", {}, RuntimeError("unavailable"))

    def fail_sales(_conn, _cursor, statement, parameters, _context, _many):
        if "from f_sales_order " in " ".join(statement.lower().split()):
            attempts.append(parameters)
            raise failure

    def committed(_session):
        commits.append(True)

    event.listen(db.get_bind(), "before_cursor_execute", fail_sales)
    event.listen(db, "after_commit", committed)
    try:
        with pytest.raises(OperationalError) as caught:
            reference.current_contract_references(db, [ledger, *fallback_ids])
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", fail_sales)
        event.remove(db, "after_commit", committed)
    assert caught.value is failure
    assert len(attempts) == 1
    assert all(f"FAIL-SALES-{i}" in attempts[0].values() for i in range(8))
    assert commits == []
    assert not db.new and not db.dirty


@pytest.mark.parametrize("autoflush", [False, True])
def test_assignment_query_fault_preserves_pending_caller_work_and_zero_fact_assignment(db, autoflush):
    """An unavailable DB is not 'no matching demand'; validation cannot autoflush."""
    project, issue, lines, part = _facts(db)
    _demand_prices(db, project=project, part=part, prices=(("VALID-DEMAND", Decimal("10")),))
    first, second = sorted(lines, key=lambda row: row.issue_line_id)
    # Hydrate caller-locked entities before deliberately enabling autoflush.
    for entity in (project, issue, *lines):
        db.refresh(entity)
    db.autoflush = autoflush
    # This is caller-owned work, intentionally dirty before entering the primitive.
    project.display_name = "caller pending change"
    failure = OperationalError("synthetic assignment query", {}, RuntimeError("unavailable"))
    statements = []

    def intercept(_conn, _cursor, statement, _parameters, _context, _many):
        sql = " ".join(statement.lower().split())
        statements.append(sql)
        if "from f_maintenance_order " in sql and "maintenance_source_order_assignment" in sql:
            raise failure

    event.listen(db.get_bind(), "before_cursor_execute", intercept)
    try:
        with pytest.raises(OperationalError) as caught:
            facts.apply_locked_site_issue_edits(
                db, project=project, issue=issue,
                line_changes={first.issue_line_id: {"remark": "must remain absent"},
                              second.issue_line_id: {"demand_order_no": "VALID-DEMAND"}},
                header_changes={"receiver": "must remain absent"},
                operated_by="tester", reason="query fault before assignment",
            )
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", intercept)
    assert caught.value is failure
    assert set(db.dirty) == {project} and not db.new
    assert statements and all(sql.startswith("select ") for sql in statements)
    # Catch-and-commit of unrelated caller work must still not persist partial edits.
    db.commit()
    db.expire_all()
    assert project.display_name == "caller pending change"
    assert issue.receiver == "接收人" and issue.version == 1
    assert all(row.remark is None and row.demand_order_no is None and row.version == 1 for row in lines)


def test_real_postgres_audit_failure_rolls_back_already_flushed_fact_and_event(db):
    """Fail at the final fact-audit INSERT after P3 has emitted its correction."""
    project, issue, lines, part = _facts(db)
    _demand_prices(db, project=project, part=part, prices=(("AUDIT-DEMAND", Decimal("10")),))
    target = lines[0]
    observed_event_inserts = []
    injected = []

    def fail_final_audit(_conn, cursor, statement, parameters, _context, _many):
        sql = " ".join(statement.lower().split())
        if sql.startswith("insert into maintenance_site_issue_return_event"):
            observed_event_inserts.append(True)
        if (sql.startswith("insert into maintenance_project_operation_audit")
                and "site_issue_facts_edited" in parameters.values()):
            injected.append(True)
            # Real PostgreSQL transaction-aborting error; no fake success or mock ORM.
            cursor.execute("SELECT 1 / 0")

    event.listen(db.get_bind(), "before_cursor_execute", fail_final_audit)
    try:
        with pytest.raises(psycopg.errors.DivisionByZero):
            facts.apply_locked_site_issue_edits(
                db, project=project, issue=issue,
                line_changes={target.issue_line_id: {"quantity": "3", "demand_order_no": "AUDIT-DEMAND"}},
                header_changes={"receiver": "should roll back"},
                operated_by="tester", reason="final audit failure",
            )
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", fail_final_audit)
        db.rollback()  # The primitive deliberately leaves transaction ownership to its caller.
    assert injected == [True] and observed_event_inserts
    db.expire_all()
    assert (target.quantity, target.demand_order_no, target.unit_cost, target.version) == (
        Decimal("2.000"), None, None, 1,
    )
    assert issue.receiver == "接收人" and issue.version == 1
    for model in (MaintenanceProjectOperationAudit, MaintenanceSiteIssueReturnEvent):
        assert db.scalar(select(func.count()).select_from(model)) == 0


def test_complete_cost_evidence_survives_fact_audit_jsonb_roundtrip(db):
    """Snapshot refactoring must not quietly turn internal audits into public DTOs."""
    project, issue, lines, _part = _facts(db)
    target = lines[0]
    target.manual_unit_cost = target.unit_cost = target.unit_cost_ex_tax = Decimal("12.34")
    target.manual_unit_cost_inc_tax = target.unit_cost_inc_tax = Decimal("13.94")
    target.cost_amount = target.cost_amount_ex_tax = Decimal("24.68")
    target.cost_amount_inc_tax = Decimal("27.88")
    target.manual_evidence = "private source document"
    target.cost_source = "manual"
    target.reference_side = "purchase"
    target.reference_sample_ids = ["private-source-id"]
    target.reference_samples = [{"id": "private-source-id", "unit_cost": "12.34", "nested": {"source": "private"}}]
    target.reference_sample_count = 1
    target.reference_window_from = date(2026, 9, 13)
    target.reference_window_to = date(2026, 9, 27)
    db.commit()
    result = facts.apply_locked_site_issue_edits(
        db, project=project, issue=issue,
        line_changes={target.issue_line_id: {"remark": "audit only edit"}},
        header_changes=None, operated_by="tester", reason="evidence roundtrip",
    )
    before, after = result.before, result.after
    db.commit()
    db.expire_all()
    audit = db.scalar(select(MaintenanceProjectOperationAudit).where(
        MaintenanceProjectOperationAudit.action == "site_issue_facts_edited"))
    assert audit.before_json == before and audit.after_json == after
    for snapshot in (audit.before_json, audit.after_json):
        stored = snapshot["lines"][target.issue_line_id]
        assert stored["manual_evidence"] == "private source document"
        assert stored["manual_unit_cost_inc_tax"] == "13.94"
        assert stored["cost_amount_ex_tax"] == "24.68" and stored["cost_amount_inc_tax"] == "27.88"
        assert stored["reference_sample_ids"] == ["private-source-id"]
        assert stored["reference_samples"][0]["nested"] == {"source": "private"}
        assert stored["reference_sample_count"] == 1
        assert stored["reference_window_from"] == "2026-09-13"
        assert stored["reference_window_to"] == "2026-09-27"
        assert stored["tax_rate_used"] == "0.13" and stored["is_active"] is True
    assert before["lines"][target.issue_line_id]["version"] == 1
    assert after["lines"][target.issue_line_id]["version"] == 2

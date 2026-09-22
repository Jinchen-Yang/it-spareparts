"""Shared metrics use real current facts, independently of demand/cost models."""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.maintenance import FMaintenanceLine
from app.models.maintenance_doc_import import (
    MaintenanceDocHeadRow,
    MaintenanceDocImportBatch,
    MaintenanceRkdReturnLine,
)
from app.models.maintenance_project_operations import MaintenanceSiteIssue, MaintenanceSiteIssueLine
from app.services import maintenance_return_metrics as metrics
from tests.test_maintenance_analytics import _line, _part, _project
from tests.test_maintenance_return_receipts_api import _wbdd


def _issue(
    db, project, part, qty, *, status="confirmed", active=True,
    issue_date=date(2026, 9, 10), source_order_id=None, no_return=None,
):
    issue = MaintenanceSiteIssue(
        issue_id=str(uuid4()), project_id=project.project_id, issue_no=f"SYN-{uuid4()}",
        issue_date=issue_date, raw_status=status,
        status_mapping_state="unmapped" if status == "unknown" else "mapped",
        normalized_status=status, status_mapping_version="synthetic-metric-v1",
        source="page_manual",
    )
    db.add(issue)
    db.flush()
    line = MaintenanceSiteIssueLine(
        issue_line_id=str(uuid4()), issue_id=issue.issue_id, line_no=1,
        part_id=part.id, pn=part.pn_std, quantity=Decimal(qty),
        source_order_id=source_order_id, no_return=no_return, is_active=active,
        algorithm_version="synthetic-metric-v1",
    )
    db.add(line)
    db.flush()
    return line


def _receipt(
    db, project, part, qty, *, pn=None, condition="坏品", status="active",
    occurred_at=datetime(2026, 9, 10, tzinfo=UTC), source_order_id=None,
    source="manual", description=None,
):
    batch_id = head_row_id = None
    if source == "rkd_import":
        batch = MaintenanceDocImportBatch(
            batch_id=str(uuid4()), doc_type="rkd_inbound", file_hash=uuid4().hex,
            filename="synthetic-rkd.xlsx", idempotency_key=str(uuid4()),
            uploaded_by="synthetic", head_rows=1, line_rows=1, issue_rows=0,
            status="applied", applied_by="synthetic", applied_at=datetime(2026, 9, 10, tzinfo=UTC),
        )
        db.add(batch)
        db.flush()
        head = MaintenanceDocHeadRow(
            row_id=str(uuid4()), batch_id=batch.batch_id, row_no=1,
            raw_json={}, head_no=f"RKD-{uuid4()}", head_date=date(2026, 9, 10),
            category="采购入库", data_status="已生效", project_id=project.project_id,
        )
        db.add(head)
        db.flush()
        batch_id, head_row_id = batch.batch_id, head.row_id
    row = MaintenanceRkdReturnLine(
        rkd_line_id=str(uuid4()), batch_id=batch_id, head_row_id=head_row_id,
        project_id=project.project_id, source_order_id=source_order_id,
        source=source, source_ref=f"synthetic:{uuid4()}", head_no=f"SYN-{uuid4()}",
        part_id=part.id if part else None, pn=pn or part.pn_std, qty=Decimal(qty),
        description=description, test_result=condition, line_status=status,
        occurred_at=occurred_at,
        voided_at=datetime(2026, 9, 11, tzinfo=UTC) if status == "voided" else None,
        voided_by="synthetic" if status == "voided" else None,
        source_payload={"category": "采购入库"} if source == "rkd_import" else None,
    )
    db.add(row)
    db.flush()
    return row


def test_current_confirmed_issues_and_all_receipts_are_independent_facts(db):
    project = _project(db, "METRIC-STATUS")
    project.no_return_default = True
    part = _part(db, "METRIC-STATUS-PN")
    _issue(db, project, part, "10", no_return=True)
    _issue(db, project, part, "3", status="corrected")
    for status in ("draft", "void", "unknown"):
        _issue(db, project, part, "100", status=status)
    _issue(db, project, part, "100", active=False)
    _receipt(db, project, part, "4")
    _receipt(db, project, part, "3", condition="成品")
    _receipt(db, project, part, "2", condition=None)
    _receipt(db, project, part, "1", condition="故障", source="rkd_import")
    _receipt(db, project, part, "100", status="voided")
    db.commit()

    assert metrics.project_totals(db)[project.project_id] == {
        "issued_qty": Decimal("13"),
        "returned_qty": Decimal("10"),
        "bad_returned_qty": Decimal("5"),
    }
    pn = metrics.pn_totals(db)[0]
    assert (pn["issued_qty"], pn["returned_qty"], pn["bad_returned_qty"]) == (
        Decimal("13"), Decimal("10"), Decimal("5"),
    )


def test_same_period_uses_issue_date_and_inclusive_shanghai_return_days(db):
    project = _project(db, "METRIC-DATE")
    part = _part(db, "METRIC-DATE-PN")
    for issue_date, qty in ((date(2026, 8, 31), "9"), (date(2026, 9, 1), "5"), (date(2026, 9, 2), "9")):
        _issue(db, project, part, qty, issue_date=issue_date)
    for moment, qty in (
        (datetime(2026, 8, 31, 15, 59, 59, 999999, tzinfo=UTC), "100"),
        (datetime(2026, 8, 31, 16, tzinfo=UTC), "2"),
        (datetime(2026, 9, 1, 15, 59, 59, 999999, tzinfo=UTC), "3"),
        (datetime(2026, 9, 1, 16, tzinfo=UTC), "100"),
        (None, "7"),
    ):
        _receipt(db, project, part, qty, occurred_at=moment)
    db.commit()

    scope = dict(date_from=date(2026, 9, 1), date_to=date(2026, 9, 1))
    assert metrics.project_totals(db, **scope)[project.project_id] == {
        "issued_qty": Decimal("5"), "returned_qty": Decimal("5"), "bad_returned_qty": Decimal("5"),
    }
    pn = metrics.pn_totals(db, **scope)[0]
    assert (pn["issued_qty"], pn["returned_qty"]) == (Decimal("5"), Decimal("5"))
    assert metrics.project_totals(db)[project.project_id]["returned_qty"] == Decimal("212")
    assert metrics.project_totals(db, date_from=date(2026, 9, 1))[project.project_id]["returned_qty"] == Decimal("105")
    assert metrics.project_totals(db, date_to=date(2026, 9, 1))[project.project_id]["returned_qty"] == Decimal("105")


def test_empty_project_and_order_filters_never_expand_scope(db):
    first, second, empty = (_project(db, name) for name in ("METRIC-P1", "METRIC-P2", "METRIC-EMPTY"))
    part = _part(db, "METRIC-SCOPE-PN")
    order = _wbdd(db, project=first)
    _issue(db, first, part, "10", source_order_id=order.raw_order_id)
    _issue(db, first, part, "20")
    _issue(db, second, part, "100")
    _receipt(db, first, part, "4", source_order_id=order.raw_order_id)
    _receipt(db, first, part, "8")
    _receipt(db, second, part, "100")
    db.commit()

    assert metrics.project_totals(db, project_ids=[]) == {}
    assert metrics.pn_totals(db, project_ids=set()) == []
    assert metrics.project_totals(db, source_order_ids=[]) == {}
    assert metrics.pn_totals(db, source_order_ids=set()) == []
    scoped = metrics.project_totals(db, project_ids=[first.project_id, empty.project_id])
    assert set(scoped) == {first.project_id, empty.project_id}
    assert scoped[first.project_id]["issued_qty"] == Decimal("30")
    assert scoped[empty.project_id] == {"issued_qty": Decimal(0), "returned_qty": Decimal(0), "bad_returned_qty": Decimal(0)}
    order_scope = dict(project_ids=[first.project_id], source_order_ids=[order.raw_order_id])
    assert metrics.project_totals(db, **order_scope)[first.project_id] == {
        "issued_qty": Decimal("10"), "returned_qty": Decimal("4"), "bad_returned_qty": Decimal("4"),
    }
    pn = metrics.pn_totals(db, **order_scope)[0]
    assert (pn["issued_qty"], pn["returned_qty"], pn["project_ids"]) == (
        Decimal("10"), Decimal("4"), [first.project_id],
    )


def test_mixed_identified_and_raw_pns_accumulate_without_demand_rows(db):
    first, second = (_project(db, name) for name in ("METRIC-ID1", "METRIC-ID2"))
    part = _part(db, "CANONICAL-PN", "Current canonical description")
    _issue(db, first, part, "10")
    _receipt(db, first, part, "2", pn="old-pn-spelling")
    _receipt(db, first, None, "3", pn=" canonical-pn ")
    _receipt(db, second, None, "4", pn="CANONICAL-PN")
    _receipt(db, first, None, "6", pn="UNMATCHED-PN", description="Receipt-only metadata")
    _receipt(db, first, None, "1", pn=" unmatched-pn ")
    db.commit()

    rows = metrics.pn_totals(db)
    assert len(rows) == 2
    canonical = next(row for row in rows if row["part_id"] == part.id)
    assert canonical == {
        "part_id": part.id, "pn": part.pn_std, "description": part.description,
        "project_ids": sorted([first.project_id, second.project_id]),
        "issued_qty": Decimal("10"), "returned_qty": Decimal("9"), "bad_returned_qty": Decimal("9"),
    }
    unmatched = next(row for row in rows if row["part_id"] is None)
    assert (unmatched["pn"], unmatched["description"], unmatched["issued_qty"], unmatched["returned_qty"]) == (
        "UNMATCHED-PN", "Receipt-only metadata", Decimal("0"), Decimal("7"),
    )


def test_ambiguous_case_insensitive_pn_does_not_guess_part_identity(db):
    project = _project(db, "METRIC-AMBIGUOUS")
    first = _part(db, "Case-PN")
    second = _part(db, "CASE-PN")
    _issue(db, project, first, "2")
    _issue(db, project, second, "3")
    _receipt(db, project, None, "1", pn=" case-pn ")
    db.commit()

    rows = metrics.pn_totals(db)
    assert len(rows) == 3
    assert sum(row["issued_qty"] for row in rows) == Decimal("5")
    assert sum(row["returned_qty"] for row in rows) == Decimal("1")
    assert next(row for row in rows if row["part_id"] is None)["returned_qty"] == Decimal("1")


def test_existing_part_without_issue_still_supplies_receipt_only_metadata(db):
    project = _project(db, "METRIC-ONLY")
    part = _part(db, "METADATA-PN", "No demand or issue required")
    _receipt(db, project, None, "2", pn="metadata-pn")
    db.commit()
    row = metrics.pn_totals(db)[0]
    assert (row["part_id"], row["pn"], row["description"], row["issued_qty"]) == (
        part.id, part.pn_std, part.description, Decimal("0"),
    )
    assert metrics.rate_pct(row["returned_qty"], row["issued_qty"]) is None


@pytest.mark.parametrize(("returned", "issued", "expected"), [
    ("0", "0", None), ("4", "0", None), ("0", "10", 0.0),
    ("4", "10", 40.0), ("12", "10", 120.0), ("1", "16", 6.3),
])
def test_rate_zero_denominator_zero_returns_and_over_returns(returned, issued, expected):
    assert metrics.rate_pct(Decimal(returned), Decimal(issued)) == expected


def test_reversed_date_window_rejected(db):
    with pytest.raises(ValueError, match="date_from"):
        metrics.project_totals(db, date_from=date(2026, 9, 2), date_to=date(2026, 9, 1))


def test_card_lifetime_sentinels_do_not_overflow_timezone_or_next_day(db):
    project = _project(db, "METRIC-LIFETIME")
    part = _part(db, "METRIC-LIFETIME-PN")
    _issue(db, project, part, "10")
    _receipt(db, project, part, "4")
    _receipt(db, project, part, "2", occurred_at=None)
    db.commit()

    assert metrics.project_totals(db, date_from=date.min)[project.project_id]["returned_qty"] == Decimal("6")
    assert metrics.project_totals(db, date_from=date.min, date_to=date.max)[project.project_id] == {
        "issued_qty": Decimal("10"), "returned_qty": Decimal("4"), "bad_returned_qty": Decimal("4"),
    }


def test_demand_part_scope_keeps_project_order_and_identity_together(db):
    project = _project(db, "METRIC-PART-SCOPE")
    other = _project(db, "METRIC-PART-OTHER")
    first = _part(db, "METRIC-PART-A")
    second = _part(db, "METRIC-PART-B")
    order_a = _wbdd(db, project=project, order_no="WBDD-METRIC-A")
    order_b = _wbdd(db, project=project, order_no="WBDD-METRIC-B")
    scope = {
        (project.project_id, order_a.raw_order_id, first.id),
        (project.project_id, order_b.raw_order_id, second.id),
    }
    _issue(db, project, first, "10", source_order_id=order_a.raw_order_id)
    _issue(db, project, second, "20", source_order_id=order_b.raw_order_id)
    # Neither an allowed order alone nor an allowed PN alone is sufficient.
    _issue(db, project, second, "100", source_order_id=order_a.raw_order_id)
    _issue(db, project, first, "100", source_order_id=order_b.raw_order_id)
    _issue(db, other, first, "100", source_order_id=order_a.raw_order_id)
    _receipt(db, project, first, "1", source_order_id=order_a.raw_order_id)
    _receipt(db, project, None, "2", pn=" metric-part-a ", source_order_id=order_a.raw_order_id)
    _receipt(db, project, second, "3", source_order_id=order_b.raw_order_id)
    _receipt(db, project, None, "4", pn="metric-part-b", source_order_id=order_b.raw_order_id)
    _receipt(db, project, first, "100", source_order_id=order_b.raw_order_id)
    # An explicit different part ID must win over misleading but matching text.
    _receipt(db, project, second, "100", pn=first.pn_std, source_order_id=order_a.raw_order_id)
    _receipt(db, other, first, "100", source_order_id=order_a.raw_order_id)
    _receipt(db, project, first, "100")
    db.commit()

    assert metrics.project_totals(db, demand_part_scope=scope) == {
        project.project_id: {"issued_qty": Decimal("30"), "returned_qty": Decimal("10"), "bad_returned_qty": Decimal("10")},
    }
    rows = metrics.pn_totals(db, demand_part_scope=scope)
    assert len(rows) == 2
    assert {row["part_id"]: (row["issued_qty"], row["returned_qty"]) for row in rows} == {
        first.id: (Decimal("10"), Decimal("3")),
        second.id: (Decimal("20"), Decimal("7")),
    }
    assert metrics.pn_totals(db, project_ids=[other.project_id], demand_part_scope=scope) == []
    assert metrics.pn_totals(db, source_order_ids=[], demand_part_scope=scope) == []
    assert metrics.project_totals(db, demand_part_scope=set()) == {}
    assert metrics.pn_totals(db, demand_part_scope=[]) == []


def test_demand_part_scope_does_not_guess_ambiguous_raw_identity(db):
    project = _project(db, "METRIC-PART-AMBIGUOUS")
    first = _part(db, "Scoped-Case")
    second = _part(db, "SCOPED-CASE")
    order = _wbdd(db, project=project)
    scope = [(project.project_id, order.raw_order_id, first.id)]
    _receipt(db, project, first, "1", source_order_id=order.raw_order_id)
    _receipt(db, project, second, "10", source_order_id=order.raw_order_id)
    _receipt(db, project, None, "100", pn=" scoped-case ", source_order_id=order.raw_order_id)
    db.commit()

    rows = metrics.pn_totals(db, demand_part_scope=scope)
    assert len(rows) == 1
    assert rows[0]["part_id"] == first.id
    assert rows[0]["returned_qty"] == Decimal("1")


def test_demand_part_scope_leaves_demand_amounts_unchanged_and_does_not_split_same_pn(db):
    project = _project(db, "METRIC-PART-COST")
    part = _part(db, "METRIC-PART-COST-PN")
    demand = _line(
        db, project, part, order_no="WBDD-METRIC-COST", order_date=date(2026, 9, 1),
        qty=Decimal("100"), return_qty=Decimal("20"),
        cost_inc=Decimal("800"), cost_ex=Decimal("700"),
    )
    from app.models.maintenance import FMaintenanceOrder

    order = db.get(FMaintenanceOrder, demand.order_id)
    second_line = FMaintenanceLine(
        raw_line_id=str(uuid4()), order_id=order.id, line_no=2,
        part_id=part.id, pn_std=part.pn_std, qty=Decimal("2"), return_qty=Decimal("0"),
        cost_source="manual", cost_tax_basis="inc", cost_amount=Decimal("20"),
        cost_amount_inc_tax=Decimal("20"), cost_amount_ex_tax=Decimal("18"),
        is_active=True, import_batch_id=order.import_batch_id,
    )
    db.add(second_line)
    _issue(db, project, part, "10", source_order_id=order.raw_order_id)
    _receipt(db, project, part, "4", source_order_id=order.raw_order_id)
    db.commit()
    before = list(db.execute(select(FMaintenanceLine.__table__)))
    scope = [(project.project_id, order.raw_order_id, part.id)]

    totals = metrics.project_totals(db, demand_part_scope=scope)[project.project_id]
    assert totals["issued_qty"] == Decimal("10")
    assert totals["returned_qty"] == Decimal("4")
    assert metrics.rate_pct(totals["returned_qty"], totals["issued_qty"]) == 40.0
    assert len(metrics.pn_totals(db, demand_part_scope=scope)) == 1
    assert list(db.execute(select(FMaintenanceLine.__table__))) == before

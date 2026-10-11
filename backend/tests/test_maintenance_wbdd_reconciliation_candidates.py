"""Immutable membership, conservative coverage and confirmation fingerprints."""
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder, MaintenanceManualCostOverride
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.models.system import SysImportBatch
from app.services import maintenance_demands
from app.services.maintenance_wbdd_reconciliation import build_candidates
from tests.test_maintenance_analytics import _part, _project
from tests.test_maintenance_return_metrics import _issue

BEFORE = datetime(2026, 9, 1, tzinfo=timezone.utc)
CAPTURED = datetime(2026, 10, 1, tzinfo=timezone.utc)
AFTER = datetime(2026, 10, 2, tzinfo=timezone.utc)


def batch(db, *, at=BEFORE):
    row = SysImportBatch(filename="synthetic.xlsx", file_type="maintenance",
                         file_hash=uuid4().hex, status="success", uploaded_at=at)
    db.add(row)
    db.flush()
    return row


def order(db, project, seq, *, day="20260910", raw=None, source=None, status="已生效"):
    source = source or batch(db)
    row = FMaintenanceOrder(raw_order_id=raw or f"raw-{uuid4()}",
        order_no=f"WBDD-{day}-{seq:04d}", order_date=BEFORE.date(), data_status=status,
        import_batch_id=source.id, created_at=source.uploaded_at)
    db.add(row)
    db.flush()
    if project:
        db.add(MaintenanceSourceOrderAssignment(assignment_id=str(uuid4()),
            source_order_id=row.raw_order_id, project_id=project.project_id,
            is_active=True, created_by="test"))
        db.flush()
    return row


def line(db, parent, part, *, raw=None, source=None, active=True):
    row = FMaintenanceLine(raw_line_id=raw or f"line-{uuid4()}", order_id=parent.id,
        line_no=1, part_id=part.id, pn_std=part.pn_std, pn_raw=part.pn_std,
        qty=Decimal("3"), return_qty=Decimal("0"), is_active=active,
        import_batch_id=(source.id if source else parent.import_batch_id))
    db.add(row)
    db.flush()
    return row


def snapshot(*members, blocked=None, mode="upsert"):
    return {"version": 1, "sheet_name": "合成原件", "row_count": len(members),
        "captured_at": CAPTURED.isoformat(), "mode": mode,
        "orders": [{"raw_order_id": parent.raw_order_id, "order_no": parent.order_no,
                    "line_ids": [row.raw_line_id for row in rows], "row_numbers": [n + 1],
                    "blocked_reasons": []} for n, (parent, rows) in enumerate(members)],
        "line_ids": [row.raw_line_id for _, rows in members for row in rows],
        "blocked_reasons": blocked or [], "ignored_wbdd_sheets": []}


def test_daily_ranges_never_bridge_dates_or_include_outside_edges(db):
    project, part = _project(db, "REC-DAYS"), _part(db, "REC-DAYS")
    parents = [order(db, project, seq) for seq in (1, 2, 3, 4)]
    for parent in parents:
        line(db, parent, part)
    old = order(db, project, 7, day="20260609")
    middle = order(db, project, 5, day="20260701")
    result = build_candidates(db, snapshot((parents[0], []), (parents[2], []), (old, [])))
    assert [row["source_order_id"] for row in result["missing_orders"]] == [parents[1].raw_order_id]
    assert middle.raw_order_id not in str(result)
    assert parents[3].raw_order_id not in str(result)
    assert len(result["coverage"]["daily_windows"]) == 2


def test_same_parent_uses_raw_line_ids_even_with_bad_values_or_changed_sequence(db):
    project, part = _project(db, "REC-LINES"), _part(db, "REC-LINES")
    parent = order(db, project, 1)
    kept, missing = line(db, parent, part), line(db, parent, part)
    kept.line_no = 99
    capture = snapshot((parent, [kept]))
    result = build_candidates(db, capture)
    assert [row["line_id"] for row in result["missing_lines"]] == [missing.id]
    capture["orders"][0]["blocked_reasons"] = ["bad_number"]
    assert build_candidates(db, capture)["missing_lines"] == []
    # A child present elsewhere / unparseable in the source cannot be deleted.
    capture["orders"][0]["blocked_reasons"] = []
    capture["line_ids"].append(missing.raw_line_id)
    assert build_candidates(db, capture)["missing_lines"] == []


def test_legacy_batch_requires_snapshot_and_batch_mutation_does_not_change_members(db):
    project, part = _project(db, "REC-IMMUTABLE"), _part(db, "REC-IMMUTABLE")
    parent = order(db, project, 1)
    first, second = line(db, parent, part), line(db, parent, part)
    upload = batch(db, at=CAPTURED)
    assert build_candidates(db, upload)["readiness"] == "snapshot_required"
    upload.report_json = {"_wbdd_source_snapshot": snapshot((parent, [first]))}
    parent.import_batch_id = upload.id
    first.import_batch_id = upload.id
    db.flush()
    result = build_candidates(db, upload)
    assert [row["line_id"] for row in result["missing_lines"]] == [second.id]
    assert result["batch_id"] == upload.id


def test_manual_late_inactive_and_tombstone_records_are_excluded(db):
    project, part = _project(db, "REC-GUARDS"), _part(db, "REC-GUARDS")
    lower, upper = order(db, project, 1), order(db, project, 9)
    manual = order(db, project, 2, raw="page-manual-protected")
    workbook = order(db, project, 3, raw="workbook-create-protected")
    late = order(db, project, 4, source=batch(db, at=AFTER))
    invalid = order(db, project, 5, status="已取消")
    dead = order(db, project, 6)
    line(db, dead, part)
    maintenance_demands.void_fast(db, source_order_ids=[dead.raw_order_id],
        reason="synthetic", operated_by="tester", idempotency_key="rec-tombstone-test")
    manual_child = line(db, lower, part, raw="manual-line:synthetic")
    late_child = line(db, lower, part, source=batch(db, at=AFTER))
    inactive = line(db, lower, part, active=False)
    result = build_candidates(db, snapshot((lower, []), (upper, [])))
    for excluded in (manual, workbook, late, invalid, dead):
        assert excluded.raw_order_id not in {row["source_order_id"] for row in result["missing_orders"]}
    assert not {manual_child.id, late_child.id, inactive.id} & {row["line_id"] for row in result["missing_lines"]}


def test_whole_missing_order_with_manual_or_later_child_is_retained(db):
    project, part = _project(db, "REC-PROTECT"), _part(db, "REC-PROTECT")
    lower, upper = order(db, project, 1), order(db, project, 4)
    manual, late = order(db, project, 2), order(db, project, 3)
    line(db, manual, part, raw="manual-line:protected")
    line(db, late, part, source=batch(db, at=AFTER))
    result = build_candidates(db, snapshot((lower, []), (upper, [])))
    assert result["missing_orders"] == []
    assert any("整单已保留" in item for item in result["warnings"])


def test_scope_applies_before_candidates_and_empty_scope_is_empty(db):
    mine, other = _project(db, "REC-MINE"), _project(db, "REC-OTHER")
    part = _part(db, "REC-SCOPE")
    left, right = order(db, mine, 1), order(db, mine, 4)
    own, foreign = order(db, mine, 2), order(db, other, 3)
    line(db, own, part)
    line(db, foreign, part)
    capture = snapshot((left, []), (right, []))
    result = build_candidates(db, capture, allowed_project_ids={mine.project_id})
    assert [row["source_order_id"] for row in result["missing_orders"]] == [own.raw_order_id]
    empty = build_candidates(db, capture, allowed_project_ids=set())
    assert empty["total_candidate_count"] == 0


def test_digest_tracks_manual_changes_and_related_site_state(db):
    project, part = _project(db, "REC-DIGEST"), _part(db, "REC-DIGEST")
    parent = order(db, project, 1)
    missing = line(db, parent, part)
    site = _issue(db, project, part, "2", source_order_id=parent.raw_order_id)
    site.source_line_id = missing.raw_line_id
    db.flush()
    capture = snapshot((parent, []))
    first = build_candidates(db, capture)
    assert first["missing_lines"][0]["linked_site_line_count"] == 1
    missing.manual_override = {"qty": {"value": "3", "source_value": "1"}}
    db.flush()
    second = build_candidates(db, capture)
    assert second["candidate_digest"] != first["candidate_digest"]
    override = MaintenanceManualCostOverride(line_id=missing.id, unit_cost_ex_tax=Decimal("2"),
        unit_cost_inc_tax=Decimal("2.26"), active=True, updated_by="test")
    db.add(override)
    db.flush()
    third = build_candidates(db, capture)
    assert third["candidate_digest"] != second["candidate_digest"]
    site.quantity = Decimal("4")
    db.flush()
    assert build_candidates(db, capture)["candidate_digest"] != third["candidate_digest"]


def test_failed_snapshot_skip_mode_and_candidate_limit(db):
    project = _project(db, "REC-LIMIT")
    left, right = order(db, project, 1), order(db, project, 5)
    for seq in (2, 3, 4):
        order(db, project, seq)
    capture = snapshot((left, []), (right, []))
    result = build_candidates(db, capture, limit=2)
    assert result["missing_count"] == 3 and len(result["missing_orders"]) == 2
    assert result["truncated"] is True
    assert build_candidates(db, snapshot((left, []), blocked=["identity_conflict"]))["readiness"] == "blocked"
    assert build_candidates(db, snapshot((left, []), (right, []), mode="skip"))["total_candidate_count"] == 0


def test_digest_is_identical_after_orm_expiry_and_numeric_scale_roundtrip(db):
    project, part = _project(db, "REC-SCALE"), _part(db, "REC-SCALE")
    parent = order(db, project, 1)
    line(db, parent, part)
    capture = snapshot((parent, []))
    original = build_candidates(db, capture)["candidate_digest"]
    db.expire_all()
    assert build_candidates(db, capture)["candidate_digest"] == original


def test_unmatched_site_references_are_reported_and_guarded_without_guessing(db):
    project, part = _project(db, "REC-UNMATCHED"), _part(db, "REC-UNMATCHED")
    parent = order(db, project, 1)
    line(db, parent, part)
    unmatched = _issue(db, project, part, "5", source_order_id=parent.raw_order_id)
    unmatched.source_line_id = "unknown-source-line"
    db.flush()
    capture = snapshot((parent, []))
    first = build_candidates(db, capture)
    assert first["missing_lines"][0]["linked_site_line_count"] == 0
    assert first["missing_lines"][0]["unmatched_site_line_count"] == 1
    unmatched.quantity = Decimal("6")
    db.flush()
    assert build_candidates(db, capture)["candidate_digest"] != first["candidate_digest"]


def test_later_batch_id_is_protected_even_with_earlier_transaction_timestamp(db):
    project, part = _project(db, "REC-BATCH-ID"), _part(db, "REC-BATCH-ID")
    parent = order(db, project, 1)
    line(db, parent, part)
    upload = batch(db, at=CAPTURED)
    upload.report_json = {"_wbdd_source_snapshot": snapshot((parent, []))}
    later = line(db, parent, part, source=batch(db, at=BEFORE))
    db.flush()
    assert later.id not in {row["line_id"] for row in build_candidates(db, upload)["missing_lines"]}

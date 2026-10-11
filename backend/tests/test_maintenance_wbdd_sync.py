"""D-27 confirmation is bounded, auditable, atomic and replay-safe."""
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.maintenance import MaintenanceDemandTombstone
from app.models.maintenance_project_operations import MaintenanceProjectOperationAudit, MaintenanceSiteIssue, MaintenanceSiteIssueLine
from app.models.system import SysAuditLog
from app.services import maintenance_wbdd_sync as sync
from app.services.maintenance_wbdd_reconciliation import build_candidates
from tests.test_maintenance_analytics import _part, _project
from tests.test_maintenance_return_metrics import _issue, _receipt
from tests.test_maintenance_wbdd_reconciliation_candidates import batch, order, line, snapshot, CAPTURED, BEFORE


def prepared(db, *, whole=True):
    project, part = _project(db, "SYNC"), _part(db, "SYNC-PN")
    left, missing, right = [order(db, project, n) for n in (1, 2, 3)]
    first, second = line(db, missing, part), line(db, missing, part)
    upload = batch(db, at=CAPTURED)
    upload.report_json = {"_wbdd_source_snapshot": (
        snapshot((left, []), (right, [])) if whole else snapshot((missing, [first])))}
    db.flush()
    return project, part, missing, first, second, upload


def apply(db, upload, *, orders=(), lines=(), digest=None, key="synthetic-sync-key", scope=None):
    return sync.apply_reconciliation(db, batch_id=upload.id,
        expected_digest=digest or build_candidates(db, upload, allowed_project_ids=scope)["candidate_digest"],
        source_order_ids=list(orders), line_ids=list(lines), reason="原件已删，确认同步", operated_by="tester",
        allowed_project_ids=scope, idempotency_key=key)


def test_confirm_whole_order_voids_exact_children_and_site_but_preserves_receipts(db):
    project, part, parent, first, second, upload = prepared(db)
    site = _issue(db, project, part, "2", source_order_id=parent.raw_order_id)
    site.source_line_id = first.raw_line_id
    receipt = _receipt(db, project, part, "1", source_order_id=parent.raw_order_id)
    unrelated = _issue(db, project, part, "9", source_order_id=parent.raw_order_id)
    # No exact source-line reference: do not infer a destructive relation.
    db.flush()
    candidates = build_candidates(db, upload)
    assert candidates["missing_orders"][0]["unmatched_site_line_count"] == 1
    assert candidates["unmatched_site_line_count"] == 1
    assert any("1条领用将保留" in warning for warning in candidates["warnings"])
    result = apply(db, upload, orders=[parent.raw_order_id])
    assert result == {"batch_id": upload.id, "voided_orders": 1, "voided_lines": 2,
                      "voided_site_lines": 1, "voided_site_issues": 1, "replayed": False}
    assert not first.is_active and not second.is_active and not site.is_active
    site_header = db.get(MaintenanceSiteIssue, site.issue_id)
    assert site_header.raw_status == site_header.normalized_status == "void"
    assert site_header.version == 2
    assert unrelated.is_active
    assert receipt.line_status == "active" and receipt.qty == Decimal("1")
    assert db.get(MaintenanceDemandTombstone, parent.raw_order_id).restored_at is None
    audits = list(db.scalars(select(MaintenanceProjectOperationAudit).where(
        MaintenanceProjectOperationAudit.entity_type == "maintenance_line",
        MaintenanceProjectOperationAudit.action == "VOID")))
    assert len(audits) == 2 and all(row.before_json and row.after_json for row in audits)


def test_partial_line_confirmation_keeps_parent_and_other_lines(db):
    project, part, parent, first, second, upload = prepared(db, whole=False)
    site = _issue(db, project, part, "2", source_order_id=parent.raw_order_id)
    site.source_line_id = second.raw_line_id
    db.flush()
    result = apply(db, upload, lines=[second.id])
    assert result["voided_lines"] == 1 and result["voided_orders"] == 0
    assert first.is_active and not second.is_active and not site.is_active
    assert db.get(MaintenanceDemandTombstone, parent.raw_order_id) is None


def test_stale_digest_or_non_candidate_selection_writes_nothing(db):
    _, _, parent, first, second, upload = prepared(db)
    digest = build_candidates(db, upload)["candidate_digest"]
    first.qty = Decimal("99")
    db.flush()
    with pytest.raises(sync.ReconciliationConflict, match="变化"):
        apply(db, upload, orders=[parent.raw_order_id], digest=digest)
    assert first.is_active and second.is_active
    with pytest.raises(sync.ReconciliationConflict, match="清单"):
        apply(db, upload, orders=["invented"])
    assert db.get(MaintenanceDemandTombstone, parent.raw_order_id) is None


def test_replay_is_noop_and_restored_or_modified_state_rejects_old_key(db):
    _, _, parent, first, _, upload = prepared(db)
    digest = build_candidates(db, upload)["candidate_digest"]
    first_result = apply(db, upload, orders=[parent.raw_order_id], digest=digest)
    replay = apply(db, upload, orders=[parent.raw_order_id], digest=digest)
    assert {**replay, "replayed": False} == first_result
    first.is_active = True
    db.flush()
    with pytest.raises(sync.ReconciliationConflict, match="恢复或修改"):
        apply(db, upload, orders=[parent.raw_order_id], digest=digest)


def test_changed_payload_same_key_and_empty_permission_scope_are_rejected(db):
    _, _, parent, first, _, upload = prepared(db)
    digest = build_candidates(db, upload)["candidate_digest"]
    apply(db, upload, orders=[parent.raw_order_id], digest=digest)
    with pytest.raises(sync.ReconciliationConflict, match="幂等"):
        apply(db, upload, orders=[], lines=[first.id], digest=digest)
    with pytest.raises(sync.ReconciliationConflict):
        apply(db, upload, orders=[parent.raw_order_id], scope=set(), key="empty-scope-key")


def test_cascade_failure_rolls_back_every_business_change(db, monkeypatch):
    project, part, parent, first, second, upload = prepared(db)
    site = _issue(db, project, part, "2", source_order_id=parent.raw_order_id)
    site.source_line_id = first.raw_line_id
    db.commit()
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic downstream failure")
    monkeypatch.setattr(sync.requirements, "record_corrections", fail)
    with pytest.raises(RuntimeError, match="synthetic"):
        apply(db, upload, orders=[parent.raw_order_id])
    db.rollback()
    assert first.is_active and second.is_active and site.is_active
    assert db.get(MaintenanceDemandTombstone, parent.raw_order_id) is None


def test_new_upload_supersedes_unconfirmed_old_candidates(db):
    _, _, parent, _, _, upload = prepared(db)
    digest = build_candidates(db, upload)["candidate_digest"]
    newer = batch(db, at=CAPTURED)
    newer.report_json = dict(upload.report_json)
    db.flush()
    with pytest.raises(sync.ReconciliationConflict, match="更新的原件"):
        apply(db, upload, orders=[parent.raw_order_id], digest=digest)


def test_unassigned_order_uses_global_integer_identity_audit(db):
    part = _part(db, "SYNC-UNASSIGNED")
    left, missing, right = [order(db, None, n) for n in (1, 2, 3)]
    detail = line(db, missing, part)
    upload = batch(db, at=CAPTURED)
    upload.report_json = {"_wbdd_source_snapshot": snapshot((left, []), (right, []))}
    db.flush()
    result = apply(db, upload, orders=[missing.raw_order_id])
    assert result["voided_orders"] == result["voided_lines"] == 1
    audit = db.scalar(select(SysAuditLog).where(SysAuditLog.entity_type == "maintenance_line"))
    assert audit.entity_id == detail.id and audit.after_json["is_active"] is False


def test_partial_site_void_keeps_sibling_and_bumps_issue_version(db):
    project, part, parent, first, second, upload = prepared(db, whole=False)
    target = _issue(db, project, part, "2", source_order_id=parent.raw_order_id)
    target.source_line_id = second.raw_line_id
    sibling = MaintenanceSiteIssueLine(issue_line_id="synthetic-kept-site-line", issue_id=target.issue_id,
        line_no=2, part_id=part.id, pn=part.pn_std, quantity=Decimal("1"),
        source_order_id=parent.raw_order_id, source_line_id=first.raw_line_id,
        is_active=True, algorithm_version="synthetic-metric-v1")
    db.add(sibling)
    db.flush()
    result = apply(db, upload, lines=[second.id])
    header = db.get(MaintenanceSiteIssue, target.issue_id)
    assert result["voided_site_lines"] == 1 and result["voided_site_issues"] == 0
    assert not target.is_active and sibling.is_active
    assert header.normalized_status == "confirmed" and header.version == 2
    assert target.version == 2


def test_newest_batch_id_wins_when_transaction_timestamps_are_reversed(db):
    _, _, parent, _, _, upload = prepared(db)
    newer = batch(db, at=BEFORE)
    newer.report_json = dict(upload.report_json)
    db.flush()
    with pytest.raises(sync.ReconciliationConflict, match="更新的原件"):
        apply(db, upload, orders=[parent.raw_order_id])
    result = apply(db, newer, orders=[parent.raw_order_id], key="newest-id-confirm")
    assert result["voided_orders"] == 1


def test_sync_removes_active_analytics_facts_without_cutover_or_return_deletion(db, monkeypatch):
    from app.config import get_settings
    from app.services import maintenance_analytics
    monkeypatch.setattr(get_settings(), "maintenance_cutover_enabled", False)
    project, part, parent, first, second, upload = prepared(db)
    for detail in (first, second):
        detail.cost_source, detail.cost_tax_basis = "direct", "inc"
        detail.cost_amount = detail.cost_amount_inc_tax = Decimal("30")
        detail.cost_amount_ex_tax = Decimal("26.55")
    site = _issue(db, project, part, "2", source_order_id=parent.raw_order_id)
    site.source_line_id = first.raw_line_id
    receipt = _receipt(db, project, part, "1", source_order_id=parent.raw_order_id)
    db.flush()
    before = maintenance_analytics.pn_ranking(db, range_="all", can_cost=True)["summary"]
    assert Decimal(before["total_effective_qty"]) == 6
    assert Decimal(before["total_cost_inc"]["value"]) == 60
    assert Decimal(before["total_issued_qty"]) == 2
    apply(db, upload, orders=[parent.raw_order_id])
    after = maintenance_analytics.pn_ranking(db, range_="all", can_cost=True)["summary"]
    assert Decimal(after["total_effective_qty"]) == 0
    assert Decimal(after["total_cost_inc"]["value"]) == 0
    assert Decimal(after["total_issued_qty"]) == 0
    assert Decimal(after["total_receipt_qty"]) == Decimal(before["total_receipt_qty"]) == 1
    assert receipt.line_status == "active"

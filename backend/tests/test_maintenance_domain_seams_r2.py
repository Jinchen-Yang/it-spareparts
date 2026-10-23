"""R2 compatibility; non-effective sources are observed behavior, not new policy."""
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.maintenance import FMaintenanceOrder
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.services import maintenance_manual_site_issue as manual
from app.services import maintenance_project_operations as operations
from app.services.maintenance_demand_project_reference import demand_has_active_project_assignment
from app.services.maintenance_operation_errors import MaintenanceOperationError
from app.services.maintenance_site_issue_facts import SiteIssueEditError, _line_snapshot, apply_locked_site_issue_edits
from app.services.maintenance_site_issue_representation import site_issue_line_dict
from tests.test_maintenance_site_issue_facts import _facts, _demand_prices


@pytest.mark.parametrize("case", ["missing", "foreign", "inactive", "active", "non_effective"])
def test_assignment_scope_and_legacy_error_cause(db, case):
    project, issue, lines, part = _facts(db)
    demand_no = "R2-DEMAND"
    if case != "missing":
        owner, owner_part = project, part
        if case == "foreign":
            owner, _, _, owner_part = _facts(db)
        _demand_prices(db, project=owner, part=owner_part, prices=((demand_no, Decimal("10")),))
        if case == "inactive":
            assignment = db.scalar(select(MaintenanceSourceOrderAssignment).where(
                MaintenanceSourceOrderAssignment.project_id == project.project_id))
            assignment.is_active = False
            assignment.version += 1
            assignment.archived_by = "tester"
            assignment.archived_at = datetime.now(UTC)
        if case == "non_effective":
            db.scalar(select(FMaintenanceOrder).where(
                FMaintenanceOrder.order_no == demand_no)).data_status = "草稿"
        db.commit()
    expected = case in {"active", "non_effective"}
    assert demand_has_active_project_assignment(db, project_id=project.project_id, demand_order_no=demand_no) is expected
    if expected:
        assert manual._resolve_demand_project(db, project_id=project.project_id, demand_order_no=demand_no) is None
        return
    with pytest.raises(MaintenanceOperationError) as legacy:
        manual._resolve_demand_project(db, project_id=project.project_id, demand_order_no=demand_no)
    with pytest.raises(SiteIssueEditError) as caught:
        apply_locked_site_issue_edits(
            db, project=project, issue=issue,
            line_changes={lines[0].issue_line_id: {"remark": "must not persist"},
                          lines[1].issue_line_id: {"demand_order_no": demand_no}},
            header_changes={"receiver": "must not persist"}, operated_by="tester", reason="R2 compatibility",
        )
    assert caught.value.code == "site_issue_edit_demand_project_mismatch"
    assert caught.value.status_code == 409
    assert caught.value.message == str(legacy.value) == "关联需求单不属于当前项目，请核对单号或留空"
    assert type(caught.value.__cause__) is operations.MaintenanceOperationError is MaintenanceOperationError
    assert str(caught.value.__cause__) == str(legacy.value)
    assert not db.dirty and not db.new
    assert issue.receiver == "接收人" and issue.version == 1
    assert all(line.remark is None and line.version == 1 for line in lines)


@pytest.mark.parametrize("source,kind,estimate,label", [
    (None, "missing", False, "待补价格"),
    ("maint_demand", "maintenance_demand_evidence", False, "关联维保需求单价"),
    ("direct_purchase", "purchase_evidence", False, "关联采购单价"),
    ("purchase_window", "purchase_evidence", False, "采购前后 7 天数量加权"),
    ("sales_window", "sales_estimate", True, "估算（销售前后 7 天数量加权）"),
    ("manual", "manual_confirmed", False, "人工确认单价"),
    ("future", "unknown", True, "未知成本证据"),
])
def test_fact_representation_preserves_hidden_cost_evidence(db, source, kind, estimate, label):
    _, _, lines, _ = _facts(db)
    row = lines[0]
    row.manual_unit_cost = Decimal("12.345")
    row.manual_evidence = "private evidence"
    row.unit_cost = Decimal("0")
    row.cost_amount = Decimal("-1.235")
    row.tax_rate_used = Decimal("0.130000")
    row.cost_source = source
    row.reference_sample_ids = ["secret-id"]
    row.reference_samples = [{"unit_cost": "10.00"}]
    row.reference_window_from = date(2026, 10, 1)
    expected = {
        "issue_line_id": row.issue_line_id, "line_no": 1, "part_id": row.part_id,
        "pn": row.pn, "quantity": "2.000", "delivery_line_id": None,
        "source_order_id": None, "source_line_id": None, "serial_number": None,
        "no_return": None, "remark": None, "demand_order_no": None,
        "linked_purchase_line_id": None, "manual_unit_cost": "12.34",
        "manual_unit_cost_inc_tax": None, "manual_evidence": "private evidence",
        "unit_cost": "0.00", "cost_amount": "-1.24", "unit_cost_ex_tax": None,
        "unit_cost_inc_tax": None, "cost_amount_ex_tax": None, "cost_amount_inc_tax": None,
        "tax_rate_used": "0.13", "cost_source": source, "cost_evidence_kind": kind,
        "cost_is_estimate": estimate, "cost_source_label": label, "price_basis": "ex_tax",
        "reference_side": None, "reference_sample_ids": ["secret-id"], "reference_sample_count": 0,
        "reference_samples": [{"unit_cost": "10.00"}], "reference_window_from": "2026-10-01",
        "reference_window_to": None, "algorithm_version": "contract-v1", "version": 1,
    }
    assert operations.site_issue_line_dict is site_issue_line_dict
    assert site_issue_line_dict(row) == expected
    assert _line_snapshot(row) == {**expected, "is_active": True}
    db.rollback()  # Snapshot-only evidence values intentionally need not satisfy write constraints.

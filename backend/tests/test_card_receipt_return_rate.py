"""卡墙返还率：有效收货（所有件况）/已确认实际领用数量，不按 PN 匹配。"""

from datetime import date
from decimal import Decimal
from uuid import uuid4

from app.models.dimensions import DimPart
from app.models.maintenance import FMaintenanceLine
from app.services.maintenance_boss_board import _card_receipt_rates
from app.services.maintenance_return_receipts import register_receipt, void_receipt
from app.services.maintenance_project_operations import create_site_issue
from tests.test_maintenance_return_receipts_api import _wbdd
from tests.test_site_issue_v2_api import _project


def _issued(db, project, part, qty=8):
    create_site_issue(
        db, project_id=project.project_id, issue_no="SITE-" + uuid4().hex[:8],
        issue_date=date(2026, 9, 1), raw_status="confirmed",
        status_mapping_state="mapped", normalized_status="confirmed",
        status_mapping_version="test", source="legacy", import_batch_id=None,
        lines=[{"issue_line_id": str(uuid4()), "line_no": 1, "part_id": part.id,
                "pn": part.pn_std, "quantity": Decimal(qty), "no_return": True}],
        operated_by="tester", reason="actual issue fixture",
    )


def test_card_rate_all_conditions_unassigned_and_void(db):
    project = _project(db, project_id=str(uuid4()))
    order = _wbdd(db, project=project)
    part = DimPart(pn_std="DEMAND-PN")
    db.add(part)
    db.flush()
    db.add(
        FMaintenanceLine(
            raw_line_id=str(uuid4()),
            order_id=order.id,
            line_no=1,
            part_id=part.id,
            pn_std=part.pn_std,
            pn_raw=part.pn_std,
            qty=Decimal("10"),
            is_active=True,
            import_batch_id=order.import_batch_id,
        )
    )
    db.flush()
    _issued(db, project, part)
    for condition in (None, "成品", "坏品", "废品"):
        register_receipt(
            db,
            project_id=project.project_id,
            pn="OTHER-PN",
            qty=3,
            condition=condition,
            operated_by="tester",
        )
    result = _card_receipt_rates(db, [project.project_id])[project.project_id]
    assert result == {
        "returned_qty": "12.000",
        "issued_qty": "8.000",
        "rate_pct": 150.0,
        "state": "ready",
    }
    extra = register_receipt(
        db, project_id=project.project_id, pn="VOID-PN", qty=5, operated_by="tester"
    )
    void_receipt(
        db,
        receipt_id=extra["receipt_id"],
        expected_version=1,
        reason="重复",
        operated_by="tester",
    )
    assert _card_receipt_rates(db, [project.project_id])[project.project_id] == result


def test_card_rate_missing_denominator_is_not_zero_percent(db):
    project = _project(db, project_id=str(uuid4()))
    register_receipt(
        db, project_id=project.project_id, pn="RETURN-PN", qty=2, operated_by="tester"
    )
    result = _card_receipt_rates(db, [project.project_id])[project.project_id]
    assert result["returned_qty"] == "2.000"
    assert result["rate_pct"] is None
    assert result["state"] == "basis_incomplete"


def test_card_rate_demand_missing_lines_cannot_hide_actual_issues(db):
    project = _project(db, project_id=str(uuid4()))
    first = _wbdd(db, project=project)
    _wbdd(db, project=project, order_no="WBDD-MISSING")
    part = DimPart(pn_std="ONLY-KNOWN-LINE")
    db.add(part)
    db.flush()
    db.add(
        FMaintenanceLine(
            raw_line_id=str(uuid4()),
            order_id=first.id,
            line_no=1,
            part_id=part.id,
            pn_std=part.pn_std,
            pn_raw=part.pn_std,
            qty=Decimal("10"),
            is_active=True,
            import_batch_id=first.import_batch_id,
        )
    )
    db.flush()
    result = _card_receipt_rates(db, [project.project_id])[project.project_id]
    assert result["issued_qty"] == "0.000"
    assert result["state"] == "basis_incomplete"
    _issued(db, project, part, qty=10)
    result = _card_receipt_rates(db, [project.project_id])[project.project_id]
    assert result["issued_qty"] == "10.000"
    assert result["state"] == "ready"
    assert result["rate_pct"] == 0.0

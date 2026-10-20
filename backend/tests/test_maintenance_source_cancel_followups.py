"""D-34: each cancellation guard independently prevents partial writes."""
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from app.models.maintenance_project_operations import (
    MaintenanceSiteIssue,
    MaintenanceSiteIssueDeliverySource,
)
from app.services import maintenance_source_cancel_cascade as cascade
from tests.test_maintenance_source_cancel_cascade import confirm_body, setup
from tests.test_maintenance_source_cancel_concurrency import _business_snapshot


def _inject_one_impact_drift(monkeypatch, *, call_number, field):
    """Keep the HTTP seam real; perturb only one read to isolate its guard.

    A persistent drift reaches both checks and cannot prove either check is
    independently necessary. This controlled fault models different evidence
    before and after row-lock acquisition without changing any stored facts.
    """
    original = cascade._impact
    calls = 0

    def impact_with_drift(*args, **kwargs):
        nonlocal calls
        result = original(*args, **kwargs)
        calls += 1
        if calls != call_number:
            return result
        impact = dict(result[0])
        impact[field] = False if field == "executable" else "0" * 64
        return impact, *result[1:]

    monkeypatch.setattr(cascade, "_impact", impact_with_drift)


@pytest.mark.parametrize("field", ["executable", "expected_digest"])
def test_pre_row_lock_recheck_rejects_transient_drift_without_writes(
    db, tmp_path, monkeypatch, field,
):
    client, _, _, _, _, _, _, _, _, _, detail_url = setup(db, tmp_path)
    body = confirm_body(client, detail_url)
    before = _business_snapshot(db)
    db.commit()
    _inject_one_impact_drift(monkeypatch, call_number=1, field=field)

    response = client.post(detail_url + "/confirm", json=body)

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "source_cancellation_changed"
    db.expire_all()
    assert _business_snapshot(db) == before


@pytest.mark.parametrize("field", ["executable", "expected_digest"])
def test_post_row_lock_recheck_rejects_new_drift_without_writes(
    db, tmp_path, monkeypatch, field,
):
    client, _, _, _, _, _, _, _, _, _, detail_url = setup(db, tmp_path)
    body = confirm_body(client, detail_url)
    before = _business_snapshot(db)
    db.commit()
    _inject_one_impact_drift(monkeypatch, call_number=2, field=field)

    response = client.post(detail_url + "/confirm", json=body)

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "source_cancellation_changed"
    db.expire_all()
    assert _business_snapshot(db) == before


def test_delivery_identity_collision_is_warning_only_and_never_voided(db, tmp_path):
    client, project, _, parent, lines, exact, _, _, _, _, detail_url = setup(db, tmp_path)
    delivery = MaintenanceSiteIssueDeliverySource(
        delivery_line_id=str(uuid4()),
        adapter_key="synthetic_delivery_v1",
        project_id=project.project_id,
        source_order_id=parent.raw_order_id,
        source_line_id=lines[0].raw_line_id,
        delivery_no="SYN-CANCEL-DELIVERY",
        delivery_date=date(2026, 10, 20),
        part_id=lines[0].part_id,
        pn=lines[0].pn_std,
        delivered_quantity=Decimal(2),
        mapping_state="ready",
        mapping_version="synthetic-delivery-map-v1",
    )
    db.add(delivery)
    db.flush()
    exact.delivery_line_id = delivery.delivery_line_id
    # Keep every other reliable-lineage condition true, including an issue
    # source other than site_issue_v2; only delivery identity must exclude it.
    issue = db.get(MaintenanceSiteIssue, exact.issue_id)
    assert issue.source == "page_manual"
    assert exact.source_line_id == lines[0].raw_line_id
    assert exact.source_order_id == parent.raw_order_id
    db.commit()

    detail_response = client.get(detail_url)
    assert detail_response.status_code == 200, detail_response.text
    detail = detail_response.json()
    assert detail["exact_site_lines"] == []
    kept = next(row for row in detail["unlinked_site_lines"]
                if row["issue_line_id"] == exact.issue_line_id)
    assert kept["link_reason"] == "仓库来源身份"
    assert any("将保留" in warning for warning in detail["warnings"])

    response = client.post(detail_url + "/confirm", json=confirm_body(client, detail_url))

    assert response.status_code == 200, response.text
    assert response.json()["voided_site_lines"] == 0
    db.expire_all()
    assert exact.is_active
    assert issue.normalized_status == "confirmed"
    assert all(not line.is_active for line in lines)
    assert delivery.is_active and delivery.delivered_quantity == Decimal(2)

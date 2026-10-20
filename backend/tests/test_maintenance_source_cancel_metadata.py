"""Read-only D-34 labels do not alter the cancellation execution contract."""
from uuid import uuid4

from app.models.dimensions import DimPart
from app.models.maintenance import FMaintenanceLine
from tests.test_maintenance_return_metrics import _issue
from tests.test_maintenance_source_cancel_cascade import confirm_body, setup
from tests.test_maintenance_source_cancel_concurrency import _business_snapshot

ALL_PROTECTED = [
    "description", "line_note", "part_id", "pn_raw", "pn_std", "qty",
    "return_qty", "serial_numbers",
]


def test_detail_labels_manual_legacy_and_field_protected_demands_without_writes(db, tmp_path):
    client, project, _, parent, lines, exact, unknown, _, _, _, detail_url = setup(db, tmp_path)
    lines[0].manual_override = {
        "qty": {"value": "3", "source_value": "2", "updated_by": "private-operator"},
        "pn_raw": {"value": lines[0].pn_raw, "source_value": "PRIVATE-PN"},
    }
    lines[1].edited_source = "workbook_manual"
    added = []
    for raw_id in ("manual-line:one", "page-manual-two", "workbook-create-three", "SOURCE-PLAIN"):
        row = FMaintenanceLine(
            raw_line_id=raw_id, order_id=parent.id, part_id=lines[0].part_id,
            pn_std=lines[0].pn_std, qty=1, edited_source="wbdd",
            import_batch_id=parent.import_batch_id,
        )
        db.add(row)
        added.append(row)
    db.flush()
    manual_issue = _issue(
        db, project, db.get(DimPart, lines[0].part_id), "1", source_order_id=parent.raw_order_id,
    )
    manual_issue.source_line_id = added[0].raw_line_id
    # A conflicting header cannot inherit labels merely by matching a raw ID.
    unknown.source_line_id = added[0].raw_line_id
    unknown.source_order_id = "different-source-order"
    db.commit()
    before = _business_snapshot(db)

    response = client.get(detail_url)

    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail["executable"] is True
    demands = {row["line_id"]: row for row in detail["demands"]}
    assert demands[lines[0].id]["manual_created"] is False
    assert demands[lines[0].id]["protected_fields"] == ["part_id", "pn_raw", "pn_std", "qty"]
    assert demands[lines[1].id]["manual_created"] is False
    assert demands[lines[1].id]["protected_fields"] == ALL_PROTECTED
    for row in added[:3]:
        assert demands[row.id]["manual_created"] is True
        assert demands[row.id]["protected_fields"] == ALL_PROTECTED
    assert demands[added[3].id]["manual_created"] is False
    assert demands[added[3].id]["protected_fields"] == []
    sites = {row["issue_line_id"]: row for row in detail["exact_site_lines"]}
    assert sites[exact.issue_line_id]["source_demand_line_id"] == lines[0].id
    assert sites[exact.issue_line_id]["manual_created"] is False
    assert sites[exact.issue_line_id]["protected_fields"] == ["part_id", "pn_raw", "pn_std", "qty"]
    assert sites[manual_issue.issue_line_id]["manual_created"] is True
    assert sites[manual_issue.issue_line_id]["protected_fields"] == ALL_PROTECTED
    kept = detail["unlinked_site_lines"][0]
    assert kept["issue_line_id"] == unknown.issue_line_id
    assert kept["source_demand_line_id"] is None
    assert kept["manual_created"] is False and kept["protected_fields"] == []
    assert "private-operator" not in response.text and "PRIVATE-PN" not in response.text
    db.expire_all()
    assert _business_snapshot(db) == before

    confirmed = client.post(detail_url + "/confirm", json=confirm_body(client, detail_url))
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["voided_lines"] == 6
    assert confirmed.json()["voided_site_lines"] == 2


def test_protection_ledger_drift_still_conflicts_and_original_receipt_still_replays(db, tmp_path):
    client, _, _, _, lines, _, _, _, _, _, detail_url = setup(db, tmp_path)
    old_body = confirm_body(client, detail_url)
    lines[0].manual_override = {"line_note": {"value": "人工保留", "source_value": None}}
    db.commit()
    response = client.get(detail_url)
    assert response.status_code == 200
    detail = response.json()
    assert detail["demands"][0]["protected_fields"] == ["line_note"]
    assert detail["expected_digest"] != old_body["expected_digest"]
    assert client.post(detail_url + "/confirm", json=old_body).status_code == 409
    body = {**old_body, "expected_digest": detail["expected_digest"], "idempotency_key": str(uuid4())}
    first = client.post(detail_url + "/confirm", json=body)
    assert first.status_code == 200
    replay = client.post(detail_url + "/confirm", json=body)
    assert replay.status_code == 200
    assert replay.json() == {**first.json(), "replayed": True}

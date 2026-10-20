"""Unknown network outcomes can be resolved by a scoped, read-only receipt."""
from tests.boss_board_helpers import client_for, make_project
from tests.test_maintenance_source_cancel_cascade import confirm_body, setup
from tests.test_maintenance_source_cancel_concurrency import _business_snapshot


def test_confirmation_status_is_read_only_before_and_after_candidate_disappears(db, tmp_path):
    client, _, _, _, _, _, _, _, _, url, detail_url = setup(db, tmp_path)
    body = confirm_body(client, detail_url)
    params = {"idempotency_key": body["idempotency_key"]}
    before = _business_snapshot(db)
    response = client.get(detail_url + "/confirmation-status", params=params)
    assert response.status_code == 200, response.text
    assert response.json() == {"result": None}
    assert response.headers["Cache-Control"] == "no-store"
    db.expire_all()
    assert _business_snapshot(db) == before

    first = client.post(detail_url + "/confirm", json=body)
    assert first.status_code == 200, first.text
    assert client.get(url).json()["items"] == []
    assert client.get(detail_url).status_code == 409
    db.expire_all()
    before = _business_snapshot(db)
    response = client.get(detail_url + "/confirmation-status", params=params)
    assert response.status_code == 200, response.text
    assert response.json() == {"result": {**first.json(), "replayed": True}}
    assert response.headers["Cache-Control"] == "no-store"
    db.expire_all()
    assert _business_snapshot(db) == before
    assert "request_digest" not in response.text and "operated_by" not in response.text


def test_confirmation_status_is_bound_to_actor_project_candidate_and_permission(db, tmp_path):
    client, _, _, parent, _, _, _, _, batch, _, detail_url = setup(db, tmp_path)
    body = confirm_body(client, detail_url)
    assert client.post(detail_url + "/confirm", json=body).status_code == 200
    params = {"idempotency_key": body["idempotency_key"]}
    other_admin = client_for(db, username="other-cancel-admin", role="admin")
    response = other_admin.get(detail_url + "/confirmation-status", params=params)
    assert response.status_code == 200, response.text
    assert response.json() == {"result": None}
    no_action = client_for(db, username="cancel-status-no-action", role="boss", overrides={
        "page_maintenance": True, "action_maintenance_demand_delete": False,
    })
    assert no_action.get(detail_url + "/confirmation-status", params=params).status_code == 403
    outsider = client_for(db, username="cancel-status-outsider", overrides={
        "page_maintenance": True, "action_maintenance_demand_delete": True,
    })
    assert outsider.get(detail_url + "/confirmation-status", params=params).status_code == 403
    other_project = make_project(db, "OTHER-RECEIPT-PROJECT")
    db.commit()
    mismatched = [
        detail_url.rsplit("/", 1)[0] + "/different-order",
        f"/api/maintenance/projects/{other_project.project_id}/source-cancellations/{batch.id}/{parent.raw_order_id}",
    ]
    for path in mismatched:
        assert client.get(path + "/confirmation-status", params=params).status_code == 409
    assert client.get(detail_url + "/confirmation-status",
                      params={"idempotency_key": "short"}).status_code == 422

"""The real Shop delegation exchanges exact-byte proof for atomic create+binding."""
# ruff: noqa: F811
import json
from uuid import uuid4

from tests.test_pn_catalog_access import authority, cerbos_url  # noqa: F401
from tests.test_pn_catalog_authority_http import http, exchange  # noqa: F401


def test_shop_create_reference_json_uuid_and_exact_recovery(http, authority):
    source_id = uuid4()
    key = uuid4()
    target = "/api/pn-catalog/v1/parts"
    body = json.dumps({"pn_std": "SHOP-EXACT-REFERENCE", "external_reference": {
        "namespace": "shop", "source_id": str(source_id)}}).encode()
    headers, _ = exchange(http, authority[2], "POST", target, body, key=key)
    response = http.post(target, content=body, headers={**headers, "content-type": "application/json"})
    assert response.status_code == 201, response.text
    pid = response.json()["data"]["part_id"]
    target = f"/api/pn-catalog/v1/references/shop/{source_id}"
    headers, _ = exchange(http, authority[2], "GET", target)
    found = http.get(target, headers=headers)
    assert found.status_code == 200, found.text
    assert found.json()["data"]["part_id"] == pid
    assert found.json()["data"]["canonical_part_id"] == pid
    target = f"/api/pn-catalog/v1/references?namespace=shop&source_ids={source_id}&source_ids={uuid4()}"
    headers, _ = exchange(http, authority[2], "GET", target)
    bulk = http.get(target, headers=headers)
    assert bulk.status_code == 200, bulk.text
    assert [row["state"] for row in bulk.json()["data"]] == ["bound", "unresolved"]


def test_shop_binds_selected_existing_pn_and_recovers_typed_receipt(http, authority):
    target = "/api/pn-catalog/v1/parts"
    body = json.dumps({"pn_std": "SELECTED-EXISTING-PN"}).encode()
    headers, _ = exchange(http, authority[2], "POST", target, body, key=uuid4())
    created = http.post(target, content=body, headers={**headers, "content-type": "application/json"})
    assert created.status_code == 201, created.text
    value = created.json()["data"]
    target = f'/api/pn-catalog/v1/parts/{value["part_id"]}/references'
    key = uuid4()
    body = json.dumps({"namespace": "shop", "source_id": str(uuid4()),
                       "expected_identity_revision": value["identity_revision"]}).encode()
    headers, _ = exchange(http, authority[2], "POST", target, body, key=key)
    bound = http.post(target, content=body, headers={**headers, "content-type": "application/json"})
    assert bound.status_code == 200, bound.text
    assert bound.json()["data"]["part_id"] == value["part_id"]
    target = f"/api/pn-catalog/v1/requests/{key}"
    headers, _ = exchange(http, authority[2], "GET", target)
    receipt = http.get(target, headers=headers)
    assert receipt.status_code == 200, receipt.text
    assert receipt.json()["data"] == bound.json()["data"]
    assert receipt.json()["meta"]["replayed"]

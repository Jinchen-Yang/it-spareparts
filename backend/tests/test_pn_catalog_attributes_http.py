"""Live Cerbos fields, native/delegated HTTP and global receipt dispatch."""
# ruff: noqa: F811
import json
from uuid import uuid4

from sqlalchemy import select

from app.models.authorization import SysAuthorizationGrant
from app.pn_catalog.models import CommandReceipt
from tests.test_pn_catalog_authority_http import http, native, exchange, seed  # noqa: F401
from tests.test_pn_catalog_access import authority  # noqa: F401
from tests.authz_pdp_helpers import cerbos_url  # noqa: F401


def test_attributes_native_delegated_null_replay_and_current_field_revocation(db, authority, http):
    pid = seed(db)
    path = f"/api/pn-catalog/v1/parts/{pid}/attributes"
    access = http.get(f"/api/pn-catalog/v1/parts/{pid}/access", headers=native(authority[0])).json()["data"]
    assert {"read_attributes", "update_attributes"} <= set(access["allowed_actions"])
    assert "attributes" in access["writable_fields"]
    assert "fill_status" in access["readable_fields"] and "fill_status" not in access["writable_fields"]
    first = http.get(path, headers=native(authority[0]))
    assert first.status_code == 200 and first.json()["data"]["items"] == {}
    key = uuid4()
    body = json.dumps({"expected_identity_revision":0,"field_versions":{"custom":0},
                      "changes":{"custom":{"operation":"set","value":None}}}).encode()
    headers, _ = exchange(http, authority[2], "PATCH", path, body, key=key)
    written = http.patch(path, content=body, headers={**headers, "Content-Type":"application/json"})
    assert written.status_code == 200, written.text
    assert written.json()["data"]["items"]["custom"]["value"] is None
    assert not written.json()["data"]["items"]["custom"]["deleted"]
    headers, _ = exchange(http, authority[2], "PATCH", path, body, key=key)
    replay = http.patch(path, content=body, headers={**headers, "Content-Type":"application/json"})
    assert replay.status_code == 200 and replay.json()["meta"]["replayed"]
    receipt_path = f"/api/pn-catalog/v1/requests/{key}"
    headers, _ = exchange(http, authority[2], "GET", receipt_path)
    assert http.get(receipt_path, headers=headers).json()["data"] == written.json()["data"]
    grant = db.scalar(select(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id==authority[0].id,
        SysAuthorizationGrant.resource=="part_master"))
    grant.field_permissions = [f for f in grant.field_permissions if f != "attributes"]
    grant.version += 1
    db.commit()
    denied = http.get(path, headers=native(authority[0]))
    assert denied.status_code == 403 and "custom" not in denied.text
    headers, _ = exchange(http, authority[2], "GET", receipt_path)
    denied = http.get(receipt_path, headers=headers)
    assert denied.status_code == 403 and "custom" not in denied.text
    assert len(db.scalars(select(CommandReceipt)).all()) == 1


def test_attribute_http_baseline_and_strict_shape(db, authority, http):
    pid = seed(db)
    path = f"/api/pn-catalog/v1/parts/{pid}/attributes"
    headers = {**native(authority[0]), "Idempotency-Key":str(uuid4())}
    missing = http.patch(path, json={"field_versions":{"k":0},"changes":{"k":{"operation":"delete"}}},headers=headers)
    assert missing.status_code == 428
    wrong = http.patch(path,json={"expected_identity_revision":0,"field_versions":{"k":0},
                                "changes":{"k":{"operation":"set"}}},headers=headers)
    assert wrong.status_code == 422
    oversized = http.patch(path,content=b'x'*65537,headers=headers)
    assert oversized.status_code == 413

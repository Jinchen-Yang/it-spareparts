"""Migration recovery remains native-only under the real current authority."""

# ruff: noqa: F811
from uuid import uuid4
from sqlalchemy import select
from app.models.authorization import SysAuthorizationGrant
from app.pn_catalog.migration_models import LegacySourceRecord
from tests.test_pn_catalog_authority_http import http, native, exchange  # noqa: F401
from tests.test_pn_catalog_access import authority  # noqa: F401
from tests.authz_pdp_helpers import cerbos_url  # noqa: F401
from tests.test_pn_catalog_migration_media_state import record, prepare, apply


def test_native_review_recovery_receipt_and_current_permission(db, authority, http):
    user, _, subject = authority
    payload = {
        "request_id": str(uuid4()),
        "actor_id": str(subject),
        "status": "running",
    }
    value, command = prepare(
        db, [record("manual_research", payload, payload["request_id"])]
    )
    apply(db, value, command)
    db.commit()
    path = (
        f"/api/pn-catalog/v1/migration/snapshots/{value.namespace}/{value.snapshot_id}"
    )
    headers = native(user)
    snapshots = http.get("/api/pn-catalog/v1/migration/snapshots", headers=headers)
    assert snapshots.status_code == 200, snapshots.text
    assert snapshots.json()["data"][0]["verification_scope"] == "isolated_copy"
    response = http.get(path + "/records", headers=headers)
    assert response.status_code == 200, response.text
    row = response.json()["data"][0]
    assert row["state"] == "recovery_required"
    evidence_path = (
        f"/api/pn-catalog/v1/migration/records/{row['source_record_id']}/evidence"
    )
    evidence = http.get(evidence_path, headers=headers)
    assert evidence.status_code == 200, evidence.text
    assert evidence.json()["data"]["payload"] == payload
    assert evidence.json()["data"]["source_sha256"] == row["source_sha256"]
    body = {
        "decision": "retire_fenced",
        "expected_revision": row["revision"],
        "source_sha256": row["source_sha256"],
        "evidence_ref": "isolated-review:no-remaining-effect",
        "reason": "已核对并结束原任务",
        "external_outcome": "no_external_effect",
    }
    key = str(uuid4())
    result = http.post(
        f"/api/pn-catalog/v1/migration/records/{row['source_record_id']}/recovery",
        json=body,
        headers={**headers, "Idempotency-Key": key},
    )
    assert result.status_code == 200, result.text
    assert result.json()["data"]["state"] == "historical_terminal"
    receipt = http.get("/api/pn-catalog/v1/migration/requests/" + key, headers=headers)
    assert receipt.status_code == 200 and receipt.json()["meta"]["replayed"], (
        receipt.text
    )
    progress = http.get(path, headers=headers)
    assert (
        progress.json()["data"]["live_complete"]
        and not progress.json()["data"]["cutover_ready"]
    )
    grant = db.scalar(
        select(SysAuthorizationGrant).where(
            SysAuthorizationGrant.user_id == user.id,
            SysAuthorizationGrant.resource == "part_master",
        )
    )
    grant.field_permissions = [
        f for f in grant.field_permissions if f != "legacy_evidence"
    ]
    grant.version += 1
    db.commit()
    assert http.get(evidence_path, headers=headers).status_code == 403
    assert (
        http.get(
            "/api/pn-catalog/v1/migration/requests/" + key, headers=headers
        ).status_code
        == 403
    )
    # Full historical records are immutable even after a recovery decision.
    assert db.scalar(select(LegacySourceRecord)).payload == payload


def test_shop_delegation_cannot_reach_native_migration_route(db, authority, http):
    path = "/api/pn-catalog/v1/migration/snapshots"
    headers, _ = exchange(http, authority[2], "GET", path)
    assert http.get(path, headers=headers).status_code in {401, 403}

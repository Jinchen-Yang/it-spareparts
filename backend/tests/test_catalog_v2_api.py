"""D-36 v2 HTTP seam: isolated PG, fixed publications and independent credentials."""
import hashlib
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import get_settings
from app.db import get_db
from app.main import app

PREFIX = "/api/integrations/catalog/v2"
TOKEN = "synthetic-catalog-v2-read-token-for-tests"
V1_TOKEN = "synthetic-independent-v1-catalog-token"


@pytest.fixture()
def v2_client(db, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "catalog_v2_read_clients", {
        "shop": SecretStr(hashlib.sha256(TOKEN.encode()).hexdigest()),
        "other": SecretStr(hashlib.sha256(b"other-client-token").hexdigest()),
    })
    monkeypatch.setattr(settings, "resale_catalog_token_sha256",
                        SecretStr(hashlib.sha256(V1_TOKEN.encode()).hexdigest()))
    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = lambda: db
    try:
        with TestClient(app) as client:
            client.headers["Authorization"] = f"Bearer {TOKEN}"
            yield client
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = previous


def assert_error(response, status, code):
    assert response.status_code == status, response.text
    error = response.json()["error"]
    assert set(error) == {"code", "message", "retryable", "request_id", "details"}
    assert error["code"] == code
    assert isinstance(error["details"], dict)


def test_disabled_v2_and_independent_v1_credential(v2_client, monkeypatch):
    body = {"schema_version": 2, "request_id": str(uuid4())}
    assert_error(v2_client.post(f"{PREFIX}/snapshots", json=body,
                               headers={"Authorization": f"Bearer {V1_TOKEN}"}),
                 401, "invalid_service_credential")
    monkeypatch.setattr(get_settings(), "catalog_v2_read_clients", {})
    assert_error(v2_client.post(f"{PREFIX}/snapshots", json=body),
                 404, "integration_disabled")


def test_read_credentials_do_not_expose_d2_routes(v2_client):
    for route in ("commands", "duplicate-checks"):
        assert v2_client.post(f"{PREFIX}/{route}", json={}).status_code == 404
    assert v2_client.get(f"{PREFIX}/commands/{uuid4()}").status_code == 404


def _published(db, count=3):
    from app.models.catalog_v2 import CatalogV2Epoch, CatalogV2Projection
    from app.services.catalog_v2.canonical import node_hash
    epoch = CatalogV2Epoch(status="ready")
    db.add(epoch)
    db.flush()
    nodes = []
    for i in range(1, count + 1):
        node = dict(part_id=i, pn_std=f"API-{i}", status="active", merged_into_id=None,
                    canonical_part_id=i, needs_review=False, is_excluded=False,
                    machine_or_part=None, description=None, brand=None, category_major=None,
                    category_minor=None, unit=None, locked_fields=[], specs=[],
                    aggregate_revision=1, identity_revision=1)
        node["content_hash"] = node_hash(node)
        nodes.append(node)
        db.add(CatalogV2Projection(epoch_id=epoch.id, kind="node", entity_id=i,
                                  part_id=i, data=node, content_hash=node["content_hash"]))
    db.commit()
    return epoch.id, nodes


def _snapshot(client, request_id=None):
    response = client.post(f"{PREFIX}/snapshots", json={
        "schema_version": 2, "request_id": request_id or str(uuid4()),
    })
    assert response.status_code == 200, response.text
    return response.json()


def test_snapshot_is_idempotent_materialized_and_paged_without_source_reads(db, v2_client):
    from app.models.catalog_v2 import CatalogV2Projection
    epoch, nodes = _published(db)
    request_id = str(uuid4())
    receipt = _snapshot(v2_client, request_id)
    retry = _snapshot(v2_client, request_id)
    for field in ("snapshot_id", "publication_epoch", "watermark", "expires_at", "content_hash"):
        assert receipt[field] == retry[field]
    assert receipt["request_id"] == request_id
    assert receipt["publication_epoch"] == epoch
    assert receipt["node_count"] == 3
    assert receipt["alias_count"] == 0
    first = v2_client.get(f"{PREFIX}/list", params={"snapshot_id": receipt["snapshot_id"], "limit": 1})
    assert first.status_code == 200, first.text
    page = first.json()
    assert page["items"] == [{"kind": "node", "node": nodes[0]}]
    assert page["next_cursor"] and page["complete"] is False
    # The published projection advances between HTTP pages; this snapshot stays fixed.
    projection = db.get(CatalogV2Projection, (epoch, "node", 2))
    projection.data = dict(projection.data, description="changed after snapshot")
    db.commit()
    next_page = v2_client.get(f"{PREFIX}/list", params={
        "snapshot_id": receipt["snapshot_id"], "limit": 1, "cursor": page["next_cursor"],
    }).json()
    assert next_page["items"] == [{"kind": "node", "node": nodes[1]}]
    assert next_page["content_hash"] == receipt["content_hash"]
    final = v2_client.get(f"{PREFIX}/list", params={
        "snapshot_id": receipt["snapshot_id"], "limit": 1, "cursor": next_page["next_cursor"],
    }).json()
    assert final["complete"] is True and final["next_cursor"] is None
    assert final["items"] == [{"kind": "node", "node": nodes[2]}]


@pytest.mark.parametrize("body,code", [
    ({"schema_version": 3, "request_id": str(uuid4())}, "unsupported_schema"),
    ({"schema_version": 2, "request_id": str(uuid4()), "extra": 1}, "invalid_field"),
    ({"schema_version": 2, "request_id": "not-a-uuid"}, "invalid_field"),
    ({"schema_version": 2.0, "request_id": str(uuid4())}, "invalid_field"),
    ({"schema_version": True, "request_id": str(uuid4())}, "invalid_field"),
])
def test_snapshot_rejects_unknown_fields_and_invalid_json_types(v2_client, body, code):
    assert_error(v2_client.post(f"{PREFIX}/snapshots", json=body), 422, code)


def test_snapshot_request_body_limit_duplicate_keys_and_not_ready(v2_client):
    assert_error(v2_client.post(f"{PREFIX}/snapshots", content=b" " * (65536 + 1)),
                 413, "payload_too_large")
    assert_error(v2_client.post(f"{PREFIX}/snapshots", content='{"schema_version":2,"schema_version":2}'),
                 422, "invalid_field")
    assert_error(v2_client.post(f"{PREFIX}/snapshots", json={
        "schema_version": 2, "request_id": str(uuid4()),
    }), 503, "publication_not_ready")


def test_list_fragments_are_persisted_repeatable_recoverable_and_client_bound(db, v2_client, monkeypatch):
    import base64
    import json
    from app.models.catalog_v2 import CatalogV2Projection
    from app.services.catalog_v2.canonical import node_hash
    epoch, _ = _published(db, 1)
    row = db.get(CatalogV2Projection, (epoch, "node", 1))
    row.data = dict(row.data, description="合成分片😀" * 1500)
    row.data = dict(row.data, content_hash=node_hash(row.data))
    row.content_hash = row.data["content_hash"]
    db.commit()
    monkeypatch.setattr(get_settings(), "catalog_v2_response_max_bytes", 4096)
    monkeypatch.setattr(get_settings(), "catalog_v2_fragment_max_bytes", 1999)
    receipt = _snapshot(v2_client)
    params = {"snapshot_id": receipt["snapshot_id"], "limit": 1}
    first = v2_client.get(f"{PREFIX}/list", params=params)
    assert first.status_code == 200, first.text
    first = first.json()
    assert first["transfer_mode"] == "base64_fragments"
    assert first["part_count"] > 1
    repeated = v2_client.get(f"{PREFIX}/list", params=params).json()
    assert repeated["payload_id"] == first["payload_id"]
    assert repeated["part_hash"] == first["part_hash"]
    assert_error(v2_client.get(f"{PREFIX}/list", params=dict(params, cursor=first["next_cursor"]),
                              headers={"Authorization": "Bearer other-client-token"}),
                 422, "invalid_cursor")
    assert_error(v2_client.get(f"{PREFIX}/list", params=dict(params, cursor=first["next_cursor"], limit=2)),
                 422, "invalid_cursor")
    chunks, fragment = [], first
    while True:
        raw = base64.b64decode(fragment["payload_fragment"], validate=True)
        assert hashlib.sha256(raw).hexdigest() == fragment["part_hash"]
        chunks.append(raw)
        if fragment["complete"]:
            break
        response = v2_client.get(f"{PREFIX}/list", params=dict(params, cursor=fragment["next_cursor"]))
        assert len(response.content) <= 4096
        fragment = response.json()
        assert len(chunks) <= first["part_count"]
    payload = b"".join(chunks)
    assert hashlib.sha256(payload).hexdigest() == first["payload_hash"]
    assert hashlib.sha256(b"".join(reversed(chunks))).hexdigest() != first["payload_hash"]
    assert hashlib.sha256(payload[:-1]).hexdigest() != first["payload_hash"]
    logical = json.loads(payload)
    assert logical["items"][0]["node"]["description"] == row.data["description"]
    assert logical["complete"] and logical["next_cursor"] is None


def test_empty_snapshot_and_feed_use_contract_literal_hash(db, v2_client):
    epoch, _ = _published(db, 0)
    snapshot = _snapshot(v2_client)
    assert snapshot["content_hash"] == "cc504394eabdea844ec9aa5c696825dffd0cf087e071f7b7c343dc06ba7a10cd"
    page = v2_client.get(f"{PREFIX}/list", params={"snapshot_id": snapshot["snapshot_id"]}).json()
    assert page["items"] == [] and page["complete"] and page["next_cursor"] is None
    changes = v2_client.get(f"{PREFIX}/changes", params={"publication_epoch": epoch, "after": 0})
    assert changes.status_code == 200, changes.text
    assert {key: changes.json()[key] for key in (
        "batch_id", "published_sequence", "previous_sequence", "transaction_groups", "items",
        "alias_moves", "batch_hash", "complete", "next_cursor",
    )} == dict(batch_id=None, published_sequence=0, previous_sequence=0,
               transaction_groups=[], items=[], alias_moves=[], batch_hash=None,
               complete=True, next_cursor=None)
    digest = v2_client.get(f"{PREFIX}/digests", params={"snapshot_id": snapshot["snapshot_id"]}).json()
    assert digest["segments"] == [{"start_part_id": 1, "end_part_id_exclusive": 1001,
        "node_count": 0, "alias_count": 0,
        "content_hash": "cc504394eabdea844ec9aa5c696825dffd0cf087e071f7b7c343dc06ba7a10cd"}]


def test_cursors_bind_client_snapshot_route_limit_and_reject_tampering(db, v2_client):
    _published(db)
    snapshot, another = _snapshot(v2_client), _snapshot(v2_client)
    query = {"snapshot_id": snapshot["snapshot_id"], "limit": 1}
    first = v2_client.get(f"{PREFIX}/list", params=query).json()
    cursor = first["next_cursor"]
    # Cursor reveals no position, identity, or JSON; only a random signed handle.
    assert "snapshot" not in cursor and snapshot["snapshot_id"] not in cursor
    for params in (dict(query, cursor=cursor[:-1] + ("A" if cursor[-1] != "A" else "B")),
                   dict(query, cursor=cursor.split(".")[0] + ".é"),
                   dict(query, cursor=cursor, limit=2),
                   dict(query, cursor=cursor, snapshot_id=another["snapshot_id"])):
        assert_error(v2_client.get(f"{PREFIX}/list", params=params), 422, "invalid_cursor")
    assert_error(v2_client.get(f"{PREFIX}/digests", params=dict(query, cursor=cursor)),
                 422, "invalid_cursor")
    assert_error(v2_client.get(f"{PREFIX}/list", params=dict(query, cursor=cursor),
                              headers={"Authorization": "Bearer other-client-token"}),
                 422, "invalid_cursor")
    for field in ({"extra": 1}, {"limit": 501}, {"limit": 0}, {"limit": "1.0"}):
        assert_error(v2_client.get(f"{PREFIX}/list", params=dict(query, **field)), 422, "invalid_field")
    assert_error(v2_client.get(f"{PREFIX}/list", params=[("snapshot_id", snapshot["snapshot_id"]),
                                                      ("limit", "1"), ("limit", "2")]),
                 422, "invalid_field")


def test_snapshot_expiry_epoch_change_retention_and_missing_watermark(db, v2_client):
    from datetime import datetime, timedelta, timezone
    from app.models.catalog_v2 import CatalogV2Epoch, CatalogV2Snapshot
    epoch, _ = _published(db)
    snapshot = _snapshot(v2_client)
    row = db.get(CatalogV2Snapshot, snapshot["snapshot_id"])
    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    assert_error(v2_client.get(f"{PREFIX}/list", params={"snapshot_id": row.snapshot_id}),
                 410, "snapshot_expired")
    assert_error(v2_client.post(f"{PREFIX}/snapshots", json={"schema_version": 2, "request_id": row.request_id}),
                 410, "snapshot_expired")
    query = {"publication_epoch": epoch, "after": 1}
    response = v2_client.get(f"{PREFIX}/changes", params=query)
    assert_error(response, 410, "cursor_expired")
    assert response.json()["error"]["details"] == {"reason": "watermark_unavailable"}
    epoch_row = db.get(CatalogV2Epoch, epoch)
    epoch_row.retained_from_sequence = 1
    epoch_row.watermark = 1
    db.commit()
    response = v2_client.get(f"{PREFIX}/changes", params=dict(query, after=0))
    assert_error(response, 410, "cursor_expired")
    assert response.json()["error"]["details"] == {"reason": "retention_expired"}
    epoch_row.active = False
    db.flush()
    db.add(CatalogV2Epoch(status="ready"))
    db.commit()
    response = v2_client.get(f"{PREFIX}/changes", params=query)
    assert_error(response, 410, "cursor_expired")
    assert response.json()["error"]["details"] == {"reason": "epoch_changed"}


def test_current_parts_and_v1_dtos_stay_separate(db, v2_client, monkeypatch):
    from app.db import engine
    from app.models.dimensions import DimPart
    from app.services.catalog_v2.publisher import seed, publish_once
    part = DimPart(pn_std="HTTP-SOURCE-CURRENT", description="original")
    db.add(part)
    db.commit()
    part_id = part.id
    db.rollback()
    epoch = seed(engine)
    first = v2_client.get(f"{PREFIX}/parts/{part_id}")
    assert first.status_code == 200, first.text
    assert first.json()["read_basis"] == "source_current"
    assert first.json()["publication_status"] == "published"
    assert first.json()["publication_epoch"] == epoch
    part = db.get(DimPart, part_id)
    part.description = "source ahead"
    db.commit()
    pending = v2_client.get(f"{PREFIX}/parts/{part_id}").json()
    assert pending["node"]["description"] == "source ahead"
    assert pending["publication_status"] == "pending" and pending["published_sequence"] is None
    db.rollback()
    publish_once(engine)
    assert v2_client.get(f"{PREFIX}/parts/{part_id}").json()["publication_status"] == "published"
    assert_error(v2_client.get(f"{PREFIX}/parts/999999"), 404, "part_not_found")
    assert_error(v2_client.get(f"{PREFIX}/parts/0"), 422, "invalid_field")
    assert_error(v2_client.get(f"{PREFIX}/parts/9007199254740992"), 422, "invalid_field")
    headers = {"Authorization": f"Bearer {V1_TOKEN}"}
    old_list = v2_client.post("/api/integrations/resale/parts/list", json={}, headers=headers).json()
    assert old_list["schemaVersion"] == 1
    assert set(old_list["items"][0]) == {
        "partId", "pnStd", "description", "brand", "categoryMajor", "categoryMinor", "unit",
        "needsReview", "isExcluded", "lockedFields", "updatedAt", "specs", "status",
        "mergedIntoId", "mergedIntoPnStd", "machineOrPart",
    }
    old_snapshot = v2_client.post("/api/integrations/resale/parts/snapshots",
                                  json={"partIds": [part_id]}, headers=headers).json()
    assert old_snapshot["schemaVersion"] == 1
    assert set(old_snapshot["items"][0]) == {
        "requestedPartId", "partId", "status", "pnStd", "description", "brand",
        "categoryMajor", "categoryMinor", "unit", "needsReview", "isExcluded", "lockedFields",
        "updatedAt", "specs", "salesRecordCount", "salesRecordCountAsOf",
    }
    monkeypatch.setattr(get_settings(), "catalog_v2_response_max_bytes", 64)
    assert_error(v2_client.get(f"{PREFIX}/parts/{part_id}"), 413, "aggregate_too_large")


def test_background_publisher_is_opt_in_and_stops_on_shutdown(monkeypatch):
    from app.services.catalog_v2 import publisher
    calls = []

    async def loop(stop, engine, interval):
        calls.append(("start", interval))
        await stop.wait()
        calls.append(("stop", interval))

    monkeypatch.setattr(publisher, "publisher_loop", loop)
    monkeypatch.setattr(get_settings(), "catalog_v2_publisher_enabled", False)
    with TestClient(app):
        pass
    assert calls == []
    monkeypatch.setattr(get_settings(), "catalog_v2_publisher_enabled", True)
    with TestClient(app):
        pass
    assert calls == [("start", 30), ("stop", 30)]


def test_changes_fragments_restore_one_atomic_batch_and_never_advance_empty_feed(db, v2_client, monkeypatch):
    import base64
    import json
    from app.db import engine
    from app.models.dimensions import DimPart
    from app.services.catalog_v2.canonical import batch_hash
    from app.services.catalog_v2.publisher import seed, publish_once
    part = DimPart(pn_std="API-LARGE-BATCH")
    db.add(part)
    db.commit()
    part_id = part.id
    db.rollback()
    epoch = seed(engine)
    monkeypatch.setattr(get_settings(), "catalog_v2_response_max_bytes", 4096)
    monkeypatch.setattr(get_settings(), "catalog_v2_fragment_max_bytes", 1201)
    part = db.get(DimPart, part_id)
    part.description = "完整规格与事务😀" * 1300
    db.commit()
    db.rollback()
    publish_once(engine)
    query = {"publication_epoch": epoch, "after": 0, "limit": 1}
    first = v2_client.get(f"{PREFIX}/changes", params=query)
    assert first.status_code == 200, first.text
    fragment = first.json()
    assert fragment["transfer_mode"] == "base64_fragments"
    payload_hash, chunks = fragment["payload_hash"], []
    first_id = fragment["payload_id"]
    assert v2_client.get(f"{PREFIX}/changes", params=query).json()["payload_id"] == first_id
    assert_error(v2_client.get(f"{PREFIX}/changes", params=dict(query, cursor=fragment["next_cursor"], limit=2)),
                 422, "invalid_cursor")
    assert_error(v2_client.get(f"{PREFIX}/changes", params=dict(query, cursor=fragment["next_cursor"]),
                              headers={"Authorization": "Bearer other-client-token"}),
                 422, "invalid_cursor")
    while True:
        raw = base64.b64decode(fragment["payload_fragment"], validate=True)
        assert hashlib.sha256(raw).hexdigest() == fragment["part_hash"]
        chunks.append(raw)
        if fragment["complete"]:
            assert fragment["next_cursor"] is None
            break
        response = v2_client.get(f"{PREFIX}/changes", params=dict(query, cursor=fragment["next_cursor"]))
        assert response.status_code == 200, response.text
        assert len(response.content) <= 4096
        fragment = response.json()
    payload = b"".join(chunks)
    assert hashlib.sha256(payload).hexdigest() == payload_hash
    logical = json.loads(payload)
    assert batch_hash(logical) == logical["batch_hash"]
    assert logical["previous_sequence"] == 0
    assert logical["items"][0]["node"]["part_id"] == part_id
    applied = logical["published_sequence"]
    empty = v2_client.get(f"{PREFIX}/changes", params=dict(query, after=applied)).json()
    assert empty["batch_id"] is None
    assert empty["previous_sequence"] == empty["published_sequence"] == applied


def test_list_fragment_tail_continues_to_next_logical_page(db, v2_client, monkeypatch):
    import base64
    import json
    from app.models.catalog_v2 import CatalogV2Projection
    from app.services.catalog_v2.canonical import node_hash
    epoch, _ = _published(db, 2)
    row = db.get(CatalogV2Projection, (epoch, "node", 1))
    row.data = dict(row.data, description="完整大节点" * 1400)
    row.data = dict(row.data, content_hash=node_hash(row.data))
    row.content_hash = row.data["content_hash"]
    db.commit()
    monkeypatch.setattr(get_settings(), "catalog_v2_response_max_bytes", 4096)
    monkeypatch.setattr(get_settings(), "catalog_v2_fragment_max_bytes", 1999)
    snapshot = _snapshot(v2_client)
    query = {"snapshot_id": snapshot["snapshot_id"], "limit": 1}
    fragment = v2_client.get(f"{PREFIX}/list", params=query).json()
    chunks = []
    while True:
        chunks.append(base64.b64decode(fragment["payload_fragment"]))
        if fragment["complete"]:
            break
        fragment = v2_client.get(f"{PREFIX}/list", params=dict(query, cursor=fragment["next_cursor"])).json()
    logical = json.loads(b"".join(chunks))
    assert logical["complete"] is False
    assert logical["next_cursor"] == fragment["next_cursor"]
    last = v2_client.get(f"{PREFIX}/list", params=dict(query, cursor=fragment["next_cursor"])).json()
    assert last["items"][0]["node"]["part_id"] == 2
    assert last["complete"] and last["next_cursor"] is None


def test_digests_include_empty_segments_and_fixed_watermark(db, v2_client):
    from app.models.catalog_v2 import CatalogV2Epoch, CatalogV2Projection
    from app.services.catalog_v2.canonical import node_hash
    epoch, nodes = _published(db, 1)
    high = dict(nodes[0], part_id=2001, canonical_part_id=2001, pn_std="API-HIGH")
    high["content_hash"] = node_hash(high)
    alias = dict(alias_id=1, pn_raw="OLD-HIGH", part_id=2001, pn_std="API-HIGH", status=None, deleted=False)
    db.add(CatalogV2Projection(epoch_id=epoch, kind="node", entity_id=2001, part_id=2001,
                              data=high, content_hash=high["content_hash"]))
    db.add(CatalogV2Projection(epoch_id=epoch, kind="alias", entity_id=1, part_id=2001,
                              data=alias, content_hash="0" * 64))
    db.commit()
    snapshot = _snapshot(v2_client)
    first = v2_client.get(f"{PREFIX}/digests", params={"snapshot_id": snapshot["snapshot_id"], "limit": 2})
    assert first.status_code == 200, first.text
    first = first.json()
    assert [(s["node_count"], s["alias_count"]) for s in first["segments"]] == [(1, 0), (0, 0)]
    assert first["segments"][1]["content_hash"] == "cc504394eabdea844ec9aa5c696825dffd0cf087e071f7b7c343dc06ba7a10cd"
    second = v2_client.get(f"{PREFIX}/digests", params={"snapshot_id": snapshot["snapshot_id"],
                              "limit": 2, "cursor": first["next_cursor"]}).json()
    assert second["segments"][0]["start_part_id"] == 2001
    assert second["segments"][0]["node_count"] == second["segments"][0]["alias_count"] == 1
    assert second["complete"] and second["next_cursor"] is None
    row = db.get(CatalogV2Epoch, epoch)
    row.watermark = 2
    db.commit()
    fixed = v2_client.get(f"{PREFIX}/digests", params={"publication_epoch": epoch, "watermark": 0})
    assert fixed.status_code == 200, fixed.text
    assert fixed.json()["segments"] == first["segments"] + second["segments"]
    unavailable = v2_client.get(f"{PREFIX}/digests", params={"publication_epoch": epoch, "watermark": 1})
    assert_error(unavailable, 410, "cursor_expired")
    assert unavailable.json()["error"]["details"] == {"reason": "watermark_unavailable"}


def test_concurrent_same_snapshot_request_reuses_one_materialization(db, v2_client):
    from concurrent.futures import ThreadPoolExecutor
    _published(db)
    request_id = str(uuid4())
    body = {"schema_version": 2, "request_id": request_id}
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(v2_client.post, f"{PREFIX}/snapshots", json=body) for _ in range(2)]
        responses = [future.result(timeout=30) for future in futures]
    assert [response.status_code for response in responses] == [200, 200]
    assert responses[0].json()["snapshot_id"] == responses[1].json()["snapshot_id"]
    assert responses[0].json()["expires_at"] == responses[1].json()["expires_at"]

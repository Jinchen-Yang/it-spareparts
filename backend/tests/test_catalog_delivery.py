"""D-36 creation outbox: transaction, lease/CAS, bounded transport and read contract."""
import asyncio
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest
from alembic import command
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings, get_settings
from app.db import SessionLocal, engine
from app.main import app
from app.models.catalog_delivery import CatalogDelivery
from app.models.dimensions import DimPart
from app.services import catalog_delivery as svc, master_edit
from tests.conftest import _alembic_cfg
from tests.test_presales_part_creation import _client, _unbound, actors  # noqa: F401

TOKEN = "synthetic-catalog-sync-token-for-tests"
URL = "/api/integrations/resale/parts/by-ids"


def _settings(**kw):
    return Settings(_env_file=None, shop_catalog_sync_url="https://shop.example/api/integrations/catalog/parts/sync",
                    shop_catalog_sync_token=SecretStr(TOKEN), **kw)


def _create(db, pn="OUTBOX-PN"):
    return master_edit.create_part(db, pn_std=pn, force=True)


@pytest.mark.parametrize("entry", ["master", "binding", "shop"])
@pytest.mark.parametrize("fail", [False, True])
def test_all_three_create_entrypoints_commit_or_rollback_outbox(db, actors, monkeypatch, entry, fail):
    pn = "FRESH-GPU-90817"
    body = {"pn_std": pn}
    client = _client(actors.buyer)
    if entry == "binding":
        _, line = _unbound(db, actors, pn)
        body["expected_version"] = line.version
        path = f"/api/presales/lines/{line.id}/create-part"
    elif entry == "shop":
        monkeypatch.setattr(get_settings(), "pn_master_integration_token_sha256",
                            SecretStr(hashlib.sha256(TOKEN.encode()).hexdigest()))
        client = TestClient(app, headers={"Authorization": f"Bearer {TOKEN}", "X-Actor-System": "shop",
                                         "X-Actor-User": "synthetic", "X-Request-Id": str(uuid4())})
        path = "/api/integrations/pn-master/parts"
    else:
        path = "/api/parts/master"
    original = master_edit.create_part_in_transaction
    captured = []

    def create_then_fail(session, **kwargs):
        result = original(session, **kwargs)
        captured.append(session.scalar(select(func.count()).select_from(CatalogDelivery)))
        if fail:
            raise master_edit.MasterEditError("synthetic rollback after enqueue")
        return result

    monkeypatch.setattr(master_edit, "create_part_in_transaction", create_then_fail)
    network = Mock(side_effect=AssertionError("create must not send HTTP"))
    monkeypatch.setattr(svc, "deliver", network)
    response = client.post(path, json=body)
    assert captured == [1]
    expected_status = (422 if entry == "binding" else 400) if fail else 200
    assert response.status_code == expected_status
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(CatalogDelivery)) == (0 if fail else 1)
    assert db.scalar(select(func.count()).select_from(DimPart)) == (0 if fail else 1)
    network.assert_not_called()
    if entry == "shop" and not fail:
        assert client.post(path, json=body).json() == response.json()
        assert db.scalar(select(func.count()).select_from(CatalogDelivery)) == 1


def test_uncommitted_creation_cannot_be_claimed_and_only_outer_commit_wakes(db, monkeypatch):
    loop = Mock()
    monkeypatch.setattr(svc, "_wake_loop", loop)
    monkeypatch.setattr(svc, "_wake_event", SimpleNamespace(set=Mock()))
    with db.begin_nested():
        master_edit.create_part_in_transaction(db, pn_std="WAKE-PN", force=True)
    loop.call_soon_threadsafe.assert_not_called()
    assert svc.claim_one() is None
    db.commit()
    loop.call_soon_threadsafe.assert_called_once()
    assert svc.claim_one() is not None


def test_rollback_never_wakes_or_leaves_task(db, monkeypatch):
    loop = Mock()
    monkeypatch.setattr(svc, "_wake_loop", loop)
    monkeypatch.setattr(svc, "_wake_event", SimpleNamespace(set=Mock()))
    master_edit.create_part_in_transaction(db, pn_std="ROLLBACK-PN", force=True)
    db.rollback()
    loop.call_soon_threadsafe.assert_not_called()
    assert svc.claim_one() is None


def test_concurrent_workers_lease_recovery_and_stale_ack_cas(db):
    _create(db, "LEASE-A")
    _create(db, "LEASE-B")
    with ThreadPoolExecutor(max_workers=2) as workers:
        claims = list(workers.map(lambda _: svc.claim_one(), range(2)))
    assert all(claims) and len({c.delivery_id for c in claims}) == 2
    assert svc.claim_one() is None
    stale = claims[0]
    db.execute(update(CatalogDelivery).where(CatalogDelivery.delivery_id == stale.delivery_id)
               .values(lease_until=datetime.now(timezone.utc) - timedelta(seconds=1)))
    db.commit()
    assert not svc.acknowledge(stale, "applied")
    fresh = svc.claim_one()
    assert fresh.delivery_id == stale.delivery_id and fresh.lease_token != stale.lease_token
    assert fresh.attempts == 2
    assert not svc.acknowledge(stale, "applied")
    assert svc.acknowledge(fresh, "unchanged")
    assert not svc.acknowledge(fresh, "applied")


def test_retry_backoff_and_conflict_observability(db):
    created = _create(db)
    claim = svc.claim_one()
    assert svc.acknowledge(claim, "transport_error")
    assert svc.claim_one() is None
    row = db.scalar(select(CatalogDelivery))
    assert row.state == "pending" and row.last_error_code == "transport_error"
    assert timedelta(seconds=4) <= row.available_at - row.updated_at <= timedelta(seconds=6)
    row.available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    retry = svc.claim_one()
    assert retry.delivery_id == claim.delivery_id and retry.attempts == 2
    assert svc.acknowledge(retry, "conflict")
    db.expire_all()
    status = svc.read_status(db, created["id"])
    assert status["status"] == "conflict" and status["lastErrorCode"] == "manual_conflict"
    assert svc.claim_one() is None


def _claim():
    return svc.Claim(uuid4(), 7, uuid4(), 1)


def _receipt(claim, **changes):
    return {"schemaVersion": 1, "deliveryId": str(claim.delivery_id), "partId": claim.part_id,
            "status": "applied", **changes}


@pytest.mark.parametrize("changes", [{"partId": 8}, {"deliveryId": str(uuid4())}, {"schemaVersion": True},
                                     {"partId": "7"}, {"status": "ok"}, {"unexpected": "secret"}])
def test_invalid_success_receipts_retry(changes):
    claim = _claim()
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=_receipt(claim, **changes)))
    assert asyncio.run(svc.deliver(claim, _settings(), transport=transport)) == "invalid_receipt"


@pytest.mark.parametrize("status", ["applied", "unchanged", "conflict"])
def test_exact_paired_request_receipt_and_no_transaction_over_http(db, status):
    _create(db)
    claim = svc.claim_one()

    def peer(request):
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert json.loads(request.content) == {"schemaVersion": 1, "deliveryId": str(claim.delivery_id),
                                               "partId": claim.part_id}
        # A new connection can lock it: claim transaction was committed before HTTP.
        with SessionLocal() as other, other.begin():
            row = other.scalar(select(CatalogDelivery).with_for_update(nowait=True))
            assert row.state == "delivering"
        return httpx.Response(200, json=_receipt(claim, status=status))

    result = asyncio.run(svc.deliver(claim, _settings(), transport=httpx.MockTransport(peer)))
    assert result == status and svc.acknowledge(claim, result)


def test_redirect_response_limit_and_errors_do_not_leak(caplog):
    claim = _claim()
    seen = []

    def redirect(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://other.example/secret"})

    assert asyncio.run(svc.deliver(claim, _settings(), transport=httpx.MockTransport(redirect))) == "remote_http_error"
    assert len(seen) == 1
    oversized = httpx.MockTransport(lambda _: httpx.Response(200, content=b"secret" * 1000))
    assert asyncio.run(svc.deliver(claim, _settings(), transport=oversized)) == "invalid_receipt"

    def failure(_):
        raise httpx.ConnectError("secret-token raw-SQL-and-response")

    assert asyncio.run(svc.deliver(claim, _settings(), transport=httpx.MockTransport(failure))) == "transport_error"
    assert "secret-token" not in caplog.text and "raw-SQL" not in caplog.text


def test_absolute_network_deadline(monkeypatch):
    async def slow(_):
        await asyncio.sleep(1)
        return httpx.Response(200)

    monkeypatch.setattr(svc, "HTTP_DEADLINE_SECONDS", 0.01)
    assert asyncio.run(svc.deliver(_claim(), _settings(), transport=httpx.MockTransport(slow))) == "deadline_exceeded"


def test_worker_default_off():
    async def check():
        worker = svc.DeliveryWorker(_settings())
        await worker.start()
        assert worker.task is None
        await worker.close()
    asyncio.run(check())


def test_enabled_worker_wakes_after_commit_and_finishes_claim_on_shutdown(db, monkeypatch):
    # Make a polling fallback too slow for this test: progress must come from commit wakeup.
    monkeypatch.setattr(svc, "POLL_SECONDS", 60)
    original_claim = svc.claim_one

    async def check():
        loop = asyncio.get_running_loop()
        first_poll = asyncio.Event()
        entered = asyncio.Event()
        release = asyncio.Event()

        def claim():
            result = original_claim()
            loop.call_soon_threadsafe(first_poll.set)
            return result

        async def peer(claim, _settings):
            entered.set()
            await release.wait()
            return "applied"

        monkeypatch.setattr(svc, "claim_one", claim)
        monkeypatch.setattr(svc, "deliver", peer)
        worker = svc.DeliveryWorker(_settings(shop_catalog_sync_enabled=True))
        await worker.start()
        try:
            await asyncio.wait_for(first_poll.wait(), 10)
            # Session commits from the request thread; the loop wake must be thread-safe.
            def create():
                with SessionLocal() as session:
                    return _create(session, "WORKER-COMMIT")
            created = await asyncio.to_thread(create)
            await asyncio.wait_for(entered.wait(), 10)
        finally:
            release.set()
            await asyncio.wait_for(worker.close(), 10)
        return created

    created = asyncio.run(check())
    db.expire_all()
    assert svc.read_status(db, created["id"])["status"] == "succeeded"


@pytest.mark.parametrize("url", ["https://u:p@shop.example/api/integrations/catalog/parts/sync",
    "https://shop.example/api/integrations/catalog/parts/sync?q=1", "https://shop.example/api/integrations/catalog/parts/sync#",
    "https://shop.example/wrong", "http://shop.example/api/integrations/catalog/parts/sync",
    "https://shop.example:99999/api/integrations/catalog/parts/sync"])
def test_unsafe_urls_rejected(url):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, shop_catalog_sync_url=url)


def test_enabled_requires_complete_independent_configuration():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, shop_catalog_sync_enabled=True)
    assert _settings(shop_catalog_sync_enabled=True).shop_catalog_sync_enabled
    with pytest.raises(ValidationError) as caught:
        _settings(shop_gallery_token=SecretStr(TOKEN))
    assert TOKEN not in str(caught.value)
    with pytest.raises(ValidationError) as invalid:
        Settings(_env_file=None, shop_catalog_sync_token="invalid secret credential")
    assert "invalid secret credential" not in str(invalid.value)


@pytest.fixture()
def catalog_client(monkeypatch):
    monkeypatch.setattr(get_settings(), "resale_catalog_token_sha256",
                        SecretStr(hashlib.sha256(TOKEN.encode()).hexdigest()))
    return TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"})


def test_by_ids_exact_shared_whitelist_current_version_and_original_tombstone(db, catalog_client):
    a = DimPart(pn_std="IDENTITY-A", description="x" * 4100, is_excluded=True)
    b = DimPart(pn_std="IDENTITY-B")
    db.add_all([a, b])
    db.flush()
    a.status, a.merged_into_id = "merged", b.id
    db.commit()
    response = catalog_client.post(URL, json={"partIds": [a.id, b.id, a.id, 999999]})
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    by_ids = response.json()
    listed = catalog_client.post("/api/integrations/resale/parts/list", json={}).json()
    assert by_ids == {"schemaVersion": 1, "items": sorted(listed["items"], key=lambda x: x["partId"])}
    original = by_ids["items"][0]
    assert original["partId"] == a.id and original["mergedIntoId"] == b.id
    assert original["isExcluded"] is True and original["description"] == "x" * 4000
    assert not {"salesRecordCount", "price", "inventory"} & set(original)
    b.description = "updated source after enqueue"
    db.commit()
    assert catalog_client.post(URL, json={"partIds": [b.id]}).json()["items"][0]["description"] == b.description


@pytest.mark.parametrize("body", [{"partIds": []}, {"partIds": [True]}, {"partIds": ["1"]},
    {"partIds": [0]}, {"partIds": [2**31]}, {"partIds": [1] * 101}, {"partIds": [1], "cursor": "x"}])
def test_by_ids_strict_bounded_request(catalog_client, body):
    assert catalog_client.post(URL, json=body).status_code == 422


def test_by_ids_default_off_and_wrong_credentials(catalog_client, monkeypatch):
    assert catalog_client.post(URL, json={"partIds": [1]}, headers={"Authorization": "Bearer wrong"}).status_code == 401
    monkeypatch.setattr(get_settings(), "resale_catalog_token_sha256", SecretStr(""))
    assert catalog_client.post(URL, json={"partIds": [1]}).status_code == 404


def test_by_ids_database_failure_is_retryable_and_never_logs_sql(catalog_client, monkeypatch, caplog):
    def fail(*_args):
        raise SQLAlchemyError("secret-token raw-SQL-and-response")
    monkeypatch.setattr("app.api.resale_catalog_export._identity_items", fail)
    response = catalog_client.post(URL, json={"partIds": [1]})
    assert response.status_code == 503
    assert response.json() == {"detail": "PN 目录暂不可用"}
    assert response.headers["cache-control"] == "no-store"
    assert "secret-token" not in caplog.text and "raw-SQL" not in caplog.text


def test_status_read_only_permission_and_no_secret(db, actors):
    created = _create(db)
    path = f"/api/parts/master/{created['id']}/catalog-sync"
    assert TestClient(app).get(path).status_code == 403  # Existing HTTPBearer missing-header contract.
    assert _client(actors.seller).get(path).status_code == 403
    response = _client(actors.buyer).get(path)
    assert response.status_code == 200
    assert response.json()["status"] == "pending" and response.json()["enabled"] is False
    assert not {"lease_token", "token", "url"} & set(response.json())
    assert db.scalar(select(CatalogDelivery.attempts)) == 0


def test_outbox_migration_roundtrip_preserves_part(db):
    created = _create(db)
    part_id = created["id"]
    db.close()
    try:
        command.downgrade(_alembic_cfg(), "d6e8f0a2b4c6")
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT to_regclass('catalog_delivery')")) is None
            assert conn.scalar(select(DimPart.id).where(DimPart.id == part_id)) == part_id
        command.upgrade(_alembic_cfg(), "head")
        with engine.connect() as conn:
            assert conn.scalar(select(func.count()).select_from(CatalogDelivery)) == 0
    finally:
        command.upgrade(_alembic_cfg(), "head")

"""HTTP gates, durable rejection audit, atomic recovery and real concurrency."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Event

import pytest
from app import config
from app.api import maintenance_return_receipt_import as api
from app.db import SessionLocal
from app.models.system import SysAuditLog
from app.services import maintenance_doc_import as legacy
from sqlalchemy import func, select

from tests.test_maintenance_return_receipts_api import _client
from tests.test_return_receipt_import import (
    Batch,
    Receipt,
    apply,
    imports,
    ledger,
    preview,
    workbook,
)
from tests.test_return_receipt_import import project as project

BASE = "/api/maintenance/doc-imports/return-receipts/jobs"


def client(db, username="tester", **kwargs):
    client = _client(db, username=username, **kwargs)
    client.app.include_router(api.router, prefix="/api")
    return client


def upload(client, data=None, key="upload-api-key"):
    response = client.post(
        BASE,
        files={"file": ("test.xlsx", data or workbook())},
        headers={"Idempotency-Key": key},
    )
    assert response.status_code == 202, response.text
    return response.json()["batch_id"]


def payload(client, batch_id):
    response = client.get(f"{BASE}/{batch_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ready", body
    return {"plan_hash": body["plan_hash"], "preview_token": body["preview_token"]}


def test_http_standard_job_apply_and_archive_path_is_private(db, project):
    c = client(db)
    batch_id = upload(c)
    body = payload(c, batch_id)
    response = c.get(f"{BASE}/{batch_id}")
    assert "storage_path" not in response.text
    assert response.json()["rows_total"] == 1
    response = c.post(f"{BASE}/{batch_id}/apply", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["applied_lines"] == 1
    assert c.post(f"{BASE}/{batch_id}/apply", json=body).status_code == 409


def test_owner_gate_and_request_validation_are_durably_audited(
    db, project, monkeypatch
):
    monkeypatch.setattr(config, "ENABLE_ACCESS_LOG", False)
    c = client(db)
    batch_id = upload(c)
    other = client(db, username="other")
    denied = other.get(f"{BASE}/{batch_id}")
    assert denied.status_code == 403
    assert "rows" not in denied.json()
    assert c.post(f"{BASE}/{batch_id}/apply", json={}).status_code == 422
    db.expire_all()
    failures = list(
        db.scalars(
            select(SysAuditLog).where(
                SysAuditLog.entity_type == "return_receipt_attempt"
            )
        )
    )
    assert {row.after_json["status_code"] for row in failures} == {403, 422}
    assert "preview_token" not in str([row.after_json for row in failures])


def test_http_possible_manual_duplicate_requires_its_own_confirmation(db, project):
    ledger.register_receipt(
        db,
        project_id=project.project_id,
        pn="MEMORY",
        qty=2,
        occurred_at=datetime(2026, 9, 11, 17, tzinfo=UTC),
        operated_by="tester",
    )
    db.commit()
    c = client(db)
    batch_id = upload(c)
    frozen = payload(c, batch_id)
    job = c.get(f"{BASE}/{batch_id}").json()
    assert job["possible_duplicates_count"] == 1
    assert len(job["rows"][0]["possible_duplicates"]) == 1
    refused = c.post(f"{BASE}/{batch_id}/apply", json=frozen)
    assert refused.status_code == 422, refused.text
    assert (
        refused.json()["detail"]["code"] == "possible_duplicate_confirmation_required"
    )
    assert c.get(f"{BASE}/{batch_id}").json()["status"] == "ready"
    invalid = c.post(
        f"{BASE}/{batch_id}/apply",
        json={**frozen, "confirm_possible_duplicates": "false"},
    )
    assert invalid.status_code == 422
    applied = c.post(
        f"{BASE}/{batch_id}/apply",
        json={**frozen, "confirm_possible_duplicates": True},
    )
    assert applied.status_code == 200, applied.text
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Receipt)) == 2


def test_formal_route_uses_boss_gate_and_works_with_beta_disabled(
    db, project, monkeypatch
):
    settings = config.get_settings()
    monkeypatch.setattr(settings, "maintenance_beta_enabled", False)
    c = client(db)
    batch_id = upload(c)
    assert c.get(f"{BASE}/{batch_id}").status_code == 200
    monkeypatch.setattr(settings, "maintenance_boss_dashboard_enabled", False)
    assert c.get(f"{BASE}/{batch_id}").status_code == 404


def test_stale_http_apply_has_failed_state_and_can_retry(db, project):
    c = client(db)
    first = upload(c)
    assert c.post(f"{BASE}/{first}/apply", json=payload(c, first)).status_code == 200
    second = upload(c, workbook(qty="3"), "upload-second-key")
    frozen = payload(c, second)
    db.expire_all()
    row = db.scalar(select(Receipt))
    ledger.update_receipt(
        db,
        receipt_id=row.rkd_line_id,
        expected_version=1,
        updates={"qty": 8},
        reason="manual",
        operated_by="tester",
    )
    db.commit()
    response = c.post(
        f"{BASE}/{second}/apply",
        json={**frozen, "confirm_changes": True, "reason": "source correction"},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "stale_preview"
    assert c.get(f"{BASE}/{second}").json()["status"] == "failed"
    assert c.post(f"{BASE}/{second}/retry").status_code == 202
    assert c.get(f"{BASE}/{second}").json()["status"] == "ready"


def test_mid_apply_exception_rolls_back_entire_batch_and_fails_job(
    db, project, monkeypatch
):
    c = client(db)
    batch_id = upload(
        c,
        workbook(
            extra=[
                {
                    "备件明细.备件PN": "DISK",
                    "备件明细.入库数量": "3",
                    "备件明细.数据ID(不可修改)": "SECOND",
                    "备件明细.序号": "2",
                }
            ]
        ),
    )
    frozen = payload(c, batch_id)
    original = ledger._audit
    calls = []

    def failing_audit(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("simulated write failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(ledger, "_audit", failing_audit)
    assert c.post(f"{BASE}/{batch_id}/apply", json=frozen).status_code == 500
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Receipt)) == 0
    assert c.get(f"{BASE}/{batch_id}").json()["status"] == "failed"


def test_tampered_archive_is_nonterminal_and_writes_nothing(db, project):
    c = client(db)
    batch_id = upload(c)
    frozen = payload(c, batch_id)
    db.expire_all()
    batch = db.get(Batch, batch_id)
    Path(batch.report_json["storage_path"]).write_bytes(b"corrupt")
    response = c.post(f"{BASE}/{batch_id}/apply", json=frozen)
    assert response.status_code == 500
    assert c.get(f"{BASE}/{batch_id}").json()["status"] == "ready"
    assert db.scalar(select(func.count()).select_from(Receipt)) == 0


def test_parallel_distinct_previews_create_same_source_at_most_once(db, project):
    batches = [preview(db, workbook()) for _ in range(2)]
    frozen = [
        (b.batch_id, b.report_json["plan_hash"], b.report_json["preview_token"])
        for b in batches
    ]
    db.commit()
    barrier = Barrier(2)

    def worker(item):
        with SessionLocal() as session:
            barrier.wait(timeout=5)
            try:
                imports.apply(
                    session, item[0], "tester", plan_hash=item[1], preview_token=item[2]
                )
                return "applied"
            except imports.ImportError as exc:
                session.rollback()
                return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(worker, frozen))
    assert sorted(outcomes) == ["applied", "stale_preview"]
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Receipt)) == 1


def test_cancelling_running_worker_prevents_late_ready_state(db, project, monkeypatch):
    batch, _ = imports.enqueue(
        db, workbook(), "test.xlsx", "tester", "cancel-running-key"
    )
    batch_id = batch.batch_id
    entered, release = Event(), Event()
    original = imports.parse_standard

    def pause(data):
        entered.set()
        assert release.wait(timeout=10)
        return original(data)

    monkeypatch.setattr(imports, "parse_standard", pause)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(imports.run_job, batch_id, 1)
        assert entered.wait(timeout=5)
        imports.control(db, batch_id, "cancel")
        release.set()
        future.result(timeout=10)
    db.expire_all()
    assert imports._load(db, batch_id).report_json["status"] == "cancelled"


def test_abandoned_worker_lease_can_be_retried(db, project):
    batch, _ = imports.enqueue(db, workbook(), "test.xlsx", "tester", "abandoned-key")
    imports._state(
        batch,
        status="processing",
        started_at=(datetime.now(UTC) - timedelta(minutes=20)).isoformat(),
    )
    db.commit()
    batch = imports.control(db, batch.batch_id, "retry")
    imports.run_job(batch.batch_id, batch.report_json["generation"])
    assert imports._load(db, batch.batch_id).report_json["status"] == "ready"


def test_legacy_apply_cannot_bypass_token_or_duplicate_native_facts(db, project):
    batch = preview(db, workbook(condition="坏品"))
    with pytest.raises(legacy.DocBatchError, match="专用"):
        legacy.apply_batch(db, batch.batch_id, "tester")
    db.rollback()
    apply(db, batch)
    parsed = legacy.parse_doc_workbook(
        "rkd_inbound", workbook(condition="坏品"), "legacy.xlsx"
    )
    legacy_id = legacy.store_preview(db, parsed, "tester", idempotency_key="legacy-key")
    with pytest.raises(legacy.DocBatchError):
        legacy.apply_batch(db, legacy_id, "tester")
    assert db.scalar(select(func.count()).select_from(Receipt)) == 1

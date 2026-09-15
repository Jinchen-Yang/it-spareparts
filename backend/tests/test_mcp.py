"""MCP acceptance against a disposable migrated PostgreSQL database."""

import hashlib
import uuid
import pytest
from sqlalchemy import select, text
from app.config import get_settings
from app.mcp.core import SCOPES
from app.mcp.worker import run_one
from app.models.mcp import McpAuditEvent, McpRecord
from tests.test_maintenance_acceptance_checklist import _client, _project, _xlsx, svc


@pytest.fixture
def employee(db, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "mcp_enabled", True)
    monkeypatch.setattr(s, "mcp_confirm_enabled", True)
    monkeypatch.setattr(s, "mcp_public_base_url", "http://testserver")
    c = _client(db, username="mcp-admin", permissions={}, role="admin")
    r = c.post("/api/mcp-office/credentials", json={"scopes": sorted(SCOPES)})
    assert r.status_code == 200, r.text
    c.pat = r.json()
    return c


def call(c, name, **args):
    r = c.post("/api/mcp-office/tools", json={"name": name, "arguments": args})
    assert r.status_code == 200, r.text
    return r.json()["data"]


def upload(c, data=None):
    data = data or _xlsx([("设备报告归档", "是"), ("备件清单签字", "否")])
    u = call(
        c,
        "pf_create_upload_session",
        files=[
            {
                "name": "checklist.xlsx",
                "size_bytes": len(data),
                "mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            }
        ],
        purpose="document_import",
        idempotency_key=str(uuid.uuid4()),
    )
    r = c.put(
        f"/api/mcp-office/uploads/{u['upload_session_id']}/{u['entries'][0]['entry_id']}",
        content=data,
    )
    assert r.status_code == 200, r.text
    return r.json()["file_id"]


def preview(c, project, data=None):
    fid = upload(c, data)
    job = call(
        c,
        "pf_preview_import",
        file_ids=[fid],
        family="acceptance_checklist",
        project_id=project.project_id,
        idempotency_key=str(uuid.uuid4()),
    )
    assert run_one()
    result = call(c, "pf_get_job", job_id=job["job_id"])
    assert result["status"] == "ready", result
    p = call(
        c,
        "pf_get_import_preview",
        preview_id=result["result"]["items"][0]["preview_id"],
    )
    return p


def test_complete_roundtrip_and_receipt(db, employee):
    c = employee
    p = _project(db)
    pre = preview(c, p)
    assert pre["summary"]["item_rows"] == 2
    assert svc.project_checklist(db, p.project_id)["current"] is None
    url = f"/api/mcp-office/reviews/{pre['preview_id']}/confirm"
    denied = c.post(
        url,
        json={"preview_hash": pre["preview_hash"]},
        headers={"Authorization": "Bearer " + c.pat["token"]},
    )
    assert denied.status_code == 403
    r = c.post(url, json={"preview_hash": pre["preview_hash"]})
    assert r.status_code == 200, r.text
    assert c.post(url, json={"preview_hash": pre["preview_hash"]}).json() == r.json()
    db.expire_all()
    assert svc.project_checklist(db, p.project_id)["current"]["item_rows"] == 2
    assert (
        len(list(db.scalars(select(McpRecord).where(McpRecord.kind == "receipt")))) == 1
    )
    job = call(
        c,
        "pf_create_export",
        export_kind="acceptance_checklist",
        project_ids=[p.project_id],
        idempotency_key=str(uuid.uuid4()),
    )
    assert run_one()
    result = call(c, "pf_get_job", job_id=job["job_id"])
    assert result["status"] == "succeeded", result
    artifact = result["result"]["artifact_id"]
    info = call(c, "pf_get_download", artifact_id=artifact)
    r = c.get(f"/api/mcp-office/artifacts/{artifact}/download")
    assert r.status_code == 200, r.text
    assert hashlib.sha256(r.content).hexdigest() == info["sha256"]
    assert db.scalar(
        select(McpAuditEvent).where(McpAuditEvent.stage == "download_completed")
    )


def test_stale_preview_blocked(db, employee):
    p = _project(db)
    pre = preview(employee, p)
    from tests.test_maintenance_acceptance_checklist import _preview

    bid = _preview(db, p, _xlsx([("另一个人的更新", "是")]))
    svc.apply_batch(db, bid, operated_by="tester")
    db.commit()
    r = employee.post(
        f"/api/mcp-office/reviews/{pre['preview_id']}/confirm",
        json={"preview_hash": pre["preview_hash"]},
    )
    assert r.status_code == 409, r.text
    assert (
        svc.project_checklist(db, p.project_id)["current"]["items"][0]["requirement"]
        == "另一个人的更新"
    )


def test_revoke_and_owner_isolation(db, employee):
    c = employee
    fid = upload(c)
    other = _client(db, username="mcp-other", permissions={}, role="admin")
    r = other.post(
        "/api/mcp-office/tools",
        json={"name": "pf_inspect_document", "arguments": {"file_id": fid}},
    )
    assert r.status_code == 404, r.text
    assert (
        c.delete("/api/mcp-office/credentials/" + c.pat["credential_id"]).status_code
        == 200
    )
    r = c.post(
        "/api/mcp-office/tools",
        json={"name": "pf_get_capabilities", "arguments": {}},
        headers={"Authorization": "Bearer " + c.pat["token"]},
    )
    assert r.status_code == 401


def test_idempotency_invalid_scope_origin_and_disabled(db, employee, monkeypatch):
    c = employee
    args = dict(
        files=[{"name": "a.txt", "size_bytes": 1, "mime": "text/plain"}],
        purpose="attachment_organize",
        idempotency_key="stable",
    )
    a = call(c, "pf_create_upload_session", **args)
    assert (
        call(c, "pf_create_upload_session", **args)["upload_session_id"]
        == a["upload_session_id"]
    )
    args["files"][0]["size_bytes"] = 2
    r = c.post(
        "/api/mcp-office/tools",
        json={"name": "pf_create_upload_session", "arguments": args},
    )
    assert r.status_code == 409
    assert (
        c.get(
            "/api/mcp-office/credentials", headers={"Origin": "https://evil.invalid"}
        ).status_code
        == 403
    )
    narrow = c.post("/api/mcp-office/credentials", json={"scopes": ["mcp.use"]}).json()[
        "token"
    ]
    r = c.post(
        "/api/mcp-office/tools",
        json={"name": "pf_search_projects", "arguments": {"query": "x"}},
        headers={"Authorization": "Bearer " + narrow},
    )
    assert r.status_code == 403
    monkeypatch.setattr(get_settings(), "mcp_enabled", False)
    assert c.get("/mcp-office").status_code == 404


def test_audit_append_only(db, employee):
    call(employee, "pf_get_capabilities")
    with pytest.raises(Exception):
        db.execute(text("DELETE FROM mcp_audit_event"))
    db.rollback()
    assert db.scalar(select(McpAuditEvent))


def test_mcp_protocol(db, employee):
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        c.headers.update(
            {
                "Authorization": "Bearer " + employee.pat["token"],
                "Accept": "application/json, text/event-stream",
            }
        )

        def rpc(method, params=None):
            r = c.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": method,
                    "params": params or {},
                },
            )
            assert r.status_code == 200, r.text
            return r.json()

        init = rpc(
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "acceptance", "version": "1"},
            },
        )
        assert init["result"]["serverInfo"]["name"]
        listed = rpc("tools/list")
        assert len(listed["result"]["tools"]) == 16
        result = rpc("tools/call", {"name": "pf_get_capabilities", "arguments": {}})
        assert not result["result"].get("isError"), result
        assert (
            result["result"]["structuredContent"]["data"]["actor_display"]
            == "mcp-admin"
        )


def test_audit_failure_rolls_back_business_write(db, employee, monkeypatch):
    from app.mcp import workflows

    p = _project(db)
    pre = preview(employee, p)

    def fail(*a, **k):
        raise RuntimeError("synthetic audit failure")

    monkeypatch.setattr(workflows, "event", fail)
    with pytest.raises(RuntimeError):
        employee.post(
            f"/api/mcp-office/reviews/{pre['preview_id']}/confirm",
            json={"preview_hash": pre["preview_hash"]},
        )
    db.expire_all()
    assert svc.project_checklist(db, p.project_id)["current"] is None
    assert not db.scalar(select(McpRecord).where(McpRecord.kind == "receipt"))


def test_pat_revocation_invalidates_pending_preview(db, employee):
    c = employee
    p = _project(db)
    jwt = c.headers["Authorization"]
    c.headers["Authorization"] = "Bearer " + c.pat["token"]
    pre = preview(c, p)
    c.headers["Authorization"] = jwt
    c.delete("/api/mcp-office/credentials/" + c.pat["credential_id"])
    r = c.post(
        f"/api/mcp-office/reviews/{pre['preview_id']}/confirm",
        json={"preview_hash": pre["preview_hash"]},
    )
    assert r.status_code == 401, r.text
    assert svc.project_checklist(db, p.project_id)["current"] is None


def test_expense_workbook_roundtrip(db, employee):
    p = _project(db)
    c = employee
    job = call(
        c,
        "pf_create_export",
        export_kind="expense_collection",
        project_ids=[p.project_id],
        idempotency_key=str(uuid.uuid4()),
    )
    assert run_one()
    r = call(c, "pf_get_job", job_id=job["job_id"])
    assert r["status"] == "succeeded", r
    data = c.get(
        "/api/mcp-office/artifacts/" + r["result"]["artifact_id"] + "/download"
    ).content
    fid = upload(c, data)
    j = call(
        c,
        "pf_preview_import",
        family="expense_collection",
        project_id=p.project_id,
        file_ids=[fid],
        idempotency_key=str(uuid.uuid4()),
    )
    assert run_one()
    r = call(c, "pf_get_job", job_id=j["job_id"])
    assert r["status"] == "ready", r
    pre = call(
        c, "pf_get_import_preview", preview_id=r["result"]["items"][0]["preview_id"]
    )
    r = c.post(
        f"/api/mcp-office/reviews/{pre['preview_id']}/confirm",
        json={"preview_hash": pre["preview_hash"]},
    )
    assert r.status_code == 200, r.text


def test_upload_limits_and_audit(db, employee):
    c = employee
    u = call(
        c,
        "pf_create_upload_session",
        files=[{"name": "a.txt", "size_bytes": 1, "mime": "text/plain"}],
        purpose="attachment_organize",
        idempotency_key="size-limit",
    )
    r = c.put(
        f"/api/mcp-office/uploads/{u['upload_session_id']}/{u['entries'][0]['entry_id']}",
        content=b"too large",
    )
    assert r.status_code == 413
    assert db.scalar(
        select(McpAuditEvent).where(
            McpAuditEvent.operation == "file_upload", McpAuditEvent.stage == "failed"
        )
    )


def test_migration_matches_models(db):
    from alembic import command
    from tests.conftest import _alembic_cfg

    command.check(_alembic_cfg())


def test_worker_recovers_lease_and_rechecks_revocation(db, employee):
    from app.mcp.core import now
    from datetime import timedelta

    p = _project(db)
    c = employee
    job = call(
        c,
        "pf_create_export",
        export_kind="acceptance_checklist",
        project_ids=[p.project_id],
        idempotency_key="recover",
    )
    row = db.get(McpRecord, job["job_id"])
    row.state = "running"
    row.updated_at = now() - timedelta(seconds=700)
    db.commit()
    assert run_one()
    assert call(c, "pf_get_job", job_id=row.id)["status"] == "succeeded"
    jwt = c.headers["Authorization"]
    c.headers["Authorization"] = "Bearer " + c.pat["token"]
    job = call(
        c,
        "pf_create_export",
        export_kind="acceptance_checklist",
        project_ids=[p.project_id],
        idempotency_key="revoked-worker",
    )
    c.headers["Authorization"] = jwt
    c.delete("/api/mcp-office/credentials/" + c.pat["credential_id"])
    assert run_one()
    db.expire_all()
    row = db.get(McpRecord, job["job_id"])
    assert row.state == "failed"
    assert row.payload["error"]["code"] == "unauthenticated"


def test_expired_file_cleanup_retains_audit(db, employee):
    from datetime import timedelta
    from app.mcp.core import now
    from app.mcp.cleanup import cleanup_files
    from app.mcp.workflows import data_path

    fid = upload(employee)
    row = db.get(McpRecord, fid)
    row.expires_at = now() - timedelta(seconds=1)
    db.commit()
    assert cleanup_files() >= 1
    assert not data_path(fid).exists()
    assert db.scalar(
        select(McpAuditEvent).where(McpAuditEvent.detail["file_id"].astext == fid)
    )


def test_migration_downgrade_guard(db, employee):
    from alembic import command
    from tests.conftest import _alembic_cfg

    with pytest.raises(Exception, match="(?i)(refus|non.empty|data|非空)"):
        command.downgrade(_alembic_cfg(), "e3a7b9c2d4f6")
    # Refusal leaves the deployed schema intact.
    command.check(_alembic_cfg())


def test_connector_contract():
    import json, re
    from pathlib import Path
    from app.mcp.tools import REGISTRY

    base = Path(__file__).resolve().parents[2] / "connectors/workbuddy/partflow"
    config = json.loads((base / "mcp.json").read_text())
    schema = json.loads((base / "token-schema.json").read_text())
    assert set(re.findall(r"\$\{(\w+)\}", json.dumps(config))) == {
        x["key"] for x in schema["fields"]
    }
    assert (
        next(x for x in schema["fields"] if x["key"] == "PARTFLOW_TOKEN")["type"]
        == "password"
    )
    assert len(config["mcpServers"]) == 1
    for path in (base / "skills").glob("*/SKILL.md"):
        assert set(re.findall(r"\bpf_\w+", path.read_text())) <= set(REGISTRY), path


def test_http_limits_and_no_store(db, employee):
    response = employee.get("/api/mcp-office/credentials")
    assert response.headers["cache-control"] == "no-store"
    response = employee.post(
        "/api/mcp-office/tools",
        content=b"x" * (128 * 1024 + 1),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413


def test_protocol_identity_isolated_between_concurrent_clients(db, employee):
    from concurrent.futures import ThreadPoolExecutor
    from fastapi.testclient import TestClient
    from app.main import app

    other = _client(db, username="mcp-second", permissions={}, role="admin")
    pat = other.post(
        "/api/mcp-office/credentials", json={"scopes": ["mcp.use"]}
    ).json()["token"]
    with TestClient(app) as client:

        def ask(token):
            response = client.post(
                "/mcp",
                headers={
                    "Authorization": "Bearer " + token,
                    "Accept": "application/json, text/event-stream",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "pf_get_capabilities", "arguments": {}},
                },
            )
            assert response.status_code == 200, response.text
            return response.json()["result"]["structuredContent"]["data"]

        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(ask, employee.pat["token"])
            b = pool.submit(ask, pat)
            assert a.result()["actor_display"] == "mcp-admin"
            assert b.result()["actor_display"] == "mcp-second"
            assert b.result()["enabled_tools"] == ["pf_get_capabilities"]


def test_crash_retry_exhaustion_is_audited(db, employee):
    from datetime import timedelta
    from app.mcp.core import now

    p = _project(db)
    job = call(
        employee,
        "pf_create_export",
        export_kind="acceptance_checklist",
        project_ids=[p.project_id],
        idempotency_key="exhausted",
    )
    row = db.get(McpRecord, job["job_id"])
    row.state = "running"
    row.updated_at = now() - timedelta(seconds=700)
    row.payload = {**row.payload, "attempt": 3}
    db.commit()
    assert run_one()
    db.expire_all()
    assert db.get(McpRecord, job["job_id"]).state == "failed"
    assert db.scalar(
        select(McpAuditEvent).where(
            McpAuditEvent.detail["code"].astext == "retry_exhausted"
        )
    )


def test_audit_range_is_at_most_31_calendar_days(db, employee):
    call(employee, "pf_search_audit", date_from="2026-01-01", date_to="2026-01-31")
    response = employee.post(
        "/api/mcp-office/tools",
        json={
            "name": "pf_search_audit",
            "arguments": {"date_from": "2026-01-01", "date_to": "2026-02-01"},
        },
    )
    assert response.status_code == 400


def test_read_audit_identifies_returned_records_without_raw_search(db, employee):
    p = _project(db, "审计检索测试项目")
    result = call(employee, "pf_search_projects", query="审计检索")
    assert result["items"][0]["project_id"] == p.project_id
    events = list(
        db.scalars(
            select(McpAuditEvent).where(McpAuditEvent.operation == "pf_search_projects")
        )
    )
    completed = next(e for e in events if e.stage == "completed")
    requested = next(e for e in events if e.stage == "requested")
    assert completed.detail["result_refs"] == [{"project_id": p.project_id}]
    assert requested.detail["arguments_hash"]
    assert "query" not in requested.detail


def test_parallel_upload_requests_respect_employee_quota(db, employee):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    args = {
        "files": [{"name": "a.txt", "size_bytes": 1, "mime": "text/plain"}],
        "purpose": "attachment_organize",
    }
    for i in range(9):
        call(employee, "pf_create_upload_session", **args, idempotency_key=f"quota-{i}")
    barrier = Barrier(2)

    def submit(i):
        barrier.wait(timeout=5)
        return employee.post(
            "/api/mcp-office/tools",
            json={
                "name": "pf_create_upload_session",
                "arguments": {**args, "idempotency_key": f"parallel-{i}"},
            },
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(submit, range(2))) == [200, 429]

"""Read-only integration export: token, cross-project pagination and field whitelist."""

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from app.api import resale_export
from app.config import Settings
from app.models.dimensions import DimPart
from app.models.maintenance_doc_import import MaintenanceRkdReturnLine
from tests.test_site_issue_v2_api import _project

TOKEN = "synthetic-readonly-" + "s" * 40


def test_export_disabled_by_default_and_invalid_hash_rejected():
    assert not Settings(_env_file=None).resale_export_token_sha256.get_secret_value()
    with pytest.raises(ValidationError):
        Settings(_env_file=None, resale_export_token_sha256=SecretStr("not-a-digest"))


def _client(db, monkeypatch, *, enabled: bool = True) -> TestClient:
    digest = hashlib.sha256(TOKEN.encode()).hexdigest() if enabled else ""
    monkeypatch.setattr(
        resale_export,
        "get_settings",
        lambda: SimpleNamespace(resale_export_token_sha256=SecretStr(digest)),
    )
    app = FastAPI()
    app.include_router(resale_export.router, prefix="/api")
    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {TOKEN}"
    return client


def _receipt(db, project_id: str, *, status="active", serials=None, condition="坏品"):
    receipt = MaintenanceRkdReturnLine(
        rkd_line_id=str(uuid4()),
        project_id=project_id,
        source="manual",
        head_no="LOCAL-RETURN",
        source_ref=f"manual:{uuid4()}",
        pn="PN-BROKEN-1",
        description="故障备件",
        qty=Decimal(len(serials) if serials else 1),
        serial_numbers=serials or [],
        test_result=condition,
        receipt_kind="part",
        line_status=status,
        voided_at=datetime.now(UTC) if status == "voided" else None,
        voided_by="test" if status == "voided" else None,
        void_reason="synthetic correction" if status == "voided" else None,
        note="private-source-note",
        evidence_ref="private-evidence-reference",
        created_by="test",
    )
    db.add(receipt)
    db.commit()
    return receipt


def test_disabled_and_invalid_token_never_read_rows(db, monkeypatch):
    project = _project(db, project_id=str(uuid4()))
    _receipt(db, project.project_id, serials=["SN-01", "SN-02"])
    client = _client(db, monkeypatch, enabled=False)
    assert client.get("/api/integrations/resale/bad-receipts").status_code == 404
    client = _client(db, monkeypatch)
    client.headers["Authorization"] = "Bearer " + "wrong" * 10
    assert client.get("/api/integrations/resale/bad-receipts").status_code == 401
    client.headers.pop("Authorization")
    assert client.get("/api/integrations/resale/bad-receipts").status_code == 401


def test_cross_project_pagination_whitelist_and_voided_state(db, monkeypatch):
    project_a = _project(db, project_id=str(uuid4()))
    project_b = _project(db, project_id=str(uuid4()))
    one = _receipt(db, project_a.project_id, serials=["SN-01", "SN-02"])
    two = _receipt(db, project_b.project_id, status="voided", serials=["SN-03"])
    _receipt(db, project_a.project_id, serials=[])
    _receipt(db, project_a.project_id, serials=["SN-GOOD"], condition="成品")
    client = _client(db, monkeypatch)
    first = client.get("/api/integrations/resale/bad-receipts", params={"limit": 1})
    assert first.status_code == 200, first.text
    assert first.headers["Cache-Control"] == "no-store"
    assert first.json()["hasMore"] is True
    assert first.json()["schemaVersion"] == 1
    second = client.get(
        "/api/integrations/resale/bad-receipts",
        params={"limit": 1, "after": first.json()["nextCursor"]},
    )
    assert second.status_code == 200, second.text
    assert second.json()["hasMore"] is False
    rows = first.json()["items"] + second.json()["items"]
    assert {row["receiptId"] for row in rows} == {one.rkd_line_id, two.rkd_line_id}
    assert {row["lineStatus"] for row in rows} == {"active", "voided"}
    assert {row["qty"] for row in rows} == {"1.000", "2.000"}
    assert set(rows[0]) == {
        "receiptId",
        "version",
        "lineStatus",
        "projectId",
        "pn",
        "qty",
        "serialNumbers",
        "condition",
        "receiptKind",
        "occurredAt",
        "changedAt",
    }
    assert "private-source-note" not in first.text + second.text
    assert "private-evidence-reference" not in first.text + second.text

    exact = client.get(
        "/api/integrations/resale/bad-receipts", params={"receipt_id": one.rkd_line_id}
    )
    assert exact.status_code == 200
    assert [item["receiptId"] for item in exact.json()["items"]] == [one.rkd_line_id]
    group = client.get(
        "/api/integrations/resale/bad-receipts",
        params=[("receipt_ids", one.rkd_line_id), ("receipt_ids", two.rkd_line_id)],
    )
    assert group.status_code == 200
    assert {item["receiptId"] for item in group.json()["items"]} == {
        one.rkd_line_id,
        two.rkd_line_id,
    }
    assert (
        client.get(
            "/api/integrations/resale/bad-receipts", params={"after": "not-a-uuid"}
        ).status_code
        == 422
    )


def test_opt_in_v2_keeps_legacy_shape_and_links_source_part_without_private_notes(
    db, monkeypatch
):
    project = _project(db, project_id=str(uuid4()))
    part = DimPart(pn_std="PN-BROKEN-1", description="规范描述")
    db.add(part)
    db.flush()
    receipt = _receipt(db, project.project_id, serials=["SN-01"])
    receipt.part_id = part.id
    db.commit()
    client = _client(db, monkeypatch)

    legacy = client.get(
        "/api/integrations/resale/bad-receipts",
        params={"receipt_id": receipt.rkd_line_id},
    )
    assert legacy.status_code == 200
    assert legacy.json()["schemaVersion"] == 1
    assert "partId" not in legacy.json()["items"][0]

    enriched = client.get(
        "/api/integrations/resale/bad-receipts",
        params={"receipt_id": receipt.rkd_line_id, "contract": 2},
    )
    assert enriched.status_code == 200
    assert enriched.json()["schemaVersion"] == 2
    item = enriched.json()["items"][0]
    assert item["partId"] == part.id
    assert item["description"] == "故障备件"
    assert "private-source-note" not in enriched.text
    assert "private-evidence-reference" not in enriched.text
    assert (
        client.get(
            "/api/integrations/resale/bad-receipts", params={"contract": 3}
        ).status_code
        == 422
    )

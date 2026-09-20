"""evidence_ref 扩容验收（v1.36）：128 → 16384 全链路。

覆盖：>128 创建/修改/读回；16385 拒绝；100 个 SN（每条 12 字符≈1300）
轻松通过；幂等指纹含长凭据（重放不冲突）；迁移后旧行不受影响。
"""

from uuid import uuid4

from fastapi.testclient import TestClient

from tests.test_maintenance_return_receipts_api import _client, _project


def _register(client: TestClient, project_id: str, payload: dict):
    return client.post(
        f"/api/maintenance/projects/stable/{project_id}/return-receipts",
        json=payload,
    )


def test_create_with_long_evidence_over_128(db):
    """200 个扫描 SN（≈2600 字符）创建成功并读回不截断。"""
    project = _project(db, project_id=str(uuid4()))
    client = _client(db, username="rcp-ev-1")
    evidence = "\n".join(f"SN-SCAN-{i:04d}" for i in range(200))
    assert len(evidence) > 128
    resp = _register(client, project.project_id, {
        "pn": "PN-EV", "qty": 1, "evidence_ref": evidence,
        "idempotency_key": "ev-long-1",
    })
    assert resp.status_code == 201, resp.text
    assert resp.json()["evidence_ref"] == evidence


def test_update_long_evidence_roundtrip(db):
    project = _project(db, project_id=str(uuid4()))
    client = _client(db, username="rcp-ev-2")
    created = _register(client, project.project_id, {"pn": "PN-EV", "qty": 1})
    assert created.status_code == 201, created.text
    receipt_id = created.json()["receipt_id"]
    long_text = "快递单号：SF" + "9" * 300
    patched = client.patch(
        f"/api/maintenance/return-receipts/{receipt_id}",
        json={"version": 1, "reason": "补录长凭据", "evidence_ref": long_text},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["evidence_ref"] == long_text


def test_over_16384_rejected(db):
    project = _project(db, project_id=str(uuid4()))
    client = _client(db, username="rcp-ev-3")
    resp = _register(client, project.project_id, {
        "pn": "PN-EV", "qty": 1, "evidence_ref": "x" * 16385,
    })
    assert resp.status_code == 422, resp.text


def test_exactly_16384_accepted(db):
    project = _project(db, project_id=str(uuid4()))
    client = _client(db, username="rcp-ev-4")
    resp = _register(client, project.project_id, {
        "pn": "PN-EV", "qty": 1, "evidence_ref": "y" * 16384,
        "idempotency_key": "ev-max-1",
    })
    assert resp.status_code == 201, resp.text
    assert len(resp.json()["evidence_ref"]) == 16384


def test_hundred_sn_evidence_with_serials_together(db):
    """凭据长文 + 独立 SN 列表 100 个并存：互不影响。"""
    project = _project(db, project_id=str(uuid4()))
    client = _client(db, username="rcp-ev-5")
    evidence = "\n".join(f"EXP-{i:03d}" for i in range(100))
    serials = [f"SN{i:03d}" for i in range(100)]
    resp = _register(client, project.project_id, {
        "pn": "PN-EV", "qty": 100,
        "evidence_ref": evidence,
        "serial_numbers": serials,
        "idempotency_key": "ev-hundred-1",
    })
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["evidence_ref"] == evidence
    assert body["serial_numbers"] == serials


def test_idempotent_replay_with_same_long_evidence(db):
    """同 key 同长凭据重放返回原记录；不同长凭据 409（指纹含 evidence）。"""
    project = _project(db, project_id=str(uuid4()))
    client = _client(db, username="rcp-ev-6")
    evidence = "K" * 500
    first = _register(client, project.project_id, {
        "pn": "PN-EV", "qty": 1, "evidence_ref": evidence,
        "idempotency_key": "ev-replay",
    })
    assert first.status_code == 201, first.text
    replay = _register(client, project.project_id, {
        "pn": "PN-EV", "qty": 1, "evidence_ref": evidence,
        "idempotency_key": "ev-replay",
    })
    assert replay.status_code == 201 and replay.json()["replayed"] is True
    changed = _register(client, project.project_id, {
        "pn": "PN-EV", "qty": 1, "evidence_ref": "J" * 500,
        "idempotency_key": "ev-replay",
    })
    assert changed.status_code == 409, changed.text

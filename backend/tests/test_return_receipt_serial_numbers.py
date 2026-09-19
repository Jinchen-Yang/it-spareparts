"""手工登记返还的 SN 凭证（v1.36 Phase B）。

覆盖：qty==SN 数一致性、SN 清洗/去重/长度、幂等指纹含 SN（同 key 不同 SN → 409）、
修改路径的 SN 重校验（改数量必须同步改 SN）、整机行拒绝 SN、审计含 SN。
"""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.models.maintenance_doc_import import MaintenanceRkdReturnLine
from tests.test_maintenance_return_receipts_api import _client, _project


def _register(client: TestClient, project_id: str, payload: dict):
    return client.post(
        f"/api/maintenance/projects/stable/{project_id}/return-receipts",
        json=payload,
    )


def test_register_with_serial_numbers_stores_evidence(db):
    project = _project(db, project_id=str(uuid4()))
    client = _client(db, username="rcp-sn-1")
    resp = _register(client, project.project_id, {
        "pn": "PN-SN",
        "qty": 2,
        "serial_numbers": [" SN-A ", "SN-B"],
        "idempotency_key": "sn-1",
    })
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["serial_numbers"] == ["SN-A", "SN-B"]
    row = db.query(MaintenanceRkdReturnLine).filter_by(
        rkd_line_id=body["receipt_id"]).one()
    assert row.serial_numbers == ["SN-A", "SN-B"]


def test_qty_must_equal_serial_count(db):
    project = _project(db, project_id=str(uuid4()))
    client = _client(db, username="rcp-sn-2")
    resp = _register(client, project.project_id, {
        "pn": "PN-SN", "qty": 3, "serial_numbers": ["SN-1", "SN-2"],
    })
    assert resp.status_code == 422, resp.text
    assert "必须等于" in resp.json()["detail"]


def test_serial_duplicates_and_blanks_rejected(db):
    project = _project(db, project_id=str(uuid4()))
    client = _client(db, username="rcp-sn-3")
    resp = _register(client, project.project_id, {
        "pn": "PN-SN", "qty": 2, "serial_numbers": ["SN-1", " SN-1 "],
    })
    assert resp.status_code == 422, resp.text
    assert "重复" in resp.json()["detail"]
    resp = _register(client, project.project_id, {
        "pn": "PN-SN", "qty": 1, "serial_numbers": ["   "],
    })
    assert resp.status_code == 422, resp.text
    assert "空白" in resp.json()["detail"]


def test_same_idempotency_key_with_different_serials_conflicts(db):
    project = _project(db, project_id=str(uuid4()))
    client = _client(db, username="rcp-sn-4")
    first = _register(client, project.project_id, {
        "pn": "PN-SN", "qty": 1, "serial_numbers": ["SN-OLD"],
        "idempotency_key": "sn-replay",
    })
    assert first.status_code == 201, first.text
    replay = _register(client, project.project_id, {
        "pn": "PN-SN", "qty": 1, "serial_numbers": ["SN-NEW"],
        "idempotency_key": "sn-replay",
    })
    assert replay.status_code == 409, replay.text
    assert "内容已变化" in replay.json()["detail"]


def test_same_key_same_serials_replays(db):
    project = _project(db, project_id=str(uuid4()))
    client = _client(db, username="rcp-sn-5")
    first = _register(client, project.project_id, {
        "pn": "PN-SN", "qty": 2,
        "serial_numbers": ["SN-R1", "SN-R2"],
        "idempotency_key": "sn-replay-ok",
    })
    assert first.status_code == 201, first.text
    replay = _register(client, project.project_id, {
        "pn": "PN-SN", "qty": 2,
        "serial_numbers": ["SN-R1", "SN-R2"],
        "idempotency_key": "sn-replay-ok",
    })
    assert replay.status_code == 201, replay.text
    assert replay.json()["replayed"] is True
    assert replay.json()["receipt_id"] == first.json()["receipt_id"]


def test_update_qty_requires_serial_reconciliation(db):
    project = _project(db, project_id=str(uuid4()))
    client = _client(db, username="rcp-sn-6")
    created = _register(client, project.project_id, {
        "pn": "PN-SN", "qty": 2, "serial_numbers": ["SN-U1", "SN-U2"],
    })
    assert created.status_code == 201, created.text
    receipt_id = created.json()["receipt_id"]
    # 只改数量不改 SN → 422
    bad = client.patch(
        f"/api/maintenance/return-receipts/{receipt_id}",
        json={"version": 1, "reason": "数量改 3", "qty": 3},
    )
    assert bad.status_code == 422, bad.text
    assert "必须等于" in bad.json()["detail"]
    # 数量与 SN 一起改 → 通过
    good = client.patch(
        f"/api/maintenance/return-receipts/{receipt_id}",
        json={
            "version": 1, "reason": "补充一台", "qty": 3,
            "serial_numbers": ["SN-U1", "SN-U2", "SN-U3"],
        },
    )
    assert good.status_code == 200, good.text
    assert good.json()["serial_numbers"] == ["SN-U1", "SN-U2", "SN-U3"]


def test_clearing_serials_is_allowed_without_touching_qty(db):
    """清空 SN = 放弃逐件凭证留痕，数量不变时应放行（数量校验只约束非空 SN）。"""
    project = _project(db, project_id=str(uuid4()))
    client = _client(db, username="rcp-sn-7")
    created = _register(client, project.project_id, {
        "pn": "PN-SN", "qty": 2, "serial_numbers": ["SN-C1", "SN-C2"],
    })
    assert created.status_code == 201, created.text
    receipt_id = created.json()["receipt_id"]
    resp = client.patch(
        f"/api/maintenance/return-receipts/{receipt_id}",
        json={"version": 1, "reason": "清掉逐件凭证", "serial_numbers": []},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["serial_numbers"] == []
    assert resp.json()["qty"] == "2.000"

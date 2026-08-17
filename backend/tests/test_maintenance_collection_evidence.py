"""F6 回款提醒凭证：上传 → 独立目录文件 + yml sidecar + DB md5；关闭=已上传凭证。"""

import hashlib
import io
from datetime import date

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from app import auth
from app.api import maintenance_collection_evidence, maintenance_collection_reminders
from app.config import get_settings
from app.models.maintenance_collection_evidence import (
    MaintenanceCollectionEvidence,
)
from app.models.maintenance_manager import BusinessFile
from app.models.system import SysUser
from app.services import maintenance_collection_evidence as evidence_service
from tests.test_maintenance_collection_reminders_api import (
    _milestone,
    _project,
    _sys_user,
)


def _evidence_client(db, *, username: str) -> TestClient:
    user = db.scalar(select(SysUser).where(SysUser.username == username))
    if user is None:
        user = _sys_user(
            db, username=username, role="admin", follow_up_action=True
        )
    app = FastAPI()
    app.include_router(auth.router, prefix="/api")
    app.include_router(maintenance_collection_reminders.router, prefix="/api")
    app.include_router(maintenance_collection_evidence.router, prefix="/api")
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": username, "password": "synthetic-password-123"},
    )
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    return client


def _png_bytes(color: str = "white") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture()
def seeded_milestone(db):
    admin = _sys_user(
        db, username="evidence_admin", role="admin", follow_up_action=True
    )
    project, pc = _project(db, suffix="evidence", manager=admin)
    milestone = _milestone(
        db,
        project_id=project,
        project_contract_id=pc,
        milestone_id="rem-m-evidence",
        planned_date=date(2026, 8, 20),
    )
    return {"milestone_id": milestone.milestone_id, "project_id": project}


def test_save_evidence_writes_file_yml_and_md5_row(db, seeded_milestone, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "raw_file_dir", str(tmp_path))
    content = _png_bytes()
    payload = evidence_service.save_evidence(
        db,
        milestone_id=seeded_milestone["milestone_id"],
        operator="合成上传人",
        filename="巡检报告.png",
        mime_type="image/png",
        content=content,
    )
    db.commit()
    # 文件落盘在 DB commit 之后（round-6 Blocker 6：DB 行先定案）
    evidence_service.write_evidence_files(
        file_id=payload["file_id"],
        object_key=payload["object_key"],
        content=content,
        meta={
            "file_id": payload["file_id"],
            "milestone_id": seeded_milestone["milestone_id"],
            "original_filename": payload["original_filename"],
            "mime_type": payload["mime_type"],
            "size_bytes": len(content),
            "md5": payload["md5"],
            "sha256": payload["sha256"],
            "uploaded_by": "合成上传人",
            "uploaded_at": "2026-08-16T00:00:00+00:00",
            "storage": "local",
        },
    )
    assert payload["replayed"] is False
    assert payload["md5"] == hashlib.md5(content).hexdigest()
    assert payload["sha256"] == hashlib.sha256(content).hexdigest()
    row = db.scalar(
        select(MaintenanceCollectionEvidence).where(
            MaintenanceCollectionEvidence.evidence_id == payload["evidence_id"]
        )
    )
    assert row is not None
    assert row.milestone_id == seeded_milestone["milestone_id"]
    file_row = db.get(BusinessFile, payload["file_id"])
    assert file_row is not None
    # 文件存独立目录 + yml 元信息 sidecar
    data_path = tmp_path / file_row.object_key
    assert data_path.read_bytes() == content
    sidecar = tmp_path / "collection_evidence" / file_row.file_id[:2] / f"{file_row.file_id}.yml"
    meta = yaml.safe_load(sidecar.read_text(encoding="utf-8"))
    assert meta["md5"] == payload["md5"]
    assert meta["milestone_id"] == seeded_milestone["milestone_id"]
    assert meta["uploaded_by"] == "合成上传人"
    # DB 只记 md5：元数据记录存在且一致
    assert row.md5 == hashlib.md5(content).hexdigest()


def test_save_evidence_replays_same_md5(db, seeded_milestone, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "raw_file_dir", str(tmp_path))
    content = _png_bytes()
    first = evidence_service.save_evidence(
        db,
        milestone_id=seeded_milestone["milestone_id"],
        operator="合成上传人",
        filename="巡检报告.png",
        mime_type="image/png",
        content=content,
    )
    db.commit()
    replay = evidence_service.save_evidence(
        db,
        milestone_id=seeded_milestone["milestone_id"],
        operator="合成上传人",
        filename="巡检报告-副本.png",
        mime_type="image/png",
        content=content,
    )
    assert replay["replayed"] is True
    assert replay["evidence_id"] == first["evidence_id"]
    assert len(evidence_service.list_evidence(db, seeded_milestone["milestone_id"])) == 1


def test_upload_closes_reminder_and_replay_stays_idempotent(db, seeded_milestone, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "raw_file_dir", str(tmp_path))
    client = _evidence_client(db, username="evidence_api")
    # 无凭证仍不可手工关闭（凭证是关闭依据）
    denied = client.post(
        f"/api/maintenance/collection-milestones/{seeded_milestone['milestone_id']}/follow-ups",
        json={
            "expected_version": 1,
            "idempotency_key": "evidence-handle-denied",
            "action": "handle",
        },
    )
    assert denied.status_code == 422, denied.text
    assert denied.json()["detail"]["code"] == "invalid_request"
    assert "凭证" in denied.json()["detail"]["message"]

    content = _png_bytes()
    # 上传即关闭（round-5 Blocker 5）
    upload = client.post(
        f"/api/maintenance/collection-milestones/{seeded_milestone['milestone_id']}/evidence",
        files={"file": ("巡检报告.png", content, "image/png")},
    )
    assert upload.status_code == 201, upload.text
    assert upload.json()["md5"] == hashlib.md5(content).hexdigest()
    assert upload.json()["closed"] is True

    from app.models.maintenance_manager import MaintenanceCollectionMilestone

    milestone = db.get(
        MaintenanceCollectionMilestone, seeded_milestone["milestone_id"]
    )
    assert milestone.follow_up_status == "handled"

    # 已关闭后再次手工 handle → 422
    handled = client.post(
        f"/api/maintenance/collection-milestones/{seeded_milestone['milestone_id']}/follow-ups",
        json={
            "expected_version": 2,
            "idempotency_key": "evidence-handle-ok",
            "action": "handle",
            "note": "凭证已上传",
        },
    )
    assert handled.status_code == 422, handled.text

    # 同 md5 重放：幂等返回既有凭证，不重复关闭
    replay = client.post(
        f"/api/maintenance/collection-milestones/{seeded_milestone['milestone_id']}/evidence",
        files={"file": ("巡检报告-副本.png", content, "image/png")},
    )
    assert replay.status_code == 201, replay.text
    assert replay.json()["replayed"] is True
    assert replay.json()["closed"] is True  # 已关闭：重放按实际状态报告

    listing = client.get(
        f"/api/maintenance/collection-milestones/{seeded_milestone['milestone_id']}/evidence"
    )
    assert listing.status_code == 200
    rows = listing.json()["rows"]
    assert len(rows) == 1
    assert rows[0]["md5"] == hashlib.md5(content).hexdigest()
    assert rows[0]["original_filename"] == "巡检报告.png"


def test_upload_rejects_wrong_mime_and_missing_milestone(db, seeded_milestone, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "raw_file_dir", str(tmp_path))
    client = _evidence_client(db, username="evidence_api_bad")
    bad = client.post(
        f"/api/maintenance/collection-milestones/{seeded_milestone['milestone_id']}/evidence",
        files={"file": ("巡检报告.png", _png_bytes(), "application/pdf")},
    )
    assert bad.status_code == 422

    missing = client.post(
        "/api/maintenance/collection-milestones/no-such-milestone/evidence",
        files={"file": ("巡检报告.png", _png_bytes(), "image/png")},
    )
    assert missing.status_code == 404

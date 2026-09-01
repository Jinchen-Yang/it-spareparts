"""F6 回款提醒凭证：上传 → 独立目录文件 + yml sidecar + DB md5；关闭=已上传凭证。"""

import hashlib
import io
import zipfile
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


def _jpeg_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(buffer, format="JPEG")
    return buffer.getvalue()


def _office_bytes(required_member: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        package.writestr("[Content_Types].xml", "<Types/>")
        package.writestr(required_member, "<document/>")
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
    # 生产 API 会在 DB commit 前完成这一步；服务层测试显式保持同一顺序。
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
    db.commit()
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


def test_file_write_failure_rolls_back_evidence_and_keeps_reminder_open(
    db, seeded_milestone, tmp_path, monkeypatch,
):
    """磁盘失败不能先关闭提醒；DB 新行与状态转换必须一起保持未提交。"""
    monkeypatch.setattr(get_settings(), "raw_file_dir", str(tmp_path))
    client = _evidence_client(db, username="evidence_disk_failure")

    def fail_write(**_kwargs):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(evidence_service, "write_evidence_files", fail_write)
    response = client.post(
        f"/api/maintenance/collection-milestones/{seeded_milestone['milestone_id']}/evidence",
        files={"file": ("巡检报告.png", _png_bytes(), "image/png")},
    )
    assert response.status_code == 500, response.text
    assert response.json()["detail"]["code"] == "file_write_failed"

    from app.models.maintenance_manager import MaintenanceCollectionMilestone

    db.expire_all()
    milestone = db.get(
        MaintenanceCollectionMilestone, seeded_milestone["milestone_id"]
    )
    assert milestone.follow_up_status != "handled"
    assert evidence_service.active_evidence_count(
        db, seeded_milestone["milestone_id"]
    ) == 0


def test_replay_repairs_missing_files_before_closing_reminder(
    db, seeded_milestone, tmp_path, monkeypatch,
):
    """兼容旧异常：DB 已有 active 行但磁盘缺失时，同内容重放先修文件再关闭。"""
    monkeypatch.setattr(get_settings(), "raw_file_dir", str(tmp_path))
    content = _png_bytes("blue")
    orphan = evidence_service.save_evidence(
        db,
        milestone_id=seeded_milestone["milestone_id"],
        operator="历史上传人",
        filename="历史凭证.png",
        mime_type="image/png",
        content=content,
    )
    db.commit()
    data_path, meta_path = evidence_service.evidence_paths(
        orphan["file_id"], orphan["object_key"]
    )
    assert not data_path.exists()
    assert not meta_path.exists()

    client = _evidence_client(db, username="evidence_repair")
    response = client.post(
        f"/api/maintenance/collection-milestones/{seeded_milestone['milestone_id']}/evidence",
        files={"file": ("重传凭证.png", content, "image/png")},
    )
    assert response.status_code == 201, response.text
    assert response.json()["replayed"] is True
    assert response.json()["closed"] is True
    assert data_path.read_bytes() == content
    assert meta_path.exists()


def test_replay_sidecar_failure_restores_existing_binary_and_sidecar(
    db, seeded_milestone, tmp_path, monkeypatch,
):
    """重放的第二次 rename 失败时，不能删除 DB active 行对应的旧文件对。"""
    monkeypatch.setattr(get_settings(), "raw_file_dir", str(tmp_path))
    content = _png_bytes("green")
    existing = evidence_service.save_evidence(
        db,
        milestone_id=seeded_milestone["milestone_id"],
        operator="历史上传人",
        filename="历史凭证.png",
        mime_type="image/png",
        content=content,
    )
    evidence_service.write_evidence_files(
        file_id=existing["file_id"],
        object_key=existing["object_key"],
        content=content,
        meta={
            "file_id": existing["file_id"],
            "milestone_id": seeded_milestone["milestone_id"],
            "original_filename": existing["original_filename"],
            "mime_type": existing["mime_type"],
            "size_bytes": existing["size_bytes"],
            "md5": existing["md5"],
            "sha256": existing["sha256"],
            "uploaded_by": existing["uploaded_by"],
            "uploaded_at": existing["uploaded_at"],
            "storage": "local",
        },
    )
    db.commit()
    data_path, meta_path = evidence_service.evidence_paths(
        existing["file_id"], existing["object_key"]
    )
    old_data_inode = data_path.stat().st_ino
    old_meta = meta_path.read_bytes()

    def fail_sidecar(_file_id, _meta):
        raise OSError("synthetic sidecar failure after binary replace")

    monkeypatch.setattr(evidence_service, "_write_meta_sidecar", fail_sidecar)
    client = _evidence_client(db, username="evidence_replay_disk_failure")
    response = client.post(
        f"/api/maintenance/collection-milestones/{seeded_milestone['milestone_id']}/evidence",
        files={"file": ("历史凭证-重传.png", content, "image/png")},
    )
    assert response.status_code == 500, response.text
    assert response.json()["detail"]["code"] == "file_write_failed"

    assert data_path.read_bytes() == content
    assert data_path.stat().st_ino == old_data_inode
    assert meta_path.read_bytes() == old_meta
    assert evidence_service.active_evidence_count(
        db, seeded_milestone["milestone_id"]
    ) == 1
    assert not list(data_path.parent.glob("*.rollback"))


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


def test_upload_rejects_empty_and_oversized_collection_evidence(
    db, seeded_milestone, tmp_path, monkeypatch
):
    monkeypatch.setattr(get_settings(), "raw_file_dir", str(tmp_path))
    client = _evidence_client(db, username="evidence_api_size")
    url = (
        f"/api/maintenance/collection-milestones/"
        f"{seeded_milestone['milestone_id']}/evidence"
    )

    empty = client.post(
        url,
        files={"file": ("空凭证.pdf", b"", "application/pdf")},
    )
    assert empty.status_code == 422, empty.text

    oversized = client.post(
        url,
        files={
            "file": (
                "超大凭证.pdf",
                b"%PDF-" + b"x" * (20 * 1024 * 1024),
                "application/pdf",
            )
        },
    )
    assert oversized.status_code == 413, oversized.text


@pytest.mark.parametrize(
    ("filename", "content", "mime_type"),
    [
        ("伪造巡检图.png", b"\x89PNG\r\n\x1a\nnot-an-image", "image/png"),
        (
            "含启动动作.pdf",
            b"%PDF-1.7\n1 0 obj<</OpenAction 2 0 R>>endobj\n%%EOF",
            "application/pdf",
        ),
    ],
)
def test_upload_rejects_forged_or_active_collection_evidence(
    db,
    seeded_milestone,
    tmp_path,
    monkeypatch,
    filename,
    content,
    mime_type,
):
    monkeypatch.setattr(get_settings(), "raw_file_dir", str(tmp_path))
    client = _evidence_client(db, username="evidence_api_unsafe")

    response = client.post(
        f"/api/maintenance/collection-milestones/{seeded_milestone['milestone_id']}/evidence",
        files={"file": (filename, content, mime_type)},
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "invalid_request"


@pytest.mark.parametrize(
    ("filename", "content", "mime_type"),
    [
        ("巡检报告.pdf", b"%PDF-1.7\n%%EOF", "application/pdf"),
        (
            "巡检报告.docx",
            _office_bytes("word/document.xml"),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        (
            "巡检台账.xlsx",
            _office_bytes("xl/workbook.xml"),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
        ("现场照片.png", _png_bytes(), "image/png"),
        ("现场照片.jpg", _jpeg_bytes(), "image/jpeg"),
        ("现场照片.jpeg", _jpeg_bytes(), "image/jpeg"),
    ],
)
def test_upload_accepts_documented_collection_evidence_types(
    db,
    seeded_milestone,
    tmp_path,
    monkeypatch,
    filename,
    content,
    mime_type,
):
    monkeypatch.setattr(get_settings(), "raw_file_dir", str(tmp_path))
    client = _evidence_client(db, username="evidence_api_supported")

    response = client.post(
        f"/api/maintenance/collection-milestones/{seeded_milestone['milestone_id']}/evidence",
        files={"file": (filename, content, mime_type)},
    )

    assert response.status_code == 201, response.text
    assert response.json()["original_filename"] == filename
    assert response.json()["mime_type"] == mime_type

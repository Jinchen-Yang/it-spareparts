"""Public contract and fail-closed security tests for acceptance reports."""

from __future__ import annotations

from datetime import UTC, date, datetime
import io
from pathlib import Path
from uuid import uuid4
import zipfile

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
import pytest
from sqlalchemy import func, select

from app import auth
from app.api import maintenance_acceptance as acceptance_api
from app.auth import hash_password
from app.config import get_settings
from app.models.maintenance_manager import (
    BusinessFile,
    BusinessFileDownloadAudit,
    MaintenanceAcceptanceDeliverable,
    MaintenanceAcceptanceOperation,
)
from app.models.maintenance_project import (
    MaintenanceProject,
    MaintenanceProjectUserAssignment,
)
from app.models.system import SysUser
from app.services import maintenance_acceptance as acceptance_service


def _client(db, *, username: str, role: str, permissions: dict | None = None) -> tuple[TestClient, SysUser]:
    user = SysUser(
        username=username,
        role=role,
        display_name=f"合成账号 {username}",
        password_hash=hash_password("synthetic-password-123"),
        permissions=permissions,
    )
    db.add(user)
    db.commit()
    app = FastAPI()
    app.include_router(auth.router, prefix="/api")
    app.include_router(acceptance_api.router, prefix="/api")
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": username, "password": "synthetic-password-123"},
    )
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    return client, user


def _project(db, *, suffix: str, manager: SysUser | None, configured: bool = True):
    project = MaintenanceProject(
        project_id=f"acceptance-project-{suffix}",
        project_code=f"ACC-{suffix}",
        display_name=f"合成验收项目 {suffix}",
        lifecycle_status="ongoing",
    )
    db.add(project)
    db.flush()
    if manager is not None:
        db.add(
            MaintenanceProjectUserAssignment(
                assignment_id=f"acceptance-assignment-{suffix}",
                project_id=project.project_id,
                responsibility_type="primary_manager",
                user_id=manager.id,
                assigned_at=datetime.now(UTC),
                assigned_by="synthetic-admin",
                assignment_reason="验收 API 行级范围测试",
            )
        )
    deliverable = MaintenanceAcceptanceDeliverable(
        deliverable_id=str(uuid4()),
        project_id=project.project_id,
        deliverable_type="acceptance_report",
        due_date=date(2026, 8, 31) if configured else None,
        submission_status="not_submitted",
        approval_status="not_reviewed",
        configuration_state=("configured" if configured else "pending_business_configuration"),
        version=1,
    )
    db.add(deliverable)
    db.commit()
    return project, deliverable


def _png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (4, 4), color=(35, 80, 120)).save(output, format="PNG")
    return output.getvalue()


def test_manager_upload_submit_admin_review_download_are_scoped_idempotent_and_audited(db):
    manager, manager_user = _client(
        db,
        username="acceptance_manager",
        role="purchaser",
        permissions={
            "page_maintenance": True,
            "action_maintenance_acceptance_submit": True,
            "action_maintenance_acceptance_review": False,
        },
    )
    reviewer, _reviewer_user = _client(
        db,
        username="acceptance_reviewer",
        role="admin",
    )
    owned, deliverable = _project(db, suffix="owned", manager=manager_user)
    _other, _other_deliverable = _project(db, suffix="other", manager=None)

    search = manager.post("/api/maintenance/acceptance-deliverables/search", json={})
    assert search.status_code == 200, search.text
    assert [row["project_id"] for row in search.json()["rows"]] == [owned.project_id]

    image = _png()
    upload = manager.post(
        f"/api/maintenance/projects/stable/{owned.project_id}/acceptance/attachments",
        data={"expected_version": "1"},
        files={"file": ("验收现场.png", image, "image/png")},
        headers={"Idempotency-Key": "attachment-key-1"},
    )
    assert upload.status_code == 200, upload.text
    uploaded = upload.json()
    assert uploaded["version"] == 2
    assert uploaded["replayed"] is False

    replay = manager.post(
        f"/api/maintenance/projects/stable/{owned.project_id}/acceptance/attachments",
        data={"expected_version": "1"},
        files={"file": ("验收现场.png", image, "image/png")},
        headers={"Idempotency-Key": "attachment-key-1"},
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["file_id"] == uploaded["file_id"]
    assert replay.json()["replayed"] is True
    conflicting_reuse = manager.post(
        f"/api/maintenance/projects/stable/{owned.project_id}/acceptance/attachments",
        data={"expected_version": "1"},
        files={"file": ("另一个文件名.png", image, "image/png")},
        headers={"Idempotency-Key": "attachment-key-1"},
    )
    assert conflicting_reuse.status_code == 409

    submit = manager.post(
        f"/api/maintenance/projects/stable/{owned.project_id}/acceptance/submit",
        json={"expected_version": 2},
        headers={"Idempotency-Key": "submit-key-1"},
    )
    assert submit.status_code == 200, submit.text
    assert submit.json()["version"] == 3
    repeated_submit = manager.post(
        f"/api/maintenance/projects/stable/{owned.project_id}/acceptance/submit",
        json={"expected_version": 2},
        headers={"Idempotency-Key": "submit-key-1"},
    )
    assert repeated_submit.status_code == 200
    assert repeated_submit.json()["replayed"] is True

    denied_review = manager.post(
        f"/api/maintenance/acceptance-deliverables/{deliverable.deliverable_id}/review",
        json={"expected_version": 3, "decision": "approve"},
        headers={"Idempotency-Key": "manager-must-not-review"},
    )
    assert denied_review.status_code == 403

    approved = reviewer.post(
        f"/api/maintenance/acceptance-deliverables/{deliverable.deliverable_id}/review",
        json={"expected_version": 3, "decision": "approve"},
        headers={"Idempotency-Key": "admin-approve-key-1"},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["approval_status"] == "approved"

    downloaded = manager.get(
        f"/api/maintenance/acceptance-files/{uploaded['file_id']}"
    )
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == image
    assert downloaded.headers["cache-control"] == "no-store"
    assert downloaded.headers["x-content-type-options"] == "nosniff"

    db.expire_all()
    assert db.scalar(select(func.count()).select_from(BusinessFile)) == 1
    assert db.scalar(select(func.count()).select_from(MaintenanceAcceptanceOperation)) == 3
    audit = db.scalar(select(BusinessFileDownloadAudit))
    assert audit is not None
    assert audit.downloaded_by == "acceptance_manager"


def test_acceptance_direct_routes_enforce_row_scope_and_block_self_approval(db):
    manager, manager_user = _client(
        db,
        username="acceptance_scope_manager",
        role="purchaser",
        permissions={
            "page_maintenance": True,
            "action_maintenance_acceptance_submit": True,
            "action_maintenance_acceptance_review": False,
        },
    )
    administrator, _administrator_user = _client(
        db,
        username="acceptance_self_review_admin",
        role="admin",
    )
    unowned, _unowned_deliverable = _project(
        db,
        suffix="unowned-direct",
        manager=None,
    )

    denied_read = manager.get(
        f"/api/maintenance/projects/stable/{unowned.project_id}/acceptance"
    )
    assert denied_read.status_code == 403
    denied_upload = manager.post(
        f"/api/maintenance/projects/stable/{unowned.project_id}/acceptance/attachments",
        data={"expected_version": "1"},
        files={"file": ("report.png", _png(), "image/png")},
        headers={"Idempotency-Key": "unowned-upload"},
    )
    assert denied_upload.status_code == 403

    self_project, self_deliverable = _project(
        db,
        suffix="self-review",
        manager=None,
    )
    uploaded = administrator.post(
        f"/api/maintenance/projects/stable/{self_project.project_id}/acceptance/attachments",
        data={"expected_version": "1"},
        files={"file": ("self-report.png", _png(), "image/png")},
        headers={"Idempotency-Key": "self-review-upload"},
    )
    assert uploaded.status_code == 200, uploaded.text
    submitted = administrator.post(
        f"/api/maintenance/projects/stable/{self_project.project_id}/acceptance/submit",
        json={"expected_version": 2},
        headers={"Idempotency-Key": "self-review-submit"},
    )
    assert submitted.status_code == 200, submitted.text
    rejected = administrator.post(
        f"/api/maintenance/acceptance-deliverables/{self_deliverable.deliverable_id}/review",
        json={"expected_version": 3, "decision": "approve"},
        headers={"Idempotency-Key": "self-review-must-fail"},
    )
    assert rejected.status_code == 409
    assert "提交人与审批人" in rejected.json()["detail"]
    db.expire_all()
    current = db.get(MaintenanceAcceptanceDeliverable, self_deliverable.deliverable_id)
    assert current.approval_status == "not_reviewed"


def test_acceptance_attachment_rejects_bad_metadata_content_and_leaves_no_rows_or_files(db):
    manager, manager_user = _client(
        db,
        username="acceptance_security_manager",
        role="purchaser",
        permissions={
            "page_maintenance": True,
            "action_maintenance_acceptance_submit": True,
        },
    )
    project, _deliverable = _project(db, suffix="security", manager=manager_user)
    before_paths = set(Path(get_settings().raw_file_dir).rglob("*"))

    wrong_mime = manager.post(
        f"/api/maintenance/projects/stable/{project.project_id}/acceptance/attachments",
        data={"expected_version": "1"},
        files={"file": ("report.png", _png(), "application/pdf")},
        headers={"Idempotency-Key": "bad-mime"},
    )
    assert wrong_mime.status_code == 415

    active_pdf = b"%PDF-1.7\n1 0 obj<</OpenAction 2 0 R>>endobj\n%%EOF"
    blocked_pdf = manager.post(
        f"/api/maintenance/projects/stable/{project.project_id}/acceptance/attachments",
        data={"expected_version": "1"},
        files={"file": ("report.pdf", active_pdf, "application/pdf")},
        headers={"Idempotency-Key": "active-pdf"},
    )
    assert blocked_pdf.status_code == 415

    db.expire_all()
    assert db.scalar(select(func.count()).select_from(BusinessFile)) == 0
    assert db.scalar(select(func.count()).select_from(MaintenanceAcceptanceOperation)) == 0
    after_paths = set(Path(get_settings().raw_file_dir).rglob("*"))
    assert {path for path in after_paths - before_paths if path.is_file()} == set()


def test_acceptance_content_validation_rejects_path_names_external_office_and_oversize():
    with pytest.raises(acceptance_service.MaintenanceAcceptanceUnsupported, match="文件名"):
        acceptance_service.validate_attachment(
            filename="../report.png",
            mime_type="image/png",
            content=_png(),
        )

    package = io.BytesIO()
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<document/>")
        archive.writestr(
            "word/_rels/document.xml.rels",
            '<Relationships><Relationship TargetMode = "External" Target="https://example.invalid"/></Relationships>',
        )
    with pytest.raises(acceptance_service.MaintenanceAcceptanceUnsupported, match="外部链接"):
        acceptance_service.validate_attachment(
            filename="report.docx",
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            content=package.getvalue(),
        )

    with pytest.raises(acceptance_service.MaintenanceAcceptanceTooLarge):
        acceptance_service.validate_attachment(
            filename="report.pdf",
            mime_type="application/pdf",
            content=b"%PDF-1.7\n" + b"x" * acceptance_service.MAX_ACCEPTANCE_FILE_BYTES,
        )


def test_download_integrity_failure_is_fail_closed_and_not_audited(db):
    manager, manager_user = _client(
        db,
        username="acceptance_integrity_manager",
        role="purchaser",
        permissions={
            "page_maintenance": True,
            "action_maintenance_acceptance_submit": True,
        },
    )
    project, _deliverable = _project(db, suffix="integrity", manager=manager_user)
    image = _png()
    upload = manager.post(
        f"/api/maintenance/projects/stable/{project.project_id}/acceptance/attachments",
        data={"expected_version": "1"},
        files={"file": ("report.png", image, "image/png")},
        headers={"Idempotency-Key": "integrity-upload"},
    )
    assert upload.status_code == 200, upload.text
    db.expire_all()
    file_row = db.get(BusinessFile, upload.json()["file_id"])
    stored = Path(get_settings().raw_file_dir) / file_row.object_key
    stored.write_bytes(b"tampered")

    response = manager.get(f"/api/maintenance/acceptance-files/{file_row.file_id}")
    assert response.status_code == 409
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(BusinessFileDownloadAudit)) == 0

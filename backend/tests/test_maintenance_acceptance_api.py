"""Public contract and fail-closed security tests for acceptance reports."""

from __future__ import annotations

from datetime import UTC, date, datetime
import io
from pathlib import Path
from urllib.parse import quote
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
    BusinessFileLink,
    MaintenanceAcceptanceDeliverable,
    MaintenanceAcceptanceOperation,
)
from app.models.maintenance_project import (
    MaintenanceProject,
    MaintenanceProjectUserAssignment,
)
from app.models.system import SysUser
from app.services import maintenance_acceptance as acceptance_service


def _client(db, *, username: str, role: str, permissions: dict | None = None,
            salesperson_name: str | None = None) -> tuple[TestClient, SysUser]:
    user = SysUser(
        username=username,
        role=role,
        display_name=f"合成账号 {username}",
        salesperson_name=salesperson_name,
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


def _project(db, *, suffix: str, manager: SysUser | None, configured: bool = True,
             salesperson: str | None = None):
    project = MaintenanceProject(
        project_id=f"acceptance-project-{suffix}",
        project_code=f"ACC-{suffix}",
        display_name=f"合成验收项目 {suffix}",
        lifecycle_status="ongoing",
        salesperson=salesperson,
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


def _png(*, color: tuple[int, int, int] = (35, 80, 120)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (4, 4), color=color).save(output, format="PNG")
    return output.getvalue()


def test_manager_upload_submit_download_are_scoped_idempotent_and_audited(db):
    manager, manager_user = _client(
        db,
        username="acceptance_manager",
        role="purchaser",
        permissions={
            "page_maintenance": True,
            "action_maintenance_acceptance_submit": True,
        },
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
    # 2026-08-24 客户拍板：提交即生效，无需独立审批。
    assert submit.json()["approval_status"] == "approved"
    repeated_submit = manager.post(
        f"/api/maintenance/projects/stable/{owned.project_id}/acceptance/submit",
        json={"expected_version": 2},
        headers={"Idempotency-Key": "submit-key-1"},
    )
    assert repeated_submit.status_code == 200
    assert repeated_submit.json()["replayed"] is True

    db.expire_all()
    current = db.get(MaintenanceAcceptanceDeliverable, deliverable.deliverable_id)
    assert current.approval_status == "approved"
    assert current.approved_by == current.submitted_by == "acceptance_manager"
    assert current.approved_at is not None

    # 生效后仍可补充附件并重新提交新版本（审批锁定已随免审批取消）。
    # 注意换不同内容：同 sha256 会命中 2026-08-25 内容去重而回放。
    extra = manager.post(
        f"/api/maintenance/projects/stable/{owned.project_id}/acceptance/attachments",
        data={"expected_version": 3},
        files={"file": ("补充材料.png", _png(color=(200, 90, 40)), "image/png")},
        headers={"Idempotency-Key": "attachment-key-2"},
    )
    assert extra.status_code == 200, extra.text
    assert extra.json()["version"] == 4
    resubmit = manager.post(
        f"/api/maintenance/projects/stable/{owned.project_id}/acceptance/submit",
        json={"expected_version": 4},
        headers={"Idempotency-Key": "submit-key-2"},
    )
    assert resubmit.status_code == 200, resubmit.text
    assert resubmit.json()["approval_status"] == "approved"
    assert resubmit.json()["version"] == 5

    # 审批端点已随独立审批一并移除。
    gone = manager.post(
        f"/api/maintenance/acceptance-deliverables/{deliverable.deliverable_id}/review",
        json={"expected_version": 5, "decision": "approve"},
        headers={"Idempotency-Key": "review-endpoint-removed"},
    )
    assert gone.status_code == 404

    downloaded = manager.get(
        f"/api/maintenance/acceptance-files/{uploaded['file_id']}"
    )
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == image
    assert downloaded.headers["cache-control"] == "no-store"
    assert downloaded.headers["x-content-type-options"] == "nosniff"

    db.expire_all()
    assert db.scalar(select(func.count()).select_from(BusinessFile)) == 2
    assert db.scalar(select(func.count()).select_from(MaintenanceAcceptanceOperation)) == 4
    audit = db.scalar(select(BusinessFileDownloadAudit))
    assert audit is not None
    assert audit.downloaded_by == "acceptance_manager"


def test_acceptance_direct_routes_enforce_row_scope_and_submit_takes_effect(db):
    manager, manager_user = _client(
        db,
        username="acceptance_scope_manager",
        role="purchaser",
        permissions={
            "page_maintenance": True,
            "action_maintenance_acceptance_submit": True,
        },
    )
    administrator, _administrator_user = _client(
        db,
        username="acceptance_direct_admin",
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

    # 提交即生效：提交人即生效人（自审限制已随独立审批取消）。
    self_project, self_deliverable = _project(
        db,
        suffix="self-effect",
        manager=None,
    )
    uploaded = administrator.post(
        f"/api/maintenance/projects/stable/{self_project.project_id}/acceptance/attachments",
        data={"expected_version": "1"},
        files={"file": ("self-report.png", _png(), "image/png")},
        headers={"Idempotency-Key": "self-effect-upload"},
    )
    assert uploaded.status_code == 200, uploaded.text
    submitted = administrator.post(
        f"/api/maintenance/projects/stable/{self_project.project_id}/acceptance/submit",
        json={"expected_version": 2},
        headers={"Idempotency-Key": "self-effect-submit"},
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["approval_status"] == "approved"
    db.expire_all()
    current = db.get(MaintenanceAcceptanceDeliverable, self_deliverable.deliverable_id)
    assert current.approval_status == "approved"
    assert current.approved_by == current.submitted_by


def test_acceptance_attachment_accepts_any_type_and_reports_uploader_name(db):
    """2026-08-26 客户口径：附件不做类型/内容限制——MIME 与扩展名不一致、
    含启动动作的 PDF（此前都会 415）现在照常入库；同时上传人姓名进列表。"""
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

    wrong_mime = manager.post(
        f"/api/maintenance/projects/stable/{project.project_id}/acceptance/attachments",
        data={"expected_version": "1"},
        files={"file": ("报告.zip", b"PK\x03\x04arbitrary-bytes", "application/x-zip-compressed")},
        headers={"Idempotency-Key": "any-type-zip"},
    )
    assert wrong_mime.status_code == 200, wrong_mime.text
    assert wrong_mime.json()["uploaded_by_name"]

    active_pdf = b"%PDF-1.7\n1 0 obj<</OpenAction 2 0 R>>endobj\n%%EOF"
    blocked_pdf = manager.post(
        f"/api/maintenance/projects/stable/{project.project_id}/acceptance/attachments",
        data={"expected_version": "1"},
        files={"file": ("report.pdf", active_pdf, "application/pdf")},
        headers={"Idempotency-Key": "active-pdf"},
    )
    assert blocked_pdf.status_code == 200, blocked_pdf.text

    custom_file = manager.post(
        f"/api/maintenance/projects/stable/{project.project_id}/acceptance/attachments",
        data={"expected_version": "1"},
        files={
            "file": (
                "客户原始材料.evidence",
                b"customer-controlled-arbitrary-content",
                "application/x-maintenance-evidence",
            )
        },
        headers={"Idempotency-Key": "any-type-custom"},
    )
    assert custom_file.status_code == 200, custom_file.text
    assert custom_file.json()["mime_type"] == "application/x-maintenance-evidence"

    current = manager.get(
        f"/api/maintenance/projects/stable/{project.project_id}/acceptance"
    ).json()
    names = {item["uploaded_by_name"] for item in current["attachments"]}
    assert names == {"合成账号 acceptance_security_manager"}  # _client 的 display_name


def test_acceptance_upload_preserves_256_character_name_with_long_suffix(db):
    manager, manager_user = _client(
        db,
        username="acceptance_long_suffix_manager",
        role="purchaser",
        permissions={
            "page_maintenance": True,
            "action_maintenance_acceptance_submit": True,
        },
    )
    project, _deliverable = _project(db, suffix="long-suffix", manager=manager_user)
    filename = "a." + "x" * 254
    mime_type = "application/x-long-suffix"
    content = b"long-suffix-storage-regression"
    assert len(filename) == 256

    upload = manager.post(
        f"/api/maintenance/projects/stable/{project.project_id}/acceptance/attachments",
        files={"file": (filename, content, mime_type)},
    )
    assert upload.status_code == 200, upload.text
    uploaded = upload.json()
    assert uploaded["original_filename"] == filename
    assert uploaded["mime_type"] == mime_type

    download = manager.get(
        f"/api/maintenance/acceptance-files/{uploaded['file_id']}"
    )
    assert download.status_code == 200, download.text
    assert download.content == content


@pytest.mark.parametrize(
    "filename",
    ["验收报告.签字扫描", "report.📄", "report.data;name=other", "验收报告"],
    ids=["chinese-extension", "emoji-extension", "semicolon-extension", "no-extension"],
)
def test_acceptance_arbitrary_filename_upload_download_roundtrip(db, filename):
    manager, manager_user = _client(
        db,
        username="acceptance_filename_manager",
        role="purchaser",
        permissions={
            "page_maintenance": True,
            "action_maintenance_acceptance_submit": True,
        },
    )
    project, _deliverable = _project(db, suffix="free-name", manager=manager_user)
    content = b"arbitrary-acceptance-file"
    upload = manager.post(
        f"/api/maintenance/projects/stable/{project.project_id}/acceptance/attachments",
        files={"file": (filename, content, "application/octet-stream")},
    )
    assert upload.status_code == 200, upload.text
    uploaded = upload.json()
    assert uploaded["original_filename"] == filename
    assert uploaded["uploaded_by_name"] == manager_user.display_name

    current = manager.get(
        f"/api/maintenance/projects/stable/{project.project_id}/acceptance"
    )
    assert current.status_code == 200, current.text
    assert current.json()["attachments"][0]["original_filename"] == filename

    download = manager.get(
        f"/api/maintenance/acceptance-files/{uploaded['file_id']}"
    )
    assert download.status_code == 200, download.text
    assert download.content == content
    assert download.headers["content-disposition"] == (
        "attachment; filename=acceptance-report.bin; "
        f"filename*=UTF-8''{quote(filename, safe='')}"
    )
    assert download.headers["content-disposition"].isascii()
    assert download.headers["cache-control"] == "no-store"
    assert download.headers["x-content-type-options"] == "nosniff"
    assert download.headers["content-security-policy"] == "sandbox"
    db.expire_all()
    audit = db.scalar(select(BusinessFileDownloadAudit))
    assert audit is not None
    assert audit.downloaded_by == manager_user.username


def test_acceptance_content_validation_keeps_filename_safety_and_oversize():
    with pytest.raises(acceptance_service.MaintenanceAcceptanceUnsupported, match="文件名"):
        acceptance_service.validate_attachment(
            filename="../report.png",
            mime_type="image/png",
            content=_png(),
        )

    # 2026-08-26 客户口径：带外部链接的 Office 文件不再被拒——原样通过。
    package = io.BytesIO()
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<document/>")
        archive.writestr(
            "word/_rels/document.xml.rels",
            '<Relationships><Relationship TargetMode = "External" Target="https://example.invalid"/></Relationships>',
        )
    safe_name, extension, stored_mime = acceptance_service.validate_attachment(
        filename="report.docx",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        content=package.getvalue(),
    )
    assert (safe_name, extension, stored_mime) == (
        "report.docx",
        ".docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

    # 缺失 MIME 时回退 octet-stream（浏览器/客户端偶尔不申报）。
    assert acceptance_service.validate_attachment(
        filename="扫描件.dat", mime_type=None, content=b"\x00\x01",
    )[2] == "application/octet-stream"

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


def test_upload_and_submit_work_without_preconfigured_deliverable(db):
    """2026-08-25 客户拍板：验收只是个上传的地方，没有截止日概念——
    项目从未配置交付行（无月度全量表截止日）也能直接上传并提交。
    此前 _locked_deliverable 因截止日未配置直接拒绝，业务根本无法上传。"""
    manager, manager_user = _client(
        db,
        username="acceptance_no_config_manager",
        role="purchaser",
        permissions={
            "page_maintenance": True,
            "action_maintenance_acceptance_submit": True,
        },
    )
    project, _deliverable = _project(db, suffix="no-config", manager=manager_user)
    db.query(MaintenanceAcceptanceDeliverable).filter(
        MaintenanceAcceptanceDeliverable.project_id == project.project_id).delete()
    db.commit()

    # 新契约（2026-08-25 去版本握手）：上传只 POST 文件本身，不再带
    # expected_version 表单域（服务端忽略旧客户端残留字段）。
    upload = manager.post(
        f"/api/maintenance/projects/stable/{project.project_id}/acceptance/attachments",
        files={"file": ("验收报告.png", _png(), "image/png")},
        headers={"Idempotency-Key": "no-config-upload-1"},
    )
    assert upload.status_code == 200, upload.text
    assert upload.json()["version"] >= 1

    submit = manager.post(
        f"/api/maintenance/projects/stable/{project.project_id}/acceptance/submit",
        json={"expected_version": upload.json()["version"]},
        headers={"Idempotency-Key": "no-config-submit-1"},
    )
    assert submit.status_code == 200, submit.text
    assert submit.json()["approval_status"] == "approved"
    db.expire_all()
    fresh = db.scalar(select(MaintenanceAcceptanceDeliverable).where(
        MaintenanceAcceptanceDeliverable.project_id == project.project_id))
    assert fresh is not None
    assert fresh.due_date is None
    assert fresh.approval_status == "approved"


def test_sales_row_key_search_follows_salesperson_scope(db):
    """2026-08-25 行级口径对齐：开 own_maintenance_projects_only 的 sales
    按「负责人 ∪ 台账 salesperson」并集可见——能直达 GET 的项目也要能在
    search 搜到（此前 search 只用负责人口径，sales 能传却搜不到）。"""
    sales, _sales_user = _client(
        db,
        username="acceptance_sales",
        role="sales",
        permissions={
            "page_maintenance": True,
            "own_maintenance_projects_only": True,
            "action_maintenance_acceptance_submit": True,
        },
        salesperson_name="销售甲",
    )
    mine, _mine_deliverable = _project(
        db, suffix="sales-mine", manager=None, salesperson="销售甲"
    )
    other, _other_deliverable = _project(
        db, suffix="sales-other", manager=None, salesperson="销售乙"
    )

    search = sales.post("/api/maintenance/acceptance-deliverables/search", json={})
    assert search.status_code == 200, search.text
    assert [row["project_id"] for row in search.json()["rows"]] == [mine.project_id]

    direct = sales.get(
        f"/api/maintenance/projects/stable/{mine.project_id}/acceptance"
    )
    assert direct.status_code == 200, direct.text
    denied = sales.get(
        f"/api/maintenance/projects/stable/{other.project_id}/acceptance"
    )
    assert denied.status_code == 403


def test_scoped_viewer_search_matches_direct_get(db):
    """viewer（负责人挂靠 + 行键）的 search 结果集与直达 GET 同一并集。"""
    viewer, viewer_user = _client(
        db,
        username="acceptance_viewer",
        role="maintenance_manager",
        permissions={
            "page_maintenance": True,
            "own_maintenance_projects_only": True,
        },
        salesperson_name="维保张三",
    )
    owned, _owned_deliverable = _project(db, suffix="viewer-owned", manager=viewer_user)
    sales_side, _sales_deliverable = _project(
        db, suffix="viewer-sales", manager=None, salesperson="维保张三"
    )
    other, _other_deliverable = _project(
        db, suffix="viewer-other", manager=None, salesperson="别人"
    )

    search = viewer.post("/api/maintenance/acceptance-deliverables/search", json={})
    assert search.status_code == 200, search.text
    visible = {row["project_id"] for row in search.json()["rows"]}
    assert visible == {owned.project_id, sales_side.project_id}
    for project in (owned, sales_side):
        got = viewer.get(
            f"/api/maintenance/projects/stable/{project.project_id}/acceptance"
        )
        assert got.status_code == 200, got.text
    denied = viewer.get(
        f"/api/maintenance/projects/stable/{other.project_id}/acceptance"
    )
    assert denied.status_code == 403


def test_upload_requires_submit_action_even_with_page(db):
    """page=true/action=false：能进页面 ≠ 能上传——上传必须 403。"""
    manager, manager_user = _client(
        db,
        username="acceptance_page_only",
        role="purchaser",
        permissions={"page_maintenance": True},
    )
    project, _deliverable = _project(db, suffix="page-only", manager=manager_user)

    upload = manager.post(
        f"/api/maintenance/projects/stable/{project.project_id}/acceptance/attachments",
        files={"file": ("report.png", _png(), "image/png")},
    )
    assert upload.status_code == 403


def test_same_content_upload_dedupes_without_idempotency_key(db):
    """2026-08-25 内容去重（替代被删的版本握手）：同 sha256 重复上传
    （双击/超时重试，无幂等头、不同文件名）只产生一条文件记录，第二次
    按幂等回放形态返回 replayed=True，不落盘、不建行/链接。"""
    manager, manager_user = _client(
        db,
        username="acceptance_dedup_manager",
        role="purchaser",
        permissions={
            "page_maintenance": True,
            "action_maintenance_acceptance_submit": True,
        },
    )
    project, _deliverable = _project(db, suffix="dedup", manager=manager_user)
    image = _png()
    url = f"/api/maintenance/projects/stable/{project.project_id}/acceptance/attachments"

    first = manager.post(url, files={"file": ("首传.png", image, "image/png")})
    assert first.status_code == 200, first.text
    assert first.json()["replayed"] is False

    second = manager.post(url, files={"file": ("重试改名.png", image, "image/png")})
    assert second.status_code == 200, second.text
    assert second.json()["replayed"] is True
    assert second.json()["file_id"] == first.json()["file_id"]
    assert second.json()["sha256"] == first.json()["sha256"]

    db.expire_all()
    assert db.scalar(select(func.count()).select_from(BusinessFile)) == 1
    assert db.scalar(select(func.count()).select_from(BusinessFileLink)) == 1


def test_delete_last_attachment_of_submitted_deliverable_returns_409(db):
    """已提交验收的不变式：至少保留一份 active 附件——删最后一份 409，
    可先上传新附件再删（守卫 fail-closed，附件不被误删）。"""
    manager, manager_user = _client(
        db,
        username="acceptance_guard_manager",
        role="purchaser",
        permissions={
            "page_maintenance": True,
            "action_maintenance_acceptance_submit": True,
        },
    )
    project, _deliverable = _project(db, suffix="guard", manager=manager_user)
    upload = manager.post(
        f"/api/maintenance/projects/stable/{project.project_id}/acceptance/attachments",
        files={"file": ("唯一附件.png", _png(), "image/png")},
    )
    assert upload.status_code == 200, upload.text
    submit = manager.post(
        f"/api/maintenance/projects/stable/{project.project_id}/acceptance/submit",
        json={"expected_version": upload.json()["version"]},
        headers={"Idempotency-Key": "guard-submit-1"},
    )
    assert submit.status_code == 200, submit.text

    delete = manager.delete(
        f"/api/maintenance/projects/stable/{project.project_id}"
        f"/acceptance/attachments/{upload.json()['file_id']}"
    )
    assert delete.status_code == 409, delete.text
    assert "至少保留一份附件" in delete.json()["detail"]

    got = manager.get(
        f"/api/maintenance/projects/stable/{project.project_id}/acceptance"
    )
    assert [a["file_id"] for a in got.json()["attachments"]] == [
        upload.json()["file_id"]
    ]


def test_delete_attachment_writes_operation_ledger(db):
    """删除写操作台账：operation_type='attachment_archive'，含交付行/项目/
    操作人与结果快照（2026-08-25 起删除不再是「无痕」操作）。"""
    manager, manager_user = _client(
        db,
        username="acceptance_ledger_manager",
        role="purchaser",
        permissions={
            "page_maintenance": True,
            "action_maintenance_acceptance_submit": True,
        },
    )
    project, deliverable = _project(db, suffix="ledger", manager=manager_user)
    upload = manager.post(
        f"/api/maintenance/projects/stable/{project.project_id}/acceptance/attachments",
        files={"file": ("待删.png", _png(), "image/png")},
    )
    assert upload.status_code == 200, upload.text

    delete = manager.delete(
        f"/api/maintenance/projects/stable/{project.project_id}"
        f"/acceptance/attachments/{upload.json()['file_id']}"
    )
    assert delete.status_code == 200, delete.text
    assert delete.json()["archived"] is True

    db.expire_all()
    operation = db.scalar(
        select(MaintenanceAcceptanceOperation).where(
            MaintenanceAcceptanceOperation.operation_type == "attachment_archive"
        )
    )
    assert operation is not None
    assert operation.deliverable_id == deliverable.deliverable_id
    assert operation.project_id == project.project_id
    assert operation.operated_by == "acceptance_ledger_manager"
    assert operation.result_json["file_id"] == upload.json()["file_id"]
    assert operation.result_json["archived"] is True


def test_reupload_same_file_after_delete_succeeds(db):
    """删除后重传同文件成功：归档链接不参与内容去重，重传落新行、
    新 file_id，且新附件可正常下载。"""
    manager, manager_user = _client(
        db,
        username="acceptance_reupload_manager",
        role="purchaser",
        permissions={
            "page_maintenance": True,
            "action_maintenance_acceptance_submit": True,
        },
    )
    project, _deliverable = _project(db, suffix="reupload", manager=manager_user)
    image = _png()
    url = f"/api/maintenance/projects/stable/{project.project_id}/acceptance/attachments"

    first = manager.post(url, files={"file": ("报告.png", image, "image/png")})
    assert first.status_code == 200, first.text
    delete = manager.delete(
        f"/api/maintenance/projects/stable/{project.project_id}"
        f"/acceptance/attachments/{first.json()['file_id']}"
    )
    assert delete.status_code == 200, delete.text

    second = manager.post(url, files={"file": ("报告.png", image, "image/png")})
    assert second.status_code == 200, second.text
    assert second.json()["replayed"] is False
    assert second.json()["file_id"] != first.json()["file_id"]

    downloaded = manager.get(
        f"/api/maintenance/acceptance-files/{second.json()['file_id']}"
    )
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == image


def test_download_deleted_attachment_returns_404(db):
    """已删附件下载 404：软删链接即刻从受控下载通道消失（文件字节仅
    留档审计，不再可经 API 取出）。"""
    manager, manager_user = _client(
        db,
        username="acceptance_deleted_dl_manager",
        role="purchaser",
        permissions={
            "page_maintenance": True,
            "action_maintenance_acceptance_submit": True,
        },
    )
    project, _deliverable = _project(db, suffix="deleted-dl", manager=manager_user)
    upload = manager.post(
        f"/api/maintenance/projects/stable/{project.project_id}/acceptance/attachments",
        files={"file": ("将被删除.png", _png(), "image/png")},
    )
    assert upload.status_code == 200, upload.text
    delete = manager.delete(
        f"/api/maintenance/projects/stable/{project.project_id}"
        f"/acceptance/attachments/{upload.json()['file_id']}"
    )
    assert delete.status_code == 200, delete.text

    gone = manager.get(f"/api/maintenance/acceptance-files/{upload.json()['file_id']}")
    assert gone.status_code == 404


# ---------- 附件体积上限（2026-09-04：验收 50MB，回款凭证仍 20MB）----------

def test_acceptance_attachment_limit_is_50mb_and_evidence_stays_20mb():
    """两个域各用各的上限：改一个不得把另一个顺手带走。"""
    from app.services import maintenance_attachment_validation as av

    assert av.MAX_ACCEPTANCE_ATTACHMENT_BYTES == 50 * 1024 * 1024
    assert av.MAX_MAINTENANCE_ATTACHMENT_BYTES == 20 * 1024 * 1024
    assert acceptance_service.MAX_ACCEPTANCE_FILE_BYTES == 50 * 1024 * 1024


def test_acceptance_accepts_a_file_over_the_old_20mb_limit():
    """旧上限之上、新上限之内的附件必须收下——这条就是本次改动的验收点。"""
    payload = b"%PDF-1.7\n" + b"x" * (30 * 1024 * 1024)
    name, ext, mime = acceptance_service.validate_attachment(
        filename="现场验收.pdf", mime_type="application/pdf", content=payload)
    assert (name, ext) == ("现场验收.pdf", ".pdf")
    assert mime == "application/pdf"


def test_acceptance_still_refuses_over_50mb_with_a_message_matching_the_limit():
    with pytest.raises(acceptance_service.MaintenanceAcceptanceTooLarge) as raised:
        acceptance_service.validate_attachment(
            filename="超大.pdf", mime_type="application/pdf",
            content=b"x" * (acceptance_service.MAX_ACCEPTANCE_FILE_BYTES + 1))
    # 文案必须跟着上限走，不能再写死 20MB
    assert "50MB" in str(raised.value)


def test_collection_evidence_limit_unchanged_at_20mb():
    from app.services import maintenance_attachment_validation as av

    with pytest.raises(av.AttachmentTooLarge) as raised:
        av.validate_collection_evidence_attachment(
            filename="回单.pdf", mime_type="application/pdf",
            content=b"x" * (av.MAX_MAINTENANCE_ATTACHMENT_BYTES + 1))
    assert "20MB" in str(raised.value)

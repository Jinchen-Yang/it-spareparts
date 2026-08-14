"""Task 4 车道 B2：上传链安全红测（Step 4.1/4.2）。

覆盖：
- 未鉴权/非实名 admin/无显式 action/无 data_profit：在读取请求体之前拒绝。
- 单个 .xls；8 MiB Content-Length 先验与流式限额同时生效。
- Idempotency-Key 8–128 必填；缺失/过短 422。
- preview 创建批次 + 受控原件证据；项目/绑定/milestone/operation 零写入。
- 日志绝不包含原始文件名或业务行。
- 原件下载：同一高风险权限、审计记录、attachment disposition、no-store。
"""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import date
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from app import auth
from app import permissions as _perms
from app.api import maintenance_collection_plan_imports
from app.auth import hash_password
from app.models.maintenance_manager import (
    MaintenanceCollectionMilestone,
    MaintenanceCollectionMilestoneOperation,
    MaintenanceCollectionPlanImportBatch,
    MaintenanceCollectionPlanSourceBinding,
)
from app.models.maintenance_project import (
    MaintenanceProject,
    MaintenanceProjectContract,
)
from app.models.maintenance_project_operations import MaintenanceProjectOperationAudit
from app.models.system import SysUser
from tests.test_maintenance_collection_plan_xls import (
    ORDERED_HEADERS,
    build_synthetic_biff8,
)

PREVIEW_URL = "/api/maintenance/collection-plan-imports/preview"
MAX_PREVIEW_BYTES = 8 * 1024 * 1024


@pytest.fixture(autouse=True)
def _fixed_business_today(monkeypatch):
    monkeypatch.setattr(
        maintenance_collection_plan_imports,
        "business_today",
        lambda: date(2026, 8, 14),
    )


def _sys_user(
    db,
    *,
    username: str,
    role: str = "admin",
    import_action: bool = False,
) -> SysUser:
    graph = _perms.effective(role, None)
    template = dict(graph)
    overrides = {}
    if import_action:
        template["action_maintenance_collection_plan_import"] = False
        overrides["action_maintenance_collection_plan_import"] = True
    user = SysUser(
        username=username,
        role=role,
        display_name=f"合成{username}",
        password_hash=hash_password("synthetic-password-123"),
        template_perms=template,
        perm_overrides=overrides or None,
    )
    db.add(user)
    db.commit()
    return user


def _client(db, *, username: str, role: str = "admin", import_action: bool = False):
    user = db.scalar(select(SysUser).where(SysUser.username == username))
    if user is None:
        user = _sys_user(
            db,
            username=username,
            role=role,
            import_action=import_action,
        )
    app = FastAPI()
    app.include_router(auth.router, prefix="/api")
    app.include_router(maintenance_collection_plan_imports.router, prefix="/api")
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": username, "password": "synthetic-password-123"},
    )
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    return client, user


def _valid_workbook() -> bytes:
    headers = list(ORDERED_HEADERS)
    rows = [headers]
    row = [None] * 64
    row[0] = "SEC-ORDER-0001"
    row[4] = "合成项目 X"
    row[9] = 100.0
    row[16] = "2026年9月"
    row[17] = 100.0
    rows.append(row)
    return build_synthetic_biff8([{"name": "维保项目清单", "cells": _cells(rows)}])


def _cells(rows):
    cells = []
    for r, values in enumerate(rows):
        for c, value in enumerate(values):
            if value is not None:
                cells.append((r, c, value))
    return cells


def _upload(client, content, *, key="preview-key-0001", filename="plan.xls"):
    return client.post(
        PREVIEW_URL,
        files={"file": (filename, content, "application/vnd.ms-excel")},
        headers={"Idempotency-Key": key},
    )


def _count(db, model):
    return db.scalar(select(func.count()).select_from(model))


def test_unauthenticated_rejected_before_body_read(db):
    """未鉴权在读取任何请求体之前拒绝（即使 content-type 不是 multipart）。"""
    client, _ = _client(db, username="upload_anon_user", role="admin", import_action=True)
    del client.headers["Authorization"]
    response = client.post(
        PREVIEW_URL,
        content=b"not even a body",
        headers={"Content-Type": "text/plain"},
    )
    assert response.status_code == 401


def test_non_admin_rejected_before_body_read(db):
    _sys_user(db, username="upload_purchaser", role="purchaser")
    client, _ = _client(db, username="upload_purchaser_api", role="purchaser", import_action=True)
    response = client.post(
        PREVIEW_URL,
        content=b"garbage",
        headers={"Content-Type": "text/plain"},
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "permission_denied"


def test_admin_without_explicit_action_403(db):
    client, _ = _client(db, username="upload_no_action", role="admin", import_action=False)
    response = client.post(
        PREVIEW_URL,
        content=b"garbage",
        headers={"Content-Type": "text/plain"},
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "permission_denied"


def test_wrong_extension_415(db):
    client, _ = _client(db, username="upload_ext_user", role="admin", import_action=True)
    response = _upload(client, _valid_workbook(), filename="plan.xlsx")
    assert response.status_code == 415, response.text
    assert response.json()["detail"]["code"] == "unsupported_media_type"


def test_oversized_content_length_preflight_413(db):
    """Content-Length 先验：声明超过 8 MiB 就在读取请求体前拒绝。"""
    client, _ = _client(db, username="upload_big_user", role="admin", import_action=True)
    response = client.post(
        PREVIEW_URL,
        content=b"small",
        headers={
            "Content-Type": "multipart/form-data; boundary=synthetic-boundary",
            "Content-Length": str(MAX_PREVIEW_BYTES + 1),
            "Idempotency-Key": "content-length-key",
        },
    )
    assert response.status_code == 413, response.text
    assert response.json()["detail"]["code"] == "upload_too_large"


def test_streaming_limit_413_when_content_length_absent(db):
    """流式限额：无 Content-Length（chunked）时流式读取仍封顶 8 MiB。"""
    client, _ = _client(db, username="upload_stream_user", role="admin", import_action=True)
    boundary = "synthetic-boundary-7f8a"
    part_header = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="big.xls"\r\n'
        "Content-Type: application/vnd.ms-excel\r\n\r\n"
    ).encode()
    part_footer = f"\r\n--{boundary}--\r\n".encode()
    payload_size = MAX_PREVIEW_BYTES + 1024

    def body_chunks():
        yield part_header
        remaining = payload_size
        while remaining > 0:
            chunk = b"x" * min(512 * 1024, remaining)
            remaining -= len(chunk)
            yield chunk
        yield part_footer

    response = client.post(
        PREVIEW_URL,
        content=body_chunks(),
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Idempotency-Key": "stream-limit-key-01",
        },
    )
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "upload_too_large"


def test_preview_rejects_missing_and_short_idempotency_key(db):
    client, _ = _client(db, username="upload_key_user", role="admin", import_action=True)
    missing = client.post(
        PREVIEW_URL,
        files={"file": ("plan.xls", _valid_workbook(), "application/vnd.ms-excel")},
    )
    assert missing.status_code == 422
    assert missing.json()["detail"]["code"] == "invalid_request"
    short = _upload(client, _valid_workbook(), key="short")
    assert short.status_code == 422


def test_preview_creates_batch_and_evidence_with_zero_domain_writes(db):
    client, user = _client(
        db, username="upload_evidence_user", role="admin", import_action=True
    )
    content = _valid_workbook()
    digest = hashlib.sha256(content).hexdigest()
    response = _upload(client, content)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "valid"
    assert payload["file_sha256"] == digest
    assert payload["contract_version"] == "project-manager-xls-v1"

    db.expire_all()
    batch = db.scalar(select(MaintenanceCollectionPlanImportBatch))
    assert batch is not None
    assert batch.owner_user_id == user.id
    assert batch.file_size == len(content)
    assert batch.original_filename == "plan.xls"
    assert batch.storage_key != "plan.xls"
    assert len(batch.storage_key) == 32
    # 零领域事实写入：项目/绑定/milestone/operation 都没有。
    assert _count(db, MaintenanceProject) == 0
    assert _count(db, MaintenanceCollectionPlanSourceBinding) == 0
    assert _count(db, MaintenanceCollectionMilestone) == 0
    assert _count(db, MaintenanceCollectionMilestoneOperation) == 0
    # 受控原件证据落盘：raw_file_dir/maintenance-collection-plans/<storage_key>。
    raw_dir = Path(os.environ["RAW_FILE_DIR"]) / "maintenance-collection-plans"
    evidence = raw_dir / batch.storage_key
    assert evidence.is_file()
    assert evidence.read_bytes() == content


def test_preview_error_batch_keeps_evidence_and_issues(db):
    client, _ = _client(
        db, username="upload_error_user", role="admin", import_action=True
    )
    headers = list(ORDERED_HEADERS)
    row = [None] * 64
    row[0] = "SEC-ORDER-ORPHAN"
    row[4] = "合成项目 O"
    row[16] = "2026年9月"  # 有日期无金额 → blocker
    content = build_synthetic_biff8(
        [{"name": "维保项目清单", "cells": _cells([headers, row])}]
    )
    response = _upload(client, content)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "error"
    assert payload["can_apply"] is False
    assert any(issue["code"] == "orphan_date" for issue in payload["issues"])
    db.expire_all()
    batch = db.scalar(select(MaintenanceCollectionPlanImportBatch))
    assert batch.status == "error"
    assert batch.plan_json is not None
    assert batch.file_sha256 == hashlib.sha256(content).hexdigest()
    assert (Path(os.environ["RAW_FILE_DIR"]) / "maintenance-collection-plans" / batch.storage_key).is_file()


def test_preview_contract_error_persists_error_batch_evidence_at_api(db):
    """合同级失败（表头漂移）→ 422，但仍保留 error 批次哈希与受控原件证据。"""
    client, _ = _client(
        db, username="upload_contract_error", role="admin", import_action=True
    )
    headers = list(ORDERED_HEADERS)
    headers[16] = "回款时间X"
    content = build_synthetic_biff8([{"name": "Plan", "cells": _cells([headers])}])
    response = _upload(client, content, key="contract-error-key-01")
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "invalid_request"
    assert detail["issues"][0]["code"] == "header_signature_mismatch"
    db.expire_all()
    batch = db.scalar(select(MaintenanceCollectionPlanImportBatch))
    assert batch is not None
    assert batch.status == "error"
    assert batch.plan_json is None
    assert batch.file_sha256 == hashlib.sha256(content).hexdigest()
    assert (Path(os.environ["RAW_FILE_DIR"]) / "maintenance-collection-plans" / batch.storage_key).is_file()
    # 同 key 重试 → 重放同一合同错误，不产生新批次。
    again = _upload(client, content, key="contract-error-key-01")
    assert again.status_code == 422
    assert _count(db, MaintenanceCollectionPlanImportBatch) == 1


def test_logs_never_contain_original_filename_or_business_rows(db, caplog):
    client, _ = _client(
        db, username="upload_log_user", role="admin", import_action=True
    )
    with caplog.at_level(logging.DEBUG, logger="access"):
        response = _upload(
            client,
            _valid_workbook(),
            filename="secret-customer-master-2026.xls",
            key="log-safety-key-0001",
        )
    assert response.status_code == 200
    log_text = caplog.text
    assert "secret-customer-master-2026" not in log_text
    assert "SEC-ORDER-0001" not in log_text
    assert "合成项目" not in log_text


def test_source_file_download_requires_permission_audit_and_disposition(db):
    from datetime import UTC, datetime

    from app.models.maintenance_manager import MaintenanceCollectionPlanSourceBinding

    client, user = _client(
        db, username="upload_download_user", role="admin", import_action=True
    )
    # 先给订单一个既有绑定，原件下载审计需要绑定项目。
    project = MaintenanceProject(
        project_id="upload-download-project",
        project_code="UPL-DL-001",
        display_name="合成下载项目",
        lifecycle_status="ongoing",
        is_active=True,
    )
    db.add(project)
    db.flush()
    contract = MaintenanceProjectContract(
        project_contract_id="upload-download-pc",
        project_id=project.project_id,
        contract_id="UPL-DL-CONTRACT",
        contract_no="XS-UPL-DL-001",
        contract_amount=None,
        contract_status="active",
        status_mapping_state="mapped",
        status_mapping_version="synthetic-v1",
        included_in_total=True,
        effective_from=date(2026, 1, 1),
        source="synthetic-test",
    )
    db.add(contract)
    db.flush()
    db.add(
        MaintenanceCollectionPlanSourceBinding(
            binding_id="upload-download-binding",
            source_system="project_manager_xls_v1",
            external_order_no="SEC-ORDER-0001",
            project_id=project.project_id,
            project_contract_id=contract.project_contract_id,
            binding_status="reviewed",
            reviewed_by=user.id,
            reviewed_at=datetime.now(UTC),
            version=1,
        )
    )
    db.commit()

    content = _valid_workbook()
    preview = _upload(client, content, key="download-key-0001")
    assert preview.status_code == 200
    batch_id = preview.json()["batch_id"]

    denied_client, _ = _client(
        db, username="upload_download_denied", role="admin", import_action=False
    )
    denied = denied_client.get(
        f"/api/maintenance/collection-plan-imports/{batch_id}/source-file"
    )
    assert denied.status_code == 403

    response = client.get(
        f"/api/maintenance/collection-plan-imports/{batch_id}/source-file"
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/vnd.ms-excel"
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["cache-control"] == "no-store"
    assert response.content == content
    db.expire_all()
    audit = db.scalar(
        select(MaintenanceProjectOperationAudit).where(
            MaintenanceProjectOperationAudit.action == "source_file_download"
        )
    )
    assert audit is not None
    assert audit.entity_id == batch_id
    assert audit.operated_by == "upload_download_user"
    assert audit.project_id == project.project_id


def test_source_file_missing_batch_404(db):
    client, _ = _client(
        db, username="upload_missing_user", role="admin", import_action=True
    )
    response = client.get(
        "/api/maintenance/collection-plan-imports/no-such-batch/source-file"
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "not_found"

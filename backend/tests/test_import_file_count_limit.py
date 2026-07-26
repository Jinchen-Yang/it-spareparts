from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api import imports as imports_api
from app.auth import hash_password
from app.config import MAX_IMPORT_FILES
from app.etl import pipeline, precheck as import_precheck
from app.main import app
from app.models.system import (
    SysAccessLog,
    SysAuditLog,
    SysImportBatch,
    SysImportError,
    SysImportJob,
    SysRawFile,
    SysUser,
)


_XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_SPEC_MAX_IMPORT_FILES = 20
_LIMIT_DETAIL = f"一次最多导入 {_SPEC_MAX_IMPORT_FILES} 个文件，请分批处理"


def test_import_file_limit_matches_specification():
    assert MAX_IMPORT_FILES == _SPEC_MAX_IMPORT_FILES


@pytest.fixture()
def import_client(db):
    db.add(
        SysUser(
            username="file-limit-admin",
            role="admin",
            display_name="文件数量限制管理员",
            password_hash=hash_password("adminpw"),
        )
    )
    db.commit()
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": "file-limit-admin", "password": "adminpw"},
    )
    assert login.status_code == 200, login.text
    client.headers.update({"Authorization": f"Bearer {login.json()['token']}"})
    return client


def _files(count: int):
    return [
        ("files", (f"file-{index}.xlsx", b"not-read", _XLSX_CONTENT_TYPE))
        for index in range(count)
    ]


@pytest.mark.parametrize(
    "endpoint", ["/api/import/precheck", "/api/import/upload-batch"]
)
def test_import_endpoints_reject_requests_without_files(import_client, endpoint):
    response = import_client.post(endpoint)

    assert response.status_code == 422


@pytest.mark.parametrize("mode", ["skip", "upsert"])
def test_precheck_rejects_more_than_20_files_before_any_processing(
    import_client, monkeypatch, mode
):
    save = Mock()
    file_hash = Mock()
    inspect = Mock()
    success_hash_query = Mock()
    failed_file_result = Mock()
    apply_matches = Mock()
    build_response = Mock()
    monkeypatch.setattr(imports_api, "_save_upload_to_temp", save)
    monkeypatch.setattr(pipeline, "sha256_file", file_hash)
    monkeypatch.setattr(import_precheck, "inspect_file", inspect)
    monkeypatch.setattr(pipeline, "successful_batch_ids_by_hash", success_hash_query)
    monkeypatch.setattr(import_precheck, "failed_file_result", failed_file_result)
    monkeypatch.setattr(import_precheck, "apply_exact_success_matches", apply_matches)
    monkeypatch.setattr(import_precheck, "response", build_response)

    response = import_client.post(
        "/api/import/precheck",
        params={"mode": mode},
        files=_files(_SPEC_MAX_IMPORT_FILES + 1),
    )

    assert response.status_code == 400
    assert response.json() == {"detail": _LIMIT_DETAIL}
    for collaborator in (
        save,
        file_hash,
        inspect,
        success_hash_query,
        failed_file_result,
        apply_matches,
        build_response,
    ):
        collaborator.assert_not_called()


@pytest.mark.parametrize("mode", ["skip", "upsert"])
def test_upload_batch_rejects_more_than_20_files_atomically_before_side_effects(
    db, import_client, monkeypatch, mode
):
    models = (
        SysImportJob,
        SysImportBatch,
        SysRawFile,
        SysImportError,
        SysAccessLog,
        SysAuditLog,
    )
    counts_before = {
        model: db.scalar(select(func.count()).select_from(model)) for model in models
    }
    save = Mock(side_effect=AssertionError("save must not run"))
    audit = Mock()
    thread = Mock()
    monkeypatch.setattr(imports_api, "_save_upload_to_temp", save)
    monkeypatch.setattr(imports_api, "record_access_log", audit)
    monkeypatch.setattr(imports_api, "threading", SimpleNamespace(Thread=thread))

    response = import_client.post(
        "/api/import/upload-batch",
        params={"mode": mode},
        files=_files(_SPEC_MAX_IMPORT_FILES + 1),
    )

    assert response.status_code == 400
    assert response.json() == {"detail": _LIMIT_DETAIL}
    save.assert_not_called()
    audit.assert_not_called()
    thread.assert_not_called()
    db.expire_all()
    assert {
        model: db.scalar(select(func.count()).select_from(model)) for model in models
    } == counts_before


@pytest.mark.parametrize("mode", ["skip", "upsert"])
def test_precheck_allows_exactly_20_files_into_existing_flow(
    import_client, monkeypatch, tmp_path, mode
):
    save = Mock(
        side_effect=[
            str(tmp_path / f"nonexistent-{index}.xlsx")
            for index in range(_SPEC_MAX_IMPORT_FILES)
        ]
    )
    file_hash = Mock(return_value="hash")
    inspect = Mock(return_value={"filename": "file.xlsx"})
    success_hash_query = Mock(return_value={})
    apply_matches = Mock()
    build_response = Mock(return_value={"file_count": _SPEC_MAX_IMPORT_FILES})
    monkeypatch.setattr(imports_api, "_save_upload_to_temp", save)
    monkeypatch.setattr(pipeline, "sha256_file", file_hash)
    monkeypatch.setattr(import_precheck, "inspect_file", inspect)
    monkeypatch.setattr(pipeline, "successful_batch_ids_by_hash", success_hash_query)
    monkeypatch.setattr(import_precheck, "apply_exact_success_matches", apply_matches)
    monkeypatch.setattr(import_precheck, "response", build_response)

    response = import_client.post(
        "/api/import/precheck",
        params={"mode": mode},
        files=_files(_SPEC_MAX_IMPORT_FILES),
    )

    assert response.status_code == 200
    assert response.json() == {"file_count": _SPEC_MAX_IMPORT_FILES}
    assert save.call_count == _SPEC_MAX_IMPORT_FILES
    assert file_hash.call_count == _SPEC_MAX_IMPORT_FILES
    assert inspect.call_count == _SPEC_MAX_IMPORT_FILES
    success_hash_query.assert_called_once()
    apply_matches.assert_called_once()
    build_response.assert_called_once()


@pytest.mark.parametrize("mode", ["skip", "upsert"])
def test_upload_batch_allows_exactly_20_files_into_existing_flow(
    db, import_client, monkeypatch, mode
):
    save = Mock(
        side_effect=[
            f"/tmp/nonexistent-{index}.xlsx" for index in range(_SPEC_MAX_IMPORT_FILES)
        ]
    )
    audit = Mock()
    thread = Mock()
    monkeypatch.setattr(imports_api, "_save_upload_to_temp", save)
    monkeypatch.setattr(imports_api, "record_access_log", audit)
    monkeypatch.setattr(imports_api, "threading", SimpleNamespace(Thread=thread))

    response = import_client.post(
        "/api/import/upload-batch",
        params={"mode": mode},
        files=_files(_SPEC_MAX_IMPORT_FILES),
    )

    assert response.status_code == 200
    assert response.json()["total_files"] == _SPEC_MAX_IMPORT_FILES
    assert save.call_count == _SPEC_MAX_IMPORT_FILES
    audit.assert_called_once()
    thread.assert_called_once()
    thread.return_value.start.assert_called_once()
    assert db.scalar(select(func.count()).select_from(SysImportJob)) == 1

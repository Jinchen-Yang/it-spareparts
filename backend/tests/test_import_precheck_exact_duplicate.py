import hashlib
import io
import os

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import func, select

from app.api import imports as imports_api
from app.auth import hash_password
from app.db import SessionLocal
from app.etl import pipeline
from app.main import app
from app.models.system import SysImportBatch, SysImportJob, SysRawFile, SysUser


_XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.fixture()
def import_client(db):
    db.add(
        SysUser(
            username="exact-duplicate-admin",
            role="admin",
            display_name="精确重复预检管理员",
            password_hash=hash_password("adminpw"),
        )
    )
    db.commit()
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": "exact-duplicate-admin", "password": "adminpw"},
    )
    assert login.status_code == 200, login.text
    client.headers.update({"Authorization": f"Bearer {login.json()['token']}"})
    return client


def _purchase_workbook_bytes(part_number: str = "PN-EXACT") -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "采购"
    sheet.append(
        [
            "采购单号(必填)",
            "数据ID(不可修改)",
            "明细.数据ID(不可修改)",
            "明细.产品名称(必填)",
            "明细.单价(必填)",
        ]
    )
    sheet.append(["CGDD-EXACT", "PO-EXACT", "PL-EXACT", part_number, 100])
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def _precheck(client: TestClient, files: list[tuple[str, bytes]], mode: str = "skip"):
    response = client.post(
        f"/api/import/precheck?mode={mode}",
        files=[
            ("files", (filename, payload, _XLSX_CONTENT_TYPE))
            for filename, payload in files
        ],
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_skip_precheck_blocks_same_bytes_success_without_creating_records(
    db, import_client
):
    payload = _purchase_workbook_bytes()
    original = SysImportBatch(
        filename="original-name.xlsx",
        file_type="purchase",
        file_hash=hashlib.sha256(payload).hexdigest(),
        status="success",
    )
    db.add(original)
    db.commit()
    counts_before = {
        model: db.scalar(select(func.count()).select_from(model))
        for model in (SysImportJob, SysImportBatch, SysRawFile)
    }

    result = _precheck(import_client, [("renamed-copy.xlsx", payload)])

    assert result["mode"] == "skip"
    assert result["has_errors"] is False
    assert result["can_import_all"] is False
    assert result["files"][0]["exact_success_match"] == {"batch_id": original.id}
    assert result["files"][0]["blocked_reason"] == "exact_success_duplicate"
    assert result["files"][0]["can_import"] is False
    assert {
        model: db.scalar(select(func.count()).select_from(model))
        for model in (SysImportJob, SysImportBatch, SysRawFile)
    } == counts_before


def test_precheck_matches_only_success_hash_not_filename_or_other_statuses(
    db, import_client
):
    status_payloads = {
        status: _purchase_workbook_bytes(f"PN-{status.upper()}")
        for status in ("failed", "processing", "superseded")
    }
    for status, payload in status_payloads.items():
        db.add(
            SysImportBatch(
                filename=f"{status}.xlsx",
                file_type="purchase",
                file_hash=hashlib.sha256(payload).hexdigest(),
                status=status,
            )
        )
    successful_payload = _purchase_workbook_bytes("PN-SUCCESS")
    successful = SysImportBatch(
        filename="successful-original.xlsx",
        file_type="purchase",
        file_hash=hashlib.sha256(successful_payload).hexdigest(),
        status="success",
    )
    db.add(successful)
    db.commit()

    result = _precheck(
        import_client,
        [
            ("failed.xlsx", status_payloads["failed"]),
            ("processing.xlsx", status_payloads["processing"]),
            ("superseded.xlsx", status_payloads["superseded"]),
            ("successful-renamed.xlsx", successful_payload),
            (
                "successful-original.xlsx",
                _purchase_workbook_bytes("PN-DIFFERENT-BYTES"),
            ),
        ],
    )

    files = {file_result["filename"]: file_result for file_result in result["files"]}
    for filename in ("failed.xlsx", "processing.xlsx", "superseded.xlsx"):
        assert files[filename]["exact_success_match"] is None
        assert files[filename]["blocked_reason"] is None
        assert files[filename]["can_import"] is True
    assert files["successful-renamed.xlsx"]["exact_success_match"] == {
        "batch_id": successful.id
    }
    assert (
        files["successful-renamed.xlsx"]["blocked_reason"] == "exact_success_duplicate"
    )
    assert files["successful-original.xlsx"]["exact_success_match"] is None
    assert files["successful-original.xlsx"]["blocked_reason"] is None


def test_upsert_precheck_reports_match_without_blocking_or_adding_warning(
    db, import_client
):
    payload = _purchase_workbook_bytes("PN-UPsert")
    successful = SysImportBatch(
        filename="upsert-original.xlsx",
        file_type="purchase",
        file_hash=hashlib.sha256(payload).hexdigest(),
        status="success",
    )
    db.add(successful)
    db.commit()

    result = _precheck(import_client, [("upsert-repair.xlsx", payload)], mode="upsert")
    file_result = result["files"][0]

    assert result["mode"] == "upsert"
    assert result["has_errors"] is False
    assert result["can_import_all"] is True
    assert file_result["exact_success_match"] == {"batch_id": successful.id}
    assert file_result["blocked_reason"] is None
    assert file_result["can_import"] is True
    assert file_result["severity"] == "info"
    assert file_result["warning"] is None
    assert file_result["issues"] == []


def test_multifile_precheck_queries_success_hashes_once_and_preserves_order(
    db, import_client, monkeypatch
):
    first_payload = _purchase_workbook_bytes("PN-FIRST")
    second_payload = _purchase_workbook_bytes("PN-SECOND")
    db.add(
        SysImportBatch(
            filename="existing-second.xlsx",
            file_type="purchase",
            file_hash=hashlib.sha256(second_payload).hexdigest(),
            status="success",
        )
    )
    db.commit()
    calls: list[set[str]] = []
    real_query = pipeline.successful_batch_ids_by_hash

    def counted_query(session, file_hashes):
        calls.append(file_hashes)
        return real_query(session, file_hashes)

    monkeypatch.setattr(pipeline, "successful_batch_ids_by_hash", counted_query)

    result = _precheck(
        import_client,
        [("first.xlsx", first_payload), ("second.xlsx", second_payload)],
    )

    assert len(calls) == 1
    assert calls[0] == {
        hashlib.sha256(first_payload).hexdigest(),
        hashlib.sha256(second_payload).hexdigest(),
    }
    assert [file_result["filename"] for file_result in result["files"]] == [
        "first.xlsx",
        "second.xlsx",
    ]


def test_precheck_removes_each_temporary_file_before_batch_hash_query(
    import_client, monkeypatch
):
    temporary_paths: list[str] = []
    real_save = imports_api._save_upload_to_temp

    def tracked_save(file, name):
        path = real_save(file, name)
        temporary_paths.append(path)
        return path

    def successful_query(session, file_hashes):
        assert len(temporary_paths) == 2
        assert all(not os.path.exists(path) for path in temporary_paths)
        return {}

    monkeypatch.setattr(imports_api, "_save_upload_to_temp", tracked_save)
    monkeypatch.setattr(pipeline, "successful_batch_ids_by_hash", successful_query)

    _precheck(
        import_client,
        [
            ("first.xlsx", _purchase_workbook_bytes("PN-REMOVE-1")),
            ("second.xlsx", _purchase_workbook_bytes("PN-REMOVE-2")),
        ],
    )


def test_precheck_database_failure_propagates_and_cleans_every_temporary_file(
    import_client, monkeypatch
):
    temporary_paths: list[str] = []
    real_save = imports_api._save_upload_to_temp

    def tracked_save(file, name):
        path = real_save(file, name)
        temporary_paths.append(path)
        return path

    def failed_query(session, file_hashes):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(imports_api, "_save_upload_to_temp", tracked_save)
    monkeypatch.setattr(pipeline, "successful_batch_ids_by_hash", failed_query)

    with pytest.raises(RuntimeError, match="database unavailable"):
        _precheck(
            import_client,
            [
                ("first.xlsx", _purchase_workbook_bytes("PN-CLEAN-1")),
                ("second.xlsx", _purchase_workbook_bytes("PN-CLEAN-2")),
            ],
        )

    assert len(temporary_paths) == 2
    assert all(not os.path.exists(path) for path in temporary_paths)


def test_run_import_lock_check_rejects_success_committed_after_precheck(
    db, import_client, tmp_path
):
    payload = _purchase_workbook_bytes("PN-RACE")
    precheck = _precheck(import_client, [("race.xlsx", payload)])
    assert precheck["files"][0]["exact_success_match"] is None
    assert precheck["files"][0]["can_import"] is True

    with SessionLocal() as competing_session:
        winner = SysImportBatch(
            filename="race-winner.xlsx",
            file_type="purchase",
            file_hash=hashlib.sha256(payload).hexdigest(),
            status="success",
        )
        competing_session.add(winner)
        competing_session.commit()
        winner_id = winner.id

    path = tmp_path / "race.xlsx"
    path.write_bytes(payload)
    with pytest.raises(pipeline.DuplicateFileError) as exc_info:
        pipeline.run_import(db, str(path), "race.xlsx", mode="skip")

    assert exc_info.value.batch_id == winner_id


def test_precheck_invalid_mode_falls_back_to_skip_and_failed_files_keep_contract(
    import_client,
):
    response = import_client.post(
        "/api/import/precheck?mode=unsupported",
        files=[("files", ("not-xlsx.txt", b"invalid", "text/plain"))],
    )

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["mode"] == "skip"
    assert result["has_errors"] is True
    assert result["files"][0]["exact_success_match"] is None
    assert result["files"][0]["blocked_reason"] is None

import asyncio
import csv
import io
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text

from app.api import imports as imports_api
from app.auth import hash_password
from app.main import app
from app.models.system import SysImportBatch, SysImportError, SysUser
from app.security import UserContext


@pytest.fixture()
def admin_client(db):
    db.add(
        SysUser(
            username="admin",
            role="admin",
            display_name="管理员",
            password_hash=hash_password("adminpw"),
        )
    )
    db.commit()
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "adminpw"},
    )
    assert login.status_code == 200, login.text
    client.headers.update({"Authorization": f"Bearer {login.json()['token']}"})
    return client


def _batch(db) -> SysImportBatch:
    batch = SysImportBatch(
        filename="errors.xlsx",
        file_type="purchase",
        file_hash="error-download-batch",
        status="failed",
    )
    db.add(batch)
    db.flush()
    return batch


def _account(admin_client: TestClient, username: str, page_import: bool) -> TestClient:
    created = admin_client.post(
        "/api/accounts",
        json={
            "username": username,
            "password": "pw123456",
            "template_code": "readonly",
            "overrides": {"page_import": page_import},
        },
    )
    assert created.status_code == 201, created.text
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": username, "password": "pw123456"},
    )
    assert login.status_code == 200, login.text
    client.headers.update({"Authorization": f"Bearer {login.json()['token']}"})
    return client


def test_downloads_all_batch_errors_as_bom_csv(db, admin_client):
    batch = _batch(db)
    db.add_all(
        [
            SysImportError(
                batch_id=batch.id,
                row_no=index + 2,
                error_type="empty_pn_inactive" if index == 500 else "bad_row",
                error_detail=f"错误 {index + 1}",
                raw_row={"secret": index},
            )
            for index in range(501)
        ]
    )
    db.commit()

    response = admin_client.get(f"/api/import/batches/{batch.id}/errors.csv")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-disposition"] == (
        f'attachment; filename="import-batch-{batch.id}-issues.csv"'
    )
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.content.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert rows[0] == ["行号", "性质", "问题类型", "问题明细"]
    assert len(rows) == 502
    assert rows[1] == ["2", "错误", "bad_row", "错误 1"]
    assert rows[-1] == ["502", "提示", "empty_pn_inactive", "错误 501"]


def test_download_releases_db_before_send_and_deletes_temp_file_after_success(
    db, monkeypatch
):
    batch = _batch(db)
    db.add(
        SysImportError(
            batch_id=batch.id,
            row_no=2,
            error_type="bad_row",
            error_detail="错误 1",
        )
    )
    db.commit()
    monkeypatch.setattr(imports_api, "record_access_log", lambda *args: None)
    response = imports_api.batch_errors_csv(
        batch.id,
        db,
        UserContext(user_id="admin", role="admin", is_authenticated=True),
    )
    path = response.path
    assert os.path.basename(path).startswith("it-data-import-issues-")

    async def send_all():
        async def receive():
            return {"type": "http.disconnect"}

        async def send(_message):
            pass

        await response({"type": "http", "method": "GET", "headers": []}, receive, send)

    assert db.in_transaction() is False
    assert os.path.exists(path)
    asyncio.run(send_all())
    assert not os.path.exists(path)


def test_download_deletes_temp_file_when_asgi_send_raises(db, monkeypatch):
    batch = _batch(db)
    db.commit()
    monkeypatch.setattr(imports_api, "record_access_log", lambda *args: None)
    response = imports_api.batch_errors_csv(
        batch.id,
        db,
        UserContext(user_id="admin", role="admin", is_authenticated=True),
    )
    path = response.path

    async def fail_send():
        async def receive():
            return {"type": "http.disconnect"}

        async def send(_message):
            raise OSError("client disconnected")

        await response({"type": "http", "method": "GET", "headers": []}, receive, send)

    with pytest.raises(OSError, match="client disconnected"):
        asyncio.run(fail_send())
    assert not os.path.exists(path)


def test_download_preserves_send_error_when_temp_file_cleanup_raises(db, monkeypatch):
    batch = _batch(db)
    db.commit()
    monkeypatch.setattr(imports_api, "record_access_log", lambda *args: None)
    response = imports_api.batch_errors_csv(
        batch.id,
        db,
        UserContext(user_id="admin", role="admin", is_authenticated=True),
    )
    path = response.path
    original_remove = imports_api.os.remove

    async def fail_send():
        async def receive():
            return {"type": "http.disconnect"}

        async def send(_message):
            raise OSError("client disconnected")

        await response({"type": "http", "method": "GET", "headers": []}, receive, send)

    monkeypatch.setattr(
        imports_api.os,
        "remove",
        lambda _path: (_ for _ in ()).throw(OSError("cleanup failed")),
    )
    try:
        with pytest.raises(OSError, match="client disconnected"):
            asyncio.run(fail_send())
    finally:
        original_remove(path)


def test_download_deletes_temp_file_when_csv_generation_raises(db, monkeypatch):
    batch = _batch(db)
    db.commit()
    created_paths = []
    original_mkstemp = imports_api.tempfile.mkstemp
    original_fdopen = imports_api.os.fdopen

    def tracked_mkstemp(*args, **kwargs):
        fd, path = original_mkstemp(*args, **kwargs)
        created_paths.append(path)
        return fd, path

    class CloseFailingOutput:
        def __init__(self, output):
            self.output = output

        def write(self, value):
            return self.output.write(value)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

        def close(self):
            self.output.close()
            raise OSError("close failed")

    def close_failing_fdopen(*args, **kwargs):
        return CloseFailingOutput(original_fdopen(*args, **kwargs))

    class FailingDb:
        def get(self, model, key):
            return db.get(model, key)

        def execute(self, _statement):
            raise RuntimeError("original query failed")

        def rollback(self):
            raise RuntimeError("rollback failed")

    monkeypatch.setattr(imports_api.tempfile, "mkstemp", tracked_mkstemp)
    monkeypatch.setattr(imports_api.os, "fdopen", close_failing_fdopen)
    monkeypatch.setattr(imports_api, "record_access_log", lambda *args: None)

    with pytest.raises(RuntimeError, match="original query failed"):
        imports_api.batch_errors_csv(
            batch.id,
            FailingDb(),
            UserContext(user_id="admin", role="admin", is_authenticated=True),
        )

    assert len(created_paths) == 1
    assert not os.path.exists(created_paths[0])


def test_download_closes_fd_and_deletes_temp_file_when_fdopen_raises(db, monkeypatch):
    batch = _batch(db)
    db.commit()
    created = []
    original_mkstemp = imports_api.tempfile.mkstemp

    def tracked_mkstemp(*args, **kwargs):
        fd, path = original_mkstemp(*args, **kwargs)
        created.append((fd, path))
        return fd, path

    def failing_fdopen(*args, **kwargs):
        raise OSError("fdopen failed")

    monkeypatch.setattr(imports_api.tempfile, "mkstemp", tracked_mkstemp)
    monkeypatch.setattr(imports_api.os, "fdopen", failing_fdopen)
    monkeypatch.setattr(imports_api, "record_access_log", lambda *args: None)

    with pytest.raises(OSError, match="fdopen failed"):
        imports_api.batch_errors_csv(
            batch.id,
            db,
            UserContext(user_id="admin", role="admin", is_authenticated=True),
        )

    assert len(created) == 1
    fd, path = created[0]
    assert not os.path.exists(path)
    try:
        os.fstat(fd)
    except OSError:
        fd_closed = True
    else:
        fd_closed = False
        os.close(fd)
    assert fd_closed


def test_download_encodes_csv_special_characters_and_neutralizes_formulas(
    db,
    admin_client,
):
    batch = _batch(db)
    db.add_all(
        [
            SysImportError(
                batch_id=batch.id,
                row_no=8,
                error_type="+危险类型",
                error_detail='  =SUM(1,2)\n含有"引号"',
                raw_row={"private": "RAW_ROW_MUST_NOT_LEAK"},
            ),
            SysImportError(
                batch_id=batch.id,
                row_no=9,
                error_type="-危险类型",
                error_detail="\t@危险明细",
            ),
            SysImportError(
                batch_id=batch.id, row_no=10, error_type="bad_row", error_detail="\r=CR"
            ),
            SysImportError(
                batch_id=batch.id, row_no=11, error_type="bad_row", error_detail="\n+LF"
            ),
            SysImportError(
                batch_id=batch.id,
                row_no=12,
                error_type="bad_row",
                error_detail="\r\n-CRLF",
            ),
            SysImportError(
                batch_id=batch.id,
                row_no=13,
                error_type="bad_row",
                error_detail=" \t\r\n@混合",
            ),
            SysImportError(
                batch_id=batch.id,
                row_no=14,
                error_type="bad_row",
                error_detail=" \t\r\n",
            ),
        ]
    )
    db.commit()

    response = admin_client.get(f"/api/import/batches/{batch.id}/errors.csv")

    assert response.status_code == 200
    rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert rows == [
        ["行号", "性质", "问题类型", "问题明细"],
        ["8", "错误", "'+危险类型", '\'  =SUM(1,2)\n含有"引号"'],
        ["9", "错误", "'-危险类型", "'\t@危险明细"],
        ["10", "错误", "bad_row", "'\r=CR"],
        ["11", "错误", "bad_row", "'\n+LF"],
        ["12", "错误", "bad_row", "'\r\n-CRLF"],
        ["13", "错误", "bad_row", "' \t\r\n@混合"],
        ["14", "错误", "bad_row", " \t\r\n"],
    ]
    assert b"RAW_ROW_MUST_NOT_LEAK" not in response.content


def test_download_returns_404_for_missing_batch(admin_client):
    response = admin_client.get("/api/import/batches/999999/errors.csv")

    assert response.status_code == 404
    assert response.json() == {"detail": "批次不存在"}


def test_download_for_batch_without_errors_contains_only_header(db, admin_client):
    batch = _batch(db)
    db.commit()

    response = admin_client.get(f"/api/import/batches/{batch.id}/errors.csv")

    assert response.status_code == 200
    assert response.content.decode("utf-8-sig") == "行号,性质,问题类型,问题明细\r\n"


def test_download_requires_authentication():
    response = TestClient(app).get("/api/import/batches/1/errors.csv")

    assert response.status_code == 401


def test_download_requires_page_import_permission(db, admin_client):
    batch = _batch(db)
    db.commit()
    denied_client = _account(admin_client, "import-download-denied", False)

    response = denied_client.get(f"/api/import/batches/{batch.id}/errors.csv")

    assert response.status_code == 403
    assert response.json() == {"detail": "无权访问该页面"}


def test_download_allows_non_admin_with_page_import_permission(db, admin_client):
    batch = _batch(db)
    db.commit()
    allowed_client = _account(admin_client, "import-download-allowed", True)

    response = allowed_client.get(f"/api/import/batches/{batch.id}/errors.csv")

    assert response.status_code == 200
    assert response.content.startswith(b"\xef\xbb\xbf")


def test_download_is_recorded_in_account_activity(db, admin_client):
    batch = _batch(db)
    db.commit()

    downloaded = admin_client.get(f"/api/import/batches/{batch.id}/errors.csv")
    activity = admin_client.get("/api/accounts/admin/activity")

    assert downloaded.status_code == 200
    assert activity.status_code == 200, activity.text
    assert {
        "action": "download_errors",
        "resource": f"import_batch:{batch.id}",
    } in [
        {"action": row["action"], "resource": row["resource"]}
        for row in activity.json()["recent"]
    ]


def test_batch_detail_reports_full_issue_count_and_orders_preview(db, admin_client):
    batch = _batch(db)
    db.add_all(
        [
            SysImportError(
                batch_id=batch.id,
                row_no=501 - index,
                error_type="empty_pn_inactive" if index == 500 else "bad_row",
                error_detail=str(index),
            )
            for index in range(501)
        ]
    )
    db.commit()

    response = admin_client.get(f"/api/import/batches/{batch.id}")

    assert response.status_code == 200
    assert response.json()["issue_count"] == 501
    assert len(response.json()["errors"]) == 500
    assert response.json()["errors"][0] == {
        "row_no": 501,
        "nature": "错误",
        "error_type": "bad_row",
        "detail": "0",
    }


def test_import_error_batch_id_order_query_uses_composite_index(db):
    indexes = inspect(db.bind).get_indexes("sys_import_error")
    assert {
        "name": "ix_import_error_batch_id_id",
        "column_names": ["batch_id", "id"],
    } in [
        {"name": index["name"], "column_names": index["column_names"]}
        for index in indexes
    ]

    batch_ids = [
        db.execute(
            text(
                "INSERT INTO sys_import_batch (filename, file_type, file_hash, status) "
                "VALUES (:filename, 'purchase', :file_hash, 'failed') RETURNING id"
            ),
            {"filename": f"plan-{index}.xlsx", "file_hash": f"plan-{index}"},
        ).scalar_one()
        for index in range(2)
    ]
    for batch_id in batch_ids:
        db.execute(
            text(
                "INSERT INTO sys_import_error (batch_id, row_no, error_type, error_detail) "
                "SELECT :batch_id, n, 'bad_row', 'detail' "
                "FROM generate_series(1, 1000) AS n"
            ),
            {"batch_id": batch_id},
        )
    db.execute(text("ANALYZE sys_import_error"))
    db.execute(text("SET LOCAL enable_seqscan = off"))
    plan = "\n".join(
        db.execute(
            text(
                "EXPLAIN SELECT row_no, error_type, error_detail FROM sys_import_error "
                "WHERE batch_id = :batch_id ORDER BY id"
            ),
            {"batch_id": batch_ids[0]},
        ).scalars()
    )
    assert "Index Scan using ix_import_error_batch_id_id" in plan

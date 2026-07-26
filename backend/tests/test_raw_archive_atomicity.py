import hashlib
import logging
import os
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api.imports import _process_import_job
from app.auth import hash_password
from app.config import get_settings
from app.etl import pipeline
from app.main import app
from app.models.inventory import Inventory
from app.models.system import (
    SysAuditLog,
    SysImportBatch,
    SysImportJob,
    SysRawFile,
    SysUser,
)


ARCHIVE_ERROR = "原始文件归档失败"
INTERNAL_ERROR = "系统处理异常，请联系管理员查看服务端日志"
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.fixture(autouse=True)
def enable_archive_loggers(monkeypatch):
    # Alembic's test-session logging setup disables loggers imported beforehand.
    monkeypatch.setattr(pipeline._log, "disabled", False)
    monkeypatch.setattr(logging.getLogger("imports"), "disabled", False)


@pytest.fixture()
def raw_dir(tmp_path, monkeypatch):
    path = tmp_path / "raw"
    monkeypatch.setattr(get_settings(), "raw_file_dir", str(path))
    return path


def _source(tmp_path: Path, content: bytes = b"archive-content") -> tuple[Path, str]:
    source = tmp_path / "source.xlsx"
    source.write_bytes(content)
    return source, hashlib.sha256(content).hexdigest()


def _temp_files(raw_dir: Path) -> list[Path]:
    return [path for path in raw_dir.iterdir() if not path.name.endswith(".xlsx")]


@pytest.mark.parametrize(
    "file_hash",
    ["", "a" * 63, "a" * 65, "A" * 64, "g" * 64, "../" + "a" * 61],
)
def test_archive_rejects_invalid_hash_with_independent_safe_error(
    tmp_path, raw_dir, file_hash
):
    source, _ = _source(tmp_path)

    with pytest.raises(pipeline.ArchiveError, match=f"^{ARCHIVE_ERROR}$") as exc_info:
        pipeline._archive(str(source), file_hash)

    assert not isinstance(exc_info.value, pipeline.ReaderError)
    assert list(raw_dir.glob("*")) == []


@pytest.mark.parametrize("destination_state", ["missing", "valid", "corrupt"])
def test_archive_revalidates_changed_source_before_reuse_or_repair(
    tmp_path, raw_dir, destination_state
):
    source, expected_hash = _source(tmp_path, b"expected")
    raw_dir.mkdir()
    destination = raw_dir / f"{expected_hash}.xlsx"
    if destination_state == "valid":
        destination.write_bytes(b"expected")
    elif destination_state == "corrupt":
        destination.write_bytes(b"old-corrupt")
    before = destination.stat() if destination.exists() else None
    before_bytes = destination.read_bytes() if destination.exists() else None
    source.write_bytes(b"changed-after-initial-hash")

    with pytest.raises(pipeline.ArchiveError, match=f"^{ARCHIVE_ERROR}$"):
        pipeline._archive(str(source), expected_hash)

    if before is None:
        assert not destination.exists()
    else:
        after = destination.stat()
        assert destination.read_bytes() == before_bytes
        assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert _temp_files(raw_dir) == []


def test_archive_reuses_valid_destination_without_rewrite(tmp_path, raw_dir):
    source, file_hash = _source(tmp_path)
    raw_dir.mkdir()
    destination = raw_dir / f"{file_hash}.xlsx"
    destination.write_bytes(source.read_bytes())
    before = destination.stat()

    result = pipeline._archive(str(source), file_hash)

    after = destination.stat()
    assert result == str(destination)
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert _temp_files(raw_dir) == []


def test_archive_rejects_destination_swapped_to_matching_symlink_before_open(
    tmp_path, raw_dir, monkeypatch
):
    source, file_hash = _source(tmp_path, b"expected")
    raw_dir.mkdir()
    destination = raw_dir / f"{file_hash}.xlsx"
    destination.write_bytes(b"expected")
    outside = tmp_path / "outside.xlsx"
    outside.write_bytes(b"expected")
    original_digest_regular = pipeline._archive_digest_regular

    def swap_then_digest(path, expected_stat):
        destination.unlink()
        destination.symlink_to(outside)
        return original_digest_regular(path, expected_stat)

    monkeypatch.setattr(pipeline, "_archive_digest_regular", swap_then_digest)

    with pytest.raises(pipeline.ArchiveError, match=f"^{ARCHIVE_ERROR}$"):
        pipeline._archive(str(source), file_hash)

    assert destination.is_symlink()
    assert destination.readlink() == outside
    assert outside.read_bytes() == b"expected"
    assert _temp_files(raw_dir) == []


def test_archive_atomically_repairs_corrupt_regular_file_once(
    tmp_path, raw_dir, caplog
):
    source, file_hash = _source(tmp_path)
    raw_dir.mkdir()
    destination = raw_dir / f"{file_hash}.xlsx"
    destination.write_bytes(b"corrupt")
    corrupt_inode = destination.stat().st_ino

    with caplog.at_level("WARNING"):
        pipeline._archive(str(source), file_hash)

    repaired = destination.stat()
    assert destination.read_bytes() == source.read_bytes()
    assert repaired.st_ino != corrupt_inode
    assert str(raw_dir) not in caplog.text
    assert file_hash not in caplog.text
    assert "corrupt raw archive" in caplog.text

    pipeline._archive(str(source), file_hash)
    reused = destination.stat()
    assert (reused.st_ino, reused.st_mtime_ns) == (
        repaired.st_ino,
        repaired.st_mtime_ns,
    )


@pytest.mark.parametrize("failure", ["copy", "flush", "fsync", "replace"])
@pytest.mark.parametrize("old_destination", [False, True])
def test_archive_failure_preserves_destination_and_cleans_own_temp(
    tmp_path, raw_dir, monkeypatch, failure, old_destination
):
    source, file_hash = _source(tmp_path)
    raw_dir.mkdir()
    destination = raw_dir / f"{file_hash}.xlsx"
    if old_destination:
        destination.write_bytes(b"old-corrupt")
        before = destination.stat()
    else:
        before = None

    target = {
        "copy": "_copy_archive_chunks",
        "flush": "_flush_archive_temp",
        "fsync": "_fsync_archive_temp",
        "replace": "_replace_archive_temp",
    }[failure]

    def fail(*args):
        if failure == "copy":
            args[1].write(b"partial")
        raise OSError(f"injected {failure} failure")

    monkeypatch.setattr(pipeline, target, fail)

    with pytest.raises(pipeline.ArchiveError, match=f"^{ARCHIVE_ERROR}$"):
        pipeline._archive(str(source), file_hash)

    if before is None:
        assert not destination.exists()
    else:
        after = destination.stat()
        assert destination.read_bytes() == b"old-corrupt"
        assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert _temp_files(raw_dir) == []


def test_archive_cleanup_failure_does_not_mask_original_error(
    tmp_path, raw_dir, monkeypatch, caplog
):
    source, file_hash = _source(tmp_path)

    def fail_copy(_source_path, temp_file):
        temp_file.write(b"partial")
        raise OSError("original write failure")

    monkeypatch.setattr(pipeline, "_copy_archive_chunks", fail_copy)
    monkeypatch.setattr(
        pipeline,
        "_remove_archive_temp",
        lambda _path: (_ for _ in ()).throw(OSError("cleanup")),
    )

    with (
        caplog.at_level("WARNING"),
        pytest.raises(pipeline.ArchiveError, match=f"^{ARCHIVE_ERROR}$") as exc_info,
    ):
        pipeline._archive(str(source), file_hash)

    assert "archive temporary file cleanup failed" in caplog.text
    assert isinstance(exc_info.value.__cause__, OSError)
    assert str(exc_info.value.__cause__) == "original write failure"


def test_archive_fdopen_cleanup_failure_does_not_mask_original_error(
    tmp_path, raw_dir, monkeypatch, caplog
):
    source, file_hash = _source(tmp_path)
    real_close = os.close
    opened_fd = None
    fd_baseline = len(os.listdir("/proc/self/fd"))

    def fail_open(fd):
        nonlocal opened_fd
        opened_fd = fd
        raise OSError("original fdopen failure")

    def fail_close(_fd):
        raise OSError("fd cleanup failure")

    monkeypatch.setattr(pipeline, "_open_archive_temp", fail_open)
    monkeypatch.setattr(pipeline, "_close_archive_fd", fail_close)

    try:
        with (
            caplog.at_level("WARNING"),
            pytest.raises(
                pipeline.ArchiveError, match=f"^{ARCHIVE_ERROR}$"
            ) as exc_info,
        ):
            pipeline._archive(str(source), file_hash)

        destination = raw_dir / f"{file_hash}.xlsx"
        assert opened_fd is not None
        assert isinstance(exc_info.value.__cause__, OSError)
        assert str(exc_info.value.__cause__) == "original fdopen failure"
        assert "archive temporary fd close failed" in caplog.text
        assert not destination.exists()
        assert _temp_files(raw_dir) == []
    finally:
        if opened_fd is not None:
            real_close(opened_fd)

    assert len(os.listdir("/proc/self/fd")) == fd_baseline


@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo"])
def test_archive_rejects_non_regular_destination_without_mutation(
    tmp_path, raw_dir, kind
):
    source, file_hash = _source(tmp_path)
    raw_dir.mkdir()
    destination = raw_dir / f"{file_hash}.xlsx"
    target = tmp_path / "outside"
    if kind == "symlink":
        target.write_bytes(b"outside-content")
        destination.symlink_to(target)
    elif kind == "directory":
        destination.mkdir()
    else:
        os.mkfifo(destination)
    before = destination.lstat()

    with pytest.raises(pipeline.ArchiveError, match=f"^{ARCHIVE_ERROR}$"):
        pipeline._archive(str(source), file_hash)

    after = destination.lstat()
    assert stat.S_IFMT(after.st_mode) == stat.S_IFMT(before.st_mode)
    assert after.st_ino == before.st_ino
    if kind == "symlink":
        assert target.read_bytes() == b"outside-content"
    assert _temp_files(raw_dir) == []


def test_four_concurrent_archives_publish_one_complete_file(tmp_path, raw_dir):
    source, file_hash = _source(tmp_path, b"concurrent-content" * 1024)
    expected = raw_dir / f"{file_hash}.xlsx"

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(pipeline._archive, str(source), file_hash) for _ in range(4)
        ]
        results = [future.result(timeout=5) for future in futures]

    assert results == [str(expected)] * 4
    assert hashlib.sha256(expected.read_bytes()).hexdigest() == file_hash
    assert [path for path in raw_dir.iterdir()] == [expected]
    assert stat.S_IMODE(expected.stat().st_mode) == 0o600


def test_concurrent_reader_retries_destination_repaired_after_hash(
    tmp_path, raw_dir, monkeypatch
):
    source, file_hash = _source(tmp_path, b"current-archive" * 1024)
    raw_dir.mkdir()
    destination = raw_dir / f"{file_hash}.xlsx"
    destination.write_bytes(b"old-corrupt-archive" * 1024)
    reader_hashed_old_destination = threading.Event()
    release_reader = threading.Event()
    reader_lstat_calls = 0
    real_lstat = os.lstat

    def archive_lstat(path):
        nonlocal reader_lstat_calls
        if threading.current_thread().name == "archive-reader":
            reader_lstat_calls += 1
            if reader_lstat_calls == 2:
                reader_hashed_old_destination.set()
                assert release_reader.wait(timeout=5)
        return real_lstat(path)

    monkeypatch.setattr(pipeline, "_archive_lstat", archive_lstat)

    reader_result = []
    reader_error = []

    def read_archive():
        try:
            reader_result.append(pipeline._archive(str(source), file_hash))
        except Exception as exc:
            reader_error.append(exc)

    reader = threading.Thread(target=read_archive, name="archive-reader")
    reader.start()
    assert reader_hashed_old_destination.wait(timeout=5)
    try:
        writer_result = pipeline._archive(str(source), file_hash)
    finally:
        release_reader.set()
        reader.join(timeout=5)

    assert not reader.is_alive()
    if reader_error:
        raise reader_error[0]
    assert [writer_result, *reader_result] == [str(destination)] * 2
    assert hashlib.sha256(destination.read_bytes()).hexdigest() == file_hash
    assert _temp_files(raw_dir) == []


def test_archive_repair_is_atomically_visible_to_repository_observer(
    tmp_path, raw_dir, monkeypatch
):
    new_bytes = b"new-complete-archive" * 1024
    old_bytes = b"old-complete-archive" * 1024
    source, file_hash = _source(tmp_path, new_bytes)
    raw_dir.mkdir()
    destination = raw_dir / f"{file_hash}.xlsx"
    destination.write_bytes(old_bytes)
    publication_ready = threading.Event()
    release_publication = threading.Event()
    original_replace = pipeline._replace_archive_temp

    def pause_before_replace(temp_path, dest_path):
        publication_ready.set()
        assert release_publication.wait(timeout=5)
        original_replace(temp_path, dest_path)

    monkeypatch.setattr(pipeline, "_replace_archive_temp", pause_before_replace)
    observations = []
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(pipeline._archive, str(source), file_hash)
        assert publication_ready.wait(timeout=5)
        for _ in range(20):
            observations.append(destination.read_bytes())
        release_publication.set()
        deadline = time.monotonic() + 5
        while not future.done() and time.monotonic() < deadline:
            observations.append(destination.read_bytes())
        assert future.result(timeout=5) == str(destination)
        observations.append(destination.read_bytes())

    assert set(observations) <= {old_bytes, new_bytes}
    assert old_bytes in observations
    assert new_bytes in observations
    assert hashlib.sha256(destination.read_bytes()).hexdigest() == file_hash
    assert _temp_files(raw_dir) == []


@pytest.mark.parametrize("old_destination", [False, True])
def test_archive_rejects_source_mutation_during_copy_and_preserves_destination(
    tmp_path, raw_dir, monkeypatch, old_destination
):
    source, file_hash = _source(tmp_path, b"initial-source")
    raw_dir.mkdir()
    destination = raw_dir / f"{file_hash}.xlsx"
    if old_destination:
        destination.write_bytes(b"old-corrupt")
        before = destination.stat()
    else:
        before = None
    original_copy = pipeline._copy_archive_chunks

    def mutate_then_copy(source_path, temp_file):
        source.write_bytes(b"mutated-before-copy")
        return original_copy(source_path, temp_file)

    monkeypatch.setattr(pipeline, "_copy_archive_chunks", mutate_then_copy)

    with pytest.raises(pipeline.ArchiveError, match=f"^{ARCHIVE_ERROR}$"):
        pipeline._archive(str(source), file_hash)

    if before is None:
        assert not destination.exists()
    else:
        after = destination.stat()
        assert destination.read_bytes() == b"old-corrupt"
        assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert _temp_files(raw_dir) == []


def _inventory_xlsx(tmp_path: Path, name: str, raw_id: str) -> Path:
    path = tmp_path / name
    pd.DataFrame(
        [{"产品库存ID": raw_id, "产品名称(PN)": "PN-A", "库存数量": 5, "仓库": "总仓"}]
    ).to_excel(path, index=False)
    return path


@pytest.fixture()
def import_client(db):
    db.add(
        SysUser(
            username="archive-admin",
            role="admin",
            display_name="Archive Admin",
            password_hash=hash_password("adminpw"),
        )
    )
    db.commit()
    client = TestClient(app, raise_server_exceptions=False)
    login = client.post(
        "/api/auth/login",
        json={"username": "archive-admin", "password": "adminpw"},
    )
    client.headers.update({"Authorization": f"Bearer {login.json()['token']}"})
    return client


def test_single_upload_archive_error_rolls_back_and_returns_generic_500(
    db, import_client, tmp_path, monkeypatch, caplog
):
    workbook = _inventory_xlsx(tmp_path, "upload.xlsx", "INV-ARCHIVE-FAIL")
    monkeypatch.setattr(
        pipeline,
        "_archive",
        lambda *_args: (_ for _ in ()).throw(pipeline.ArchiveError()),
    )

    with caplog.at_level("ERROR"):
        response = import_client.post(
            "/api/import/upload",
            files={"file": ("upload.xlsx", workbook.read_bytes(), XLSX_TYPE)},
        )

    assert response.status_code == 500
    assert response.json() == {"detail": INTERNAL_ERROR}
    assert "archive" in caplog.text.lower()
    db.expire_all()
    for model in (SysImportBatch, SysRawFile, Inventory, SysAuditLog):
        assert db.scalar(select(func.count()).select_from(model)) == 0


def test_import_worker_continues_after_archive_error_with_safe_note(
    db, tmp_path, raw_dir, monkeypatch
):
    failed = _inventory_xlsx(tmp_path, "archive-fail.xlsx", "INV-ARCHIVE-FAIL")
    successful = _inventory_xlsx(tmp_path, "archive-next.xlsx", "INV-ARCHIVE-NEXT")
    job = SysImportJob(
        created_by="archive-worker",
        mode="skip",
        total_files=2,
        status="processing",
    )
    db.add(job)
    db.commit()
    job_id = job.id
    original_archive = pipeline._archive
    secret_marker = f"SECRET:{tmp_path}:{pipeline.sha256_file(str(failed))}"

    def fail_first_archive(source_path, file_hash):
        if source_path == str(failed):
            raise pipeline.ArchiveError() from OSError(secret_marker)
        return original_archive(source_path, file_hash)

    monkeypatch.setattr(pipeline, "_archive", fail_first_archive)

    _process_import_job(
        job_id,
        [(str(failed), failed.name), (str(successful), successful.name)],
        "skip",
        "archive-worker",
    )

    db.expire_all()
    saved_job = db.get(SysImportJob, job_id)
    assert saved_job.status == "partial"
    assert saved_job.done_files == 1
    assert saved_job.error_files == 1
    assert saved_job.note == f"导入异常：{failed.name}（{INTERNAL_ERROR}）"
    assert secret_marker not in saved_job.note
    assert (
        db.scalar(
            select(func.count())
            .select_from(SysImportBatch)
            .where(SysImportBatch.filename == failed.name)
        )
        == 0
    )
    assert (
        db.scalar(
            select(func.count())
            .select_from(SysRawFile)
            .where(SysRawFile.filename == failed.name)
        )
        == 0
    )
    assert (
        db.scalar(
            select(func.count())
            .select_from(Inventory)
            .where(Inventory.raw_inventory_id == "INV-ARCHIVE-FAIL")
        )
        == 0
    )
    assert (
        db.scalar(
            select(func.count())
            .select_from(Inventory)
            .where(Inventory.raw_inventory_id == "INV-ARCHIVE-NEXT")
        )
        == 1
    )
    assert not failed.exists()
    assert not successful.exists()


def test_failed_archive_attempt_cannot_poison_successful_import_retry(
    db, tmp_path, raw_dir, monkeypatch
):
    workbook = _inventory_xlsx(tmp_path, "poison.xlsx", "INV-POISON")
    file_hash = pipeline.sha256_file(str(workbook))
    destination = raw_dir / f"{file_hash}.xlsx"
    original_copy = pipeline._copy_archive_chunks

    def fail_copy(_source_path, temp_file):
        temp_file.write(b"poison")
        raise OSError("injected first attempt")

    monkeypatch.setattr(pipeline, "_copy_archive_chunks", fail_copy)
    with pytest.raises(pipeline.ArchiveError):
        pipeline.run_import(db, str(workbook), "poison.xlsx")
    db.rollback()
    assert not destination.exists()
    for model in (SysImportBatch, SysRawFile, Inventory, SysAuditLog):
        assert db.scalar(select(func.count()).select_from(model)) == 0

    monkeypatch.setattr(pipeline, "_copy_archive_chunks", original_copy)
    batch = pipeline.run_import(db, str(workbook), "poison.xlsx")
    db.commit()

    raw = db.scalar(select(SysRawFile).where(SysRawFile.batch_id == batch.id))
    assert raw is not None
    assert raw.file_hash == file_hash
    assert Path(raw.storage_path).stem == file_hash
    assert pipeline.sha256_file(raw.storage_path) == file_hash
    assert _temp_files(raw_dir) == []

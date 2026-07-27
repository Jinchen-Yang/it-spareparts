"""原始底稿归档预检命令的只读健康路径。"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.db import SessionLocal
from app.models.system import SysImportBatch, SysRawFile
from scripts import raw_archive_check


_BACKEND = Path(__file__).resolve().parents[1]
_SCRIPT = _BACKEND / "scripts" / "raw_archive_check.py"
_MIB = 1024 * 1024
_LARGE_FILE_CHUNK = b"issue-145-streaming-contract\0" + b"x" * (
    _MIB - len(b"issue-145-streaming-contract\0")
)


def _fresh_error(*, dry_run: bool) -> dict:
    return {
        "complete": False,
        "dry_run": dry_run,
        "hash_mismatch": 0,
        "healthy": 0,
        "invalid_reference": 0,
        "missing": 0,
        "non_regular": 0,
        "read_error": 0,
        "references": 0,
        "sample_limit": 5,
        "samples": {
            "hash_mismatch": [],
            "invalid_reference": [],
            "missing": [],
            "non_regular": [],
            "read_error": [],
        },
        "status": "ERROR",
        "unique_files": 0,
    }


@pytest.mark.parametrize(
    ("variable", "sentinel"),
    [
        ("LLM_EXTRA_BODY", "secret-runtime-config-sentinel"),
        ("DATABASE_URL", "secret-malformed-dsn-sentinel"),
    ],
)
def test_cli_contains_runtime_startup_failures(tmp_path, variable, sentinel):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    environment = {
        **os.environ,
        "DATABASE_URL": os.environ["DATABASE_URL"],
        "RAW_FILE_DIR": str(raw_dir),
    }
    environment[variable] = sentinel

    completed = subprocess.run(
        [sys.executable, "-m", "scripts.raw_archive_check", "--dry-run"],
        cwd=_BACKEND,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert completed.returncode == 2
    assert (
        completed.stdout
        == json.dumps(_fresh_error(dry_run=True), sort_keys=True) + "\n"
    )
    assert completed.stderr == "raw archive check failed\n"
    output = completed.stdout + completed.stderr
    for confidential in (sentinel, "Traceback", "Pydantic", "SQLAlchemy"):
        assert confidential not in output


def test_cli_requires_explicit_dry_run_before_touching_archive_or_database(tmp_path):
    missing_root = tmp_path / "secret-missing-raw-root"
    database_url = (
        "postgresql+psycopg://secret-user:secret-password@127.0.0.1:1/secret-db"
        "?connect_timeout=1"
    )
    expected = _fresh_error(dry_run=False)

    completed = subprocess.run(
        [sys.executable, "-m", "scripts.raw_archive_check"],
        cwd=_BACKEND,
        env={
            **os.environ,
            "DATABASE_URL": database_url,
            "RAW_FILE_DIR": str(missing_root),
        },
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert completed.returncode == 2
    assert completed.stdout == json.dumps(expected, sort_keys=True) + "\n"
    assert completed.stderr == "raw archive check failed\n"
    assert str(missing_root) not in completed.stdout + completed.stderr
    assert database_url not in completed.stdout + completed.stderr
    assert "secret-user" not in completed.stdout + completed.stderr
    assert "secret-password" not in completed.stdout + completed.stderr
    assert "secret-db" not in completed.stdout + completed.stderr


@pytest.mark.parametrize(
    "arguments",
    [
        ["--dry-run=secret-parser-value-sentinel"],
        ["--dry-r"],
        ["--dry-run", "--dry-run"],
    ],
)
def test_cli_contains_parser_failures_without_loading_runtime(tmp_path, arguments):
    missing_root = tmp_path / "secret-parser-missing-root"
    runtime_sentinel = "secret-parser-runtime-sentinel"

    completed = subprocess.run(
        [sys.executable, "-m", "scripts.raw_archive_check", *arguments],
        cwd=_BACKEND,
        env={
            **os.environ,
            "DATABASE_URL": runtime_sentinel,
            "RAW_FILE_DIR": str(missing_root),
        },
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert completed.returncode == 2
    assert (
        completed.stdout
        == json.dumps(_fresh_error(dry_run=False), sort_keys=True) + "\n"
    )
    assert completed.stderr == "raw archive check failed\n"
    output = completed.stdout + completed.stderr
    for confidential in (
        *arguments,
        "secret-parser-value-sentinel",
        runtime_sentinel,
        str(missing_root),
        "usage",
        "Traceback",
    ):
        assert confidential not in output


def test_cli_rejects_unknown_arguments_without_leaking_parser_or_environment(tmp_path):
    unknown_option = "--secret-option"
    unknown_value = "secret-argument-sentinel"
    missing_root = tmp_path / "secret-unreachable-raw-root"
    database_url = (
        "postgresql+psycopg://secret-user:secret-password@127.0.0.1:1/secret-db"
        "?connect_timeout=1"
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.raw_archive_check",
            "--dry-run",
            unknown_option,
            unknown_value,
        ],
        cwd=_BACKEND,
        env={
            **os.environ,
            "DATABASE_URL": database_url,
            "RAW_FILE_DIR": str(missing_root),
        },
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert completed.returncode == 2
    assert (
        completed.stdout
        == json.dumps(_fresh_error(dry_run=False), sort_keys=True) + "\n"
    )
    assert completed.stderr == "raw archive check failed\n"
    output = completed.stdout + completed.stderr
    for confidential in (
        unknown_option,
        unknown_value,
        "usage",
        str(missing_root),
        database_url,
        "secret-user",
        "secret-password",
        "secret-db",
    ):
        assert confidential not in output


@pytest.mark.parametrize("failure", ["missing", "symlink", "permission", "database"])
def test_cli_dry_run_reports_fresh_error_without_leaking_failure(
    tmp_path, request, failure
):
    raw_dir = tmp_path / f"secret-{failure}-raw-root"
    database_url = os.environ["DATABASE_URL"]
    secrets = [str(raw_dir), database_url]

    if failure == "missing":
        pass
    elif failure == "symlink":
        target = tmp_path / "secret-symlink-target"
        target.mkdir()
        raw_dir.symlink_to(target, target_is_directory=True)
        secrets.append(str(target))
    elif failure == "permission":
        raw_dir.mkdir()
        request.addfinalizer(lambda: raw_dir.chmod(0o700))
        raw_dir.chmod(0)
    else:
        raw_dir.mkdir()
        database_url = (
            "postgresql+psycopg://secret-user:secret-password@127.0.0.1:1/secret-db"
            "?connect_timeout=1"
        )
        secrets.extend([database_url, "secret-user", "secret-password", "secret-db"])

    completed = subprocess.run(
        [sys.executable, "-m", "scripts.raw_archive_check", "--dry-run"],
        cwd=_BACKEND,
        env={
            **os.environ,
            "DATABASE_URL": database_url,
            "RAW_FILE_DIR": str(raw_dir),
        },
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert completed.returncode == 2
    assert (
        completed.stdout
        == json.dumps(_fresh_error(dry_run=True), sort_keys=True) + "\n"
    )
    assert completed.stderr == "raw archive check failed\n"
    for secret in secrets:
        assert secret not in completed.stdout + completed.stderr


@pytest.mark.parametrize(
    ("failure", "read_only", "isolation"),
    [
        ("read-only-off", "off", "repeatable read"),
        ("wrong-isolation", "on", "read committed"),
        ("execute-error", "on", "repeatable read"),
    ],
)
def test_main_database_failure_is_fresh_rolled_back_and_safe(
    monkeypatch, tmp_path, capsys, failure, read_only, isolation
):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    exception_secret = "postgresql+psycopg://secret-user:secret-password@db/secret-db"

    class FakeResult:
        def __init__(self, scalar=None):
            self.scalar = scalar

        def scalar_one(self):
            return self.scalar

    class FakeDb:
        def __init__(self):
            self.statements = []
            self.rolled_back = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, statement):
            sql = str(statement)
            self.statements.append(sql)
            if failure == "execute-error":
                raise RuntimeError(exception_secret)
            if sql == "SHOW transaction_read_only":
                return FakeResult(read_only)
            if sql == "SHOW transaction_isolation":
                return FakeResult(isolation)
            return FakeResult()

        def rollback(self):
            self.rolled_back = True

    fake_db = FakeDb()
    monkeypatch.setattr(
        raw_archive_check,
        "_load_runtime_dependencies",
        lambda: (
            SimpleNamespace(raw_file_dir=str(raw_dir)),
            lambda: fake_db,
            SysRawFile,
        ),
    )
    monkeypatch.setattr(sys, "argv", [str(_SCRIPT), "--dry-run"])

    assert raw_archive_check.main() == 2
    captured = capsys.readouterr()
    assert captured.out == json.dumps(_fresh_error(dry_run=True), sort_keys=True) + "\n"
    assert captured.err == "raw archive check failed\n"
    assert exception_secret not in captured.out + captured.err
    assert "secret-password" not in captured.out + captured.err
    assert fake_db.rolled_back is True
    if failure != "execute-error":
        assert all(not sql.startswith("SELECT ") for sql in fake_db.statements)


def test_main_discards_partial_result_when_scan_fails(monkeypatch, capsys):
    exception_secret = "secret partial failure with password=secret-password"

    def failing_scan(_raw_dir, result, _session_factory, _raw_file_model):
        result["references"] = 9
        result["unique_files"] = 4
        result["healthy"] = 3
        result["missing"] = 1
        result["samples"]["missing"].append({"file": 4})
        raise RuntimeError(exception_secret)

    monkeypatch.setattr(raw_archive_check, "_scan", failing_scan)
    monkeypatch.setattr(
        raw_archive_check,
        "_load_runtime_dependencies",
        lambda: (
            SimpleNamespace(raw_file_dir="secret-partial-root"),
            None,
            None,
        ),
    )
    monkeypatch.setattr(sys, "argv", [str(_SCRIPT), "--dry-run"])

    assert raw_archive_check.main() == 2
    captured = capsys.readouterr()
    assert captured.out == json.dumps(_fresh_error(dry_run=True), sort_keys=True) + "\n"
    assert captured.err == "raw archive check failed\n"
    assert exception_secret not in captured.out + captured.err
    assert "secret-partial-root" not in captured.out + captured.err


def test_main_verifies_read_only_repeatable_read_before_select(
    monkeypatch, tmp_path, capsys
):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()

    class FakeResult:
        def __init__(self, scalar=None):
            self.scalar = scalar

        def scalar_one(self):
            return self.scalar

        def __iter__(self):
            return iter(())

    class FakeDb:
        def __init__(self):
            self.statements = []
            self.rolled_back = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, statement):
            sql = str(statement)
            self.statements.append(sql)
            if sql == "SHOW transaction_read_only":
                return FakeResult("on")
            if sql == "SHOW transaction_isolation":
                return FakeResult("repeatable read")
            return FakeResult()

        def rollback(self):
            self.rolled_back = True

    fake_db = FakeDb()
    monkeypatch.setattr(
        raw_archive_check,
        "_load_runtime_dependencies",
        lambda: (
            SimpleNamespace(raw_file_dir=str(raw_dir)),
            lambda: fake_db,
            SysRawFile,
        ),
    )
    monkeypatch.setattr(sys, "argv", [str(_SCRIPT), "--dry-run"])

    assert raw_archive_check.main() == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["complete"] is True
    assert captured.err == ""
    assert fake_db.statements[:3] == [
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY",
        "SHOW transaction_read_only",
        "SHOW transaction_isolation",
    ]
    assert fake_db.statements[3].startswith("SELECT ")
    assert all(not sql.startswith("SELECT ") for sql in fake_db.statements[:3])
    assert fake_db.rolled_back is True


def test_main_finishes_database_transaction_before_checking_files(
    monkeypatch, tmp_path, capsys
):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    file_hash = hashlib.sha256(b"transaction-lifetime").hexdigest()
    events = []

    class FakeResult:
        def __init__(self, scalar=None, rows=()):
            self.scalar = scalar
            self.rows = rows

        def scalar_one(self):
            return self.scalar

        def __iter__(self):
            return iter(self.rows)

    class FakeDb:
        def __enter__(self):
            events.append("session-enter")
            return self

        def __exit__(self, *_args):
            events.append("session-exit-close")

        def execute(self, statement):
            sql = str(statement)
            if sql.startswith("SET "):
                events.append("set")
                return FakeResult()
            if sql == "SHOW transaction_read_only":
                events.append("show-read-only")
                return FakeResult("on")
            if sql == "SHOW transaction_isolation":
                events.append("show-isolation")
                return FakeResult("repeatable read")
            events.append("select")
            return FakeResult(rows=[(1, file_hash, str(raw_dir / f"{file_hash}.xlsx"))])

        def rollback(self):
            events.append("rollback")

    def recording_check(_dir_fd, checked_hash):
        assert checked_hash == file_hash
        events.append("check-file")
        return "healthy"

    monkeypatch.setattr(
        raw_archive_check,
        "_load_runtime_dependencies",
        lambda: (SimpleNamespace(raw_file_dir=str(raw_dir)), FakeDb, SysRawFile),
    )
    monkeypatch.setattr(raw_archive_check, "_check_file", recording_check)
    monkeypatch.setattr(sys, "argv", [str(_SCRIPT), "--dry-run"])

    assert raw_archive_check.main() == 0
    assert capsys.readouterr().err == ""
    assert events == [
        "session-enter",
        "set",
        "show-read-only",
        "show-isolation",
        "select",
        "rollback",
        "session-exit-close",
        "check-file",
    ]


@pytest.mark.parametrize("cleanup_failure", ["rollback", "close"])
def test_main_does_not_check_files_when_database_cleanup_fails(
    monkeypatch, tmp_path, capsys, cleanup_failure
):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    file_hash = hashlib.sha256(b"cleanup-failure").hexdigest()
    checked_hashes = []

    class FakeResult:
        def __init__(self, scalar=None, rows=()):
            self.scalar = scalar
            self.rows = rows

        def scalar_one(self):
            return self.scalar

        def __iter__(self):
            return iter(self.rows)

    class FakeDb:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            if cleanup_failure == "close":
                raise RuntimeError("secret-close-failure")

        def execute(self, statement):
            sql = str(statement)
            if sql == "SHOW transaction_read_only":
                return FakeResult("on")
            if sql == "SHOW transaction_isolation":
                return FakeResult("repeatable read")
            if sql.startswith("SELECT "):
                return FakeResult(
                    rows=[(1, file_hash, str(raw_dir / f"{file_hash}.xlsx"))]
                )
            return FakeResult()

        def rollback(self):
            if cleanup_failure == "rollback":
                raise RuntimeError("secret-rollback-failure")

    monkeypatch.setattr(
        raw_archive_check,
        "_load_runtime_dependencies",
        lambda: (SimpleNamespace(raw_file_dir=str(raw_dir)), FakeDb, SysRawFile),
    )
    monkeypatch.setattr(
        raw_archive_check,
        "_check_file",
        lambda _dir_fd, checked_hash: checked_hashes.append(checked_hash),
    )
    monkeypatch.setattr(sys, "argv", [str(_SCRIPT), "--dry-run"])

    assert raw_archive_check.main() == 2
    captured = capsys.readouterr()
    assert captured.out == json.dumps(_fresh_error(dry_run=True), sort_keys=True) + "\n"
    assert captured.err == "raw archive check failed\n"
    assert checked_hashes == []
    assert "secret" not in captured.out + captured.err


def _database_snapshot(db) -> tuple[list[tuple], list[tuple]]:
    batches = db.execute(
        select(
            SysImportBatch.id,
            SysImportBatch.filename,
            SysImportBatch.file_type,
            SysImportBatch.file_hash,
            SysImportBatch.uploaded_by,
            SysImportBatch.import_job_id,
            SysImportBatch.uploaded_at,
            SysImportBatch.rows_total,
            SysImportBatch.rows_inserted,
            SysImportBatch.rows_skipped,
            SysImportBatch.rows_error,
            SysImportBatch.rows_inactive,
            SysImportBatch.status,
            SysImportBatch.report_json,
        ).order_by(SysImportBatch.id)
    ).all()
    raw_files = db.execute(
        select(
            SysRawFile.id,
            SysRawFile.batch_id,
            SysRawFile.filename,
            SysRawFile.file_hash,
            SysRawFile.storage_path,
            SysRawFile.uploaded_at,
        ).order_by(SysRawFile.id)
    ).all()
    return ([tuple(row) for row in batches], [tuple(row) for row in raw_files])


def _metadata(path: Path) -> tuple[int, ...]:
    value = path.lstat()
    return (
        value.st_mode,
        value.st_ino,
        value.st_dev,
        value.st_nlink,
        value.st_uid,
        value.st_gid,
        value.st_size,
        value.st_atime_ns,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _metadata_without_atime(path: Path) -> tuple[int, ...]:
    value = path.lstat()
    return (
        value.st_mode,
        value.st_ino,
        value.st_dev,
        value.st_nlink,
        value.st_uid,
        value.st_gid,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _archive_snapshot(raw_dir: Path) -> tuple[tuple[int, ...], tuple[tuple, ...]]:
    paths = sorted(raw_dir.iterdir(), key=lambda item: item.name)
    entries = tuple((path.name, _metadata_without_atime(path)) for path in paths)
    return _metadata_without_atime(raw_dir), entries


def _age_file(path: Path) -> None:
    written = path.lstat()
    os.utime(
        path,
        ns=(written.st_atime_ns - 86_400_000_000_000, written.st_mtime_ns),
    )


def _add_reference(db, file_hash: str, storage_path: str, label: str) -> None:
    batch = SysImportBatch(
        filename=f"secret-{label}-source.xlsx",
        file_type="purchase",
        file_hash=file_hash,
        uploaded_by="archive-test",
        rows_total=1,
        rows_inserted=1,
        status="success",
    )
    db.add(batch)
    db.flush()
    db.add(
        SysRawFile(
            batch_id=batch.id,
            filename=batch.filename,
            file_hash=file_hash,
            storage_path=storage_path,
        )
    )
    db.commit()


def _run_archive_check(raw_config: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), "--dry-run"],
        cwd=_BACKEND,
        env={
            **os.environ,
            "DATABASE_URL": os.environ["DATABASE_URL"],
            "RAW_FILE_DIR": raw_config,
        },
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )


def _write_large_file(path: Path, size_mib: int) -> str:
    digest = hashlib.sha256()
    with path.open("wb") as output:
        for _ in range(size_mib):
            output.write(_LARGE_FILE_CHUNK)
            digest.update(_LARGE_FILE_CHUNK)
    return digest.hexdigest()


def _create_large_archive(raw_dir: Path, size_mib: int) -> tuple[Path, str]:
    staging = raw_dir / "streaming-contract-staging.xlsx"
    file_hash = _write_large_file(staging, size_mib)
    archive = raw_dir / f"{file_hash}.xlsx"
    os.replace(staging, archive)
    written = archive.lstat()
    os.utime(
        archive,
        ns=(written.st_atime_ns - 86_400_000_000_000, written.st_mtime_ns),
    )
    return archive, file_hash


def _add_raw_file_reference(db, archive: Path, file_hash: str) -> None:
    batch = SysImportBatch(
        filename="large-contract-source.xlsx",
        file_type="purchase",
        file_hash=file_hash,
        uploaded_by="archive-contract",
        rows_total=1,
        rows_inserted=1,
        status="processing",
    )
    db.add(batch)
    db.flush()
    db.add(
        SysRawFile(
            batch_id=batch.id,
            filename=batch.filename,
            file_hash=file_hash,
            storage_path=str(archive),
        )
    )
    db.commit()


def _start_archive_check(raw_dir: Path) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-m", "scripts.raw_archive_check", "--dry-run"],
        cwd=_BACKEND,
        env={
            **os.environ,
            "DATABASE_URL": os.environ["DATABASE_URL"],
            "RAW_FILE_DIR": str(raw_dir),
        },
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)


def _wait_until_process_opens(
    process: subprocess.Popen[str], archive: Path, deadline: float
) -> None:
    fd_dir = Path(f"/proc/{process.pid}/fd")
    expected = str(archive)
    while time.monotonic() < deadline:
        try:
            for fd_path in fd_dir.iterdir():
                try:
                    if os.readlink(fd_path) == expected:
                        return
                except OSError:
                    continue
        except FileNotFoundError:
            break
        if process.poll() is not None:
            break
        time.sleep(0.001)
    pytest.fail("CLI did not expose the target archive fd before the timeout")


@pytest.mark.parametrize("race", ["atomic_replace", "metadata_change"])
def test_check_file_fails_closed_when_archive_changes_after_open(
    monkeypatch, tmp_path, race
):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    content = b"archive mutation boundary"
    file_hash = hashlib.sha256(content).hexdigest()
    archive = raw_dir / f"{file_hash}.xlsx"
    archive.write_bytes(content)
    replacement = raw_dir / "precreated-replacement.xlsx"
    if race == "atomic_replace":
        replacement.write_bytes(content)

    real_close = os.close
    real_fstat = os.fstat
    real_open = os.open
    real_read = os.read
    real_stat = os.stat
    original = real_stat(archive, follow_symlinks=False)
    directory_flags = (
        os.O_RDONLY
        | raw_archive_check._required_flag("O_DIRECTORY")
        | raw_archive_check._required_flag("O_NOFOLLOW")
        | raw_archive_check._required_flag("O_CLOEXEC")
    )
    dir_fd = real_open(raw_dir, directory_flags)
    mutation_happened = False
    target_reads = 0

    def mutate_on_first_archive_read(fd, size):
        nonlocal mutation_happened, target_reads
        opened = real_fstat(fd)
        if (opened.st_dev, opened.st_ino) == (original.st_dev, original.st_ino):
            target_reads += 1
            if target_reads == 1:
                if race == "atomic_replace":
                    replacement_inode = real_stat(
                        replacement, follow_symlinks=False
                    ).st_ino
                    assert replacement_inode != original.st_ino
                    os.replace(replacement, archive)
                    mutation_happened = (
                        real_stat(archive, follow_symlinks=False).st_ino
                        == replacement_inode
                    )
                else:
                    os.utime(
                        archive,
                        ns=(
                            original.st_atime_ns,
                            original.st_mtime_ns + 1_000_000_000,
                        ),
                    )
                    changed = real_stat(archive, follow_symlinks=False)
                    mutation_happened = (
                        changed.st_ino == original.st_ino
                        and changed.st_mtime_ns != original.st_mtime_ns
                    )
        return real_read(fd, size)

    monkeypatch.setattr(raw_archive_check.os, "read", mutate_on_first_archive_read)
    try:
        outcome = raw_archive_check._check_file(dir_fd, file_hash)
    finally:
        real_close(dir_fd)

    assert target_reads >= 1
    assert mutation_happened is True
    assert outcome == "read_error"
    with pytest.raises(OSError) as exc_info:
        real_fstat(dir_fd)
    assert exc_info.value.errno == errno.EBADF


def test_cli_errors_when_archive_directory_is_replaced_after_file_open(db, tmp_path):
    raw_dir = tmp_path / "secret-original-raw"
    moved_dir = tmp_path / "secret-moved-raw"
    raw_dir.mkdir()
    archive, file_hash = _create_large_archive(raw_dir, 128)
    _add_raw_file_reference(db, archive, file_hash)
    original_directory = raw_dir.stat()

    process = _start_archive_check(raw_dir)
    deadline = time.monotonic() + 5
    try:
        _wait_until_process_opens(process, archive, deadline)
        raw_dir.rename(moved_dir)
        raw_dir.mkdir()
        replacement_directory = raw_dir.stat()
        assert (replacement_directory.st_dev, replacement_directory.st_ino) != (
            original_directory.st_dev,
            original_directory.st_ino,
        )

        stdout, stderr = process.communicate(
            timeout=max(0.1, deadline - time.monotonic())
        )
    finally:
        _stop_process(process)

    assert process.returncode == 2
    assert stdout == json.dumps(_fresh_error(dry_run=True), sort_keys=True) + "\n"
    assert stderr == "raw archive check failed\n"
    output = stdout + stderr
    for secret in (raw_dir, archive, moved_dir, moved_dir / archive.name, file_hash):
        assert str(secret) not in output


def test_cli_streams_large_healthy_archive_below_memory_limit(
    db, tmp_path, record_property
):
    started = time.monotonic()
    deadline = started + 5
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    size_mib = 256
    archive, file_hash = _create_large_archive(raw_dir, size_mib)
    _add_raw_file_reference(db, archive, file_hash)
    metadata_before = _metadata(archive)
    peak_kib = 0

    process = _start_archive_check(raw_dir)
    status_path = Path(f"/proc/{process.pid}/status")
    try:
        while process.poll() is None and time.monotonic() < deadline:
            try:
                for line in status_path.read_text().splitlines():
                    if line.startswith("VmRSS:"):
                        peak_kib = max(peak_kib, int(line.split()[1]))
                        break
            except FileNotFoundError:
                pass
            time.sleep(0.001)
        if process.poll() is None:
            raise subprocess.TimeoutExpired(process.args, 5)
        stdout, stderr = process.communicate(
            timeout=max(0.1, deadline - time.monotonic())
        )
    finally:
        _stop_process(process)

    record_property("archive_size_mib", size_mib)
    record_property("peak_vmrss_kib", peak_kib)
    assert time.monotonic() < deadline
    assert process.returncode == 0, stderr
    assert stderr == ""
    payload = json.loads(stdout)
    assert {
        "complete": True,
        "status": "PASS",
        "healthy": 1,
        "read_error": 0,
    }.items() <= payload.items()
    assert _metadata(archive) == metadata_before
    assert peak_kib < 192 * 1024, f"file={size_mib} MiB, peak={peak_kib} KiB"


@pytest.mark.parametrize("failure", ["healthy", "read_error", "database_error"])
def test_main_closes_archive_fds_on_success_and_failure(
    monkeypatch, tmp_path, capsys, failure
):
    raw_dir = tmp_path / "secret-fd-raw"
    raw_dir.mkdir()
    content = b"secret-fd-archive-content"
    file_hash = hashlib.sha256(content).hexdigest()
    archive = raw_dir / f"{file_hash}.xlsx"
    archive.write_bytes(content)
    secret = f"secret-{failure}-exception"
    opened_fds = []
    file_fds = set()
    real_open = os.open
    real_read = os.read

    class FakeResult:
        def __init__(self, scalar=None, rows=()):
            self.scalar = scalar
            self.rows = rows

        def scalar_one(self):
            return self.scalar

        def __iter__(self):
            return iter(self.rows)

    class FakeDb:
        def __init__(self):
            self.rolled_back = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, statement):
            if failure == "database_error":
                raise RuntimeError(secret)
            sql = str(statement)
            if sql == "SHOW transaction_read_only":
                return FakeResult("on")
            if sql == "SHOW transaction_isolation":
                return FakeResult("repeatable read")
            if sql.startswith("SELECT "):
                return FakeResult(rows=[(1, file_hash, str(archive))])
            return FakeResult()

        def rollback(self):
            self.rolled_back = True

    def recording_open(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if os.fspath(path) in {os.fspath(raw_dir), archive.name}:
            opened_fds.append(fd)
            if os.fspath(path) == archive.name:
                file_fds.add(fd)
        return fd

    def injected_read(fd, size):
        if failure == "read_error" and fd in file_fds:
            raise OSError(errno.EIO, secret)
        return real_read(fd, size)

    fake_db = FakeDb()
    monkeypatch.setattr(
        raw_archive_check,
        "_load_runtime_dependencies",
        lambda: (
            SimpleNamespace(raw_file_dir=str(raw_dir)),
            lambda: fake_db,
            SysRawFile,
        ),
    )
    monkeypatch.setattr(raw_archive_check.os, "open", recording_open)
    monkeypatch.setattr(raw_archive_check.os, "read", injected_read)
    monkeypatch.setattr(sys, "argv", [str(_SCRIPT), "--dry-run"])

    exit_code = raw_archive_check.main()
    captured = capsys.readouterr()

    expected = _fresh_error(dry_run=True)
    if failure == "healthy":
        expected.update(
            complete=True,
            status="PASS",
            references=1,
            unique_files=1,
            healthy=1,
        )
        assert exit_code == 0
        assert captured.err == ""
        assert len(opened_fds) == 2
    elif failure == "read_error":
        expected.update(
            complete=True,
            status="FAIL",
            references=1,
            unique_files=1,
            read_error=1,
        )
        expected["samples"]["read_error"] = [{"file": 1}]
        assert exit_code == 1
        assert captured.err == ""
        assert len(opened_fds) == 2
    else:
        assert exit_code == 2
        assert captured.err == "raw archive check failed\n"
        assert len(opened_fds) == 1

    assert captured.out == json.dumps(expected, sort_keys=True) + "\n"
    assert fake_db.rolled_back is True
    output = captured.out + captured.err
    for confidential in (secret, str(raw_dir), str(archive), file_hash):
        assert confidential not in output
    for fd in opened_fds:
        with pytest.raises(OSError) as exc_info:
            os.fstat(fd)
        assert exc_info.value.errno == errno.EBADF


def test_main_errors_when_noatime_secure_open_is_unavailable(
    monkeypatch, tmp_path, capsys
):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    content = b"must-not-be-read-without-noatime"
    file_hash = hashlib.sha256(content).hexdigest()
    archive = raw_dir / f"{file_hash}.xlsx"
    archive.write_bytes(content)
    written = archive.lstat()
    os.utime(
        archive,
        ns=(written.st_atime_ns - 86_400_000_000_000, written.st_mtime_ns),
    )
    metadata_before = _metadata(archive)

    class FakeResult:
        def __init__(self, scalar=None, rows=()):
            self.scalar = scalar
            self.rows = rows

        def scalar_one(self):
            return self.scalar

        def __iter__(self):
            return iter(self.rows)

    class FakeDb:
        def __init__(self):
            self.rolled_back = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, statement):
            sql = str(statement)
            if sql == "SHOW transaction_read_only":
                return FakeResult("on")
            if sql == "SHOW transaction_isolation":
                return FakeResult("repeatable read")
            if sql.startswith("SELECT "):
                return FakeResult(rows=[(1, file_hash, str(archive))])
            return FakeResult()

        def rollback(self):
            self.rolled_back = True

    fake_db = FakeDb()
    monkeypatch.setattr(
        raw_archive_check,
        "_load_runtime_dependencies",
        lambda: (
            SimpleNamespace(raw_file_dir=str(raw_dir)),
            lambda: fake_db,
            SysRawFile,
        ),
    )
    monkeypatch.delattr(raw_archive_check.os, "O_NOATIME")
    monkeypatch.setattr(sys, "argv", [str(_SCRIPT), "--dry-run"])

    assert raw_archive_check.main() == 2
    captured = capsys.readouterr()
    assert captured.out == json.dumps(_fresh_error(dry_run=True), sort_keys=True) + "\n"
    assert captured.err == "raw archive check failed\n"
    assert _metadata(archive) == metadata_before
    assert fake_db.rolled_back is True


def test_cli_dry_run_reports_a_healthy_archive_without_mutation(db, tmp_path):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    content = b"issue-145 healthy archive"
    file_hash = hashlib.sha256(content).hexdigest()
    archive = raw_dir / f"{file_hash}.xlsx"
    archive.write_bytes(content)
    written = archive.lstat()
    old_atime_ns = written.st_atime_ns - 86_400_000_000_000
    os.utime(archive, ns=(old_atime_ns, written.st_mtime_ns))

    batch = SysImportBatch(
        filename="confidential-source.xlsx",
        file_type="purchase",
        file_hash=file_hash,
        uploaded_by="archive-test",
        rows_total=1,
        rows_inserted=1,
        status="success",
        report_json={"result": "imported"},
    )
    db.add(batch)
    db.flush()
    db.add(
        SysRawFile(
            batch_id=batch.id,
            filename=batch.filename,
            file_hash=file_hash,
            storage_path=str(archive),
        )
    )
    db.commit()

    database_before = _database_snapshot(db)
    archive_before = _metadata(archive)
    directory_before = _archive_snapshot(raw_dir)
    completed = subprocess.run(
        [sys.executable, str(_SCRIPT), "--dry-run"],
        cwd=_BACKEND,
        env={
            **os.environ,
            "DATABASE_URL": os.environ["DATABASE_URL"],
            "RAW_FILE_DIR": str(raw_dir),
        },
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )

    db.expire_all()
    assert _database_snapshot(db) == database_before
    archive_after = _metadata(archive)
    assert archive_after[7] == archive_before[7]
    assert archive_after == archive_before
    assert archive.read_bytes() == content
    assert _archive_snapshot(raw_dir) == directory_before
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""

    payload = json.loads(completed.stdout)
    assert (
        completed.stdout
        == json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
    )
    assert {
        "dry_run": True,
        "complete": True,
        "status": "PASS",
        "references": 1,
        "unique_files": 1,
        "healthy": 1,
        "missing": 0,
        "hash_mismatch": 0,
        "non_regular": 0,
        "invalid_reference": 0,
        "read_error": 0,
    }.items() <= payload.items()
    assert str(raw_dir) not in completed.stdout
    assert os.environ["DATABASE_URL"] not in completed.stdout
    assert batch.filename not in completed.stdout
    assert content.decode() not in completed.stdout


def test_cli_rejects_lexical_alias_for_canonical_archive(db, tmp_path):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    content = b"secret lexical alias archive content"
    file_hash = hashlib.sha256(content).hexdigest()
    archive = raw_dir / f"{file_hash}.xlsx"
    archive.write_bytes(content)
    written = archive.lstat()
    os.utime(
        archive,
        ns=(written.st_atime_ns - 86_400_000_000_000, written.st_mtime_ns),
    )

    batch = SysImportBatch(
        filename="secret-lexical-alias-source.xlsx",
        file_type="purchase",
        file_hash=file_hash,
        uploaded_by="archive-test",
        rows_total=1,
        rows_inserted=1,
        status="success",
    )
    db.add(batch)
    db.flush()
    storage_path = os.path.join(str(raw_dir), "..", raw_dir.name, f"{file_hash}.xlsx")
    db.add(
        SysRawFile(
            batch_id=batch.id,
            filename=batch.filename,
            file_hash=file_hash,
            storage_path=storage_path,
        )
    )
    db.commit()

    database_before = _database_snapshot(db)
    archive_before = _metadata(archive)
    completed = subprocess.run(
        [sys.executable, str(_SCRIPT), "--dry-run"],
        cwd=_BACKEND,
        env={
            **os.environ,
            "DATABASE_URL": os.environ["DATABASE_URL"],
            "RAW_FILE_DIR": str(raw_dir),
        },
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )

    db.expire_all()
    assert _database_snapshot(db) == database_before
    assert _metadata(archive) == archive_before
    assert completed.returncode == 1
    assert completed.stderr == ""
    payload = json.loads(completed.stdout)
    assert {
        "complete": True,
        "status": "FAIL",
        "references": 1,
        "unique_files": 0,
        "healthy": 0,
        "invalid_reference": 1,
    }.items() <= payload.items()
    assert payload["samples"]["invalid_reference"] == [{"reference": 1}]
    output = completed.stdout + completed.stderr
    for confidential in (str(raw_dir), storage_path, file_hash, content.decode()):
        assert confidential not in output


@pytest.mark.parametrize(
    "reference_kind",
    [
        "same-prefix-sibling",
        "absolute-config-relative-storage",
        "relative-config-absolute-storage",
        "parent-alias",
    ],
)
def test_cli_rejects_noncanonical_storage_without_touching_target(
    db, tmp_path, reference_kind
):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    content = f"secret canonical {reference_kind}".encode()
    file_hash = hashlib.sha256(content).hexdigest()
    archive = raw_dir / f"{file_hash}.xlsx"
    archive.write_bytes(content)
    _age_file(archive)

    if reference_kind == "same-prefix-sibling":
        external_dir = tmp_path / "raw-sibling"
        raw_config = str(raw_dir)
        storage_path = str(external_dir / archive.name)
    elif reference_kind == "absolute-config-relative-storage":
        external_dir = tmp_path / "relative-storage-target"
        raw_config = str(raw_dir)
        storage_path = os.path.relpath(external_dir / archive.name, _BACKEND)
    elif reference_kind == "relative-config-absolute-storage":
        external_dir = tmp_path / "absolute-storage-target"
        raw_config = os.path.relpath(raw_dir, _BACKEND)
        storage_path = str(external_dir / archive.name)
    else:
        external_dir = tmp_path / "parent-alias-target"
        raw_config = str(raw_dir)
        storage_path = os.path.join(str(raw_dir), "..", external_dir.name, archive.name)

    external_dir.mkdir()
    canary = external_dir / archive.name
    canary_content = f"secret external canary {reference_kind}".encode()
    canary.write_bytes(canary_content)
    _age_file(canary)
    _add_reference(db, file_hash, storage_path, reference_kind)
    database_before = _database_snapshot(db)
    archive_before = _metadata(archive)
    canary_before = _metadata(canary)

    completed = _run_archive_check(raw_config)

    db.expire_all()
    assert _database_snapshot(db) == database_before
    assert _metadata(archive) == archive_before
    assert _metadata(canary) == canary_before
    assert completed.returncode == 1
    assert completed.stderr == ""
    payload = json.loads(completed.stdout)
    assert {
        "complete": True,
        "status": "FAIL",
        "references": 1,
        "unique_files": 0,
        "healthy": 0,
        "invalid_reference": 1,
    }.items() <= payload.items()
    assert payload["samples"]["invalid_reference"] == [{"reference": 1}]
    output = completed.stdout + completed.stderr
    for confidential in (
        raw_config,
        storage_path,
        str(canary),
        file_hash,
        content.decode(),
        canary_content.decode(),
    ):
        assert confidential not in output


def test_main_rejects_nul_storage_without_using_it_as_a_path(
    monkeypatch, tmp_path, capsys
):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    canary = tmp_path / "secret-nul-canary.xlsx"
    canary.write_bytes(b"secret nul canary content")
    _age_file(canary)
    canary_before = _metadata(canary)
    file_hash = hashlib.sha256(b"secret nul expected content").hexdigest()
    storage_path = f"{canary}\0secret-nul-suffix"

    class FakeResult:
        def __init__(self, scalar=None, rows=()):
            self.scalar = scalar
            self.rows = rows

        def scalar_one(self):
            return self.scalar

        def __iter__(self):
            return iter(self.rows)

    class FakeDb:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, statement):
            sql = str(statement)
            if sql == "SHOW transaction_read_only":
                return FakeResult("on")
            if sql == "SHOW transaction_isolation":
                return FakeResult("repeatable read")
            if sql.startswith("SELECT "):
                return FakeResult(rows=[(1, file_hash, storage_path)])
            return FakeResult()

        def rollback(self):
            return None

    real_stat = os.stat
    real_open = os.open

    def guarded_stat(path, *args, **kwargs):
        assert path != storage_path
        return real_stat(path, *args, **kwargs)

    def guarded_open(path, *args, **kwargs):
        assert path != storage_path
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(
        raw_archive_check,
        "_load_runtime_dependencies",
        lambda: (
            SimpleNamespace(raw_file_dir=str(raw_dir)),
            FakeDb,
            SysRawFile,
        ),
    )
    monkeypatch.setattr(raw_archive_check.os, "stat", guarded_stat)
    monkeypatch.setattr(raw_archive_check.os, "open", guarded_open)
    monkeypatch.setattr(sys, "argv", [str(_SCRIPT), "--dry-run"])

    assert raw_archive_check.main() == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert {
        "complete": True,
        "status": "FAIL",
        "references": 1,
        "unique_files": 0,
        "healthy": 0,
        "invalid_reference": 1,
    }.items() <= payload.items()
    assert payload["samples"]["invalid_reference"] == [{"reference": 1}]
    assert captured.err == ""
    assert _metadata(canary) == canary_before
    output = captured.out + captured.err
    for confidential in (
        str(canary),
        storage_path,
        file_hash,
        "secret-nul-suffix",
        "\\u0000",
        "secret nul canary content",
    ):
        assert confidential not in output


@pytest.mark.parametrize("config_kind", ["dot-component", "relative-parent"])
def test_cli_accepts_storage_path_returned_by_archive_pipeline(
    db, tmp_path, config_kind
):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    if config_kind == "dot-component":
        raw_config = os.path.join(str(tmp_path), ".", raw_dir.name)
        assert "/./" in raw_config
    else:
        raw_config = os.path.relpath(raw_dir, _BACKEND)
        assert raw_config.startswith(".." + os.sep)

    content = f"secret compatible {config_kind}".encode()
    file_hash = hashlib.sha256(content).hexdigest()
    archive = raw_dir / f"{file_hash}.xlsx"
    archive.write_bytes(content)
    _age_file(archive)
    archive_before = _metadata(archive)
    storage_path = os.path.join(raw_config, archive.name)
    _add_reference(db, file_hash, storage_path, config_kind)
    database_before = _database_snapshot(db)

    completed = _run_archive_check(raw_config)

    db.expire_all()
    assert _database_snapshot(db) == database_before
    assert _metadata(archive) == archive_before
    assert completed.returncode == 0
    assert completed.stderr == ""
    payload = json.loads(completed.stdout)
    assert {
        "complete": True,
        "status": "PASS",
        "references": 1,
        "unique_files": 1,
        "healthy": 1,
        "invalid_reference": 0,
    }.items() <= payload.items()
    output = completed.stdout + completed.stderr
    for confidential in (raw_config, storage_path, file_hash, content.decode()):
        assert confidential not in output


def test_cli_validates_every_reference_before_deduplicating_with_bounded_samples(
    db, tmp_path
):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    content = b"secret canonical archive content"
    canonical_hash = hashlib.sha256(content).hexdigest()
    other_hash = hashlib.sha256(b"secret other reference").hexdigest()
    third_hash = hashlib.sha256(b"secret third reference").hexdigest()
    archive = raw_dir / f"{canonical_hash}.xlsx"
    archive.write_bytes(content)
    written = archive.lstat()
    os.utime(
        archive,
        ns=(written.st_atime_ns - 86_400_000_000_000, written.st_mtime_ns),
    )

    batch = SysImportBatch(
        filename="secret-batch-source.xlsx",
        file_type="purchase",
        file_hash=canonical_hash,
        uploaded_by="secret-archive-test-user",
        rows_total=8,
        rows_inserted=8,
        status="processing",
        report_json={"exception": "secret raw exception text"},
    )
    db.add(batch)
    db.flush()
    outside_paths = [
        tmp_path / "secret-duplicate-outside.xlsx",
        tmp_path / "secret-other-outside.xlsx",
    ]
    references = [
        (canonical_hash, str(archive)),
        (canonical_hash, str(archive)),
        (canonical_hash, str(outside_paths[0])),
        (canonical_hash.upper(), str(raw_dir / f"{canonical_hash.upper()}.xlsx")),
        (None, str(raw_dir / "secret-none-hash.xlsx")),
        ("a" * 63, str(raw_dir / f"{'a' * 63}.xlsx")),
        (other_hash, str(outside_paths[1])),
        (third_hash, None),
    ]
    filenames = []
    for reference, (file_hash, storage_path) in enumerate(references, start=1):
        filename = f"secret-reference-{reference}.xlsx"
        filenames.append(filename)
        db.add(
            SysRawFile(
                batch_id=batch.id,
                filename=filename,
                file_hash=file_hash,
                storage_path=storage_path,
            )
        )
    db.commit()

    database_before = _database_snapshot(db)
    archive_before = _metadata(archive)
    directory_before = _metadata(raw_dir)
    environment = {
        **os.environ,
        "DATABASE_URL": os.environ["DATABASE_URL"],
        "RAW_FILE_DIR": str(raw_dir),
    }
    completed_runs = [
        subprocess.run(
            [sys.executable, str(_SCRIPT), "--dry-run"],
            cwd=_BACKEND,
            env=environment,
            capture_output=True,
            check=False,
            timeout=5,
        )
        for _ in range(2)
    ]

    db.expire_all()
    assert _database_snapshot(db) == database_before
    assert _metadata(archive) == archive_before
    assert _metadata(raw_dir) == directory_before
    assert completed_runs[0].stdout == completed_runs[1].stdout
    assert all(completed.returncode == 1 for completed in completed_runs)
    assert all(completed.stderr == b"" for completed in completed_runs)

    payload = json.loads(completed_runs[0].stdout)
    assert {
        "dry_run": True,
        "complete": True,
        "status": "FAIL",
        "references": 8,
        "unique_files": 1,
        "healthy": 1,
        "missing": 0,
        "hash_mismatch": 0,
        "non_regular": 0,
        "invalid_reference": 6,
        "read_error": 0,
        "sample_limit": 5,
    }.items() <= payload.items()
    assert payload["samples"] == {
        "missing": [],
        "hash_mismatch": [],
        "non_regular": [],
        "invalid_reference": [
            {"reference": 3},
            {"reference": 4},
            {"reference": 5},
            {"reference": 6},
            {"reference": 7},
        ],
        "read_error": [],
    }
    output = completed_runs[0].stdout.decode()
    for file_hash in {canonical_hash, other_hash, third_hash, canonical_hash.upper()}:
        assert file_hash not in output
        assert file_hash[:12] not in output
    for storage_path in [str(archive), *(str(path) for path in outside_paths)]:
        assert storage_path not in output
    for filename in [batch.filename, *filenames]:
        assert filename not in output
    assert content.decode() not in output
    assert "secret raw exception text" not in output


@pytest.mark.parametrize(
    ("anomaly", "classification"),
    [
        ("missing", "missing"),
        ("hash_mismatch", "hash_mismatch"),
        ("symlink", "non_regular"),
        ("directory", "non_regular"),
        ("fifo", "non_regular"),
        ("permission_denied", "read_error"),
    ],
)
def test_cli_dry_run_classifies_file_anomaly_with_safe_sample(
    db, tmp_path, request, anomaly, classification
):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    file_hash = hashlib.sha256(f"expected-{anomaly}".encode()).hexdigest()
    archive = raw_dir / f"{file_hash}.xlsx"
    secret = f"secret-{anomaly}-archive-content"

    if anomaly == "hash_mismatch":
        archive.write_text(secret)
    elif anomaly == "symlink":
        target = tmp_path / "symlink-secret-target.xlsx"
        target.write_text(secret)
        archive.symlink_to(target)
    elif anomaly == "directory":
        archive.mkdir()
    elif anomaly == "fifo":
        os.mkfifo(archive)
    elif anomaly == "permission_denied":
        archive.write_text(secret)
        request.addfinalizer(lambda: archive.chmod(0o600))
        archive.chmod(0)

    batch = SysImportBatch(
        filename=f"{anomaly}-secret-source.xlsx",
        file_type="purchase",
        file_hash=file_hash,
        uploaded_by="archive-test",
        rows_total=1,
        rows_inserted=1,
        status="success",
        report_json={"result": "imported"},
    )
    db.add(batch)
    db.flush()
    db.add(
        SysRawFile(
            batch_id=batch.id,
            filename=batch.filename,
            file_hash=file_hash,
            storage_path=str(archive),
        )
    )
    db.commit()

    completed = subprocess.run(
        [sys.executable, str(_SCRIPT), "--dry-run"],
        cwd=_BACKEND,
        env={
            **os.environ,
            "DATABASE_URL": os.environ["DATABASE_URL"],
            "RAW_FILE_DIR": str(raw_dir),
        },
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert completed.returncode == 1
    assert completed.stderr == ""
    payload = json.loads(completed.stdout)
    assert payload["status"] == "FAIL"
    assert payload["complete"] is True
    assert payload["healthy"] == 0
    for count_key in (
        "missing",
        "hash_mismatch",
        "non_regular",
        "invalid_reference",
        "read_error",
    ):
        assert payload[count_key] == (1 if count_key == classification else 0)
    assert payload["sample_limit"] == 5
    expected_samples = {
        "missing": [],
        "hash_mismatch": [],
        "non_regular": [],
        "invalid_reference": [],
        "read_error": [],
    }
    expected_samples[classification] = [{"file": 1}]
    assert payload["samples"] == expected_samples
    assert file_hash not in completed.stdout
    assert file_hash[:12] not in completed.stdout
    assert batch.filename not in completed.stdout
    assert str(archive) not in completed.stdout
    if anomaly in {"hash_mismatch", "symlink"}:
        assert secret not in completed.stdout
        assert secret not in completed.stderr


def test_main_real_postgresql_transaction_rejects_writes_and_recovers(
    db, monkeypatch, tmp_path, capsys
):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    database_before = _database_snapshot(db)
    events = []
    rejected_sqlstates = []

    class ReadOnlyProbeSession:
        def __init__(self):
            self.session = SessionLocal()

        def __enter__(self):
            self.session.__enter__()
            events.append("session-enter")
            return self

        def __exit__(self, *args):
            try:
                return self.session.__exit__(*args)
            finally:
                events.append("session-exit-close")

        def execute(self, statement, *args, **kwargs):
            result = self.session.execute(statement, *args, **kwargs)
            if str(statement).startswith("SET TRANSACTION "):
                self.session.execute(text("SAVEPOINT archive_read_only_probe"))
                try:
                    self.session.execute(
                        text(
                            "INSERT INTO sys_raw_file "
                            "(batch_id, filename, file_hash, storage_path) VALUES "
                            "(0, 'archive-contract.xlsx', "
                            "'0000000000000000000000000000000000000000000000000000000000000000', "
                            "'archive-contract.xlsx')"
                        )
                    )
                except DBAPIError as exc:
                    rejected_sqlstates.append(exc.orig.sqlstate)
                    self.session.execute(
                        text("ROLLBACK TO SAVEPOINT archive_read_only_probe")
                    )
                    self.session.execute(
                        text("RELEASE SAVEPOINT archive_read_only_probe")
                    )
                else:
                    pytest.fail("real PostgreSQL read-only transaction accepted INSERT")
            return result

        def rollback(self):
            events.append("rollback")
            self.session.rollback()

    monkeypatch.setattr(
        raw_archive_check,
        "_load_runtime_dependencies",
        lambda: (
            SimpleNamespace(raw_file_dir=str(raw_dir)),
            ReadOnlyProbeSession,
            SysRawFile,
        ),
    )
    monkeypatch.setattr(sys, "argv", [str(_SCRIPT), "--dry-run"])

    assert raw_archive_check.main() == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == ""
    assert {
        "complete": True,
        "status": "PASS",
        "references": 0,
        "unique_files": 0,
        "healthy": 0,
    }.items() <= payload.items()
    assert rejected_sqlstates == ["25006"]
    assert events == ["session-enter", "rollback", "session-exit-close"]
    db.expire_all()
    assert _database_snapshot(db) == database_before


@pytest.mark.parametrize(
    ("replacement_kind", "expected_outcome"),
    [
        ("regular_same_content_new_inode", "read_error"),
        ("symlink", "read_error"),
        ("fifo", "non_regular"),
    ],
)
def test_main_fails_closed_when_archive_is_replaced_between_lstat_and_open(
    monkeypatch, tmp_path, capsys, replacement_kind, expected_outcome
):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    content = b"safe archive replacement contract"
    file_hash = hashlib.sha256(content).hexdigest()
    archive = raw_dir / f"{file_hash}.xlsx"
    archive.write_bytes(content)
    _age_file(archive)
    old_inode = archive.lstat().st_ino
    staged = raw_dir / "safe-staged-replacement.xlsx"
    canary = tmp_path / "secret-symlink-canary.xlsx"
    canary_content = b"secret-symlink-canary-content"
    canary_before = None
    if replacement_kind == "regular_same_content_new_inode":
        staged.write_bytes(content)
        assert staged.lstat().st_ino != old_inode
    elif replacement_kind == "symlink":
        canary.write_bytes(canary_content)
        _age_file(canary)
        canary_before = _metadata(canary)

    class FakeResult:
        def __init__(self, scalar=None, rows=()):
            self.scalar = scalar
            self.rows = rows

        def scalar_one(self):
            return self.scalar

        def __iter__(self):
            return iter(self.rows)

    class FakeDb:
        def __init__(self):
            self.rolled_back = False
            self.closed = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.closed = True

        def execute(self, statement):
            sql = str(statement)
            if sql == "SHOW transaction_read_only":
                return FakeResult("on")
            if sql == "SHOW transaction_isolation":
                return FakeResult("repeatable read")
            if sql.startswith("SELECT "):
                return FakeResult(rows=[(1, file_hash, str(archive))])
            return FakeResult()

        def rollback(self):
            self.rolled_back = True

    real_stat = os.stat
    real_open = os.open
    replacement_done = False
    opened_file_fds = []

    def replacing_stat(path, *args, **kwargs):
        nonlocal replacement_done
        value = real_stat(path, *args, **kwargs)
        if (
            not replacement_done
            and os.fspath(path) == archive.name
            and kwargs.get("follow_symlinks") is False
        ):
            replacement_done = True
            if replacement_kind == "regular_same_content_new_inode":
                os.replace(staged, archive)
            elif replacement_kind == "symlink":
                archive.unlink()
                archive.symlink_to(canary)
            else:
                archive.unlink()
                os.mkfifo(archive)
        return value

    def recording_open(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if os.fspath(path) == archive.name:
            assert flags & os.O_NONBLOCK
            opened_file_fds.append(fd)
        return fd

    fake_db = FakeDb()
    monkeypatch.setattr(
        raw_archive_check,
        "_load_runtime_dependencies",
        lambda: (
            SimpleNamespace(raw_file_dir=str(raw_dir)),
            lambda: fake_db,
            SysRawFile,
        ),
    )
    monkeypatch.setattr(raw_archive_check.os, "stat", replacing_stat)
    monkeypatch.setattr(raw_archive_check.os, "open", recording_open)
    monkeypatch.setattr(sys, "argv", [str(_SCRIPT), "--dry-run"])

    assert raw_archive_check.main() == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == ""
    assert {
        "complete": True,
        "status": "FAIL",
        "references": 1,
        "unique_files": 1,
        "healthy": 0,
        expected_outcome: 1,
    }.items() <= payload.items()
    assert payload["samples"][expected_outcome] == [{"file": 1}]
    assert replacement_done is True
    if replacement_kind == "regular_same_content_new_inode":
        assert archive.lstat().st_ino != old_inode
    elif replacement_kind == "symlink":
        assert stat.S_ISLNK(archive.lstat().st_mode)
    else:
        assert stat.S_ISFIFO(archive.lstat().st_mode)
    assert fake_db.rolled_back is True
    assert fake_db.closed is True
    if replacement_kind == "symlink":
        assert _metadata(canary) == canary_before
    output = captured.out + captured.err
    for confidential in (
        str(raw_dir),
        str(archive),
        file_hash,
        canary_content.decode(),
    ):
        assert confidential not in output
    for fd in opened_file_fds:
        with pytest.raises(OSError) as exc_info:
            os.fstat(fd)
        assert exc_info.value.errno == errno.EBADF


def test_main_deduplicates_hashes_across_streamed_database_batches(
    monkeypatch, tmp_path, capsys
):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    file_hash = hashlib.sha256(b"cross-batch duplicate contract").hexdigest()
    archive = raw_dir / f"{file_hash}.xlsx"
    events = []
    selected_statement = None

    class FakeResult:
        def __init__(self, scalar=None):
            self.scalar = scalar

        def scalar_one(self):
            return self.scalar

        def __iter__(self):
            return (
                (reference, file_hash, str(archive)) for reference in range(1, 1506)
            )

    class FakeDb:
        def __enter__(self):
            events.append("session-enter")
            return self

        def __exit__(self, *_args):
            events.append("session-exit-close")

        def execute(self, statement):
            nonlocal selected_statement
            sql = str(statement)
            if sql == "SHOW transaction_read_only":
                return FakeResult("on")
            if sql == "SHOW transaction_isolation":
                return FakeResult("repeatable read")
            if sql.startswith("SELECT "):
                selected_statement = statement
                events.append("select")
            return FakeResult()

        def rollback(self):
            events.append("rollback")

    checked_hashes = []

    def recording_check(_dir_fd, checked_hash):
        events.append("check-file")
        checked_hashes.append(checked_hash)
        return "healthy"

    monkeypatch.setattr(
        raw_archive_check,
        "_load_runtime_dependencies",
        lambda: (SimpleNamespace(raw_file_dir=str(raw_dir)), FakeDb, SysRawFile),
    )
    monkeypatch.setattr(raw_archive_check, "_check_file", recording_check)
    monkeypatch.setattr(sys, "argv", [str(_SCRIPT), "--dry-run"])

    assert raw_archive_check.main() == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == ""
    assert {
        "complete": True,
        "status": "PASS",
        "references": 1505,
        "unique_files": 1,
        "healthy": 1,
    }.items() <= payload.items()
    assert checked_hashes == [file_hash]
    assert selected_statement is not None
    assert selected_statement.get_execution_options() == {
        "stream_results": True,
        "yield_per": 1000,
    }
    assert str(selected_statement).endswith("ORDER BY sys_raw_file.id")
    assert events[-4:] == ["select", "rollback", "session-exit-close", "check-file"]


def test_main_repeated_eio_is_safe_and_closes_every_archive_fd(
    monkeypatch, tmp_path, capsys
):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    content = b"safe repeated EIO archive content"
    file_hash = hashlib.sha256(content).hexdigest()
    archive = raw_dir / f"{file_hash}.xlsx"
    archive.write_bytes(content)
    _age_file(archive)
    metadata_before = _metadata(archive)
    secret = "secret-eio-driver-sentinel"
    real_open = os.open
    real_read = os.read
    opened_file_fds = []
    file_fds = set()
    sessions = []

    class FakeResult:
        def __init__(self, scalar=None, rows=()):
            self.scalar = scalar
            self.rows = rows

        def scalar_one(self):
            return self.scalar

        def __iter__(self):
            return iter(self.rows)

    class FakeDb:
        def __init__(self):
            self.rollback_count = 0
            self.close_count = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close_count += 1

        def execute(self, statement):
            sql = str(statement)
            if sql == "SHOW transaction_read_only":
                return FakeResult("on")
            if sql == "SHOW transaction_isolation":
                return FakeResult("repeatable read")
            if sql.startswith("SELECT "):
                return FakeResult(rows=[(1, file_hash, str(archive))])
            return FakeResult()

        def rollback(self):
            self.rollback_count += 1

    def session_factory():
        session = FakeDb()
        sessions.append(session)
        return session

    def recording_open(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if os.fspath(path) == archive.name:
            opened_file_fds.append(fd)
            file_fds.add(fd)
        return fd

    def injected_read(fd, size):
        if fd in file_fds:
            raise OSError(errno.EIO, secret)
        return real_read(fd, size)

    monkeypatch.setattr(
        raw_archive_check,
        "_load_runtime_dependencies",
        lambda: (
            SimpleNamespace(raw_file_dir=str(raw_dir)),
            session_factory,
            SysRawFile,
        ),
    )
    monkeypatch.setattr(raw_archive_check.os, "open", recording_open)
    monkeypatch.setattr(raw_archive_check.os, "read", injected_read)
    monkeypatch.setattr(sys, "argv", [str(_SCRIPT), "--dry-run"])
    fd_count_before = len(list(Path("/proc/self/fd").iterdir()))

    for _ in range(100):
        assert raw_archive_check.main() == 1
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert captured.err == ""
        assert {
            "complete": True,
            "status": "FAIL",
            "references": 1,
            "unique_files": 1,
            "healthy": 0,
            "read_error": 1,
        }.items() <= payload.items()
        assert payload["samples"]["read_error"] == [{"file": 1}]
        assert secret not in captured.out

    assert len(list(Path("/proc/self/fd").iterdir())) == fd_count_before
    assert _metadata(archive) == metadata_before
    assert len(opened_file_fds) == 100
    assert sum(session.rollback_count for session in sessions) == 100
    assert sum(session.close_count for session in sessions) == 100
    for fd in opened_file_fds:
        with pytest.raises(OSError) as exc_info:
            os.fstat(fd)
        assert exc_info.value.errno == errno.EBADF

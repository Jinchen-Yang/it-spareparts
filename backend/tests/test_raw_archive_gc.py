"""Fail-closed garbage collection for unreferenced raw archives."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from app.config import DATA_CHANGE_ADVISORY_LOCK_KEY
from app.db import SessionLocal
from app.models.system import SysImportBatch, SysRawFile
from app.services import raw_archive_gc
from scripts import raw_archive_gc as raw_archive_gc_cli

_NOW_NS = 1_800_000_000_000_000_000
_DAY_NS = 24 * 60 * 60 * 1_000_000_000


def _archive(
    raw_dir: Path,
    payload: bytes,
    *,
    age_days: int = 8,
    file_hash: str | None = None,
) -> tuple[Path, str]:
    digest = file_hash or hashlib.sha256(payload).hexdigest()
    path = raw_dir / f"{digest}.xlsx"
    path.write_bytes(payload)
    mtime_ns = _NOW_NS - age_days * _DAY_NS
    os.utime(path, ns=(mtime_ns, mtime_ns))
    return path, digest


def _reference(
    db,
    *,
    path: Path,
    file_hash: str,
    file_type: str,
    stored_hash: str | None = None,
    storage_path: str | None = None,
) -> None:
    batch_hash = stored_hash or file_hash
    batch = SysImportBatch(
        filename=f"{file_type}-archive.xlsx",
        file_type=file_type,
        file_hash=batch_hash,
        status="success",
    )
    db.add(batch)
    db.flush()
    db.add(SysRawFile(
        batch_id=batch.id,
        filename=path.name,
        file_hash=batch_hash,
        storage_path=str(path) if storage_path is None else storage_path,
    ))
    db.commit()


def _run(raw_dir: Path, *, execute: bool = False):
    return raw_archive_gc.reap_orphan_archives(
        execute=execute,
        raw_dir=str(raw_dir),
        session_factory=SessionLocal,
        now_ns=_NOW_NS,
    )


def test_service_defaults_to_dry_run_and_preserves_candidate(db, tmp_path):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    path, _ = _archive(raw_dir, b"dry-run-orphan")

    result = raw_archive_gc.reap_orphan_archives(
        raw_dir=str(raw_dir),
        session_factory=SessionLocal,
        now_ns=_NOW_NS,
    )

    assert result == {
        "dry_run": True,
        "scanned": 1,
        "candidates": 1,
        "referenced": 0,
        "deleted": 0,
        "deleted_bytes": 0,
        "skipped": 0,
        "errors": 0,
    }
    assert path.exists()


def test_execute_deletes_only_digest_valid_old_orphan(db, tmp_path):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    payload = b"confirmed-unreferenced-orphan"
    path, _ = _archive(raw_dir, payload)

    result = _run(raw_dir, execute=True)

    assert result["dry_run"] is False
    assert result["scanned"] == 1
    assert result["candidates"] == 1
    assert result["referenced"] == 0
    assert result["deleted"] == 1
    assert result["deleted_bytes"] == len(payload)
    assert result["skipped"] == 0
    assert result["errors"] == 0
    assert not path.exists()


def test_commit_ack_lost_existing_reference_is_preserved(db, tmp_path):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    path, file_hash = _archive(raw_dir, b"commit-succeeded-ack-lost")
    _reference(
        db,
        path=path,
        file_hash=file_hash,
        file_type="maint_roundtrip",
    )

    result = _run(raw_dir, execute=True)

    assert result["referenced"] == 1
    assert result["candidates"] == 0
    assert result["deleted"] == 0
    assert result["errors"] == 0
    assert path.exists()


@pytest.mark.parametrize(
    "file_type",
    ["purchase", "sales", "maintenance", "maint_roundtrip"],
)
def test_reused_archive_reference_in_any_namespace_is_preserved(
    db,
    tmp_path,
    file_type,
):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    path, file_hash = _archive(raw_dir, f"reused-{file_type}".encode())
    _reference(
        db,
        path=path,
        file_hash=file_hash,
        file_type=file_type,
    )

    result = _run(raw_dir, execute=True)

    assert result["referenced"] == 1
    assert result["deleted"] == 0
    assert path.exists()


def test_storage_path_reference_alone_is_sufficient_to_preserve(db, tmp_path):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    path, file_hash = _archive(raw_dir, b"storage-path-reference")
    other_hash = hashlib.sha256(b"different-database-hash").hexdigest()
    _reference(
        db,
        path=path,
        file_hash=file_hash,
        file_type="purchase",
        stored_hash=other_hash,
    )

    result = _run(raw_dir, execute=True)

    assert result["referenced"] == 1
    assert result["deleted"] == 0
    assert path.exists()


def test_non_strict_name_symlink_and_digest_mismatch_are_skipped(db, tmp_path):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    target = tmp_path / "outside.xlsx"
    target.write_bytes(b"outside-target")
    target_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    symlink = raw_dir / f"{target_hash}.xlsx"
    symlink.symlink_to(target)
    symlink_time = _NOW_NS - 8 * _DAY_NS
    os.utime(symlink, ns=(symlink_time, symlink_time), follow_symlinks=False)

    mismatch_hash = "a" * 64
    mismatch, _ = _archive(
        raw_dir,
        b"does-not-match-the-name",
        file_hash=mismatch_hash,
    )
    uppercase_hash = hashlib.sha256(b"uppercase-name").hexdigest().upper()
    uppercase = raw_dir / f"{uppercase_hash}.xlsx"
    uppercase.write_bytes(b"uppercase-name")
    uppercase_time = _NOW_NS - 8 * _DAY_NS
    os.utime(uppercase, ns=(uppercase_time, uppercase_time))

    result = _run(raw_dir, execute=True)

    assert result["scanned"] == 3
    assert result["candidates"] == 0
    assert result["referenced"] == 0
    assert result["deleted"] == 0
    assert result["skipped"] == 3
    assert result["errors"] == 0
    assert symlink.is_symlink()
    assert target.read_bytes() == b"outside-target"
    assert mismatch.exists()
    assert uppercase.exists()


def test_default_grace_period_is_strictly_older_than_seven_days(db, tmp_path):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    recent, _ = _archive(raw_dir, b"recent", age_days=6)
    boundary, _ = _archive(raw_dir, b"exact-boundary", age_days=7)
    old, _ = _archive(raw_dir, b"old", age_days=8)

    result = _run(raw_dir, execute=True)

    assert result["scanned"] == 3
    assert result["candidates"] == 1
    assert result["deleted"] == 1
    assert result["skipped"] == 2
    assert recent.exists()
    assert boundary.exists()
    assert not old.exists()


def test_database_uncertainty_preserves_every_file(tmp_path):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    path, _ = _archive(raw_dir, b"database-uncertain")

    def failing_factory():
        raise RuntimeError("database unavailable")

    result = raw_archive_gc.reap_orphan_archives(
        execute=True,
        raw_dir=str(raw_dir),
        session_factory=failing_factory,
        now_ns=_NOW_NS,
    )

    assert result["deleted"] == 0
    assert result["errors"] == 1
    assert path.exists()


@pytest.mark.parametrize("unsafe_days", [0, 6, 3651])
def test_service_rejects_unsafe_grace_period_before_scanning(
    db,
    tmp_path,
    unsafe_days,
):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    path, _ = _archive(raw_dir, b"unsafe-grace-period")

    result = raw_archive_gc.reap_orphan_archives(
        execute=True,
        grace_period=timedelta(days=unsafe_days),
        raw_dir=str(raw_dir),
        session_factory=SessionLocal,
        now_ns=_NOW_NS,
    )

    assert result["scanned"] == 0
    assert result["deleted"] == 0
    assert result["errors"] == 1
    assert path.exists()


def test_gc_holds_import_advisory_lock_through_scan(
    db,
    tmp_path,
    monkeypatch,
):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    scan_entered = threading.Event()
    release_scan = threading.Event()
    finished = threading.Event()
    result_box: list[dict[str, int | bool]] = []
    real_scan = raw_archive_gc._scan_locked

    def paused_scan(*args, **kwargs):
        scan_entered.set()
        assert release_scan.wait(timeout=5)
        return real_scan(*args, **kwargs)

    monkeypatch.setattr(raw_archive_gc, "_scan_locked", paused_scan)

    def worker():
        result_box.append(_run(raw_dir))
        finished.set()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    assert scan_entered.wait(timeout=5)
    with SessionLocal() as competing:
        acquired = competing.scalar(
            text("SELECT pg_try_advisory_xact_lock(:key)"),
            {"key": DATA_CHANGE_ADVISORY_LOCK_KEY},
        )
        assert acquired is False
        competing.rollback()
    release_scan.set()
    thread.join(timeout=5)

    assert finished.is_set()
    assert result_box[0]["errors"] == 0


@pytest.mark.parametrize(
    ("argv", "expected_execute", "expected_days"),
    [
        ([], False, 7),
        (["--execute"], True, 7),
        (["--grace-days", "30"], False, 30),
    ],
)
def test_cli_defaults_to_dry_run_and_requires_explicit_execute(
    monkeypatch,
    capsys,
    argv,
    expected_execute,
    expected_days,
):
    calls = []

    def fake_reaper(**kwargs):
        calls.append(kwargs)
        return raw_archive_gc.empty_result(execute=kwargs["execute"])

    monkeypatch.setattr(
        raw_archive_gc_cli,
        "_load_runtime_dependencies",
        lambda: (object(), fake_reaper),
    )

    assert raw_archive_gc_cli.main(argv) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out)["dry_run"] is (not expected_execute)
    assert calls[0]["execute"] is expected_execute
    assert calls[0]["grace_period"] == timedelta(days=expected_days)


@pytest.mark.parametrize("unsafe_value", ["0", "6", "3651", "invalid"])
def test_cli_rejects_unsafe_grace_before_loading_runtime(
    monkeypatch,
    capsys,
    unsafe_value,
):
    runtime_loaded = False

    def forbidden_runtime_load():
        nonlocal runtime_loaded
        runtime_loaded = True
        raise AssertionError("unsafe grace reached runtime")

    monkeypatch.setattr(
        raw_archive_gc_cli,
        "_load_runtime_dependencies",
        forbidden_runtime_load,
    )

    assert raw_archive_gc_cli.main(
        ["--execute", "--grace-days", unsafe_value]
    ) == 2
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["dry_run"] is True
    assert result["errors"] == 1
    assert "raw archive gc failed" in captured.err
    assert runtime_loaded is False

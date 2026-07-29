"""Fail-closed garbage collection for unreferenced raw XLSX archives.

The database transaction and filesystem cannot commit atomically.  Imports
therefore publish a content-addressed archive before committing the database
row; a failed or outcome-unknown commit can leave an unreferenced file behind.
This module removes only old files that can be proven unreferenced while
holding the same PostgreSQL advisory lock used by imports.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.config import DATA_CHANGE_ADVISORY_LOCK_KEY, get_settings
from app.db import SessionLocal
from app.models.system import SysRawFile

MIN_GRACE_DAYS = 7
MAX_GRACE_DAYS = 3650
DEFAULT_GRACE_DAYS = MIN_GRACE_DAYS
_ARCHIVE_NAME_RE = re.compile(r"(?P<file_hash>[0-9a-f]{64})\.xlsx\Z")
_CHUNK_SIZE = 1024 * 1024
_METRIC_KEYS = (
    "scanned",
    "candidates",
    "referenced",
    "deleted",
    "deleted_bytes",
    "skipped",
    "errors",
)


def empty_result(*, execute: bool = False) -> dict[str, int | bool]:
    """Return the stable public result shape.

    ``execute=False`` is intentionally the service default.  Callers must opt
    in to filesystem mutation explicitly.
    """

    return {
        "dry_run": not execute,
        **{key: 0 for key in _METRIC_KEYS},
    }


def _same_object(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _same_file_state(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_size,
        left.st_mtime_ns,
        left.st_ctime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_size,
        right.st_mtime_ns,
        right.st_ctime_ns,
    )


def _required_flag(name: str) -> int:
    value = getattr(os, name, None)
    if value is None:
        raise RuntimeError("required secure-open flag is unavailable")
    return value


def _directory_fd(raw_dir: str) -> tuple[int, os.stat_result]:
    before = os.stat(raw_dir, follow_symlinks=False)
    if not stat.S_ISDIR(before.st_mode):
        raise RuntimeError("raw archive root is not a directory")
    flags = (
        os.O_RDONLY
        | _required_flag("O_DIRECTORY")
        | _required_flag("O_NOFOLLOW")
        | _required_flag("O_CLOEXEC")
    )
    fd = os.open(raw_dir, flags)
    opened = os.fstat(fd)
    if not stat.S_ISDIR(opened.st_mode) or not _same_object(before, opened):
        os.close(fd)
        raise RuntimeError("raw archive directory changed")
    return fd, opened


def _normalize_storage_path(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return os.path.abspath(os.path.normpath(value))


def _load_references(db: Session) -> tuple[set[str], set[str]]:
    hashes: set[str] = set()
    paths: set[str] = set()
    rows = db.execute(
        select(SysRawFile.file_hash, SysRawFile.storage_path)
        .execution_options(stream_results=True, yield_per=1000)
    )
    for file_hash, storage_path in rows:
        if isinstance(file_hash, str):
            hashes.add(file_hash)
        normalized = _normalize_storage_path(storage_path)
        if normalized is not None:
            paths.add(normalized)
    return hashes, paths


def _digest_if_stable(
    dir_fd: int,
    *,
    name: str,
    before: os.stat_result,
) -> tuple[str, os.stat_result]:
    flags = (
        os.O_RDONLY
        | _required_flag("O_NOFOLLOW")
        | _required_flag("O_NONBLOCK")
        | _required_flag("O_CLOEXEC")
    )
    file_fd = os.open(name, flags, dir_fd=dir_fd)
    try:
        opened = os.fstat(file_fd)
        if not stat.S_ISREG(opened.st_mode) or not _same_file_state(before, opened):
            raise RuntimeError("raw archive changed before hashing")
        digest = hashlib.sha256()
        while chunk := os.read(file_fd, _CHUNK_SIZE):
            digest.update(chunk)
        after_fd = os.fstat(file_fd)
        after_path = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        if not (
            _same_file_state(opened, after_fd)
            and _same_file_state(after_fd, after_path)
        ):
            raise RuntimeError("raw archive changed while hashing")
        return digest.hexdigest(), after_path
    finally:
        os.close(file_fd)


def _scan_locked(
    db: Session,
    *,
    raw_dir: str,
    execute: bool,
    cutoff_ns: int,
    result: dict[str, int | bool],
) -> None:
    referenced_hashes, referenced_paths = _load_references(db)
    dir_fd, directory_opened = _directory_fd(raw_dir)
    try:
        with os.scandir(dir_fd) as entries:
            for entry in entries:
                result["scanned"] += 1
                matched = _ARCHIVE_NAME_RE.fullmatch(entry.name)
                if matched is None:
                    result["skipped"] += 1
                    continue
                try:
                    before = entry.stat(follow_symlinks=False)
                except OSError:
                    result["errors"] += 1
                    continue
                if not stat.S_ISREG(before.st_mode):
                    result["skipped"] += 1
                    continue
                # "Older than seven days" is strict: an exact-boundary file is
                # retained until the next run.
                if before.st_mtime_ns >= cutoff_ns:
                    result["skipped"] += 1
                    continue

                file_hash = matched.group("file_hash")
                storage_path = os.path.abspath(
                    os.path.join(raw_dir, entry.name)
                )
                if (
                    file_hash in referenced_hashes
                    or storage_path in referenced_paths
                ):
                    result["referenced"] += 1
                    continue

                try:
                    digest, stable = _digest_if_stable(
                        dir_fd,
                        name=entry.name,
                        before=before,
                    )
                except (OSError, RuntimeError):
                    result["errors"] += 1
                    continue
                if digest != file_hash:
                    result["skipped"] += 1
                    continue

                result["candidates"] += 1
                if not execute:
                    continue

                try:
                    current = os.stat(
                        entry.name,
                        dir_fd=dir_fd,
                        follow_symlinks=False,
                    )
                    if not _same_file_state(stable, current):
                        raise RuntimeError("raw archive changed before deletion")
                    os.unlink(entry.name, dir_fd=dir_fd)
                except (OSError, RuntimeError):
                    result["errors"] += 1
                    continue
                result["deleted"] += 1
                result["deleted_bytes"] += stable.st_size

        directory_after_fd = os.fstat(dir_fd)
        directory_after_path = os.stat(raw_dir, follow_symlinks=False)
        if not (
            _same_object(directory_opened, directory_after_fd)
            and _same_object(directory_after_fd, directory_after_path)
        ):
            raise RuntimeError("raw archive directory changed")
    finally:
        os.close(dir_fd)


def reap_orphan_archives(
    *,
    execute: bool = False,
    grace_period: timedelta = timedelta(days=DEFAULT_GRACE_DAYS),
    raw_dir: str | None = None,
    session_factory: Callable[[], Session] = SessionLocal,
    now_ns: int | None = None,
) -> dict[str, int | bool]:
    """Scan and optionally remove old, unreferenced content-addressed archives.

    The function always creates its own database session.  A repeatable-read,
    read-only transaction takes ``DATA_CHANGE_ADVISORY_LOCK_KEY`` before any
    directory scan and keeps it through the final unlink.  If database or
    filesystem state cannot be proven, the affected file is retained.
    """

    result = empty_result(execute=execute)
    try:
        grace_ns = int(grace_period.total_seconds() * 1_000_000_000)
        minimum_grace_ns = MIN_GRACE_DAYS * 24 * 60 * 60 * 1_000_000_000
        maximum_grace_ns = MAX_GRACE_DAYS * 24 * 60 * 60 * 1_000_000_000
        if not minimum_grace_ns <= grace_ns <= maximum_grace_ns:
            raise ValueError("grace period is outside the safe range")
        scan_now_ns = time.time_ns() if now_ns is None else int(now_ns)
        cutoff_ns = scan_now_ns - grace_ns
        selected_raw_dir = (
            get_settings().raw_file_dir if raw_dir is None else raw_dir
        )

        with session_factory() as db:
            try:
                db.execute(
                    text(
                        "SET TRANSACTION ISOLATION LEVEL "
                        "REPEATABLE READ READ ONLY"
                    )
                )
                db.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"),
                    {"key": DATA_CHANGE_ADVISORY_LOCK_KEY},
                )
                if db.execute(
                    text("SHOW transaction_read_only")
                ).scalar_one() != "on":
                    raise RuntimeError("database transaction is not read only")
                if db.execute(
                    text("SHOW transaction_isolation")
                ).scalar_one() != "repeatable read":
                    raise RuntimeError(
                        "database transaction is not repeatable read"
                    )
                _scan_locked(
                    db,
                    raw_dir=selected_raw_dir,
                    execute=execute,
                    cutoff_ns=cutoff_ns,
                    result=result,
                )
            finally:
                db.rollback()
    except Exception:
        # The public result deliberately contains no exception text, paths,
        # connection strings, or file hashes.  Uncertainty means retain.
        result["errors"] += 1
    return result

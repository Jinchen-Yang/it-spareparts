#!/usr/bin/env python3
"""只读校验数据库引用的原始底稿归档。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from typing import Any, NoReturn

from sqlalchemy import select, text

_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
_CHUNK_SIZE = 1024 * 1024
_SAMPLE_LIMIT = 5
_COUNT_KEYS = (
    "missing",
    "hash_mismatch",
    "non_regular",
    "invalid_reference",
    "read_error",
)


class _SafeArgumentError(Exception):
    pass


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise _SafeArgumentError


class _OnceStoreTrue(argparse.Action):
    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        if getattr(namespace, self.dest, False):
            parser.error("duplicate option")
        setattr(namespace, self.dest, True)


def _result(*, dry_run: bool, status: str = "PASS") -> dict[str, Any]:
    return {
        "complete": False,
        "dry_run": dry_run,
        "status": status,
        "references": 0,
        "unique_files": 0,
        "healthy": 0,
        "sample_limit": _SAMPLE_LIMIT,
        "samples": {key: [] for key in _COUNT_KEYS},
        **{key: 0 for key in _COUNT_KEYS},
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


def _check_file(dir_fd: int, file_hash: str) -> str:
    basename = f"{file_hash}.xlsx"
    try:
        before = os.stat(basename, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "read_error"

    if not stat.S_ISREG(before.st_mode):
        return "non_regular"

    flags = (
        os.O_RDONLY
        | _required_flag("O_NOFOLLOW")
        | _required_flag("O_NONBLOCK")
        | _required_flag("O_CLOEXEC")
        | _required_flag("O_NOATIME")
    )
    try:
        file_fd = os.open(basename, flags, dir_fd=dir_fd)
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "read_error"

    try:
        opened = os.fstat(file_fd)
        if not stat.S_ISREG(opened.st_mode):
            return "non_regular"
        if not _same_file_state(before, opened):
            return "read_error"

        digest = hashlib.sha256()
        while chunk := os.read(file_fd, _CHUNK_SIZE):
            digest.update(chunk)

        after_fd = os.fstat(file_fd)
        after_path = os.stat(basename, dir_fd=dir_fd, follow_symlinks=False)
        if not (
            _same_file_state(opened, after_fd)
            and _same_file_state(after_fd, after_path)
        ):
            return "read_error"
        return "healthy" if digest.hexdigest() == file_hash else "hash_mismatch"
    except OSError:
        return "read_error"
    finally:
        os.close(file_fd)


def _load_runtime_dependencies() -> tuple[Any, Any, Any]:
    from app.config import get_settings
    from app.db import SessionLocal
    from app.models.system import SysRawFile

    return get_settings(), SessionLocal, SysRawFile


def _scan(
    raw_dir: str,
    result: dict[str, Any],
    session_factory: Any,
    raw_file_model: Any,
) -> None:
    directory_flags = (
        os.O_RDONLY
        | _required_flag("O_DIRECTORY")
        | _required_flag("O_NOFOLLOW")
        | _required_flag("O_CLOEXEC")
    )
    directory_before = os.stat(raw_dir, follow_symlinks=False)
    dir_fd = os.open(raw_dir, directory_flags)
    try:
        directory_opened = os.fstat(dir_fd)
        if not stat.S_ISDIR(directory_opened.st_mode) or not _same_object(
            directory_before, directory_opened
        ):
            raise RuntimeError("archive directory changed")

        hashes: set[str] = set()
        with session_factory() as db:
            try:
                db.execute(
                    text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                )
                if db.execute(text("SHOW transaction_read_only")).scalar_one() != "on":
                    raise RuntimeError("database transaction is not read only")
                if (
                    db.execute(text("SHOW transaction_isolation")).scalar_one()
                    != "repeatable read"
                ):
                    raise RuntimeError("database transaction is not repeatable read")
                rows = db.execute(
                    select(
                        raw_file_model.id,
                        raw_file_model.file_hash,
                        raw_file_model.storage_path,
                    )
                    .execution_options(stream_results=True, yield_per=1000)
                    .order_by(raw_file_model.id)
                )
                for _, file_hash, storage_path in rows:
                    result["references"] += 1
                    if not isinstance(file_hash, str) or not _HASH_RE.fullmatch(
                        file_hash
                    ):
                        result["invalid_reference"] += 1
                        if len(result["samples"]["invalid_reference"]) < _SAMPLE_LIMIT:
                            result["samples"]["invalid_reference"].append(
                                {"reference": result["references"]}
                            )
                        continue
                    expected_path = os.path.join(raw_dir, f"{file_hash}.xlsx")
                    if (
                        not isinstance(storage_path, str)
                        or storage_path != expected_path
                    ):
                        result["invalid_reference"] += 1
                        if len(result["samples"]["invalid_reference"]) < _SAMPLE_LIMIT:
                            result["samples"]["invalid_reference"].append(
                                {"reference": result["references"]}
                            )
                        continue
                    hashes.add(file_hash)

            finally:
                db.rollback()

        result["unique_files"] = len(hashes)
        for file_number, file_hash in enumerate(sorted(hashes), start=1):
            outcome = _check_file(dir_fd, file_hash)
            result[outcome] += 1
            if outcome != "healthy" and len(result["samples"][outcome]) < _SAMPLE_LIMIT:
                result["samples"][outcome].append({"file": file_number})

        directory_after_fd = os.fstat(dir_fd)
        directory_after_path = os.stat(raw_dir, follow_symlinks=False)
        if not (
            _same_object(directory_opened, directory_after_fd)
            and _same_object(directory_after_fd, directory_after_path)
        ):
            raise RuntimeError("archive directory changed")
    finally:
        os.close(dir_fd)


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(description="只读校验原始底稿归档", allow_abbrev=False)
    parser.add_argument(
        "--dry-run",
        action=_OnceStoreTrue,
        nargs=0,
        help="必须显式指定；该命令不提供写入模式",
    )
    return parser


def _error(dry_run: bool) -> int:
    print(json.dumps(_result(dry_run=dry_run, status="ERROR"), sort_keys=True))
    print("raw archive check failed", file=sys.stderr)
    return 2


def main() -> int:
    try:
        args = _parser().parse_args()
    except _SafeArgumentError:
        return _error(False)
    if not args.dry_run:
        return _error(False)

    result = _result(dry_run=args.dry_run)
    try:
        settings, session_factory, raw_file_model = _load_runtime_dependencies()
        _scan(settings.raw_file_dir, result, session_factory, raw_file_model)
    except Exception:
        return _error(args.dry_run)

    result["complete"] = True
    if any(result[key] for key in _COUNT_KEYS):
        result["status"] = "FAIL"
        exit_code = 1
    else:
        exit_code = 0
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

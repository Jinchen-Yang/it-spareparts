#!/usr/bin/env python3
"""Dry-run-by-default cleanup for unreferenced raw XLSX archives."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from typing import Any, NoReturn

MIN_GRACE_DAYS = 7
MAX_GRACE_DAYS = 3650
DEFAULT_GRACE_DAYS = MIN_GRACE_DAYS


class _SafeArgumentError(Exception):
    pass


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        _ = message
        raise _SafeArgumentError


def _grace_days(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError from exc
    if not MIN_GRACE_DAYS <= parsed <= MAX_GRACE_DAYS:
        raise argparse.ArgumentTypeError
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        description="清理无数据库引用的旧原始归档（默认仅预览）",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="显式执行删除；省略时为 dry-run",
    )
    parser.add_argument(
        "--grace-days",
        type=_grace_days,
        default=DEFAULT_GRACE_DAYS,
        metavar="DAYS",
        help="只处理严格早于该天数的文件，默认 7",
    )
    return parser


def _empty(*, execute: bool) -> dict[str, int | bool]:
    return {
        "dry_run": not execute,
        "scanned": 0,
        "candidates": 0,
        "referenced": 0,
        "deleted": 0,
        "deleted_bytes": 0,
        "skipped": 0,
        "errors": 1,
    }


def _load_runtime_dependencies() -> tuple[Any, Any]:
    from app.db import SessionLocal
    from app.services.raw_archive_gc import reap_orphan_archives

    return SessionLocal, reap_orphan_archives


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except _SafeArgumentError:
        print(json.dumps(_empty(execute=False), sort_keys=True))
        print("raw archive gc failed", file=sys.stderr)
        return 2

    try:
        session_factory, reaper = _load_runtime_dependencies()
        result = reaper(
            execute=args.execute,
            grace_period=timedelta(days=args.grace_days),
            session_factory=session_factory,
        )
    except Exception:
        result = _empty(execute=args.execute)

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if int(result.get("errors", 0)) > 0:
        print("raw archive gc completed with retained uncertainties", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

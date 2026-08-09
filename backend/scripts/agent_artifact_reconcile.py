#!/usr/bin/env python3
"""Reconcile Artifact v2 metadata and objects; dry-run unless ``--apply`` is explicit."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from typing import Any

MIN_GRACE_MINUTES = 5
MAX_GRACE_MINUTES = 30 * 24 * 60
DEFAULT_GRACE_MINUTES = 60


def _grace_minutes(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("invalid grace") from exc
    if not MIN_GRACE_MINUTES <= parsed <= MAX_GRACE_MINUTES:
        raise argparse.ArgumentTypeError("unsafe grace")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="保守核对 Artifact v2 状态与对象（默认仅预览）",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="显式执行修复/清理；省略时为 dry-run",
    )
    parser.add_argument(
        "--grace-minutes",
        type=_grace_minutes,
        default=DEFAULT_GRACE_MINUTES,
        metavar="MINUTES",
        help="只处理严格早于安全窗口的中间态/孤儿/临时文件，默认 60 分钟",
    )
    return parser


def _load_reconciler() -> Any:
    from app.services.agent_artifact_reconcile import reconcile_agent_artifacts

    return reconcile_agent_artifacts


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        result = _load_reconciler()(
            apply=args.apply,
            grace_period=timedelta(minutes=args.grace_minutes),
        )
    except SystemExit as exc:
        return int(exc.code or 0)
    except Exception:
        print(json.dumps({"dry_run": True, "errors": 1}, sort_keys=True))
        print("artifact reconciliation failed closed", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if int(result.get("errors", 0)):
        print("artifact reconciliation retained uncertain entries", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

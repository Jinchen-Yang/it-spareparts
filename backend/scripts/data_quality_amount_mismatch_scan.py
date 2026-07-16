#!/usr/bin/env python3
"""只读回扫存量金额关系；DEV-05C1 不提供写入模式。"""
from __future__ import annotations

import argparse
import json

from sqlalchemy import text

from app.db import SessionLocal
from app.services import data_quality_amount_mismatch


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="只读预览历史 amount_mismatch 疑点规模（不会写数据库）",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="必须显式指定；本版本只有只读预览，没有 apply 模式",
    )
    parser.add_argument("--side", choices=("purchase", "sales"))
    parser.add_argument("--sample-limit", type=int, default=20)
    return parser


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    if not args.dry_run:
        parser.error("必须显式指定 --dry-run；本版本不支持历史写入")
    with SessionLocal() as db:
        db.execute(text("SET TRANSACTION READ ONLY"))
        result = data_quality_amount_mismatch.preview_history(
            db, side=args.side, sample_limit=args.sample_limit,
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
        db.rollback()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

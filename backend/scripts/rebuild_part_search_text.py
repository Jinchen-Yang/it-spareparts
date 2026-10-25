#!/usr/bin/env python3
"""重建实时检索投影；--dry-run 只统计，重复执行不产生重复投影。"""

import argparse
import json
import os

from sqlalchemy import text
from sqlalchemy.orm import Session


def rebuild(db: Session, *, dry_run: bool = False) -> dict:
    """仅刷新检索派生数据；事务由调用方提交。"""
    rows = db.execute(text(
        "SELECT count(*) FROM dim_part WHERE status <> 'merged'"
    )).scalar_one()
    stale = db.execute(text(
        "SELECT count(*) FROM part_search_text s WHERE NOT EXISTS"
        " (SELECT 1 FROM dim_part p WHERE p.id=s.part_id AND p.status <> 'merged')"
    )).scalar_one()
    if not dry_run:
        db.execute(text(
            "DELETE FROM part_search_text s WHERE NOT EXISTS"
            " (SELECT 1 FROM dim_part p WHERE p.id=s.part_id AND p.status <> 'merged')"
        ))
        db.execute(text("SELECT refresh_part_search_text(id) FROM dim_part ORDER BY id"))
    return {"dry_run": dry_run, "rows": rows, "stale_rows": stale}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="只输出计划行数，不写入")
    args = parser.parse_args()
    if not os.environ.get("DATABASE_URL"):
        parser.error("必须通过 DATABASE_URL 显式指定目标数据库")
    from app.db import SessionLocal

    with SessionLocal() as db:
        if args.dry_run:
            db.execute(text("SET TRANSACTION READ ONLY"))
        result = rebuild(db, dry_run=args.dry_run)
        if not args.dry_run:
            db.commit()
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Apply one guarded exact-collection dedupe for a split XSDD project group."""

from __future__ import annotations

import argparse
import json

from sqlalchemy import text

from app.db import SessionLocal
from app.services.maintenance_project_identity import (
    apply_exact_collection_dedupe,
    normalize_xsdd,
    preview_historical_conflicts,
)


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--xsdd", required=True)
    parser.add_argument("--operated-by", required=True)
    parser.add_argument(
        "--apply-exact-collections",
        action="store_true",
        help="Required acknowledgement for physical deletion of exact duplicates.",
    )
    return parser.parse_args()


def main() -> int:
    args = _args()
    xsdd_norm = normalize_xsdd(args.xsdd)
    with SessionLocal() as db:
        if not args.apply_exact_collections:
            db.execute(text("SET TRANSACTION READ ONLY"))
            manifest = preview_historical_conflicts(db)
            result = next(
                (
                    row for row in manifest["conflicts"]
                    if row["xsdd_norm"] == xsdd_norm
                ),
                {"xsdd_norm": xsdd_norm, "exact_duplicate_candidates": {}},
            )
            db.rollback()
        else:
            try:
                result = apply_exact_collection_dedupe(
                    db,
                    xsdd=args.xsdd,
                    operated_by=args.operated_by,
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

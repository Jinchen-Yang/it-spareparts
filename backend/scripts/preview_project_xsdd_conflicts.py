#!/usr/bin/env python3
"""Print a read-only JSON manifest for historical same-XSDD project splits."""

from __future__ import annotations

import json

from sqlalchemy import text

from app.db import SessionLocal
from app.services.maintenance_project_identity import preview_historical_conflicts


def main() -> int:
    with SessionLocal() as db:
        db.execute(text("SET TRANSACTION READ ONLY"))
        result = preview_historical_conflicts(db)
        db.rollback()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Daily worker housekeeping: remove expired bytes, retain receipts/audit metadata."""

import time
import uuid

from app.db import SessionLocal
from app.mcp.core import now
from app.mcp.workflows import root
from app.models.mcp import McpRecord


def cleanup_files():
    removed = 0
    with SessionLocal() as db:
        db.execute(
            __import__("sqlalchemy").text(
                "SELECT pg_advisory_xact_lock(hashtextextended('mcp-cleanup',0))"
            )
        )
        for path in root().iterdir():
            try:
                if str(uuid.UUID(path.name)) != path.name or path.is_symlink():
                    continue
            except ValueError:
                continue
            row = db.get(McpRecord, path.name)
            if row and row.kind in ("file", "artifact"):
                eligible = row.expires_at <= now()
            else:
                # Files written by a transaction that rolled back are never downloadable.
                # The delay keeps cleanup clear of an in-flight DB commit.
                eligible = time.time() - path.stat().st_mtime > 86400
            if eligible:
                path.unlink(missing_ok=True)
                removed += 1
    return removed


if __name__ == "__main__":
    print({"removed_files": cleanup_files()})

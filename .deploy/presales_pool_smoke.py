"""Validate the pool's SQL projection in a read-only transaction; no HTTP identity is created."""
import json

from sqlalchemy import text

from app.db import SessionLocal


def main():
    with SessionLocal() as db:
        db.execute(text("SET TRANSACTION READ ONLY"))
        db.execute(text("SET LOCAL statement_timeout='10s'"))
        if db.scalar(text("SHOW transaction_read_only")) != "on":
            raise RuntimeError("read-only transaction required")
        if db.scalar(text("SELECT version_num FROM alembic_version")) != "e5a7c9d1f3b6":
            raise RuntimeError("unexpected database head")
        # Use the current-row projection on an eligible row without assuming that production has any.
        row = db.execute(text("""
            SELECT l.id, l.answer_state, l.current_offer_id, o.id
            FROM presales_inquiry_line l JOIN presales_inquiry i ON i.id=l.inquiry_id
            LEFT JOIN presales_offer o ON o.id=l.current_offer_id
            WHERE i.status <> 'draft' AND i.submitted_at IS NOT NULL AND l.status='active'
            ORDER BY l.id DESC LIMIT 1
        """)).first()
        checks = ["migration", "pool_projection"]
        if row:
            if row[1] not in {"pending", "skip", "quoted"} or row[2] != row[3]:
                raise RuntimeError("invalid current response projection")
            db.execute(text("""
                SELECT r.revision_no FROM presales_offer_revision r
                JOIN presales_offer o ON o.id=r.offer_id WHERE o.line_id=:line
                ORDER BY r.id DESC LIMIT 1
            """), {"line": row[0]}).all()
            checks.append("history_projection")
        db.rollback()
    print(json.dumps({"status": "passed", "mode": "readonly_database_projection", "checks": checks}))


if __name__ == "__main__":
    main()

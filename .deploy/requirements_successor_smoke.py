"""Read-only projection for the no-migration successor. It must not write business state."""
import json

from sqlalchemy import text

from app.db import SessionLocal

HELD_HEAD = "98afc0d2157b"


def main():
    with SessionLocal() as db:
        db.execute(text("SET TRANSACTION READ ONLY"))
        db.execute(text("SET LOCAL statement_timeout='15s'"))
        if db.scalar(text("SHOW transaction_read_only")) != "on":
            raise RuntimeError("read-only transaction required")
        if db.scalar(text("SELECT version_num FROM alembic_version")) != HELD_HEAD:
            raise RuntimeError("schema is not the held head")
        db.rollback()
    print(json.dumps({
        "status": "passed",
        "mode": "readonly_database_projection",
        "schema": HELD_HEAD,
        "migration_performed": False,
    }))


if __name__ == "__main__":
    main()

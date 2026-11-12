"""Read-only projection for the cycle-pool cutover. It must not write business state."""
import json

from sqlalchemy import text

from app.db import SessionLocal

TARGET = "c3e8a1d94b27"
TABLES = (
    "maintenance_repair_record",
    "maintenance_repair_record_source",
    "maintenance_repair_command",
    "maintenance_cycle_sell_intent",
)


def main():
    with SessionLocal() as db:
        db.execute(text("SET TRANSACTION READ ONLY"))
        db.execute(text("SET LOCAL statement_timeout='15s'"))
        if db.scalar(text("SHOW transaction_read_only")) != "on":
            raise RuntimeError("read-only transaction required")
        if db.scalar(text("SELECT version_num FROM alembic_version")) != TARGET:
            raise RuntimeError("schema is not the cycle-pool head")
        counts = {}
        for table in TABLES:
            counts[table] = db.scalar(text("SELECT count(*) FROM " + table))
            if counts[table] != 0:
                raise RuntimeError("cycle table is not empty")
        if db.scalar(text("SELECT to_regclass('public.maintenance_cycle_unit')")) is not None:
            raise RuntimeError("legacy cycle unit table still present")
        cycle_work = db.scalar(text(
            "SELECT count(*) FROM work_item WHERE subject_type IN ('cycle_test', 'cycle_repair')"
        ))
        if cycle_work != 0:
            raise RuntimeError("cycle work items are present")
        db.rollback()
    print(json.dumps({
        "status": "passed",
        "mode": "readonly_database_projection",
        "schema": TARGET,
        "cycle_tables_zero": True,
        "migration_performed": True,
    }))


if __name__ == "__main__":
    main()

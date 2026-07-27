"""Sub-pytest probe used by test_pytest_run_isolation.py."""

import json
import os
import time
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

if os.environ.get("ISOLATION_PROBE_COLLECTION_FAIL") == "1":
    raise RuntimeError("intentional probe collection failure")


def _write_state() -> None:
    marker = Path(os.environ["ISOLATION_PROBE_MARKER"])
    marker.write_text(
        json.dumps(
            {
                "database_name": make_url(os.environ["DATABASE_URL"]).database,
                "raw_file_dir": os.environ["RAW_FILE_DIR"],
                "pid": os.getpid(),
            }
        ),
        encoding="utf-8",
    )


def test_probe():
    run_id = os.environ["ISOLATION_PROBE_RUN_ID"]
    raw_dir = Path(os.environ["RAW_FILE_DIR"])
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / f"{run_id}.marker").write_text(run_id, encoding="utf-8")

    engine = create_engine(os.environ["DATABASE_URL"])
    held_conn = None
    held_tx = None
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS pytest_run_isolation_probe ("
                    "run_id text PRIMARY KEY, value text NOT NULL)"
                )
            )
            conn.execute(
                text("INSERT INTO pytest_run_isolation_probe VALUES (:run_id, :value)"),
                {"run_id": run_id, "value": run_id},
            )
        mode = os.environ.get("ISOLATION_PROBE_MODE", "pass")
        if mode == "hold":
            held_conn = engine.connect()
            held_tx = held_conn.begin()
            assert (
                held_conn.execute(
                    text(
                        "SELECT value FROM pytest_run_isolation_probe WHERE run_id=:run_id"
                    ),
                    {"run_id": run_id},
                ).scalar_one()
                == run_id
            )
        _write_state()

        if mode == "fail":
            pytest.fail("intentional probe failure")
        if mode == "keyboard_interrupt":
            raise KeyboardInterrupt
        if mode == "hold":
            release = Path(os.environ["ISOLATION_PROBE_RELEASE"])
            while not release.exists():
                time.sleep(0.05)
            held_conn.execute(
                text(
                    "UPDATE pytest_run_isolation_probe SET value=:value"
                    " WHERE run_id=:run_id"
                ),
                {"run_id": run_id, "value": f"{run_id}-finished"},
            )
            held_tx.commit()
            held_tx = None
            held_conn.close()
            held_conn = None
    finally:
        if held_tx is not None:
            held_tx.rollback()
        if held_conn is not None:
            held_conn.close()
        engine.dispose()

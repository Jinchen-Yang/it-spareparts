"""历史金额疑点回扫命令只允许显式 dry-run。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import func, select

from app.models.data_quality import FactDataQualityIssue
from app.models.system import SysAuditLog


_BACKEND = Path(__file__).resolve().parents[1]
_SCRIPT = _BACKEND / "scripts" / "data_quality_amount_mismatch_scan.py"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "DATABASE_URL": os.environ["DATABASE_URL"]}
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        cwd=_BACKEND,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_cli_fails_closed_without_dry_run_and_dry_run_is_json_read_only(db):
    before = (
        db.scalar(select(func.count()).select_from(FactDataQualityIssue)),
        db.scalar(select(func.count()).select_from(SysAuditLog)),
    )

    denied = _run()
    assert denied.returncode != 0
    assert "--dry-run" in denied.stderr

    allowed = _run("--dry-run", "--side", "purchase", "--sample-limit", "3")
    assert allowed.returncode == 0, allowed.stderr
    payload = json.loads(allowed.stdout)
    assert payload["dry_run"] is True
    assert payload["side"] == "purchase"
    assert payload["sample_limit"] == 3

    db.expire_all()
    assert (
        db.scalar(select(func.count()).select_from(FactDataQualityIssue)),
        db.scalar(select(func.count()).select_from(SysAuditLog)),
    ) == before

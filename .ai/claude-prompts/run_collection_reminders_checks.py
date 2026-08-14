#!/usr/bin/env python3
"""Run only reviewed collection-reminder checks; never evaluate shell text."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Final
from urllib.parse import unquote, urlsplit


ROOT: Final = Path(__file__).resolve().parents[2]
BACKEND: Final = ROOT / "backend"
FRONTEND: Final = ROOT / "frontend"
LOCAL_DATABASE_CHECK_IDS: Final[frozenset[str]] = frozenset(
    {
        "k0-migration",
        "k0-focused",
        "k0-alembic-rehearsal",
        "reminder-backend",
        "xls-import",
        "integration-backend",
        "integration-backend-full",
    }
)
LOCAL_TEST_DATABASE_RE: Final = re.compile(
    r"spareparts_test(?:_[A-Za-z0-9_]+)?\Z"
)
REHEARSAL_CHILD_ENV: Final = "COLLECTION_REMINDERS_REHEARSAL_CHILD"


COMMANDS: Final[dict[str, tuple[Path, list[str]]]] = {
    "k0-migration": (
        BACKEND,
        [
            "uv", "run", "--frozen", "--no-sync", "--extra", "dev", "pytest", "-q",
            "tests/test_maintenance_collection_reminders_migration.py",
            "tests/test_maintenance_manager_workbook_v3_migration.py",
            "tests/test_permission_center.py",
        ],
    ),
    "k0-focused": (
        BACKEND,
        [
            "uv", "run", "--frozen", "--no-sync", "--extra", "dev", "pytest", "-q",
            "tests/test_maintenance_collection_reminders_migration.py",
            "tests/test_maintenance_collection_milestones.py",
            "tests/test_maintenance_collection_reminder_schemas.py",
            "tests/test_maintenance_manager_workbook_v3_adapter.py",
            "tests/test_maintenance_manager_workbook_v3_migration.py",
            "tests/test_permission_center.py",
        ],
    ),
    "k0-alembic-heads": (
        BACKEND,
        ["uv", "run", "--frozen", "--no-sync", "--extra", "dev", "alembic", "heads"],
    ),
    "reminder-backend": (
        BACKEND,
        [
            "uv", "run", "--frozen", "--no-sync", "--extra", "dev", "pytest", "-q",
            "tests/test_maintenance_collection_reminder_logic.py",
            "tests/test_maintenance_collection_reminders_api.py",
            "tests/test_maintenance_manager_tracking_board.py",
        ],
    ),
    "xls-parser": (
        BACKEND,
        [
            "uv", "run", "--frozen", "--no-sync", "--extra", "dev", "pytest", "-q",
            "tests/test_maintenance_collection_plan_xls.py",
        ],
    ),
    "xls-import": (
        BACKEND,
        [
            "uv", "run", "--frozen", "--no-sync", "--extra", "dev", "pytest", "-q",
            "tests/test_maintenance_collection_plan_imports.py",
            "tests/test_maintenance_collection_plan_xls.py",
            "tests/test_maintenance_collection_milestones.py",
            "tests/test_maintenance_collection_plan_upload_security.py",
        ],
    ),
    "frontend-api": (
        FRONTEND,
        ["npm", "test", "--", "maintenanceCollectionReminders", "maintenanceNavigation"],
    ),
    "frontend-page": (
        FRONTEND,
        [
            "npm", "test", "--", "MaintenanceCollectionRemindersPage",
            "CollectionMilestoneFollowUpModal", "CollectionPlanImportModal",
            "maintenanceCollectionReminders", "maintenanceNavigation",
        ],
    ),
    "frontend-build": (FRONTEND, ["npm", "run", "build"]),
    "integration-backend": (
        BACKEND,
        [
            "uv", "run", "--frozen", "--no-sync", "--extra", "dev", "pytest", "-q",
            "tests/test_maintenance_collection_reminders_migration.py",
            "tests/test_maintenance_collection_milestones.py",
            "tests/test_maintenance_collection_reminder_logic.py",
            "tests/test_maintenance_collection_reminders_api.py",
            "tests/test_maintenance_collection_plan_xls.py",
            "tests/test_maintenance_collection_plan_imports.py",
            "tests/test_maintenance_collection_plan_upload_security.py",
            "tests/test_maintenance_manager_workbook_v3_adapter.py",
            "tests/test_maintenance_manager_workbooks_api.py",
            "tests/test_maintenance_manager_tracking_board.py",
            "tests/test_maintenance_beta_gate.py",
            "tests/test_permission_center.py",
            "tests/test_v120_release_control.py",
        ],
    ),
    "integration-backend-full": (
        BACKEND,
        ["uv", "run", "--frozen", "--no-sync", "--extra", "dev", "pytest", "-q"],
    ),
    "integration-frontend": (FRONTEND, ["npm", "test", "--", "--run"]),
    "git-diff-check": (ROOT, ["git", "diff", "--check"]),
    "sbom-check": (
        ROOT,
        ["python3", ".deploy/generate_dependency_sbom.py", "--check", "."],
    ),
}


def run(command: list[str], *, cwd: Path, stdout=None, env=None) -> None:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        check=False,
    )
    if completed.returncode:
        raise SystemExit(completed.returncode)


def require_local_test_database_url() -> None:
    raw = os.environ.get("DATABASE_URL")
    if not raw:
        raise SystemExit("DATABASE_URL must explicitly name a local test database")
    parsed = urlsplit(raw)
    database_name = unquote(parsed.path.removeprefix("/"))
    if (
        parsed.scheme != "postgresql+psycopg"
        or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or not LOCAL_TEST_DATABASE_RE.fullmatch(database_name)
        or parsed.query
        or parsed.fragment
    ):
        raise SystemExit(
            "DATABASE_URL must use postgresql+psycopg on a local host with a "
            "spareparts_test[_suffix] database and no query overrides"
        )


def rehearse_migrations() -> None:
    """Prove d9 -> head in an owned disposable local PostgreSQL database."""
    require_local_test_database_url()
    if os.environ.get(REHEARSAL_CHILD_ENV) != "1":
        child_env = os.environ.copy()
        child_env[REHEARSAL_CHILD_ENV] = "1"
        run(
            [
                "uv", "run", "--frozen", "--no-sync", "--extra", "dev",
                "python", str(Path(__file__).resolve()),
                "k0-alembic-rehearsal",
            ],
            cwd=BACKEND,
            env=child_env,
        )
        return

    sys.path.insert(0, str(BACKEND))
    try:
        from tests.run_isolation import (  # noqa: PLC0415
            cleanup_database_run,
            create_database_run,
        )
    finally:
        sys.path.pop(0)

    handle = None

    def record_owned_database(owned) -> None:
        nonlocal handle
        handle = owned

    try:
        created = create_database_run(
            os.environ["DATABASE_URL"],
            on_owned=record_owned_database,
        )
        if handle is None or created != handle:
            raise RuntimeError("migration rehearsal database handoff mismatch")
        child_env = os.environ.copy()
        child_env["DATABASE_URL"] = handle.database_url
        prefix = ["uv", "run", "--frozen", "--no-sync", "--extra", "dev"]
        run(
            [*prefix, "alembic", "upgrade", "d9f1a3c7e5b2"],
            cwd=BACKEND,
            env=child_env,
        )
        run(
            [*prefix, "alembic", "upgrade", "head"],
            cwd=BACKEND,
            env=child_env,
        )
        run(
            [*prefix, "alembic", "check"],
            cwd=BACKEND,
            env=child_env,
        )
    finally:
        if handle is not None:
            cleanup_database_run(handle)


def sync_dependencies() -> None:
    run(["uv", "lock"], cwd=BACKEND)
    run(["uv", "sync", "--frozen", "--extra", "dev"], cwd=BACKEND)
    requirements = BACKEND / "requirements.lock"
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=BACKEND,
            prefix=".collection-reminders-requirements-",
            delete=False,
        ) as output:
            temporary_name = output.name
            run(
                [
                    "uv", "export", "--frozen", "--no-dev",
                    "--no-emit-project", "--format", "requirements-txt",
                    "--no-header",
                ],
                cwd=BACKEND,
                stdout=output,
            )
        Path(temporary_name).replace(requirements)
        temporary_name = None
    finally:
        if temporary_name is not None:
            temporary = Path(temporary_name)
            if temporary.is_file():
                temporary.unlink()
    run(
        ["python3", ".deploy/generate_dependency_sbom.py", "--write", "."],
        cwd=ROOT,
    )
    run(
        ["python3", ".deploy/generate_dependency_sbom.py", "--check", "."],
        cwd=ROOT,
    )


def main() -> int:
    choices = sorted(
        [
            *COMMANDS,
            "k0-alembic-rehearsal",
            "k0-sync-dependencies",
            "final-sync-package-metadata",
        ]
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("check_id", choices=choices)
    args = parser.parse_args()

    if args.check_id in LOCAL_DATABASE_CHECK_IDS:
        require_local_test_database_url()

    if args.check_id == "k0-alembic-rehearsal":
        rehearse_migrations()
    elif args.check_id == "k0-sync-dependencies":
        sync_dependencies()
    elif args.check_id == "final-sync-package-metadata":
        run(["uv", "sync", "--frozen", "--extra", "dev"], cwd=BACKEND)
        run(
            [
                sys.executable,
                str(
                    ROOT
                    / ".ai/claude-prompts/freeze_collection_reminders_review.py"
                ),
                "metadata-receipt",
            ],
            cwd=ROOT,
        )
    else:
        cwd, command = COMMANDS[args.check_id]
        run(command, cwd=cwd)
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Run only reviewed collection-reminder checks; never evaluate shell text."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Final


ROOT: Final = Path(__file__).resolve().parents[2]
BACKEND: Final = ROOT / "backend"
FRONTEND: Final = ROOT / "frontend"


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
    "k0-alembic-check": (
        BACKEND,
        ["uv", "run", "--frozen", "--no-sync", "--extra", "dev", "alembic", "check"],
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


def run(command: list[str], *, cwd: Path, stdout=None) -> None:
    completed = subprocess.run(
        command,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        check=False,
    )
    if completed.returncode:
        raise SystemExit(completed.returncode)


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
        [*COMMANDS, "k0-sync-dependencies", "final-sync-package-metadata"]
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("check_id", choices=choices)
    args = parser.parse_args()

    if args.check_id == "k0-sync-dependencies":
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

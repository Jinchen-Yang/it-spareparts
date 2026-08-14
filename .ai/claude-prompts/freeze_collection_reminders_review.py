#!/usr/bin/env python3
"""Protect user files and freeze owner-separated diffs for independent review."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Final
import uuid


ROOT: Final = Path(__file__).resolve().parents[2]
BASELINE_FILE: Final = ROOT / ".git" / "maintenance-collection-reminders-user-files.json"
METADATA_RECEIPT_FILE: Final = (
    ROOT / ".git" / "maintenance-collection-reminders-metadata-sync.json"
)
REPAIR_DIR: Final = (
    ROOT / ".git" / "maintenance-collection-reminders-repairs"
)
USER_FILES: Final = (
    "docs/maintenance/维保模块员工操作培训手册.md",
    "docs/superpowers/plans/PR7-release-plan.md",
)

K0_PREFIXES: Final = (
    "backend/alembic/versions/c8e2a4f6b1d3_maintenance_collection_reminders.py",
    "backend/app/config.py",
    "backend/app/models/__init__.py",
    "backend/app/models/maintenance_manager.py",
    "backend/app/permissions.py",
    "backend/app/schemas/maintenance_collection_reminders.py",
    "backend/app/security.py",
    "backend/app/services/maintenance_collection_milestones.py",
    "backend/app/services/maintenance_manager_workbook_adapter.py",
    "backend/dependency-sbom.cdx.json",
    "backend/pyproject.toml",
    "backend/requirements.lock",
    "backend/spareparts_backend.egg-info/",
    "backend/tests/conftest.py",
    "backend/tests/test_maintenance_collection_milestones.py",
    "backend/tests/test_maintenance_collection_reminder_schemas.py",
    "backend/tests/test_maintenance_collection_reminders_migration.py",
    "backend/tests/test_maintenance_manager_workbook_v3_adapter.py",
    "backend/tests/test_maintenance_manager_workbook_v3_migration.py",
    "backend/tests/test_permission_center.py",
    "backend/tests/test_pools_api.py",
    "backend/uv.lock",
)
REMINDER_BACKEND_PREFIXES: Final = (
    "backend/app/api/maintenance_collection_reminders.py",
    "backend/app/services/maintenance_collection_reminders.py",
    "backend/tests/test_maintenance_collection_reminder_logic.py",
    "backend/tests/test_maintenance_collection_reminders_api.py",
)
XLS_IMPORTER_PREFIXES: Final = (
    "backend/app/api/maintenance_collection_plan_imports.py",
    "backend/app/services/maintenance_collection_plan_imports.py",
    "backend/app/services/maintenance_collection_plan_xls.py",
    "backend/tests/test_maintenance_collection_plan_imports.py",
    "backend/tests/test_maintenance_collection_plan_upload_security.py",
    "backend/tests/test_maintenance_collection_plan_xls.py",
)
FRONTEND_PREFIXES: Final = (
    "frontend/src/api/maintenanceCollectionReminders.ts",
    "frontend/src/api/__tests__/maintenanceCollectionReminders.test.ts",
    "frontend/src/components/maintenance/CollectionReminderDetail.tsx",
    "frontend/src/components/maintenance/CollectionMilestoneFollowUpModal.tsx",
    "frontend/src/components/maintenance/CollectionPlanImportModal.tsx",
    "frontend/src/components/maintenance/__tests__/CollectionMilestoneFollowUpModal.test.tsx",
    "frontend/src/components/maintenance/__tests__/CollectionPlanImportModal.test.tsx",
    "frontend/src/components/maintenance/maintenanceCollectionReminders.css",
    "frontend/src/components/maintenance/maintenanceLanguage.ts",
    "frontend/src/components/maintenance/maintenancePermissions.ts",
    "frontend/src/nav.tsx",
    "frontend/src/__tests__/maintenanceNavigation.test.tsx",
    "frontend/src/pages/maintenance/MaintenanceCollectionRemindersPage.tsx",
    "frontend/src/pages/maintenance/__tests__/MaintenanceCollectionRemindersPage.test.tsx",
)
INTEGRATION_PREFIXES: Final = (
    "backend/app/main.py",
    "backend/tests/test_maintenance_beta_gate.py",
)
METADATA_PREFIXES: Final = ("backend/spareparts_backend.egg-info/",)
DEPENDENCY_SENSITIVE_PATHS: Final = (
    "backend/pyproject.toml",
    "backend/uv.lock",
    "backend/requirements.lock",
    "backend/dependency-sbom.cdx.json",
)
OWNERS_BY_WAVE: Final = {
    "k0": {"schema_integrator": K0_PREFIXES},
    "parallel": {
        "reminder_backend": REMINDER_BACKEND_PREFIXES,
        "xls_importer": XLS_IMPORTER_PREFIXES,
        "reminder_frontend": FRONTEND_PREFIXES,
    },
    "metadata": {"outer_codex": METADATA_PREFIXES},
    "integration": {"schema_integrator": INTEGRATION_PREFIXES},
}
OUTER_CONTRACT_PREFIXES: Final = (
    ".ai/MAINTENANCE_COLLECTION_REMINDERS_DESIGN.md",
    ".ai/MAINTENANCE_COLLECTION_REMINDERS_IMPLEMENTATION_PLAN.md",
    ".ai/contracts/maintenance-collections/project-manager-xls-v1.yaml",
    ".ai/contracts/maintenance-collections/collection-reminders-api-v1.yaml",
    ".ai/claude-prompts/freeze_collection_reminders_review.py",
    ".ai/claude-prompts/guard_collection_reminders_tool.py",
    ".ai/claude-prompts/maintenance-collection-reminders-agents.json",
    ".ai/claude-prompts/maintenance-collection-reminders-wave-integration.md",
    ".ai/claude-prompts/maintenance-collection-reminders-wave-k0.md",
    ".ai/claude-prompts/maintenance-collection-reminders-wave-parallel.md",
    ".ai/claude-prompts/maintenance-collection-reminders-wave-repair.md",
    ".ai/claude-prompts/maintenance-collection-reminders-wave-review.md",
    ".ai/claude-prompts/run_collection_reminders_checks.py",
    ".ai/claude-prompts/run_collection_reminders_claude.py",
)
REPAIR_PREFIXES_BY_OWNER: Final = {
    "schema_integrator": tuple(
        dict.fromkeys([*K0_PREFIXES, *INTEGRATION_PREFIXES])
    ),
    "reminder_backend": REMINDER_BACKEND_PREFIXES,
    "xls_importer": XLS_IMPORTER_PREFIXES,
    "reminder_frontend": FRONTEND_PREFIXES,
    "outer_codex": OUTER_CONTRACT_PREFIXES,
}
FINAL_PREFIXES: Final = tuple(
    dict.fromkeys(
        [
            *OUTER_CONTRACT_PREFIXES,
            *(
                rule
                for owners in OWNERS_BY_WAVE.values()
                for rules in owners.values()
                for rule in rules
            ),
        ]
    )
)
FINAL_STEM: Final = "maintenance-collection-reminders-final-outer_codex"


def run(*args: str) -> bytes:
    return subprocess.check_output(args, cwd=ROOT)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def user_file_state(relative: str) -> dict[str, object]:
    target = ROOT / relative
    if not target.exists() and not target.is_symlink():
        return {"exists": False}
    metadata = target.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"protected path is not a regular file: {relative}")
    return {
        "exists": True,
        "mode": stat.S_IMODE(metadata.st_mode),
        "size": metadata.st_size,
        "sha256": sha256(target),
    }


def write_user_baseline() -> None:
    payload = {
        "schema": "maintenance-collection-reminders-user-files-v2",
        "run_id": uuid.uuid4().hex,
        "baseline_head": run("git", "rev-parse", "HEAD").decode("ascii").strip(),
        "files": {relative: user_file_state(relative) for relative in USER_FILES},
    }
    BASELINE_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(BASELINE_FILE.relative_to(ROOT))


def verify_user_baseline() -> dict[str, object]:
    if not BASELINE_FILE.is_file():
        raise ValueError("protected user-file baseline is missing; run baseline first")
    payload = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
    if payload.get("schema") != "maintenance-collection-reminders-user-files-v2":
        raise ValueError("protected user-file baseline schema is invalid")
    run_id = payload.get("run_id")
    if not isinstance(run_id, str) or not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise ValueError("protected user-file baseline run id is invalid")
    baseline_head = payload.get("baseline_head")
    if not isinstance(baseline_head, str) or not re.fullmatch(
        r"[0-9a-f]{40}", baseline_head
    ):
        raise ValueError("protected user-file baseline HEAD is invalid")
    exists = subprocess.run(
        ["git", "cat-file", "-e", f"{baseline_head}^{{commit}}"],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0
    if not exists:
        raise ValueError("protected user-file baseline HEAD is missing")
    expected = payload.get("files")
    if not isinstance(expected, dict) or set(expected) != set(USER_FILES):
        raise ValueError("protected user-file baseline path set is invalid")
    changed = [
        relative
        for relative in USER_FILES
        if expected[relative] != user_file_state(relative)
    ]
    if changed:
        raise ValueError("protected user files changed: " + ", ".join(changed))
    return payload


def metadata_file_state() -> dict[str, str]:
    state: dict[str, str] = {}
    for prefix in METADATA_PREFIXES:
        target = ROOT / prefix
        if target.is_symlink():
            raise ValueError(f"metadata path is symlinked: {prefix}")
        if not target.exists():
            continue
        for path in sorted(target.rglob("*")):
            if path.is_symlink():
                raise ValueError(
                    "metadata child path is symlinked: "
                    + path.relative_to(ROOT).as_posix()
                )
            if path.is_file():
                state[path.relative_to(ROOT).as_posix()] = sha256(path)
    return state


def write_metadata_sync_receipt() -> None:
    baseline = verify_user_baseline()
    payload = {
        "schema": "maintenance-collection-reminders-metadata-sync-v1",
        "run_id": baseline["run_id"],
        "base_head": run("git", "rev-parse", "HEAD").decode("ascii").strip(),
        "metadata_files": metadata_file_state(),
    }
    METADATA_RECEIPT_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(METADATA_RECEIPT_FILE.relative_to(ROOT))


def verify_metadata_sync_receipt(*, expected_base_head: str) -> str:
    baseline = verify_user_baseline()
    if not METADATA_RECEIPT_FILE.is_file():
        raise ValueError("metadata sync receipt is missing")
    receipt = json.loads(METADATA_RECEIPT_FILE.read_text(encoding="utf-8"))
    expected = {
        "schema": "maintenance-collection-reminders-metadata-sync-v1",
        "run_id": baseline["run_id"],
        "base_head": expected_base_head,
        "metadata_files": metadata_file_state(),
    }
    if receipt != expected:
        raise ValueError("metadata sync receipt is stale or metadata drifted")
    return sha256(METADATA_RECEIPT_FILE)


def verify_metadata_freshness(*, metadata_base_head: str) -> None:
    reviewed_head = run("git", "rev-parse", "HEAD").decode("ascii").strip()
    latest = run(
        "git",
        "rev-list",
        "-1",
        reviewed_head,
        "--",
        *DEPENDENCY_SENSITIVE_PATHS,
    ).decode("ascii").strip()
    if latest:
        require_ancestor(latest, metadata_base_head)


def required_repair_trailers(
    *, run_id: str, finding_id: str, finding_digest: str, owner: str
) -> dict[str, str]:
    return {
        "Collection-Reminder-Run-ID": run_id,
        "Collection-Reminder-Finding-ID": finding_id,
        "Collection-Reminder-Finding-SHA256": finding_digest,
        "Collection-Reminder-Repair-Owner": owner,
    }


def verify_repair_commit_trailers(
    *, head: str, run_id: str, finding_id: str, finding_digest: str, owner: str
) -> None:
    commit_message = run("git", "show", "-s", "--format=%B", head).decode(
        "utf-8", "strict"
    )
    for key, value in required_repair_trailers(
        run_id=run_id,
        finding_id=finding_id,
        finding_digest=finding_digest,
        owner=owner,
    ).items():
        matches = [
            line.split(":", 1)[1].strip()
            for line in commit_message.splitlines()
            if line.startswith(f"{key}:")
        ]
        if matches != [value]:
            raise ValueError(f"repair commit trailer is invalid: {key}")


def write_repair_closure(finding_id: str) -> None:
    """Freeze the exact committed segment that closed one reviewed finding."""

    baseline = verify_user_baseline()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", finding_id):
        raise ValueError("repair finding id is invalid")
    finding_path = REPAIR_DIR / f"{finding_id}.json"
    receipt_path = REPAIR_DIR / f"{finding_id}-launch.receipt"
    archived_manifest = REPAIR_DIR / f"{finding_id}-base.sha256"
    if any(
        path.is_symlink() or not path.is_file()
        for path in (finding_path, receipt_path, archived_manifest)
    ):
        raise ValueError("repair finding, receipt, or base manifest is missing")
    finding = json.loads(finding_path.read_text(encoding="utf-8"))
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    finding_digest = sha256(finding_path)
    owner = finding.get("owner")
    if (
        finding.get("schema") != "maintenance-collection-reminders-repair-v1"
        or finding.get("finding_id") != finding_id
        or owner not in REPAIR_PREFIXES_BY_OWNER
        or finding.get("run_id") != baseline["run_id"]
        or receipt.get("schema")
        != "maintenance-collection-reminders-repair-launch-v1"
        or receipt.get("finding_id") != finding_id
        or receipt.get("run_id") != baseline["run_id"]
        or receipt.get("owner") != owner
        or receipt.get("finding_sha256") != finding_digest
        or receipt.get("claude_writer_started") is not (owner != "outer_codex")
    ):
        raise ValueError("repair launch evidence is invalid")
    validate_repair_anchors(str(owner), finding.get("anchors"))
    archived_headers: dict[str, str] = {}
    for line in archived_manifest.read_text(encoding="utf-8").splitlines():
        if line.startswith("# ") and "=" in line:
            key, value = line[2:].split("=", 1)
            archived_headers[key] = value
    prepared_head = archived_headers.get("reviewed_head", "")
    if (
        not re.fullmatch(r"[0-9a-f]{40}", prepared_head)
        or receipt.get("prepared_head") != prepared_head
    ):
        raise ValueError("repair prepared HEAD is invalid")
    resolved_head = run("git", "rev-parse", "HEAD").decode("ascii").strip()
    require_ancestor(prepared_head, resolved_head)
    verify_repair_commit_trailers(
        head=resolved_head,
        run_id=str(baseline["run_id"]),
        finding_id=finding_id,
        finding_digest=finding_digest,
        owner=str(owner),
    )
    uncommitted = [
        relative for relative in changed_paths() if relative not in USER_FILES
    ]
    if uncommitted:
        raise ValueError(
            "repair closure requires committed changes: " + ", ".join(uncommitted)
        )
    paths = committed_diff_paths(prepared_head, resolved_head)
    if not paths:
        raise ValueError("repair closure is empty")
    unexpected = [
        relative
        for relative in paths
        if not any(
            matches_rule(relative, rule)
            for rule in REPAIR_PREFIXES_BY_OWNER[str(owner)]
        )
    ]
    if unexpected:
        raise ValueError("repair changed non-owner paths: " + ", ".join(unexpected))
    segment_patch = run("git", "diff", "--binary", prepared_head, resolved_head)
    payload = {
        "schema": "maintenance-collection-reminders-repair-closure-v1",
        "finding_id": finding_id,
        "owner": owner,
        "run_id": baseline["run_id"],
        "prepared_head": prepared_head,
        "resolved_head": resolved_head,
        "finding_sha256": finding_digest,
        "changed_paths": paths,
        "segment_patch_sha256": hashlib.sha256(segment_patch).hexdigest(),
    }
    closure_path = REPAIR_DIR / f"{finding_id}-repair.closure"
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if closure_path.exists():
        if closure_path.is_symlink() or not closure_path.is_file():
            raise ValueError("repair closure path is invalid")
        if closure_path.read_text(encoding="utf-8") != encoded:
            raise ValueError("repair closure conflicts with existing evidence")
        print(closure_path.relative_to(ROOT))
        return
    closure_path.write_text(encoded, encoding="utf-8")
    print(closure_path.relative_to(ROOT))


def changed_paths() -> list[str]:
    tracked = run("git", "diff", "--name-only", "-z", "HEAD").split(b"\0")
    untracked = run(
        "git", "ls-files", "--others", "--exclude-standard", "-z"
    ).split(b"\0")
    return sorted(
        {
            raw.decode("utf-8", "surrogateescape")
            for raw in [*tracked, *untracked]
            if raw
        }
    )


def matches_rule(relative: str, rule: str) -> bool:
    return relative == rule or (rule.endswith("/") and relative.startswith(rule))


def validate_repair_anchors(owner: str, anchors: object) -> None:
    if (
        owner not in REPAIR_PREFIXES_BY_OWNER
        or not isinstance(anchors, list)
        or not anchors
    ):
        raise ValueError("repair finding anchors are invalid")
    for anchor in anchors:
        if not isinstance(anchor, str):
            raise ValueError("repair finding anchors are invalid")
        match = re.fullmatch(
            r"([^:\n]+):([1-9][0-9]*)(?:-([1-9][0-9]*))?", anchor
        )
        if match is None:
            raise ValueError("repair finding anchors must be path:line[-line]")
        relative = match.group(1)
        start_line = int(match.group(2))
        end_line = int(match.group(3)) if match.group(3) else start_line
        if end_line < start_line:
            raise ValueError("repair finding anchor range is reversed")
        path = Path(relative)
        if (
            path.is_absolute()
            or "\\" in relative
            or any(part in {"", ".", ".."} for part in path.parts)
            or not any(
                matches_rule(relative, rule)
                for rule in REPAIR_PREFIXES_BY_OWNER[owner]
            )
        ):
            raise ValueError(f"repair finding anchor is outside owner scope: {anchor}")


def resolve_owner(wave: str, relative: str) -> str | None:
    matches = [
        owner
        for owner, prefixes in OWNERS_BY_WAVE[wave].items()
        if any(
            matches_rule(relative, prefix)
            for prefix in prefixes
        )
    ]
    if len(matches) > 1:
        raise ValueError(f"path matches multiple owners: {relative}: {matches}")
    return matches[0] if matches else None


def file_patch(relative: str) -> bytes:
    target = ROOT / relative
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", relative],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0
    if tracked:
        return run("git", "diff", "--binary", "HEAD", "--", relative)
    if not target.is_file():
        raise ValueError(f"untracked review path is not a regular file: {relative}")
    diff = subprocess.run(
        ["git", "diff", "--no-index", "--binary", "/dev/null", relative],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if diff.returncode not in (0, 1):
        raise RuntimeError(diff.stderr.decode("utf-8", "replace"))
    return diff.stdout


def committed_diff_paths(base_head: str, reviewed_head: str) -> list[str]:
    raw_paths = run(
        "git", "diff", "--name-only", "-z", base_head, reviewed_head
    ).split(b"\0")
    return sorted(
        raw.decode("utf-8", "surrogateescape") for raw in raw_paths if raw
    )


def is_ancestor(base_head: str, reviewed_head: str) -> bool:
    return subprocess.run(
        ["git", "merge-base", "--is-ancestor", base_head, reviewed_head],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0


def require_ancestor(base_head: str, reviewed_head: str) -> None:
    if not is_ancestor(base_head, reviewed_head):
        raise ValueError("review baseline is not an ancestor of reviewed HEAD")


def freeze_final() -> None:
    baseline = verify_user_baseline()
    base_head = str(baseline["baseline_head"])
    reviewed_head = run("git", "rev-parse", "HEAD").decode("ascii").strip()
    require_ancestor(base_head, reviewed_head)
    uncommitted = [
        relative for relative in changed_paths() if relative not in USER_FILES
    ]
    if uncommitted:
        raise ValueError(
            "final cumulative package requires committed implementation: "
            + ", ".join(uncommitted)
        )
    paths = committed_diff_paths(base_head, reviewed_head)
    if not paths:
        raise ValueError("final cumulative package is empty")
    unexpected = [
        relative
        for relative in paths
        if not any(matches_rule(relative, rule) for rule in FINAL_PREFIXES)
    ]
    if unexpected:
        raise ValueError(
            "unexpected committed implementation paths: " + ", ".join(unexpected)
        )
    patch = ROOT / ".git" / f"{FINAL_STEM}.patch"
    manifest = ROOT / ".git" / f"{FINAL_STEM}.sha256"
    patch.write_bytes(run("git", "diff", "--binary", base_head, reviewed_head))
    manifest_lines: list[str] = []
    for relative in paths:
        target = ROOT / relative
        manifest_lines.append(
            f"{sha256(target)}  {relative}"
            if target.is_file()
            else f"DELETED  {relative}"
        )
    header = [
        "# schema=maintenance-collection-reminders-final-review-v1",
        f"# base_head={base_head}",
        f"# reviewed_head={reviewed_head}",
        "# wave=final",
        "# owner=outer_codex",
        f"# run_id={baseline['run_id']}",
        f"# changed_paths={len(manifest_lines)}",
        f"# patch_sha256={sha256(patch)}",
        "# supersedes_all_owner_packages=true",
    ]
    manifest.write_text(
        "\n".join([*header, *manifest_lines]) + "\n", encoding="utf-8"
    )
    print(manifest.relative_to(ROOT))
    print(patch.relative_to(ROOT))


def verify_final_package() -> list[str]:
    baseline = verify_user_baseline()
    uncommitted = [
        relative for relative in changed_paths() if relative not in USER_FILES
    ]
    if uncommitted:
        raise ValueError(
            "final review workspace is moving: " + ", ".join(uncommitted)
        )
    manifest = ROOT / ".git" / f"{FINAL_STEM}.sha256"
    patch = ROOT / ".git" / f"{FINAL_STEM}.patch"
    if not manifest.is_file() or not patch.is_file():
        raise ValueError(f"review package missing: {FINAL_STEM}")
    headers: dict[str, str] = {}
    entries: list[str] = []
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if line.startswith("# ") and "=" in line:
            key, value = line[2:].split("=", 1)
            headers[key] = value
        elif line:
            entries.append(line)
    reviewed_head = run("git", "rev-parse", "HEAD").decode("ascii").strip()
    base_head = str(baseline["baseline_head"])
    expected_headers = {
        "schema": "maintenance-collection-reminders-final-review-v1",
        "base_head": base_head,
        "reviewed_head": reviewed_head,
        "wave": "final",
        "owner": "outer_codex",
        "run_id": str(baseline["run_id"]),
        "changed_paths": str(len(entries)),
        "patch_sha256": sha256(patch),
        "supersedes_all_owner_packages": "true",
    }
    for key, value in expected_headers.items():
        if headers.get(key) != value:
            raise ValueError(f"final review package header mismatch: {key}")
    require_ancestor(base_head, reviewed_head)
    paths = committed_diff_paths(base_head, reviewed_head)
    if not paths or len(entries) != len(paths):
        raise ValueError("final review package path count mismatch")
    entry_paths: list[str] = []
    for entry in entries:
        if entry.startswith("DELETED  "):
            relative = entry.removeprefix("DELETED  ")
            if (ROOT / relative).exists():
                raise ValueError(f"deleted final review path reappeared: {relative}")
        else:
            match = re.fullmatch(r"([0-9a-f]{64})  (.+)", entry)
            if match is None:
                raise ValueError("final review package entry invalid")
            digest, relative = match.groups()
            target = ROOT / relative
            if not target.is_file() or sha256(target) != digest:
                raise ValueError(f"final reviewed file drifted: {relative}")
        entry_paths.append(relative)
    if sorted(entry_paths) != paths:
        raise ValueError("final review package path set mismatch")
    expected_patch = run("git", "diff", "--binary", base_head, reviewed_head)
    if patch.read_bytes() != expected_patch:
        raise ValueError("final cumulative patch does not match reviewed commits")
    unexpected = [
        relative
        for relative in paths
        if not any(matches_rule(relative, rule) for rule in FINAL_PREFIXES)
    ]
    if unexpected:
        raise ValueError(
            "unexpected final implementation paths: " + ", ".join(unexpected)
        )
    return entries


def verify_repair_artifacts(*, baseline: dict[str, object]) -> None:
    if not REPAIR_DIR.exists():
        return
    if REPAIR_DIR.is_symlink() or not REPAIR_DIR.is_dir():
        raise ValueError("repair evidence directory is invalid")
    finding_paths = sorted(REPAIR_DIR.glob("*.json"))
    expected_names: set[str] = set()
    repair_segments: list[tuple[str, str, str]] = []
    for finding_path in finding_paths:
        if finding_path.is_symlink() or not finding_path.is_file():
            raise ValueError("repair finding path is invalid")
        finding = json.loads(finding_path.read_text(encoding="utf-8"))
        finding_id = finding.get("finding_id")
        if (
            not isinstance(finding_id, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", finding_id)
            or finding_path.name != f"{finding_id}.json"
        ):
            raise ValueError("repair finding id is invalid")
        owner = finding.get("owner")
        if owner not in REPAIR_PREFIXES_BY_OWNER:
            raise ValueError(f"repair finding owner is invalid: {finding_id}")
        if finding.get("schema") != "maintenance-collection-reminders-repair-v1":
            raise ValueError(f"repair finding schema is invalid: {finding_id}")
        if finding.get("severity") not in {"P0", "P1"}:
            raise ValueError(f"repair finding severity is invalid: {finding_id}")
        if finding.get("run_id") != baseline["run_id"]:
            raise ValueError(f"repair finding run id is stale: {finding_id}")
        summary = finding.get("summary")
        anchors = finding.get("anchors")
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 10000:
            raise ValueError(f"repair finding summary is invalid: {finding_id}")
        if not isinstance(anchors, list) or not anchors or any(
            not isinstance(anchor, str) or not anchor for anchor in anchors
        ):
            raise ValueError(f"repair finding anchors are invalid: {finding_id}")
        validate_repair_anchors(str(owner), anchors)
        archived_patch = REPAIR_DIR / f"{finding_id}-base.patch"
        archived_manifest = REPAIR_DIR / f"{finding_id}-base.sha256"
        expected_names.update(
            {finding_path.name, archived_patch.name, archived_manifest.name}
        )
        if not archived_patch.is_file() or not archived_manifest.is_file():
            raise ValueError(f"repair base package is missing: {finding_id}")
        if (
            sha256(archived_patch) != finding.get("source_final_patch_sha256")
            or sha256(archived_manifest)
            != finding.get("source_final_manifest_sha256")
        ):
            raise ValueError(f"repair base package hash mismatch: {finding_id}")
        archived_headers: dict[str, str] = {}
        for line in archived_manifest.read_text(encoding="utf-8").splitlines():
            if line.startswith("# ") and "=" in line:
                key, value = line[2:].split("=", 1)
                archived_headers[key] = value
        if (
            archived_headers.get("schema")
            != "maintenance-collection-reminders-final-review-v1"
            or archived_headers.get("run_id") != baseline["run_id"]
            or archived_headers.get("patch_sha256")
            != finding.get("source_final_patch_sha256")
        ):
            raise ValueError(f"repair base manifest is invalid: {finding_id}")
        reviewed_head = archived_headers.get("reviewed_head", "")
        if not re.fullmatch(r"[0-9a-f]{40}", reviewed_head):
            raise ValueError(f"repair base reviewed HEAD is invalid: {finding_id}")
        require_ancestor(str(baseline["baseline_head"]), reviewed_head)
        receipt_path = REPAIR_DIR / f"{finding_id}-launch.receipt"
        closure_path = REPAIR_DIR / f"{finding_id}-repair.closure"
        expected_names.update({receipt_path.name, closure_path.name})
        if receipt_path.is_symlink() or not receipt_path.is_file():
            raise ValueError(f"repair launch receipt is missing: {finding_id}")
        if closure_path.is_symlink() or not closure_path.is_file():
            raise ValueError(f"repair closure is missing: {finding_id}")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        launcher_bytes = run(
            "git",
            "show",
            f"{reviewed_head}:.ai/claude-prompts/run_collection_reminders_claude.py",
        )
        expected_receipt = {
            "schema": "maintenance-collection-reminders-repair-launch-v1",
            "finding_id": finding_id,
            "owner": owner,
            "run_id": baseline["run_id"],
            "prepared_head": reviewed_head,
            "finding_sha256": sha256(finding_path),
            "source_final_patch_sha256": finding["source_final_patch_sha256"],
            "source_final_manifest_sha256": finding[
                "source_final_manifest_sha256"
            ],
            "launcher_sha256": hashlib.sha256(launcher_bytes).hexdigest(),
            "claude_writer_started": owner != "outer_codex",
        }
        if receipt != expected_receipt:
            raise ValueError(f"repair launch receipt is invalid: {finding_id}")
        closure = json.loads(closure_path.read_text(encoding="utf-8"))
        resolved_head = closure.get("resolved_head")
        if not isinstance(resolved_head, str) or not re.fullmatch(
            r"[0-9a-f]{40}", resolved_head
        ):
            raise ValueError(f"repair resolved HEAD is invalid: {finding_id}")
        require_ancestor(reviewed_head, resolved_head)
        current_head = run("git", "rev-parse", "HEAD").decode("ascii").strip()
        require_ancestor(resolved_head, current_head)
        repair_paths = committed_diff_paths(reviewed_head, resolved_head)
        unexpected = [
            relative
            for relative in repair_paths
            if not any(
                matches_rule(relative, rule)
                for rule in REPAIR_PREFIXES_BY_OWNER[str(owner)]
            )
        ]
        if not repair_paths or unexpected:
            raise ValueError(f"repair path scope is invalid: {finding_id}")
        segment_patch = run("git", "diff", "--binary", reviewed_head, resolved_head)
        expected_closure = {
            "schema": "maintenance-collection-reminders-repair-closure-v1",
            "finding_id": finding_id,
            "owner": owner,
            "run_id": baseline["run_id"],
            "prepared_head": reviewed_head,
            "resolved_head": resolved_head,
            "finding_sha256": sha256(finding_path),
            "changed_paths": repair_paths,
            "segment_patch_sha256": hashlib.sha256(segment_patch).hexdigest(),
        }
        if closure != expected_closure:
            raise ValueError(f"repair closure is invalid: {finding_id}")
        verify_repair_commit_trailers(
            head=resolved_head,
            run_id=str(baseline["run_id"]),
            finding_id=str(finding_id),
            finding_digest=sha256(finding_path),
            owner=str(owner),
        )
        repair_segments.append((str(finding_id), reviewed_head, resolved_head))
    for index, (left_id, left_prepared, left_resolved) in enumerate(
        repair_segments
    ):
        for right_id, right_prepared, right_resolved in repair_segments[index + 1 :]:
            if not (
                is_ancestor(left_resolved, right_prepared)
                or is_ancestor(right_resolved, left_prepared)
            ):
                raise ValueError(
                    "repair segments overlap or are out of order: "
                    f"{left_id}, {right_id}"
                )
    actual_names = {
        path.name
        for path in REPAIR_DIR.iterdir()
        if path.is_file() or path.is_symlink()
    }
    if actual_names != expected_names:
        raise ValueError("repair evidence contains orphan or unexpected files")


def freeze_wave(wave: str) -> None:
    baseline = verify_user_baseline()
    base_head = run("git", "rev-parse", "HEAD").decode("ascii").strip()
    metadata_receipt_sha = (
        verify_metadata_sync_receipt(expected_base_head=base_head)
        if wave == "metadata"
        else None
    )
    by_owner: dict[str, list[str]] = {
        owner: [] for owner in OWNERS_BY_WAVE[wave]
    }
    unexpected: list[str] = []
    for relative in changed_paths():
        if relative in USER_FILES:
            continue
        owner = resolve_owner(wave, relative)
        if owner is None:
            unexpected.append(relative)
        else:
            by_owner[owner].append(relative)
    if unexpected:
        raise ValueError("unexpected workspace changes: " + ", ".join(unexpected))

    for owner, paths in by_owner.items():
        manifest = (
            ROOT / ".git" /
            f"maintenance-collection-reminders-{wave}-{owner}.sha256"
        )
        patch = (
            ROOT / ".git" /
            f"maintenance-collection-reminders-{wave}-{owner}.patch"
        )
        manifest_lines: list[str] = []
        patch_parts: list[bytes] = []
        for relative in sorted(paths):
            target = ROOT / relative
            if target.is_file():
                manifest_lines.append(f"{sha256(target)}  {relative}")
            else:
                manifest_lines.append(f"DELETED  {relative}")
            patch_parts.append(file_patch(relative))
        patch.write_bytes(b"".join(patch_parts))
        header = [
            "# schema=maintenance-collection-reminders-review-v1",
            f"# base_head={base_head}",
            f"# wave={wave}",
            f"# owner={owner}",
            f"# run_id={baseline['run_id']}",
            f"# changed_paths={len(manifest_lines)}",
            f"# patch_sha256={sha256(patch)}",
        ]
        if metadata_receipt_sha is not None:
            header.append(f"# metadata_receipt_sha256={metadata_receipt_sha}")
        manifest.write_text(
            "\n".join([*header, *manifest_lines]) + "\n",
            encoding="utf-8",
        )
        print(manifest.relative_to(ROOT))
        print(patch.relative_to(ROOT))


def verify_packages() -> None:
    baseline = verify_user_baseline()
    packages: list[tuple[str, str, list[str]]] = []
    metadata_base_head: str | None = None
    for wave, owners in OWNERS_BY_WAVE.items():
        for owner in owners:
            stem = f"maintenance-collection-reminders-{wave}-{owner}"
            manifest = ROOT / ".git" / f"{stem}.sha256"
            patch = ROOT / ".git" / f"{stem}.patch"
            if not manifest.is_file() or not patch.is_file():
                raise ValueError(f"review package missing: {stem}")
            lines = manifest.read_text(encoding="utf-8").splitlines()
            headers: dict[str, str] = {}
            entries: list[str] = []
            for line in lines:
                if line.startswith("# ") and "=" in line:
                    key, value = line[2:].split("=", 1)
                    headers[key] = value
                elif line:
                    entries.append(line)
            expected_headers = {
                "schema": "maintenance-collection-reminders-review-v1",
                "wave": wave,
                "owner": owner,
                "run_id": str(baseline["run_id"]),
                "changed_paths": str(len(entries)),
                "patch_sha256": sha256(patch),
            }
            for key, value in expected_headers.items():
                if headers.get(key) != value:
                    raise ValueError(f"review package header mismatch: {stem}: {key}")
            base_head = headers.get("base_head", "")
            if not re.fullmatch(r"[0-9a-f]{40}", base_head):
                raise ValueError(f"review package base HEAD invalid: {stem}")
            exists = subprocess.run(
                ["git", "cat-file", "-e", f"{base_head}^{{commit}}"],
                cwd=ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            ).returncode == 0
            if not exists:
                raise ValueError(f"review package base HEAD missing: {stem}")
            empty_allowed = (wave, owner) == ("metadata", "outer_codex")
            if (not entries or patch.stat().st_size == 0) and not empty_allowed:
                raise ValueError(f"review package is empty: {stem}")
            if bool(entries) != bool(patch.stat().st_size):
                raise ValueError(f"review package path/patch mismatch: {stem}")
            if wave == "metadata":
                metadata_base_head = base_head
                receipt_sha = verify_metadata_sync_receipt(
                    expected_base_head=base_head
                )
                if headers.get("metadata_receipt_sha256") != receipt_sha:
                    raise ValueError(f"metadata receipt mismatch: {stem}")
            packages.append((wave, owner, entries))

    if metadata_base_head is None:
        raise ValueError("metadata review package is missing")
    verify_metadata_freshness(metadata_base_head=metadata_base_head)

    final_entries = verify_final_package()
    packages.append(("final", "outer_codex", final_entries))
    verify_repair_artifacts(baseline=baseline)

    # A generated file may have an intentional later writer.  For example,
    # dependency sync in K0 and the post-parallel metadata barrier can both
    # regenerate egg-info.  Earlier packages remain immutable evidence (their
    # manifest and patch hashes were checked above), while only the last
    # package that records a path is expected to match the current worktree.
    last_writer: dict[str, tuple[str, str]] = {}
    for wave, owner, entries in packages:
        for entry in entries:
            relative = (
                entry.removeprefix("DELETED  ")
                if entry.startswith("DELETED  ")
                else entry.split("  ", 1)[-1]
            )
            last_writer[relative] = (wave, owner)

    # The cumulative final package is authoritative for the reviewed commit.
    # Paths absent from its net diff may have been intentionally restored to
    # their baseline state by a post-review correction, so it supersedes every
    # path mentioned by an earlier owner package as well.
    for wave, owner, entries in packages:
        if wave == "final":
            continue
        for entry in entries:
            relative = (
                entry.removeprefix("DELETED  ")
                if entry.startswith("DELETED  ")
                else entry.split("  ", 1)[-1]
            )
            last_writer[relative] = ("final", "outer_codex")

    for wave, owner, entries in packages:
        stem = f"maintenance-collection-reminders-{wave}-{owner}"
        for entry in entries:
            relative = (
                entry.removeprefix("DELETED  ")
                if entry.startswith("DELETED  ")
                else entry.split("  ", 1)[-1]
            )
            is_last_writer = last_writer.get(relative) == (wave, owner)
            if entry.startswith("DELETED  "):
                if is_last_writer and (ROOT / relative).exists():
                    raise ValueError(f"deleted review path reappeared: {relative}")
                continue
            match = re.fullmatch(r"([0-9a-f]{64})  (.+)", entry)
            if match is None:
                raise ValueError(f"review package entry invalid: {stem}")
            digest, relative = match.groups()
            target = ROOT / relative
            if is_last_writer and (
                not target.is_file() or sha256(target) != digest
            ):
                raise ValueError(f"reviewed file drifted: {relative}")
    print("all frozen review packages verified")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=[
            "baseline",
            "verify",
            "verify-final",
            "verify-packages",
            "metadata-receipt",
            "repair-close",
            "final",
            *sorted(OWNERS_BY_WAVE),
        ],
    )
    parser.add_argument("--finding-id")
    args = parser.parse_args()
    try:
        if args.action == "repair-close":
            if not args.finding_id:
                raise ValueError("repair-close requires --finding-id")
            write_repair_closure(args.finding_id)
            return 0
        if args.finding_id:
            raise ValueError("--finding-id is only valid for repair-close")
        if args.action == "baseline":
            write_user_baseline()
        elif args.action == "verify":
            verify_user_baseline()
            print("protected user files verified")
        elif args.action == "verify-packages":
            verify_packages()
        elif args.action == "verify-final":
            verify_final_package()
            print("final cumulative review package verified")
        elif args.action == "metadata-receipt":
            write_metadata_sync_receipt()
        elif args.action == "final":
            freeze_final()
        else:
            freeze_wave(args.action)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

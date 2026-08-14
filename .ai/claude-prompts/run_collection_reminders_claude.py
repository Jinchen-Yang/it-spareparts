#!/usr/bin/env python3
"""Run one guarded Claude Code wave without shell interpolation."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
PROMPT_DIR = ROOT / ".ai" / "claude-prompts"
WAVES = {
    "k0": PROMPT_DIR / "maintenance-collection-reminders-wave-k0.md",
    "parallel": PROMPT_DIR / "maintenance-collection-reminders-wave-parallel.md",
    "integration": PROMPT_DIR / "maintenance-collection-reminders-wave-integration.md",
    "repair": PROMPT_DIR / "maintenance-collection-reminders-wave-repair.md",
    "review": PROMPT_DIR / "maintenance-collection-reminders-wave-review.md",
}
ALLOWED_CHECK_IDS_BY_WAVE = {
    "k0": (
        "k0-migration",
        "k0-focused",
        "k0-alembic-heads",
        "k0-alembic-rehearsal",
        "k0-sync-dependencies",
        "sbom-check",
        "git-diff-check",
    ),
    "parallel": (
        "reminder-backend",
        "xls-parser",
        "xls-import",
        "frontend-api",
        "frontend-page",
        "frontend-build",
        "sbom-check",
        "git-diff-check",
    ),
    "integration": (
        "integration-backend",
        "integration-backend-full",
        "integration-frontend",
        "frontend-build",
        "k0-alembic-heads",
        "k0-alembic-rehearsal",
        "sbom-check",
        "git-diff-check",
    ),
    "review": (),
}
REPAIR_ALLOWED_CHECK_IDS_BY_OWNER = {
    "schema_integrator": (
        "k0-migration",
        "k0-focused",
        "k0-alembic-heads",
        "k0-alembic-rehearsal",
        "k0-sync-dependencies",
        "integration-backend",
        "integration-backend-full",
        "sbom-check",
        "git-diff-check",
    ),
    "reminder_backend": (
        "reminder-backend",
        "integration-backend",
        "integration-backend-full",
        "sbom-check",
        "git-diff-check",
    ),
    "xls_importer": (
        "xls-parser",
        "xls-import",
        "integration-backend",
        "integration-backend-full",
        "sbom-check",
        "git-diff-check",
    ),
    "reminder_frontend": (
        "frontend-api",
        "frontend-page",
        "frontend-build",
        "integration-frontend",
        "git-diff-check",
    ),
    # Framework repair is prepared and performed by the outer Git owner.  No
    # Claude writer or check allowlist is started for this owner.
    "outer_codex": (),
}
REVIEW_ALLOWED_TOOLS = "Read,Glob,Grep"
DISALLOWED_TOOLS = ",".join(
    [
        "WebSearch",
        "WebFetch",
        "Bash(git add *)",
        "Bash(git commit *)",
        "Bash(git rebase *)",
        "Bash(git merge *)",
        "Bash(git push *)",
        "Bash(git clean *)",
        "Bash(git reset *)",
        "Bash(git checkout *)",
        "Bash(ssh *)",
        "Bash(docker *)",
        "Bash(curl *)",
        "Bash(wget *)",
        "Bash(.deploy/v121_beta_release.sh *)",
        "Bash(.deploy/v122_collection_reminders_release.sh *)",
        "Bash(*release*.sh *)",
    ]
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_freezer_module():
    path = PROMPT_DIR / "freeze_collection_reminders_review.py"
    spec = importlib.util.spec_from_file_location(
        "maintenance_collection_reminders_freezer", path
    )
    if spec is None or spec.loader is None:
        raise ValueError("cannot load review freezer")
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def prepare_repair_finding(
    path_arg: str, *, owner: str
) -> tuple[dict[str, object], str]:
    repair_dir = ROOT / ".git" / "maintenance-collection-reminders-repairs"
    if repair_dir.is_symlink() or not repair_dir.is_dir():
        raise ValueError("repair evidence directory is invalid")
    candidate = Path(path_arg)
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    try:
        candidate = candidate.absolute()
        candidate.relative_to(repair_dir)
    except ValueError as exc:
        raise ValueError("repair finding must be inside the run repair directory") from exc
    if candidate.parent != repair_dir or candidate.is_symlink() or not candidate.is_file():
        raise ValueError("repair finding path is invalid")
    if candidate.stat().st_size > 65536:
        raise ValueError("repair finding exceeds 64 KiB")
    for other in repair_dir.glob("*.json"):
        if other == candidate:
            continue
        if other.is_symlink() or not other.is_file():
            raise ValueError("repair evidence contains an invalid finding path")
        other_id = other.stem
        other_closure = repair_dir / f"{other_id}-repair.closure"
        if other_closure.is_symlink() or not other_closure.is_file():
            raise ValueError(f"another repair finding is still open: {other_id}")
    if (repair_dir / f"{candidate.stem}-repair.closure").exists():
        raise ValueError("repair finding is already closed")
    raw_finding = candidate.read_bytes()
    finding_digest = hashlib.sha256(raw_finding).hexdigest()
    finding = json.loads(raw_finding.decode("utf-8"))
    finding_id = finding.get("finding_id")
    if (
        not isinstance(finding_id, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", finding_id)
        or candidate.name != f"{finding_id}.json"
    ):
        raise ValueError("repair finding id is invalid")
    baseline = json.loads(
        (
            ROOT / ".git" / "maintenance-collection-reminders-user-files.json"
        ).read_text(encoding="utf-8")
    )
    final_patch = (
        ROOT / ".git" / "maintenance-collection-reminders-final-outer_codex.patch"
    )
    final_manifest = (
        ROOT / ".git" / "maintenance-collection-reminders-final-outer_codex.sha256"
    )
    expected = {
        "schema": "maintenance-collection-reminders-repair-v1",
        "owner": owner,
        "run_id": baseline.get("run_id"),
        "source_final_patch_sha256": sha256(final_patch),
        "source_final_manifest_sha256": sha256(final_manifest),
    }
    for key, value in expected.items():
        if finding.get(key) != value:
            raise ValueError(f"repair finding field mismatch: {key}")
    if finding.get("severity") not in {"P0", "P1"}:
        raise ValueError("repair finding severity must be P0 or P1")
    if not isinstance(finding.get("summary"), str) or not finding["summary"].strip():
        raise ValueError("repair finding summary is required")
    anchors = finding.get("anchors")
    if not isinstance(anchors, list) or not anchors or any(
        not isinstance(anchor, str) or not anchor for anchor in anchors
    ):
        raise ValueError("repair finding anchors are invalid")
    load_freezer_module().validate_repair_anchors(owner, anchors)
    archive_patch = repair_dir / f"{finding_id}-base.patch"
    archive_manifest = repair_dir / f"{finding_id}-base.sha256"
    for source, target in (
        (final_patch, archive_patch),
        (final_manifest, archive_manifest),
    ):
        if target.exists():
            if target.is_symlink() or not target.is_file() or sha256(target) != sha256(source):
                raise ValueError("repair base archive conflicts with existing evidence")
        else:
            shutil.copyfile(source, target)
    return finding, finding_digest


def write_repair_launch_receipt(
    finding: dict[str, object], *, owner: str, finding_digest: str
) -> dict[str, object]:
    """Bind the exact finding bytes before either repair execution path."""

    repair_dir = ROOT / ".git" / "maintenance-collection-reminders-repairs"
    final_manifest = repair_dir / f"{finding['finding_id']}-base.sha256"
    headers: dict[str, str] = {}
    for line in final_manifest.read_text(encoding="utf-8").splitlines():
        if line.startswith("# ") and "=" in line:
            key, value = line[2:].split("=", 1)
            headers[key] = value
    prepared_head = headers.get("reviewed_head", "")
    if not re.fullmatch(r"[0-9a-f]{40}", prepared_head):
        raise ValueError("outer repair base reviewed HEAD is invalid")
    payload = {
        "schema": "maintenance-collection-reminders-repair-launch-v1",
        "finding_id": finding["finding_id"],
        "owner": owner,
        "run_id": finding["run_id"],
        "prepared_head": prepared_head,
        "finding_sha256": finding_digest,
        "source_final_patch_sha256": finding["source_final_patch_sha256"],
        "source_final_manifest_sha256": finding[
            "source_final_manifest_sha256"
        ],
        "launcher_sha256": sha256(Path(__file__).resolve()),
        # The business branch invokes subprocess.run(claude, ...) later in the
        # same guarded call.  A failed/missing launch can never produce the
        # required committed closure, so final verification remains fail closed.
        "claude_writer_started": owner != "outer_codex",
    }
    receipt = repair_dir / f"{finding['finding_id']}-launch.receipt"
    finding_path = repair_dir / f"{finding['finding_id']}.json"
    if sha256(finding_path) != finding_digest:
        raise ValueError("repair finding changed while preparing launch evidence")
    encoded = json.dumps(
        payload, ensure_ascii=False, indent=2, sort_keys=True
    ) + "\n"
    if receipt.exists():
        if receipt.is_symlink() or not receipt.is_file():
            raise ValueError("outer repair receipt path is invalid")
        if receipt.read_text(encoding="utf-8") != encoded:
            raise ValueError("outer repair receipt conflicts with existing evidence")
        return payload
    receipt.write_text(encoded, encoding="utf-8")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("wave", choices=sorted(WAVES))
    parser.add_argument("--resume", help="Claude session id from the prior reviewed wave")
    parser.add_argument("--owner", choices=sorted(REPAIR_ALLOWED_CHECK_IDS_BY_OWNER))
    parser.add_argument("--finding-file")
    args = parser.parse_args()
    if args.wave == "review" and args.resume:
        parser.error("review must start in a fresh session and cannot resume a writer")
    if args.wave == "k0" and args.resume:
        parser.error("k0 starts a new writer session")
    if args.wave in {"parallel", "integration"} and not args.resume:
        parser.error(f"{args.wave} requires the previously reviewed writer session")
    if args.wave == "repair":
        if args.resume:
            parser.error("repair starts a fresh owner-specific session")
        if not args.owner or not args.finding_file:
            parser.error("repair requires --owner and --finding-file")
    elif args.owner or args.finding_file:
        parser.error("--owner and --finding-file are only valid for repair")

    verification_action = (
        "verify-packages"
        if args.wave == "review"
        else "verify-final"
        if args.wave == "repair"
        else "verify"
    )
    protected = subprocess.run(
        [
            sys.executable,
            str(PROMPT_DIR / "freeze_collection_reminders_review.py"),
            verification_action,
        ],
        cwd=ROOT,
        check=False,
    )
    if protected.returncode:
        return protected.returncode

    finding: dict[str, object] | None = None
    if args.wave == "repair":
        try:
            finding, finding_digest = prepare_repair_finding(
                args.finding_file, owner=args.owner
            )
            launch_receipt = write_repair_launch_receipt(
                finding, owner=args.owner, finding_digest=finding_digest
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if args.owner == "outer_codex":
            finding_digest = str(launch_receipt["finding_sha256"])
            print(
                json.dumps(
                    {
                        "status": "outer_repair_evidence_prepared",
                        "finding_id": finding["finding_id"],
                        "owner": "outer_codex",
                        "claude_writer_started": False,
                        "required_commit_trailers": {
                            "Collection-Reminder-Run-ID": finding["run_id"],
                            "Collection-Reminder-Finding-ID": finding[
                                "finding_id"
                            ],
                            "Collection-Reminder-Finding-SHA256": finding_digest,
                            "Collection-Reminder-Repair-Owner": "outer_codex",
                        },
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0

    agents = json.loads(
        (PROMPT_DIR / "maintenance-collection-reminders-agents.json").read_text()
    )
    prompt = WAVES[args.wave].read_text()
    if finding is not None:
        prompt += "\n\nAssigned frozen finding:\n" + json.dumps(
            finding, ensure_ascii=False, indent=2, sort_keys=True
        )
    is_review = args.wave == "review"
    is_repair = args.wave == "repair"
    settings_path = (
        ROOT
        / ".git"
        / (
            f"maintenance-collection-reminders-claude-{args.wave}"
            + (f"-{args.owner}" if is_repair else "")
            + ".json"
        )
    )
    settings: dict[str, object] = {"permissions": {"allow": [], "deny": []}}
    if not is_review:
        guard = PROMPT_DIR / "guard_collection_reminders_tool.py"
        settings["hooks"] = {
            "PreToolUse": [
                {
                    "matcher": (
                        "Bash|Edit|Write|MultiEdit|NotebookEdit"
                        + ("|Agent" if is_repair else "")
                    ),
                    "hooks": [
                        {
                            "type": "command",
                            "command": (
                                f"python3 {guard} --wave {args.wave}"
                                + (f" --owner {args.owner}" if is_repair else "")
                            ),
                            "timeout": 10,
                        }
                    ],
                }
            ]
        }
    settings_path.write_text(
        json.dumps(settings, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    allowed_tools = REVIEW_ALLOWED_TOOLS
    if not is_review:
        check_ids = (
            REPAIR_ALLOWED_CHECK_IDS_BY_OWNER[args.owner]
            if is_repair
            else ALLOWED_CHECK_IDS_BY_WAVE[args.wave]
        )
        allowed_tools = ",".join(
            ["Read", "Glob", "Grep", "Edit", "Write"]
            + ([] if is_repair else ["Agent"])
            + [
                "Bash(python3 .ai/claude-prompts/"
                f"run_collection_reminders_checks.py {check_id})"
                for check_id in check_ids
            ]
        )
    command = [
        "claude",
        "-p",
        "--model",
        "fable",
        "--effort",
        "high",
        "--permission-mode",
        "dontAsk",
        "--settings",
        str(settings_path),
        "--output-format",
        "stream-json",
        "--verbose",
        "--agents",
        json.dumps(agents, ensure_ascii=False, separators=(",", ":")),
        "--tools",
        (
            "Read,Glob,Grep"
            if is_review
            else "Read,Glob,Grep,Edit,Write,Bash"
            if is_repair
            else "Read,Glob,Grep,Edit,Write,Agent,Bash"
        ),
        "--allowedTools",
        allowed_tools,
        "--disallowedTools",
        DISALLOWED_TOOLS + (",Agent" if is_repair else ""),
    ]
    if is_repair:
        command.extend(["--agent", args.owner, "--no-session-persistence"])
    elif not is_review:
        command.append("--forward-subagent-text")
    else:
        command.extend(["--agent", "test_reviewer", "--no-session-persistence"])
    if args.resume:
        command.extend(["--resume", args.resume])
    completed = subprocess.run(
        command,
        cwd=ROOT,
        input=prompt,
        text=True,
        check=False,
    )
    if is_repair and finding is not None:
        trailers = {
            "Collection-Reminder-Run-ID": finding["run_id"],
            "Collection-Reminder-Finding-ID": finding["finding_id"],
            "Collection-Reminder-Finding-SHA256": launch_receipt[
                "finding_sha256"
            ],
            "Collection-Reminder-Repair-Owner": args.owner,
        }
        print(
            json.dumps(
                {
                    "status": "repair_writer_exited",
                    "finding_id": finding["finding_id"],
                    "owner": args.owner,
                    "claude_exit_code": completed.returncode,
                    "required_commit_trailers": trailers,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
    return completed.returncode


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Claude PreToolUse guard: exact Bash IDs and wave-owned write paths only."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType
from typing import Final


ROOT: Final = Path(__file__).resolve().parents[2]
PROMPT_DIR: Final = ROOT / ".ai" / "claude-prompts"


def load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load guard dependency: {path.name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def deny(reason: str) -> int:
    payload = {
        "hookSpecificOutput": {"permissionDecision": "deny"},
        "systemMessage": reason,
    }
    print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
    return 2


def normalized_relative(file_path: object) -> str | None:
    if not isinstance(file_path, str) or not file_path:
        return None
    candidate = Path(file_path)
    if any(part in {".", ".."} for part in candidate.parts):
        return None
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    try:
        lexical = candidate.absolute()
        relative = lexical.relative_to(ROOT)
    except ValueError:
        return None
    current = ROOT
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return None
    return relative.as_posix()


def matches_rule(relative: str, rule: str) -> bool:
    return relative == rule or (rule.endswith("/") and relative.startswith(rule))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wave", choices=["k0", "parallel", "integration", "repair"])
    parser.add_argument("--owner")
    args = parser.parse_args()
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError) as exc:
        return deny(f"invalid PreToolUse input: {exc}")

    launcher = load_module(
        "collection_reminders_launcher_guard",
        PROMPT_DIR / "run_collection_reminders_claude.py",
    )
    freezer = load_module(
        "collection_reminders_freezer_guard",
        PROMPT_DIR / "freeze_collection_reminders_review.py",
    )
    if args.wave == "repair":
        if args.owner not in freezer.REPAIR_PREFIXES_BY_OWNER:
            return deny("repair owner is missing or invalid")
    elif args.owner is not None:
        return deny("owner is only valid for repair")
    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return deny("tool input must be an object")

    if tool_name == "Bash":
        command = tool_input.get("command")
        check_ids = (
            launcher.REPAIR_ALLOWED_CHECK_IDS_BY_OWNER[args.owner]
            if args.wave == "repair"
            else launcher.ALLOWED_CHECK_IDS_BY_WAVE[args.wave]
        )
        allowed = {
            "python3 .ai/claude-prompts/run_collection_reminders_checks.py "
            + check_id
            for check_id in check_ids
        }
        if not isinstance(command, str) or command not in allowed:
            return deny("Bash command is outside the exact wave check allowlist")
        return 0

    if tool_name in {"Edit", "Write", "MultiEdit", "NotebookEdit"}:
        relative = normalized_relative(
            tool_input.get("file_path") or tool_input.get("notebook_path")
        )
        if relative is None:
            return deny("write path is invalid, outside the repository, or symlinked")
        rules = (
            set(freezer.REPAIR_PREFIXES_BY_OWNER[args.owner])
            if args.wave == "repair"
            else {
                rule
                for owner_rules in freezer.OWNERS_BY_WAVE[args.wave].values()
                for rule in owner_rules
            }
        )
        if not any(matches_rule(relative, rule) for rule in rules):
            return deny(f"write path is outside {args.wave} ownership")
        return 0

    return deny(f"unexpected guarded tool: {tool_name}")


if __name__ == "__main__":
    sys.exit(main())

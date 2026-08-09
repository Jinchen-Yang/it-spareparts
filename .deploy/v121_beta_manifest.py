#!/usr/bin/env python3
"""Build and verify the immutable v1.21 maintenance Beta release manifest.

The generator intentionally has no default commit, image, compose or CI values.
It can only produce a manifest after the exact merged ``main`` commit and all
production observations have been supplied by the operator.
"""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


FORMAT = "v121-maintenance-beta-1"
DB_FROM = "f1c8e4a7b2d9"
DB_TO = "d9f1a3c7e5b2"
REQUIRED_FLAGS = (
    "MAINTENANCE_BETA_ENABLED",
    "REPLENISHMENT_BETA_ENABLED",
    "MAINTENANCE_CUTOVER_ENABLED",
)
SHA40 = re.compile(r"[0-9a-f]{40}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
SAFE_ACCOUNT = re.compile(r"[A-Za-z0-9_.@+-]{2,128}\Z")
GENERIC_ACCOUNTS = frozenset({"admin", "administrator", "guest", "root", "shared", "test"})
MAINTENANCE_ACTIONS = (
    "action_maintenance_roundtrip_apply",
    "action_maintenance_manager_workbook_apply",
    "action_maintenance_project_manage",
    "action_maintenance_demand_delete",
    "action_maintenance_site_issue_manage",
    "action_maintenance_bad_return_manage",
    "action_maintenance_acceptance_submit",
    "action_maintenance_acceptance_review",
    "action_maintenance_warehouse_manage",
    "action_maintenance_migration_review",
)
REPLENISHMENT_KEYS = (
    "page_replenishment_beta",
    "data_pool_price_governance",
    "action_replenishment_create",
    "action_replenishment_review",
)


class ManifestError(RuntimeError):
    pass


def _fail(message: str) -> None:
    raise ManifestError(message)


def _run(*args: str, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        args,
        cwd=cwd,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode:
        _fail(f"command failed ({' '.join(args)}): {completed.stderr.strip()}")
    return completed.stdout.strip()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _git_bytes(repo: Path, commit: str, path: str) -> bytes:
    completed = subprocess.run(
        ("git", "show", f"{commit}:{path}"),
        cwd=repo,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode:
        _fail(f"{path} is absent from target commit {commit}")
    return completed.stdout


def _literal_assignment(source: bytes, name: str, path: str) -> Any:
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        _fail(f"cannot parse migration {path}: {exc}")
    for node in tree.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        if value is None or not any(isinstance(target, ast.Name) and target.id == name for target in targets):
            continue
        try:
            return ast.literal_eval(value)
        except (ValueError, TypeError) as exc:
            _fail(f"{path}: {name} must be a literal ({exc})")
    _fail(f"{path}: missing {name}")


def _migration_inventory(repo: Path, target: str) -> list[dict[str, Any]]:
    listing = _run(
        "git",
        "ls-tree",
        "-r",
        "--name-only",
        target,
        "--",
        "backend/alembic/versions",
        cwd=repo,
    ).splitlines()
    files = [path for path in listing if path.endswith(".py") and not path.endswith("/__init__.py")]
    revisions: dict[str, dict[str, Any]] = {}
    for path in files:
        source = _git_bytes(repo, target, path)
        revision = _literal_assignment(source, "revision", path)
        down = _literal_assignment(source, "down_revision", path)
        if not isinstance(revision, str) or not revision:
            _fail(f"{path}: invalid revision")
        if revision in revisions:
            _fail(f"duplicate Alembic revision {revision}")
        if down is None:
            parents: list[str] = []
        elif isinstance(down, str):
            parents = [down]
        elif isinstance(down, (tuple, list)) and all(isinstance(item, str) for item in down):
            parents = list(down)
        else:
            _fail(f"{path}: invalid down_revision")
        revisions[revision] = {
            "revision": revision,
            "down_revisions": sorted(parents),
            "path": path,
            "sha256": _sha256_bytes(source),
        }

    for endpoint in (DB_FROM, DB_TO):
        if endpoint not in revisions:
            _fail(f"migration endpoint {endpoint} is absent from target commit")
    referenced = {
        parent for row in revisions.values() for parent in row["down_revisions"]
    }
    heads = sorted(set(revisions) - referenced)
    if heads != [DB_TO]:
        _fail(f"target commit must have exactly the expected Alembic head {DB_TO}; got {heads}")

    visiting: set[str] = set()
    memo: dict[str, set[str]] = {}

    def ancestors(revision: str) -> set[str]:
        if revision in memo:
            return set(memo[revision])
        if revision in visiting:
            _fail(f"cycle in migration graph at {revision}")
        row = revisions.get(revision)
        if row is None:
            _fail(f"migration {revision} references an unknown parent")
        visiting.add(revision)
        found = {revision}
        for parent in row["down_revisions"]:
            found.update(ancestors(parent))
        visiting.remove(revision)
        memo[revision] = found
        return set(found)

    to_ancestors = ancestors(DB_TO)
    from_ancestors = ancestors(DB_FROM)
    if DB_FROM not in to_ancestors:
        _fail(f"{DB_FROM} is not an ancestor of {DB_TO}")
    selected = to_ancestors - from_ancestors
    if DB_TO not in selected or DB_FROM in selected:
        _fail("invalid exclusive/inclusive migration range")
    return [revisions[revision] for revision in sorted(selected)]


def _parse_allowlist(path: Path, *, target: str) -> tuple[dict[str, Any], list[Path]]:
    """Validate the named-account permission graph and return privacy-safe digests."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("format") != "v121-beta-allowlist-v1":
        _fail("Beta allowlist has the wrong format")
    accounts = data.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        _fail("the intended Beta allowlist must contain at least one named account")
    evidence_rows = data.get("canary_evidence", [])
    if not isinstance(evidence_rows, list):
        _fail("canary_evidence must be a list")
    evidence: dict[tuple[str, str], Path] = {}
    copied_evidence: list[Path] = []
    for row in evidence_rows:
        if not isinstance(row, dict):
            _fail("canary evidence entry is malformed")
        username = row.get("username")
        action = row.get("action")
        evidence_path = row.get("path")
        if (
            not isinstance(username, str)
            or not isinstance(action, str)
            or not isinstance(evidence_path, str)
            or row.get("target_sha") != target
            or row.get("conclusion") != "passed"
        ):
            _fail("canary evidence must bind username, action and successful exact target SHA")
        source = (path.parent / evidence_path).resolve()
        if not source.is_file() or source.is_symlink():
            _fail(f"canary evidence file is absent or unsafe: {evidence_path}")
        if row.get("sha256") != _sha256_file(source):
            _fail(f"canary evidence digest drifted: {evidence_path}")
        try:
            evidence_data = json.loads(source.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            _fail(f"canary evidence is not JSON: {evidence_path} ({exc})")
        if not isinstance(evidence_data, dict) or evidence_data.get("format") != "v121-action-canary-v1":
            _fail(f"canary evidence has the wrong format: {evidence_path}")
        for key, value in {
            "username": username,
            "action": action,
            "target_sha": target,
            "conclusion": "passed",
            "environment": "isolated",
        }.items():
            if evidence_data.get(key) != value:
                _fail(f"canary evidence is not bound to {username}/{action}/{target}: {key}")
        executor = evidence_data.get("executor_id")
        if not isinstance(executor, str) or not SAFE_ACCOUNT.fullmatch(executor):
            _fail(f"canary evidence has an invalid executor id: {evidence_path}")
        completed_at = evidence_data.get("completed_at")
        if not isinstance(completed_at, str):
            _fail(f"canary evidence lacks a completion time: {evidence_path}")
        try:
            parsed_time = dt.datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
        except ValueError:
            _fail(f"canary evidence has an invalid completion time: {evidence_path}")
        if parsed_time.tzinfo is None:
            _fail(f"canary evidence completion time lacks a timezone: {evidence_path}")
        request = evidence_data.get("request")
        if not isinstance(request, dict) or set(request) != {"method", "path", "payload_sha256"}:
            _fail(f"canary evidence has a malformed request: {evidence_path}")
        if request["method"] not in {"POST", "PUT", "PATCH", "DELETE"}:
            _fail(f"canary evidence request is not a write method: {evidence_path}")
        if (
            not isinstance(request["path"], str)
            or not request["path"].startswith("/api/")
            or any(character.isspace() for character in request["path"])
        ):
            _fail(f"canary evidence has an invalid API path: {evidence_path}")
        if not SHA256.fullmatch(str(request["payload_sha256"])):
            _fail(f"canary evidence has an invalid payload digest: {evidence_path}")
        result = evidence_data.get("result")
        if not isinstance(result, dict) or set(result) != {
            "expected_status",
            "observed_status",
            "response_sha256",
        }:
            _fail(f"canary evidence has a malformed result: {evidence_path}")
        if (
            not isinstance(result["expected_status"], int)
            or not 200 <= result["expected_status"] < 300
            or result["observed_status"] != result["expected_status"]
        ):
            _fail(f"canary evidence did not observe the expected success status: {evidence_path}")
        if not SHA256.fullmatch(str(result["response_sha256"])):
            _fail(f"canary evidence has an invalid response digest: {evidence_path}")
        key = (username, action)
        if key in evidence:
            _fail(f"duplicate canary evidence for {username}/{action}")
        evidence[key] = source
        copied_evidence.append(source)

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    used_evidence: set[tuple[str, str]] = set()
    for index, account in enumerate(accounts, 1):
        if not isinstance(account, dict):
            _fail(f"allowlist account {index} is malformed")
        username = account.get("username")
        role = account.get("role")
        if not isinstance(username, str) or not SAFE_ACCOUNT.fullmatch(username):
            _fail(f"allowlist account {index} has an invalid username")
        if username.lower() in GENERIC_ACCOUNTS:
            _fail("the Beta pilot must not contain generic/shared accounts")
        if username in seen:
            _fail(f"duplicate allowlist account: {username}")
        if not isinstance(role, str) or not role or len(role) > 64:
            _fail(f"allowlist account {username} has an invalid role")
        seen.add(username)

        maintenance = account.get("maintenance")
        replenishment = account.get("replenishment")
        if not isinstance(maintenance, dict) or not isinstance(replenishment, dict):
            _fail(f"allowlist account {username} must include both permission domains")
        expected_maintenance = {"page_maintenance", "page_maintenance_beta", *MAINTENANCE_ACTIONS}
        if set(maintenance) != expected_maintenance:
            _fail(f"allowlist account {username} does not enumerate every Maintenance permission")
        if set(replenishment) != set(REPLENISHMENT_KEYS):
            _fail(f"allowlist account {username} does not enumerate every replenishment permission")
        if not all(isinstance(value, bool) for value in maintenance.values()):
            _fail(f"allowlist account {username} has a non-boolean Maintenance permission")
        if not all(isinstance(value, bool) for value in replenishment.values()):
            _fail(f"allowlist account {username} has a non-boolean replenishment permission")
        maintenance_enabled = maintenance["page_maintenance_beta"]
        if role == "admin" and maintenance_enabled:
            _fail("the first Maintenance Beta pilot must not contain an admin account")
        if maintenance_enabled and not maintenance["page_maintenance"]:
            _fail(f"allowlist account {username} enables Maintenance Beta without stable Maintenance")
        replenishment_enabled = replenishment["page_replenishment_beta"]
        review_callback_enabled = replenishment["action_replenishment_review"]
        if not maintenance_enabled and not replenishment_enabled and not review_callback_enabled:
            _fail(f"allowlist account {username} has no effective Beta access")
        for action in MAINTENANCE_ACTIONS:
            if maintenance[action]:
                if not maintenance_enabled:
                    _fail(f"Maintenance write {username}/{action} is enabled without its Beta page")
                key = (username, action)
                if key not in evidence:
                    _fail(f"Maintenance write {username}/{action} lacks exact-SHA canary evidence")
                used_evidence.add(key)
        for action in ("action_replenishment_create", "action_replenishment_review"):
            if replenishment[action]:
                if action == "action_replenishment_create" and not replenishment_enabled:
                    _fail(f"replenishment write {username}/{action} is enabled without its Beta page")
                key = (username, action)
                if key not in evidence:
                    _fail(f"replenishment write {username}/{action} lacks exact-SHA canary evidence")
                used_evidence.add(key)
        if replenishment["action_replenishment_create"] and not replenishment["data_pool_price_governance"]:
            _fail(f"allowlist account {username} can create replenishment without price permission")
        if replenishment_enabled and not replenishment["data_pool_price_governance"]:
            _fail(f"allowlist account {username} cannot use replenishment without price permission")
        normalized.append(
            {
                "username": username,
                "role": role,
                "maintenance": {key: maintenance[key] for key in sorted(maintenance)},
                "replenishment": {key: replenishment[key] for key in sorted(replenishment)},
            }
        )
    unused = set(evidence) - used_evidence
    if unused:
        _fail(f"unused canary evidence entries: {len(unused)}")

    normalized.sort(key=lambda row: row["username"])
    maintenance_projection = [
        {"username": row["username"], "role": row["role"], **row["maintenance"]}
        for row in normalized
    ]
    replenishment_projection = [
        {"username": row["username"], "role": row["role"], **row["replenishment"]}
        for row in normalized
    ]
    summary = {
        "account_count": len(normalized),
        "permission_graph_sha256": _sha256_bytes(_json_bytes(normalized)),
        "maintenance_effective_permissions_sha256": _sha256_bytes(
            _json_bytes(maintenance_projection)
        ),
        "replenishment_effective_permissions_sha256": _sha256_bytes(
            _json_bytes(replenishment_projection)
        ),
        "canary_evidence_count": len(used_evidence),
        "empty_stage_sha256": _sha256_bytes(b""),
    }
    return summary, copied_evidence


def _validate_ci_evidence(data: Any, *, repository: str, target: str, required: list[str]) -> None:
    if not isinstance(data, dict) or data.get("format") != "github-main-ci-v1":
        _fail("CI evidence has the wrong format")
    if data.get("repository") != repository or data.get("target_sha") != target:
        _fail("CI evidence is not bound to the requested repository and target SHA")
    if data.get("main_head_sha") != target:
        _fail("CI evidence does not prove target is the current main head")
    checks = data.get("checks")
    if not isinstance(checks, list) or not checks:
        _fail("CI evidence contains no checks")
    by_name: dict[str, dict[str, Any]] = {}
    for check in checks:
        if not isinstance(check, dict) or not isinstance(check.get("name"), str):
            _fail("CI evidence contains a malformed check")
        if check.get("status") != "completed":
            _fail(f"CI check is not completed: {check.get('name')}")
        if check.get("conclusion") not in {"success", "neutral", "skipped"}:
            _fail(f"CI check is not green: {check.get('name')}")
        by_name[check["name"]] = check
    for name in required:
        if by_name.get(name, {}).get("conclusion") != "success":
            _fail(f"required CI check is absent or not successful: {name}")


def _validate_rehearsal(
    data: Any,
    *,
    target: str,
    parent: str,
    old_app: str,
    old_frontend: str,
    candidate_compose_sha256: str,
) -> None:
    if not isinstance(data, dict) or data.get("format") != "old-images-on-d9-rehearsal-v1":
        _fail("old-image rehearsal has the wrong format")
    expected = {
        "target_sha": target,
        "parent_production_sha": parent,
        "db_head": DB_TO,
        "old_app_image_id": old_app,
        "old_frontend_image_id": old_frontend,
        "candidate_compose_sha256": candidate_compose_sha256,
        "isolated": True,
        "stable_smoke": "passed",
        "conclusion": "success",
    }
    for key, value in expected.items():
        if data.get(key) != value:
            _fail(f"old-image rehearsal is not valid for this release: {key}")


def _validate_build_evidence(
    data: Any,
    *,
    target: str,
    app_image: str,
    frontend_image: str,
    source_tar: Path,
    image_bundle: Path,
) -> None:
    if not isinstance(data, dict) or data.get("format") != "v121-beta-build-v1":
        _fail("build evidence has the wrong format")
    expected = {
        "target_sha": target,
        "app_image_id": app_image,
        "frontend_image_id": frontend_image,
        "source_tar_sha256": _sha256_file(source_tar),
        "image_bundle_sha256": _sha256_file(image_bundle),
    }
    for key, value in expected.items():
        if data.get(key) != value:
            _fail(f"build evidence is not bound to the exact artifact: {key}")


def _validate_migration_rehearsal(
    data: Any,
    *,
    target: str,
    parent: str,
    db_image: str,
    candidate_compose_sha256: str,
) -> None:
    if not isinstance(data, dict) or data.get("format") != "v121-production-copy-migration-rehearsal-v1":
        _fail("production-copy migration rehearsal has the wrong format")
    expected = {
        "target_sha": target,
        "parent_production_sha": parent,
        "from_revision": DB_FROM,
        "to_revision": DB_TO,
        "database_image_id": db_image,
        "candidate_compose_sha256": candidate_compose_sha256,
        "statement_timeout_ms": 120000,
        "lock_timeout_ms": 5000,
        "isolated": True,
        "conclusion": "success",
    }
    for key, value in expected.items():
        if data.get(key) != value:
            _fail(f"production-copy migration rehearsal mismatch: {key}")
    if not isinstance(data.get("duration_milliseconds"), int) or data["duration_milliseconds"] < 0:
        _fail("production-copy migration rehearsal lacks duration")
    if not isinstance(data.get("restore_tmpfs_size"), str) or not re.fullmatch(
        r"[1-9][0-9]*(m|g)", data["restore_tmpfs_size"]
    ):
        _fail("production-copy migration rehearsal has an invalid restore tmpfs size")
    for key in ("production_copy_dump_sha256", "pressure_samples_sha256"):
        if not SHA256.fullmatch(str(data.get(key, ""))):
            _fail(f"production-copy migration rehearsal digest is invalid: {key}")


def _validate_review_evidence(data: Any, *, target: str) -> str:
    if not isinstance(data, dict) or data.get("format") != "exact-sha-independent-review-v1":
        _fail("independent review evidence has the wrong format")
    expected = {
        "target_sha": target,
        "scope": "full-release-candidate",
        "p0_count": 0,
        "p1_count": 0,
        "conclusion": "approved",
    }
    for key, value in expected.items():
        if data.get(key) != value:
            _fail(f"independent review evidence mismatch: {key}")
    reviewer = data.get("reviewer_id")
    if not isinstance(reviewer, str) or not re.fullmatch(r"[A-Za-z0-9_.@+-]{2,128}", reviewer):
        _fail("independent review evidence has an invalid reviewer id")
    if not isinstance(data.get("completed_at"), str) or not data["completed_at"]:
        _fail("independent review evidence lacks a completion time")
    try:
        completed_at = dt.datetime.fromisoformat(data["completed_at"].replace("Z", "+00:00"))
    except ValueError:
        _fail("independent review evidence has an invalid completion time")
    if completed_at.tzinfo is None:
        _fail("independent review evidence completion time lacks a timezone")
    return reviewer


def _validate_image(value: str, label: str) -> None:
    if not IMAGE_ID.fullmatch(value):
        _fail(f"{label} must be a full sha256 Docker image ID")


def _validate_candidate_compose_contract(content: bytes) -> None:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        _fail(f"candidate Compose is not UTF-8: {exc}")
    for key in REQUIRED_FLAGS:
        pattern = re.compile(
            rf"^\s*{re.escape(key)}:\s*\$\{{{re.escape(key)}:-false\}}\s*$",
            re.MULTILINE,
        )
        if not pattern.search(text):
            _fail(f"candidate Compose does not default {key} to false")


def _capture_ci(args: argparse.Namespace) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository):
        _fail("repository must be owner/name")
    if not SHA40.fullmatch(args.target_sha):
        _fail("target SHA must be 40 lowercase hex characters")
    if args.output.exists() or args.output.is_symlink():
        _fail("CI evidence output already exists; evidence is immutable")
    branch = json.loads(_run("gh", "api", f"repos/{args.repository}/branches/main"))
    main_head = ((branch.get("commit") or {}).get("sha"))
    if main_head != args.target_sha:
        _fail("GitHub main does not point at the requested exact SHA")
    response = json.loads(
        _run(
            "gh",
            "api",
            "-H",
            "Accept: application/vnd.github+json",
            f"repos/{args.repository}/commits/{args.target_sha}/check-runs?per_page=100",
        )
    )
    runs = response.get("check_runs")
    if not isinstance(runs, list):
        _fail("GitHub check-runs response is malformed")
    evidence = {
        "format": "github-main-ci-v1",
        "repository": args.repository,
        "target_sha": args.target_sha,
        "main_head_sha": main_head,
        "captured_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "checks": [
            {
                "name": item.get("name"),
                "status": item.get("status"),
                "conclusion": item.get("conclusion"),
                "details_url": item.get("details_url"),
            }
            for item in runs
        ],
    }
    _validate_ci_evidence(
        evidence,
        repository=args.repository,
        target=args.target_sha,
        required=args.required_check,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(_json_bytes(evidence))
    os.chmod(args.output, 0o600)
    print(f"CI_EVIDENCE_SHA256={_sha256_file(args.output)}")


def _generate(args: argparse.Namespace) -> None:
    repo = args.repo.resolve()
    if not SHA40.fullmatch(args.target_sha) or not SHA40.fullmatch(args.parent_production_sha):
        _fail("target and parent production SHAs must be full lowercase 40-character hashes")
    resolved_target = _run("git", "rev-parse", f"{args.target_sha}^{{commit}}", cwd=repo)
    resolved_main = _run("git", "rev-parse", "refs/remotes/origin/main", cwd=repo)
    if resolved_target != args.target_sha or resolved_main != args.target_sha:
        _fail("target SHA is not the exact local main ref head")
    if _run(
        "git", "merge-base", "--is-ancestor", args.parent_production_sha, args.target_sha, cwd=repo
    ) not in {""}:
        _fail("unexpected merge-base output")
    # merge-base --is-ancestor signals failure through the return code handled by _run.

    for value, label in (
        (args.old_app_image_id, "old app image"),
        (args.old_frontend_image_id, "old frontend image"),
        (args.new_app_image_id, "new app image"),
        (args.new_frontend_image_id, "new frontend image"),
        (args.db_image_id, "database image"),
    ):
        _validate_image(value, label)
    if args.old_app_image_id == args.new_app_image_id:
        _fail("old and new app image IDs are identical")
    if args.old_frontend_image_id == args.new_frontend_image_id:
        _fail("old and new frontend image IDs are identical")
    if not SHA256.fullmatch(args.manifest_hmac_key_fingerprint):
        _fail("manifest HMAC key fingerprint must be a SHA-256 digest")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", args.manifest_hmac_key_id):
        _fail("manifest HMAC key id is invalid")
    for path, label in (
        (args.current_compose, "current compose"),
        (args.ci_evidence, "CI evidence"),
        (args.build_evidence, "build evidence"),
        (args.source_tar, "source tar"),
        (args.image_bundle, "image bundle"),
        (args.migration_rehearsal, "migration rehearsal"),
        (args.migration_pressure_samples, "migration pressure samples"),
        (args.beta_allowlist, "Beta allowlist"),
    ):
        if not path.is_file() or path.is_symlink():
            _fail(f"{label} must be a real file")
    if len(args.review_evidence) < 2:
        _fail("at least two independent exact-SHA reviews are required")
    reviewers: set[str] = set()
    review_sources: list[Path] = []
    for path in args.review_evidence:
        if not path.is_file() or path.is_symlink():
            _fail("independent review evidence must be a real file")
        reviewer = _validate_review_evidence(
            json.loads(path.read_text(encoding="utf-8")), target=args.target_sha
        )
        if reviewer in reviewers:
            _fail("independent reviews must have distinct reviewer ids")
        reviewers.add(reviewer)
        review_sources.append(path)

    ci_data = json.loads(args.ci_evidence.read_text(encoding="utf-8"))
    _validate_ci_evidence(
        ci_data,
        repository=args.repository,
        target=args.target_sha,
        required=args.required_check,
    )
    allowlist_summary, canary_evidence = _parse_allowlist(
        args.beta_allowlist, target=args.target_sha
    )
    build_data = json.loads(args.build_evidence.read_text(encoding="utf-8"))
    _validate_build_evidence(
        build_data,
        target=args.target_sha,
        app_image=args.new_app_image_id,
        frontend_image=args.new_frontend_image_id,
        source_tar=args.source_tar,
        image_bundle=args.image_bundle,
    )
    # git get-tar-commit-id consumes stdin, so verify through a dedicated process.
    with args.source_tar.open("rb") as archive:
        archive_probe = subprocess.run(
            ("git", "get-tar-commit-id"),
            cwd=repo,
            stdin=archive,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    if archive_probe.returncode or archive_probe.stdout.strip() != args.target_sha:
        _fail("source tar is not bound to target SHA")
    candidate_compose = _git_bytes(repo, args.target_sha, "docker-compose.yml")
    _validate_candidate_compose_contract(candidate_compose)
    candidate_compose_hash = _sha256_bytes(candidate_compose)
    migration_rehearsal_data = json.loads(
        args.migration_rehearsal.read_text(encoding="utf-8")
    )
    _validate_migration_rehearsal(
        migration_rehearsal_data,
        target=args.target_sha,
        parent=args.parent_production_sha,
        db_image=args.db_image_id,
        candidate_compose_sha256=candidate_compose_hash,
    )
    if _sha256_file(args.migration_pressure_samples) != migration_rehearsal_data.get(
        "pressure_samples_sha256"
    ):
        _fail("production-copy migration pressure samples drifted")
    inventory = _migration_inventory(repo, args.target_sha)
    inventory_hash = _sha256_bytes(_json_bytes(inventory))
    control_sources = {
        "v121_beta_manifest.py": _git_bytes(
            repo, args.target_sha, ".deploy/v121_beta_manifest.py"
        ),
        "v121_beta_release.sh": _git_bytes(
            repo, args.target_sha, ".deploy/v121_beta_release.sh"
        ),
    }
    current_compose_hash = _sha256_file(args.current_compose)

    rollback: dict[str, Any]
    rehearsal_copy: str | None = None
    if args.old_image_d9_rehearsal is None:
        rollback = {"mode": "forward_only_after_d9", "rehearsal_evidence": None}
    else:
        if not args.old_image_d9_rehearsal.is_file() or args.old_image_d9_rehearsal.is_symlink():
            _fail("old-image d9 rehearsal must be a real file")
        rehearsal_data = json.loads(args.old_image_d9_rehearsal.read_text(encoding="utf-8"))
        _validate_rehearsal(
            rehearsal_data,
            target=args.target_sha,
            parent=args.parent_production_sha,
            old_app=args.old_app_image_id,
            old_frontend=args.old_frontend_image_id,
            candidate_compose_sha256=candidate_compose_hash,
        )
        rehearsal_copy = "old-images-on-d9-rehearsal.json"
        rollback = {
            "mode": "old_images_on_d9_allowed",
            "rehearsal_evidence": {
                "path": rehearsal_copy,
                "sha256": _sha256_file(args.old_image_d9_rehearsal),
            },
        }

    if args.output_dir.exists():
        _fail("output directory already exists; immutable packages are never overwritten")
    args.output_dir.mkdir(mode=0o700, parents=True)
    ci_copy = args.output_dir / "github-main-ci.json"
    build_copy = args.output_dir / "build-evidence.json"
    migration_rehearsal_copy = args.output_dir / "production-copy-migration-rehearsal.json"
    migration_pressure_copy = args.output_dir / "production-copy-migration-pressure.jsonl"
    compose_copy = args.output_dir / "candidate-compose.yml"
    shutil.copyfile(args.ci_evidence, ci_copy)
    shutil.copyfile(args.build_evidence, build_copy)
    shutil.copyfile(args.migration_rehearsal, migration_rehearsal_copy)
    shutil.copyfile(args.migration_pressure_samples, migration_pressure_copy)
    compose_copy.write_bytes(candidate_compose)
    os.chmod(ci_copy, 0o600)
    os.chmod(build_copy, 0o600)
    os.chmod(migration_rehearsal_copy, 0o600)
    os.chmod(migration_pressure_copy, 0o600)
    os.chmod(compose_copy, 0o600)
    control_artifacts: list[dict[str, str]] = []
    for name, content in control_sources.items():
        destination = args.output_dir / name
        destination.write_bytes(content)
        os.chmod(destination, 0o700)
        control_artifacts.append({"path": name, "sha256": _sha256_file(destination)})
    if rehearsal_copy is not None:
        destination = args.output_dir / rehearsal_copy
        shutil.copyfile(args.old_image_d9_rehearsal, destination)
        os.chmod(destination, 0o600)
    canary_artifacts: list[dict[str, str]] = []
    for index, source in enumerate(sorted(canary_evidence, key=lambda item: str(item)), 1):
        relative = f"canary-evidence-{index:03d}.json"
        destination = args.output_dir / relative
        shutil.copyfile(source, destination)
        os.chmod(destination, 0o600)
        canary_artifacts.append({"path": relative, "sha256": _sha256_file(destination)})
    review_artifacts: list[dict[str, str]] = []
    for index, source in enumerate(sorted(review_sources, key=lambda item: str(item)), 1):
        relative = f"independent-review-{index:03d}.json"
        destination = args.output_dir / relative
        shutil.copyfile(source, destination)
        os.chmod(destination, 0o600)
        review_data = json.loads(destination.read_text(encoding="utf-8"))
        review_artifacts.append(
            {
                "path": relative,
                "sha256": _sha256_file(destination),
                "reviewer_id_sha256": _sha256_bytes(review_data["reviewer_id"].encode()),
            }
        )

    manifest = {
        "format": FORMAT,
        "release": "1.21.0-beta",
        "repository": args.repository,
        "target_sha": args.target_sha,
        "parent_production_sha": args.parent_production_sha,
        "generated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "database": {
            "from_revision": DB_FROM,
            "to_revision": DB_TO,
            "image_id": args.db_image_id,
            "migration_inventory_count": len(inventory),
            "migration_inventory_sha256": inventory_hash,
            "migration_inventory": inventory,
        },
        "compose": {
            "current_sha256": current_compose_hash,
            "candidate_sha256": candidate_compose_hash,
            "candidate_path": "candidate-compose.yml",
        },
        "images": {
            "old_app_id": args.old_app_image_id,
            "old_frontend_id": args.old_frontend_image_id,
            "new_app_id": args.new_app_image_id,
            "new_frontend_id": args.new_frontend_image_id,
            "app_ref": "it-spareparts-app:latest",
            "frontend_ref": "it-spareparts-frontend:latest",
        },
        "ci": {
            "path": "github-main-ci.json",
            "sha256": _sha256_file(ci_copy),
            "required_checks": sorted(args.required_check),
        },
        "build": {
            "path": "build-evidence.json",
            "sha256": _sha256_file(build_copy),
            "source_tar_sha256": _sha256_file(args.source_tar),
            "image_bundle_sha256": _sha256_file(args.image_bundle),
        },
        "migration_rehearsal": {
            "path": "production-copy-migration-rehearsal.json",
            "sha256": _sha256_file(migration_rehearsal_copy),
            "pressure_path": "production-copy-migration-pressure.jsonl",
            "pressure_sha256": _sha256_file(migration_pressure_copy),
        },
        "controls": sorted(control_artifacts, key=lambda row: row["path"]),
        "independent_reviews": review_artifacts,
        "initial_flags": {key: False for key in REQUIRED_FLAGS},
        "intended_beta_allowlist": {
            **allowlist_summary,
            "canary_evidence": canary_artifacts,
        },
        "maintenance_manifest_hmac": {
            "key_id": args.manifest_hmac_key_id,
            "key_fingerprint_sha256": args.manifest_hmac_key_fingerprint,
        },
        "rollback": rollback,
    }
    manifest_path = args.output_dir / "manifest.json"
    manifest_path.write_bytes(_json_bytes(manifest))
    os.chmod(manifest_path, 0o600)
    (args.output_dir / "manifest.sha256").write_text(
        f"{_sha256_file(manifest_path)}  manifest.json\n", encoding="ascii"
    )
    os.chmod(args.output_dir / "manifest.sha256", 0o600)
    _verify_package(args.output_dir)
    print(f"MANIFEST_SHA256={_sha256_file(manifest_path)}")


def _verify_package(package: Path) -> dict[str, Any]:
    if not package.is_dir() or package.is_symlink():
        _fail("package must be a real directory")
    manifest_path = package / "manifest.json"
    checksum_path = package / "manifest.sha256"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        _fail("manifest.json is missing or unsafe")
    if not checksum_path.is_file() or checksum_path.is_symlink():
        _fail("manifest.sha256 is missing or unsafe")
    expected_line = f"{_sha256_file(manifest_path)}  manifest.json\n"
    if checksum_path.read_text(encoding="ascii") != expected_line:
        _fail("manifest checksum does not match")
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    if data.get("format") != FORMAT or data.get("release") != "1.21.0-beta":
        _fail("manifest format/release mismatch")
    if not SHA40.fullmatch(str(data.get("target_sha", ""))) or not SHA40.fullmatch(
        str(data.get("parent_production_sha", ""))
    ):
        _fail("manifest commit identities are invalid")
    database = data.get("database") or {}
    if database.get("from_revision") != DB_FROM or database.get("to_revision") != DB_TO:
        _fail("manifest migration endpoints are invalid")
    inventory = database.get("migration_inventory")
    if not isinstance(inventory, list) or not inventory:
        _fail("manifest migration inventory is empty")
    if database.get("migration_inventory_count") != len(inventory):
        _fail("manifest migration inventory count drifted")
    if database.get("migration_inventory_sha256") != _sha256_bytes(_json_bytes(inventory)):
        _fail("manifest migration inventory digest drifted")
    if not IMAGE_ID.fullmatch(str(database.get("image_id", ""))):
        _fail("manifest database image identity is invalid")
    if not any(isinstance(row, dict) and row.get("revision") == DB_TO for row in inventory):
        _fail("manifest migration inventory does not include d9")
    compose = data.get("compose") or {}
    for key in ("current_sha256", "candidate_sha256"):
        if not SHA256.fullmatch(str(compose.get(key, ""))):
            _fail(f"manifest compose digest is invalid: {key}")
    flags = data.get("initial_flags")
    if flags != {key: False for key in REQUIRED_FLAGS}:
        _fail("all three initial flags must be exactly false")
    allowlist = data.get("intended_beta_allowlist") or {}
    if not isinstance(allowlist.get("account_count"), int) or allowlist["account_count"] < 1:
        _fail("manifest has no intended named Beta accounts")
    for key in (
        "permission_graph_sha256",
        "maintenance_effective_permissions_sha256",
        "replenishment_effective_permissions_sha256",
        "empty_stage_sha256",
    ):
        if not SHA256.fullmatch(str(allowlist.get(key, ""))):
            _fail(f"manifest allowlist digest is invalid: {key}")
    canary_artifacts = allowlist.get("canary_evidence")
    if not isinstance(canary_artifacts, list) or len(canary_artifacts) != allowlist.get(
        "canary_evidence_count"
    ):
        _fail("manifest canary evidence count drifted")
    for artifact_row in canary_artifacts:
        if not isinstance(artifact_row, dict):
            _fail("manifest canary evidence entry is malformed")
        relative = artifact_row.get("path")
        if not isinstance(relative, str) or Path(relative).name != relative:
            _fail("manifest canary evidence path is unsafe")
        artifact = package / relative
        if not artifact.is_file() or artifact.is_symlink():
            _fail("manifest canary evidence is absent or unsafe")
        if _sha256_file(artifact) != artifact_row.get("sha256"):
            _fail("manifest canary evidence digest drifted")
    hmac_data = data.get("maintenance_manifest_hmac") or {}
    if not SHA256.fullmatch(str(hmac_data.get("key_fingerprint_sha256", ""))):
        _fail("manifest HMAC key fingerprint is invalid")
    for key in ("old_app_id", "old_frontend_id", "new_app_id", "new_frontend_id"):
        _validate_image(str((data.get("images") or {}).get(key, "")), key)
    if (data.get("images") or {}).get("app_ref") != "it-spareparts-app:latest":
        _fail("manifest app image reference is invalid")
    if (data.get("images") or {}).get("frontend_ref") != "it-spareparts-frontend:latest":
        _fail("manifest frontend image reference is invalid")
    for section, path_key, digest_key in (
        (data.get("ci") or {}, "path", "sha256"),
        (data.get("build") or {}, "path", "sha256"),
        (data.get("migration_rehearsal") or {}, "path", "sha256"),
        (data.get("compose") or {}, "candidate_path", "candidate_sha256"),
    ):
        relative = section.get(path_key)
        if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts:
            _fail("manifest package path is unsafe")
        artifact = package / relative
        if not artifact.is_file() or artifact.is_symlink():
            _fail(f"package artifact is absent or unsafe: {relative}")
        if _sha256_file(artifact) != section.get(digest_key):
            _fail(f"package artifact digest drifted: {relative}")
    controls = data.get("controls")
    if not isinstance(controls, list) or {row.get("path") for row in controls if isinstance(row, dict)} != {
        "v121_beta_manifest.py",
        "v121_beta_release.sh",
    }:
        _fail("manifest control inventory is invalid")
    for row in controls:
        relative = row["path"]
        artifact = package / relative
        if not artifact.is_file() or artifact.is_symlink():
            _fail(f"control artifact is absent or unsafe: {relative}")
        if _sha256_file(artifact) != row.get("sha256"):
            _fail(f"control artifact digest drifted: {relative}")
    reviews = data.get("independent_reviews")
    if not isinstance(reviews, list) or len(reviews) < 2:
        _fail("manifest lacks two independent reviews")
    reviewers: set[str] = set()
    for row in reviews:
        if not isinstance(row, dict):
            _fail("manifest independent review entry is malformed")
        relative = row.get("path")
        if not isinstance(relative, str) or Path(relative).name != relative:
            _fail("manifest independent review path is unsafe")
        artifact = package / relative
        if not artifact.is_file() or artifact.is_symlink():
            _fail("manifest independent review evidence is absent or unsafe")
        if _sha256_file(artifact) != row.get("sha256"):
            _fail("manifest independent review evidence drifted")
        reviewer = _validate_review_evidence(
            json.loads(artifact.read_text(encoding="utf-8")), target=data["target_sha"]
        )
        if _sha256_bytes(reviewer.encode()) != row.get("reviewer_id_sha256"):
            _fail("manifest independent reviewer identity drifted")
        if reviewer in reviewers:
            _fail("manifest independent reviewers are not distinct")
        reviewers.add(reviewer)
    ci_data = json.loads((package / data["ci"]["path"]).read_text(encoding="utf-8"))
    _validate_ci_evidence(
        ci_data,
        repository=data["repository"],
        target=data["target_sha"],
        required=data["ci"]["required_checks"],
    )
    build = data.get("build") or {}
    for key in ("source_tar_sha256", "image_bundle_sha256"):
        if not SHA256.fullmatch(str(build.get(key, ""))):
            _fail(f"manifest build artifact digest is invalid: {key}")
    build_data = json.loads((package / build["path"]).read_text(encoding="utf-8"))
    if build_data.get("target_sha") != data["target_sha"]:
        _fail("build evidence target SHA drifted")
    if build_data.get("app_image_id") != data["images"]["new_app_id"]:
        _fail("build evidence app image drifted")
    if build_data.get("frontend_image_id") != data["images"]["new_frontend_id"]:
        _fail("build evidence frontend image drifted")
    if build_data.get("source_tar_sha256") != build["source_tar_sha256"]:
        _fail("build evidence source archive drifted")
    if build_data.get("image_bundle_sha256") != build["image_bundle_sha256"]:
        _fail("build evidence image bundle drifted")
    rehearsal = data.get("migration_rehearsal") or {}
    rehearsal_data = json.loads((package / rehearsal["path"]).read_text(encoding="utf-8"))
    _validate_migration_rehearsal(
        rehearsal_data,
        target=data["target_sha"],
        parent=data["parent_production_sha"],
        db_image=database["image_id"],
        candidate_compose_sha256=compose["candidate_sha256"],
    )
    pressure_path = rehearsal.get("pressure_path")
    if not isinstance(pressure_path, str) or Path(pressure_path).name != pressure_path:
        _fail("migration pressure evidence path is unsafe")
    pressure_artifact = package / pressure_path
    if not pressure_artifact.is_file() or pressure_artifact.is_symlink():
        _fail("migration pressure evidence is absent or unsafe")
    pressure_digest = _sha256_file(pressure_artifact)
    if pressure_digest != rehearsal.get("pressure_sha256"):
        _fail("migration pressure evidence digest drifted")
    if pressure_digest != rehearsal_data.get("pressure_samples_sha256"):
        _fail("migration rehearsal does not bind packaged pressure evidence")
    rollback = data.get("rollback") or {}
    if rollback.get("mode") == "forward_only_after_d9":
        if rollback.get("rehearsal_evidence") is not None:
            _fail("forward-only manifest unexpectedly has rehearsal evidence")
    elif rollback.get("mode") == "old_images_on_d9_allowed":
        evidence = rollback.get("rehearsal_evidence") or {}
        relative = evidence.get("path")
        if not isinstance(relative, str) or Path(relative).name != relative:
            _fail("rollback rehearsal path is unsafe")
        rehearsal_path = package / relative
        if not rehearsal_path.is_file() or _sha256_file(rehearsal_path) != evidence.get("sha256"):
            _fail("rollback rehearsal evidence is missing or drifted")
        _validate_rehearsal(
            json.loads(rehearsal_path.read_text(encoding="utf-8")),
            target=data["target_sha"],
            parent=data["parent_production_sha"],
            old_app=data["images"]["old_app_id"],
            old_frontend=data["images"]["old_frontend_id"],
            candidate_compose_sha256=compose["candidate_sha256"],
        )
    else:
        _fail("unknown rollback policy")
    return data


def _verify(args: argparse.Namespace) -> None:
    data = _verify_package(args.package.resolve())
    print(f"MANIFEST_SHA256={_sha256_file(args.package.resolve() / 'manifest.json')}")
    print(f"TARGET_SHA={data['target_sha']}")
    print(f"MIGRATION_COUNT={data['database']['migration_inventory_count']}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)

    capture = commands.add_parser("capture-ci")
    capture.add_argument("--repository", required=True)
    capture.add_argument("--target-sha", required=True)
    capture.add_argument("--required-check", action="append", required=True)
    capture.add_argument("--output", type=Path, required=True)
    capture.set_defaults(handler=_capture_ci)

    generate = commands.add_parser("generate")
    generate.add_argument("--repo", type=Path, required=True)
    generate.add_argument("--repository", required=True)
    generate.add_argument("--target-sha", required=True)
    generate.add_argument("--parent-production-sha", required=True)
    generate.add_argument("--current-compose", type=Path, required=True)
    generate.add_argument("--old-app-image-id", required=True)
    generate.add_argument("--old-frontend-image-id", required=True)
    generate.add_argument("--new-app-image-id", required=True)
    generate.add_argument("--new-frontend-image-id", required=True)
    generate.add_argument("--db-image-id", required=True)
    generate.add_argument("--ci-evidence", type=Path, required=True)
    generate.add_argument("--build-evidence", type=Path, required=True)
    generate.add_argument("--source-tar", type=Path, required=True)
    generate.add_argument("--image-bundle", type=Path, required=True)
    generate.add_argument("--migration-rehearsal", type=Path, required=True)
    generate.add_argument("--migration-pressure-samples", type=Path, required=True)
    generate.add_argument("--required-check", action="append", required=True)
    generate.add_argument("--review-evidence", action="append", type=Path, required=True)
    generate.add_argument("--beta-allowlist", type=Path, required=True)
    generate.add_argument("--manifest-hmac-key-id", required=True)
    generate.add_argument("--manifest-hmac-key-fingerprint", required=True)
    generate.add_argument("--old-image-d9-rehearsal", type=Path)
    generate.add_argument("--output-dir", type=Path, required=True)
    generate.set_defaults(handler=_generate)

    verify = commands.add_parser("verify")
    verify.add_argument("package", type=Path)
    verify.set_defaults(handler=_verify)
    return parser


def main() -> int:
    try:
        args = _parser().parse_args()
        args.handler(args)
    except (ManifestError, OSError, json.JSONDecodeError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

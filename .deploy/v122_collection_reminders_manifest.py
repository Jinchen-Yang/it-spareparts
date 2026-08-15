#!/usr/bin/env python3
"""Create and verify portable v1.22 collection-reminders release packages.

Every artifact is copied into a flat, immutable directory.  The manifest uses
only basenames and binds every byte, including the release tools themselves.
The preliminary package is always apply-off.  A promoted contract still needs
both preliminary and final rehearsal evidence before ``preflight`` accepts it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any


FORMAT = "v122-collection-reminders-2"
HISTORICAL_GAP_APPROVAL_FORMAT = "v122-historical-upload-gap-approval-v1"
HISTORICAL_GAP_RELEASE_FAMILY = "v122-collection-reminders"
DB_FROM = "d9f1a3c7e5b2"
DB_TO = "c8e2a4f6b1d3"
REQUIRED_RUNTIME_FLAGS = (
    "MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED",
    "MAINTENANCE_COLLECTION_CANARY_PROJECT_ID",
)
COLLECTION_ACTIONS = (
    "action_maintenance_collection_follow_up",
    "action_maintenance_collection_plan_import",
)
BACKUP_REQUIRED_ASSETS = [
    "postgres_custom_dump",
    "postgres_globals_dump",
    "uploads_archive",
    "compose_and_env_snapshot",
    "root_release_state",
    "exact_service_images",
    "wal_lsn",
    "file_counts_and_bytes",
    "sha256sums",
]
REAL_SAMPLE_SHA256 = "a783af09fa108d366a26e10fe188be52d20a9ce1fe02121bfd683d96356c8c18"
REHEARSAL_HASH_FIELDS = (
    "db_dump_sha256",
    "globals_sha256",
    "uploads_archive_sha256",
    "backup_manifest_sha256",
    "backup_checksums_sha256",
    "uploads_restore_sha256",
    "db_uploads_consistency_sha256",
    "invariants_sha256",
    "parser_result_sha256",
    "http_preview_summary_sha256",
    "http_apply_summary_sha256",
)
PACKAGE_TOOLS = (
    "v122_collection_reminders_manifest.py",
    "v122_collection_reminders_build.sh",
    "v122_collection_reminders_rehearse.sh",
    "v122_collection_reminders_release.sh",
    "v122_collection_reminders_static_test.py",
)
REQUIRED_CI_CHECKS = (
    "后端测试（pytest + 迁移链验证）",
    "前端类型检查 + 构建",
)

SHA40 = re.compile(r"[0-9a-f]{40}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
CANARY_ID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z"
)
TOP_LEVEL_YAML = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):(?:\s*(.*?))?\s*$")


class ManifestError(RuntimeError):
    pass


def _fail(message: str) -> None:
    raise ManifestError(message)


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


EMPTY_GAP_SET_SHA256 = hashlib.sha256(_canonical_bytes([])).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_file(path: Path, label: str) -> Path:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        _fail(f"{label} is missing")
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        _fail(f"{label} must be a regular non-symlink file")
    return path


def _require_new_directory(path: Path, label: str) -> Path:
    if path.exists() or path.is_symlink():
        _fail(f"{label} already exists")
    parent = path.parent.resolve(strict=True)
    if not parent.is_dir():
        _fail(f"{label} parent is not a directory")
    return path


def _validated(value: str, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        _fail(f"invalid {label}")
    return value


def _validate_canary(value: str) -> str:
    return _validated(value, CANARY_ID, "single canary project id")


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(_require_file(path, label).read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail(f"{label} is not valid UTF-8 JSON: {exc}")
    if not isinstance(value, dict):
        _fail(f"{label} must be a JSON object")
    return value


def _timezone_datetime(value: Any, label: str) -> dt.datetime:
    if not isinstance(value, str) or not value:
        _fail(f"historical gap approval {label} must be a timezone timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        _fail(f"historical gap approval {label} must be a timezone timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail(f"historical gap approval {label} must be a timezone timestamp")
    return parsed


def _validate_historical_gap_approval(
    path: Path,
    *,
    parent_production_sha: str,
) -> dict[str, Any]:
    approval_path = _require_file(path, "historical upload gap approval")
    if stat.S_IMODE(approval_path.lstat().st_mode) != 0o600:
        _fail("historical upload gap approval must be mode 600")
    value = _load_json(approval_path, "historical upload gap approval")
    expected_keys = {
        "format",
        "release_family",
        "parent_production_sha",
        "raw_only",
        "reason",
        "recorded_by",
        "approved_by",
        "recorded_at",
        "approved_at",
        "expires_at",
        "recovery_search_evidence_sha256",
        "approved_missing_refs",
    }
    if set(value) != expected_keys:
        _fail("historical upload gap approval keys mismatch")
    if value.get("format") != HISTORICAL_GAP_APPROVAL_FORMAT:
        _fail("unexpected historical upload gap approval format")
    if value.get("release_family") != HISTORICAL_GAP_RELEASE_FAMILY:
        _fail("historical upload gap approval release family mismatch")
    expected_parent = _validated(
        parent_production_sha,
        SHA40,
        "historical gap parent production SHA",
    )
    if value.get("parent_production_sha") != expected_parent:
        _fail("historical upload gap approval parent production SHA mismatch")
    if value.get("raw_only") is not True:
        _fail("historical upload gap approval must be raw-only")
    if value.get("reason") not in {"legacy_archive_loss", "backup_exhausted"}:
        _fail("historical upload gap approval reason is invalid")
    for key in ("recorded_by", "approved_by"):
        item = value.get(key)
        if (
            not isinstance(item, str)
            or not item
            or item != item.strip()
            or "\n" in item
            or "\r" in item
        ):
            _fail(f"historical upload gap approval {key} is invalid")
    if value["recorded_by"].casefold() == value["approved_by"].casefold():
        _fail("historical upload gap approval requires distinct recorder and approver")
    recorded_at = _timezone_datetime(value.get("recorded_at"), "recorded_at")
    approved_at = _timezone_datetime(value.get("approved_at"), "approved_at")
    expires_at = _timezone_datetime(value.get("expires_at"), "expires_at")
    now = dt.datetime.now(dt.timezone.utc)
    if recorded_at > approved_at or approved_at >= expires_at:
        _fail("historical upload gap approval timestamps are out of order")
    if recorded_at > now + dt.timedelta(minutes=5) or approved_at > now + dt.timedelta(
        minutes=5
    ):
        _fail("historical upload gap approval actor timestamp exceeds clock skew")
    if expires_at <= now:
        _fail("historical upload gap approval is expired")
    recovery_sha = _validated(
        str(value.get("recovery_search_evidence_sha256", "")),
        SHA256,
        "historical gap recovery search evidence SHA",
    )
    refs = value.get("approved_missing_refs")
    if not isinstance(refs, list) or not refs:
        _fail("historical upload gap approval must contain missing raw references")
    normalized: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    for index, row in enumerate(refs):
        if not isinstance(row, dict) or set(row) != {
            "raw_file_id",
            "file_hash",
            "storage_path",
        }:
            _fail(f"historical upload gap reference {index} keys mismatch")
        raw_id = row.get("raw_file_id")
        if isinstance(raw_id, bool) or not isinstance(raw_id, int) or raw_id <= 0:
            _fail(f"historical upload gap reference {index} raw_file_id is invalid")
        file_hash = _validated(
            str(row.get("file_hash", "")),
            SHA256,
            f"historical upload gap reference {index} file hash",
        )
        storage_path = row.get("storage_path")
        canonical_path = f"/app/data/raw/{file_hash}.xlsx"
        if storage_path != canonical_path:
            _fail(f"historical upload gap reference {index} storage path is not canonical")
        if raw_id in seen_ids:
            _fail("historical upload gap raw_file_id values must be unique")
        seen_ids.add(raw_id)
        normalized.append(
            {
                "raw_file_id": raw_id,
                "file_hash": file_hash,
                "storage_path": canonical_path,
            }
        )
    if normalized != sorted(
        normalized,
        key=lambda row: (row["raw_file_id"], row["file_hash"], row["storage_path"]),
    ):
        _fail("historical upload gap references must be sorted")
    return {
        "value": value,
        "approved_missing_refs": normalized,
        "approved_missing_count": len(normalized),
        "gap_set_sha256": hashlib.sha256(_canonical_bytes(normalized)).hexdigest(),
        "approval_sha256": _sha256_file(approval_path),
        "recovery_search_evidence_sha256": recovery_sha,
    }


def _stable_regular_file(path: Path, label: str) -> tuple[str, int]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        _fail(f"{label} cannot be read safely: {exc}")
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            _fail(f"{label} must be a regular non-symlink file")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            _fail(f"{label} changed while hashing")
        return digest.hexdigest(), after.st_size
    finally:
        os.close(descriptor)


def _safe_collection_relative(value: str) -> Path:
    if not value or "\\" in value or "\x00" in value:
        _fail("collection upload reference path is invalid")
    pure = Path(value)
    if pure.is_absolute() or ".." in pure.parts or pure.as_posix() != value:
        _fail("collection upload reference path escapes restored root")
    return pure


def _audit_upload_references(
    *,
    uploads_root: Path,
    references_path: Path,
    parent_production_sha: str,
    approval_path: Path | None,
) -> dict[str, Any]:
    root = uploads_root.absolute()
    try:
        root_mode = root.lstat().st_mode
    except FileNotFoundError:
        _fail("restored uploads root is missing")
    if stat.S_ISLNK(root_mode) or not stat.S_ISDIR(root_mode):
        _fail("restored uploads root must be a real directory")
    root = root.resolve(strict=True)
    references = _require_file(references_path, "DB upload references")
    try:
        lines = references.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        _fail("DB upload references must be UTF-8")
    referenced_paths: set[Path] = set()
    actual_missing: list[dict[str, Any]] = []
    seen_raw_ids: set[int] = set()
    for line_number, raw in enumerate(lines, 1):
        columns = raw.split("\t")
        if len(columns) != 5:
            _fail(f"DB upload reference row {line_number} must contain five columns")
        kind, raw_id_text, file_hash_text, storage_path, file_size_text = columns
        if kind == "raw":
            if file_size_text:
                _fail(f"raw upload reference row {line_number} size column must be empty")
            if not raw_id_text.isascii() or not raw_id_text.isdecimal():
                _fail(f"raw upload reference row {line_number} id is invalid")
            raw_id = int(raw_id_text)
            if raw_id <= 0 or raw_id in seen_raw_ids:
                _fail("raw upload reference ids must be unique positive integers")
            seen_raw_ids.add(raw_id)
            file_hash = _validated(
                file_hash_text,
                SHA256,
                f"raw upload reference row {line_number} file hash",
            )
            canonical_path = f"/app/data/raw/{file_hash}.xlsx"
            if storage_path != canonical_path:
                _fail(f"raw upload reference row {line_number} storage path is not canonical")
            candidate = root / f"{file_hash}.xlsx"
            referenced_paths.add(candidate)
            try:
                candidate_mode = candidate.lstat().st_mode
            except FileNotFoundError:
                actual_missing.append(
                    {
                        "raw_file_id": raw_id,
                        "file_hash": file_hash,
                        "storage_path": canonical_path,
                    }
                )
                continue
            if stat.S_ISLNK(candidate_mode) or not stat.S_ISREG(candidate_mode):
                _fail("referenced raw upload must be a regular non-symlink file")
            actual_hash, _actual_size = _stable_regular_file(
                candidate, "referenced raw upload"
            )
            if actual_hash != file_hash:
                _fail("referenced raw upload hash mismatch")
        elif kind == "collection":
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", raw_id_text) is None:
                _fail("collection upload reference batch id is invalid")
            expected_hash = _validated(
                file_hash_text,
                SHA256,
                f"collection upload reference row {line_number} file hash",
            )
            if (
                not file_size_text.isascii()
                or not file_size_text.isdecimal()
                or str(int(file_size_text)) != file_size_text
            ):
                _fail("collection upload reference file size is invalid")
            expected_size = int(file_size_text)
            relative = _safe_collection_relative(storage_path)
            candidate = root / "maintenance-collection-plans" / relative
            referenced_paths.add(candidate)
            try:
                candidate_mode = candidate.lstat().st_mode
            except FileNotFoundError:
                _fail("collection upload reference is missing from restored archive")
            if stat.S_ISLNK(candidate_mode) or not stat.S_ISREG(candidate_mode):
                _fail("collection upload reference must be a regular non-symlink file")
            actual_hash, actual_size = _stable_regular_file(
                candidate, "collection upload reference"
            )
            if actual_size != expected_size:
                _fail("collection upload reference size mismatch")
            if actual_hash != expected_hash:
                _fail("collection upload reference hash mismatch")
        else:
            _fail(f"DB upload reference row {line_number} has unknown kind")
    actual_missing.sort(
        key=lambda row: (row["raw_file_id"], row["file_hash"], row["storage_path"])
    )
    approval = None
    if approval_path is not None:
        approval = _validate_historical_gap_approval(
            approval_path,
            parent_production_sha=parent_production_sha,
        )
        if actual_missing != approval["approved_missing_refs"]:
            _fail("actual missing raw references do not exactly match approved historical gaps")
    elif actual_missing:
        _fail("DB raw upload reference is missing from restored archive")
    physical: set[Path] = set()
    for path in root.rglob("*"):
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            _fail("restored uploads tree changed during audit")
        if stat.S_ISREG(mode):
            physical.add(path)
    orphan_count = len(physical - referenced_paths)
    if approval is None:
        return {
            "reference_state": "complete",
            "references_complete": True,
            "reference_count": len(lines),
            "approved_missing_count": 0,
            "unexpected_missing_count": 0,
            "historical_upload_gap_set_sha256": EMPTY_GAP_SET_SHA256,
            "historical_upload_gap_approval_sha256": None,
            "recovery_search_evidence_sha256": None,
            "orphan_count": orphan_count,
            "orphan_action": "reported_not_deleted",
        }
    return {
        "reference_state": "complete_with_approved_historical_gaps",
        "references_complete": False,
        "reference_count": len(lines),
        "approved_missing_count": approval["approved_missing_count"],
        "unexpected_missing_count": 0,
        "historical_upload_gap_set_sha256": approval["gap_set_sha256"],
        "historical_upload_gap_approval_sha256": approval["approval_sha256"],
        "recovery_search_evidence_sha256": approval[
            "recovery_search_evidence_sha256"
        ],
        "orphan_count": orphan_count,
        "orphan_action": "reported_not_deleted",
    }


def _contract_values(path: Path) -> dict[str, Any]:
    text = _require_file(path, "contract").read_text(encoding="utf-8")
    values: dict[str, list[str]] = {}
    for line in text.splitlines():
        if line.startswith((" ", "\t", "#")) or not line.strip():
            continue
        match = TOP_LEVEL_YAML.fullmatch(line)
        if match:
            values.setdefault(match.group(1), []).append((match.group(2) or "").strip())
    required = (
        "contract_version",
        "contract_state",
        "production_apply_allowed",
    )
    for key in required:
        found = values.get(key, [])
        if len(found) > 1:
            _fail(f"duplicate contract key: {key}")
        if len(found) != 1:
            _fail(f"contract missing unique top-level key: {key}")
    if values["production_apply_allowed"][0] not in {"true", "false"}:
        _fail("production_apply_allowed must be a lowercase YAML boolean")
    result = {
        "version": values["contract_version"][0],
        "state": values["contract_state"][0],
        "production_apply_allowed": values["production_apply_allowed"][0]
        == "true",
        "sha256": _sha256_file(path),
    }
    valid_pair = (
        result["state"] == "approved_for_implementation"
        and result["production_apply_allowed"] is False
    ) or (
        result["state"] == "approved_for_production_candidate"
        and result["production_apply_allowed"] is True
    )
    if result["version"] != "project-manager-xls-v1" or not valid_pair:
        _fail("contract state/apply combination is not an approved release state")
    return result


def _validate_sbom(path: Path) -> dict[str, Any]:
    value = _load_json(path, "SBOM")
    if (
        value.get("bomFormat") != "CycloneDX"
        or value.get("specVersion") != "1.5"
        or not isinstance(value.get("components"), list)
        or not value["components"]
    ):
        _fail("SBOM must be a non-empty CycloneDX 1.5 document")
    return value


def _validate_ci(path: Path, target_sha: str) -> None:
    value = _load_json(path, "CI evidence")
    if value.get("format") != "v122-collection-reminders-ci-v1":
        _fail("unexpected CI evidence format")
    if value.get("target_sha") != target_sha:
        _fail("CI evidence target SHA mismatch")
    checks = value.get("required_checks")
    if not isinstance(checks, dict):
        _fail("CI evidence required_checks must be an object")
    for name in REQUIRED_CI_CHECKS:
        if checks.get(name) != "success":
            _fail(f"required CI check is not successful: {name}")


def _validate_build_evidence(
    path: Path,
    *,
    target_sha: str,
    app_image_id: str,
    frontend_image_id: str,
    compose: Path,
    source: Path,
    images: Path,
) -> None:
    value = _load_json(path, "build evidence")
    expected = {
        "format": "v122-collection-reminders-build-v2",
        "target_sha": target_sha,
        "app_image_id": app_image_id,
        "frontend_image_id": frontend_image_id,
        "candidate_compose_sha256": _sha256_file(compose),
        "source_tar_sha256": _sha256_file(source),
        "source_archive_commit": target_sha,
        "image_bundle_sha256": _sha256_file(images),
    }
    for key, wanted in expected.items():
        if value.get(key) != wanted:
            _fail(f"build evidence mismatch: {key}")


def _validate_rehearsal(
    path: Path,
    *,
    stage: str,
    target_sha: str,
    parent_sha: str,
    binding: dict[str, str],
) -> dict[str, Any]:
    value = _load_json(path, f"{stage} rehearsal evidence")
    expected = {
        "format": "v122-collection-reminders-rehearsal-v2",
        "stage": stage,
        "success": True,
        "target_sha": target_sha,
        "parent_production_sha": parent_sha,
        "from_revision": DB_FROM,
        "to_revision": DB_TO,
        "database_image_id": binding["database_image_id"],
        "app_image_id": binding["app_image_id"],
        "frontend_image_id": binding["frontend_image_id"],
        "package_manifest_sha256": binding["package_manifest_sha256"],
        "contract_sha256": binding["contract_sha256"],
        "candidate_compose_sha256": binding["candidate_compose_sha256"],
        "sample_xls_sha256": REAL_SAMPLE_SHA256,
        "parser_project_count": 3,
        "parser_milestone_count": 19,
        "db_restore": True,
        "globals_restore": True,
        "uploads_restore_verified": True,
        "preview_zero_domain_write": True,
        "synthetic_apply_verified": True,
    }
    for key, wanted in expected.items():
        if value.get(key) != wanted:
            _fail(f"{stage} rehearsal evidence mismatch: {key}")
    state = value.get("db_uploads_reference_state")
    references_complete = value.get("db_uploads_references_complete")
    approved_count = value.get("approved_missing_count")
    unexpected_count = value.get("unexpected_missing_count")
    gap_set_sha = str(value.get("historical_upload_gap_set_sha256", ""))
    approval_sha = value.get("historical_upload_gap_approval_sha256")
    recovery_sha = value.get("recovery_search_evidence_sha256")
    if isinstance(approved_count, bool) or not isinstance(approved_count, int):
        _fail(f"{stage} rehearsal upload reference approved count is invalid")
    if (
        isinstance(unexpected_count, bool)
        or not isinstance(unexpected_count, int)
        or unexpected_count != 0
    ):
        _fail(f"{stage} rehearsal upload references include unexpected missing files")
    _validated(gap_set_sha, SHA256, f"{stage} rehearsal historical gap set SHA")
    if state == "complete":
        if (
            references_complete is not True
            or approved_count != 0
            or gap_set_sha != EMPTY_GAP_SET_SHA256
            or approval_sha is not None
            or recovery_sha is not None
        ):
            _fail(f"{stage} rehearsal complete upload reference state is inconsistent")
    elif state == "complete_with_approved_historical_gaps":
        if references_complete is not False or approved_count <= 0:
            _fail(f"{stage} rehearsal approved historical gap state is inconsistent")
        _validated(
            str(approval_sha or ""),
            SHA256,
            f"{stage} rehearsal historical gap approval SHA",
        )
        _validated(
            str(recovery_sha or ""),
            SHA256,
            f"{stage} rehearsal recovery search evidence SHA",
        )
    else:
        _fail(f"{stage} rehearsal upload reference state is invalid")
    for key in REHEARSAL_HASH_FIELDS:
        _validated(
            str(value.get(key, "")),
            SHA256,
            f"{stage} rehearsal evidence {key}",
        )
    return value


def _upload_reference_binding(value: dict[str, Any]) -> tuple[Any, ...]:
    return (
        value["db_uploads_reference_state"],
        value["db_uploads_references_complete"],
        value["approved_missing_count"],
        value["unexpected_missing_count"],
        value["historical_upload_gap_set_sha256"],
        value["historical_upload_gap_approval_sha256"],
        value["recovery_search_evidence_sha256"],
    )


def _rehearsal_binding(package: Path, payload: dict[str, Any]) -> dict[str, str]:
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, dict):
        _fail("package lacks artifacts for rehearsal binding")
    return {
        "package_manifest_sha256": _sha256_file(package / "manifest.json"),
        "contract_sha256": str(payload["contract"]["sha256"]),
        "candidate_compose_sha256": str(artifacts["compose"]["sha256"]),
        "database_image_id": str(payload["database"]["image_id"]),
        "app_image_id": str(payload["images"]["app_image_id"]),
        "frontend_image_id": str(payload["images"]["frontend_image_id"]),
    }


def _binding_from_manifest(value: Any, label: str) -> dict[str, str]:
    if not isinstance(value, dict):
        _fail(f"production-ready package lacks {label} binding")
    expected_keys = {
        "target_sha",
        "manifest_sha256",
        "contract_sha256",
        "candidate_compose_sha256",
        "database_image_id",
        "app_image_id",
        "frontend_image_id",
    }
    if set(value) != expected_keys:
        _fail(f"{label} binding keys mismatch")
    return {
        "package_manifest_sha256": _validated(
            str(value["manifest_sha256"]),
            SHA256,
            f"{label} manifest SHA",
        ),
        "contract_sha256": _validated(
            str(value["contract_sha256"]),
            SHA256,
            f"{label} contract SHA",
        ),
        "candidate_compose_sha256": _validated(
            str(value["candidate_compose_sha256"]),
            SHA256,
            f"{label} compose SHA",
        ),
        "database_image_id": _validated(
            str(value["database_image_id"]),
            IMAGE_ID,
            f"{label} DB image id",
        ),
        "app_image_id": _validated(
            str(value["app_image_id"]),
            IMAGE_ID,
            f"{label} app image id",
        ),
        "frontend_image_id": _validated(
            str(value["frontend_image_id"]),
            IMAGE_ID,
            f"{label} frontend image id",
        ),
    }


def _copy_artifact(source: Path, target: Path, *, executable: bool = False) -> None:
    _require_file(source, source.name)
    with source.open("rb") as reader, target.open("xb") as writer:
        shutil.copyfileobj(reader, writer, 1024 * 1024)
        writer.flush()
        os.fsync(writer.fileno())
    target.chmod(0o700 if executable else 0o600)


def _source_tool_bytes(source_bundle: Path, target_sha: str) -> dict[str, bytes]:
    """Read release controllers from the exact git archive, never the caller tree."""

    _require_file(source_bundle, "source bundle")
    try:
        with tarfile.open(source_bundle, "r:*") as archive:
            if archive.pax_headers.get("comment") != target_sha:
                _fail("source archive is not bound to target SHA")
            members = {member.name: member for member in archive.getmembers()}
            result: dict[str, bytes] = {}
            for name in PACKAGE_TOOLS:
                archive_name = f"source/.deploy/{name}"
                member = members.get(archive_name)
                if member is None or not member.isfile():
                    _fail(f"release tool absent from target source archive: {name}")
                stream = archive.extractfile(member)
                if stream is None:
                    _fail(f"cannot read release tool from source archive: {name}")
                result[name] = stream.read()
            return result
    except (tarfile.TarError, OSError) as exc:
        _fail(f"invalid source archive: {exc}")


def _artifact_row(path: Path) -> dict[str, Any]:
    return {
        "path": path.name,
        "sha256": _sha256_file(path),
        "size": path.stat().st_size,
    }


def _write_manifest(directory: Path, payload: dict[str, Any]) -> None:
    manifest = directory / "manifest.json"
    with manifest.open("xb") as stream:
        stream.write(_canonical_bytes(payload))
        stream.flush()
        os.fsync(stream.fileno())
    manifest.chmod(0o600)
    digest = _sha256_file(manifest)
    checksum = directory / "manifest.sha256"
    with checksum.open("x", encoding="ascii") as stream:
        stream.write(f"{digest}  manifest.json\n")
        stream.flush()
        os.fsync(stream.fileno())
    checksum.chmod(0o600)
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _publish_directory(staging: Path, output: Path) -> None:
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.replace(staging, output)
    parent_fd = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def _new_staging(output: Path) -> Path:
    _require_new_directory(output, "output package")
    return Path(tempfile.mkdtemp(prefix=f".{output.name}.tmp-", dir=output.parent))


def _copy_candidate_artifacts(args: argparse.Namespace, staging: Path) -> dict[str, dict[str, Any]]:
    sources = {
        "compose": (Path(args.candidate_compose), "candidate-compose.yml", False),
        "contract": (Path(args.contract), "contract.yaml", False),
        "sbom": (Path(args.sbom), "dependency-sbom.cdx.json", False),
        "build_evidence": (Path(args.build_evidence), "build-evidence.json", False),
        "source_bundle": (Path(args.source_bundle), "source.tar", False),
        "image_bundle": (Path(args.image_bundle), "images.tar", False),
        "ci_evidence": (Path(args.ci_evidence), "ci-evidence.json", False),
    }
    artifacts: dict[str, dict[str, Any]] = {}
    for key, (source, name, executable) in sources.items():
        target = staging / name
        _copy_artifact(source, target, executable=executable)
        artifacts[key] = _artifact_row(target)
    tools = _source_tool_bytes(Path(args.source_bundle), args.target_sha)
    for name, content in tools.items():
        target = staging / name
        with target.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        target.chmod(0o700)
        artifacts[f"tool_{name.removesuffix('.py').removesuffix('.sh')}"] = _artifact_row(target)
    return artifacts


def _base_payload(args: argparse.Namespace, artifacts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    target_sha = _validated(args.target_sha, SHA40, "target SHA")
    parent_sha = _validated(args.parent_production_sha, SHA40, "parent production SHA")
    if target_sha == parent_sha:
        _fail("target SHA must differ from parent production SHA")
    app_id = _validated(args.app_image_id, IMAGE_ID, "app image id")
    frontend_id = _validated(args.frontend_image_id, IMAGE_ID, "frontend image id")
    database_id = _validated(args.database_image_id, IMAGE_ID, "database image id")
    previous_app = _validated(args.previous_app_image_id, IMAGE_ID, "previous app image id")
    previous_frontend = _validated(
        args.previous_frontend_image_id,
        IMAGE_ID,
        "previous frontend image id",
    )
    compose = Path(args.candidate_compose)
    contract = _contract_values(Path(args.contract))
    _validate_sbom(Path(args.sbom))
    _validate_ci(Path(args.ci_evidence), target_sha)
    _validate_build_evidence(
        Path(args.build_evidence),
        target_sha=target_sha,
        app_image_id=app_id,
        frontend_image_id=frontend_id,
        compose=compose,
        source=Path(args.source_bundle),
        images=Path(args.image_bundle),
    )
    compose_text = _require_file(compose, "candidate compose").read_text(encoding="utf-8")
    for flag in REQUIRED_RUNTIME_FLAGS:
        if flag not in compose_text:
            _fail(f"candidate compose does not wire {flag}")
    return {
        "format": FORMAT,
        "created_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "target_sha": target_sha,
        "parent_production_sha": parent_sha,
        "database": {"from": DB_FROM, "to": DB_TO, "image_id": database_id},
        "images": {
            "app_image_id": app_id,
            "frontend_image_id": frontend_id,
        },
        "previous_images": {
            "app_image_id": previous_app,
            "frontend_image_id": previous_frontend,
            "database_image_id": database_id,
        },
        "contract": contract,
        "runtime_flags": {
            "maintenance_collection_plan_apply_enabled": False,
            "maintenance_collection_canary_project_id": _validate_canary(
                args.canary_project_id
            ),
        },
        "actions": list(COLLECTION_ACTIONS),
        "backup": {"required_assets": BACKUP_REQUIRED_ASSETS},
        "artifacts": artifacts,
        "production_ready": False,
    }


def build(args: argparse.Namespace) -> int:
    output = Path(args.output).absolute()
    staging = _new_staging(output)
    try:
        artifacts = _copy_candidate_artifacts(args, staging)
        payload = _base_payload(args, artifacts)
        _write_manifest(staging, payload)
        _publish_directory(staging, output)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return 0


def _package_dir(value: str) -> Path:
    path = Path(value).absolute()
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        _fail("package directory is missing")
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        _fail("package must be a real directory")
    return path


def _verify_package(path_value: str) -> tuple[Path, dict[str, Any]]:
    package = _package_dir(path_value)
    manifest = _require_file(package / "manifest.json", "manifest")
    checksum = _require_file(package / "manifest.sha256", "manifest checksum")
    expected_line = f"{_sha256_file(manifest)}  manifest.json\n"
    if checksum.read_text(encoding="ascii") != expected_line:
        _fail("manifest.sha256 mismatch")
    payload = _load_json(manifest, "manifest")
    if payload.get("format") != FORMAT:
        _fail("unexpected manifest format")
    target = _validated(str(payload.get("target_sha", "")), SHA40, "target SHA")
    parent = _validated(
        str(payload.get("parent_production_sha", "")),
        SHA40,
        "parent production SHA",
    )
    if target == parent:
        _fail("target SHA equals parent production SHA")
    database = payload.get("database")
    if not isinstance(database, dict) or database.get("from") != DB_FROM or database.get("to") != DB_TO:
        _fail("manifest database path is not d9->c8")
    _validated(str(database.get("image_id", "")), IMAGE_ID, "database image id")
    for group in ("images", "previous_images"):
        values = payload.get(group)
        if not isinstance(values, dict):
            _fail(f"manifest missing {group}")
        for key, value in values.items():
            if not key.endswith("image_id"):
                _fail(f"unexpected {group} key")
            _validated(str(value), IMAGE_ID, f"{group}.{key}")
    runtime = payload.get("runtime_flags")
    if not isinstance(runtime, dict) or runtime.get("maintenance_collection_plan_apply_enabled") is not False:
        _fail("package runtime apply flag must start false")
    _validate_canary(str(runtime.get("maintenance_collection_canary_project_id", "")))
    if payload.get("actions") != list(COLLECTION_ACTIONS):
        _fail("collection action list mismatch")
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        _fail("manifest artifacts must be a non-empty object")
    expected_names = {"manifest.json", "manifest.sha256"}
    for key, row in artifacts.items():
        if not isinstance(key, str) or not isinstance(row, dict):
            _fail("invalid artifact entry")
        name = row.get("path")
        if not isinstance(name, str) or Path(name).name != name or name in expected_names:
            _fail("artifact path must be a unique basename")
        expected_names.add(name)
        artifact = _require_file(package / name, f"artifact {name}")
        if row.get("sha256") != _sha256_file(artifact) or row.get("size") != artifact.stat().st_size:
            _fail(f"artifact hash/size drift: {name}")
    actual_names = {entry.name for entry in package.iterdir()}
    if actual_names != expected_names:
        _fail("package contains missing or unmanifested files")

    contract_path = package / artifacts.get("contract", {}).get("path", "")
    contract = _contract_values(contract_path)
    if payload.get("contract") != contract:
        _fail("contract metadata/hash mismatch")
    _validate_sbom(package / artifacts.get("sbom", {}).get("path", ""))
    _validate_ci(package / artifacts.get("ci_evidence", {}).get("path", ""), target)
    _validate_build_evidence(
        package / artifacts.get("build_evidence", {}).get("path", ""),
        target_sha=target,
        app_image_id=payload["images"]["app_image_id"],
        frontend_image_id=payload["images"]["frontend_image_id"],
        compose=package / artifacts.get("compose", {}).get("path", ""),
        source=package / artifacts.get("source_bundle", {}).get("path", ""),
        images=package / artifacts.get("image_bundle", {}).get("path", ""),
    )
    for name in PACKAGE_TOOLS:
        if name not in expected_names:
            _fail(f"self-verifying package tool is missing: {name}")

    production_ready = payload.get("production_ready")
    if not isinstance(production_ready, bool):
        _fail("production_ready must be boolean")
    if production_ready:
        if contract["state"] != "approved_for_production_candidate" or contract["production_apply_allowed"] is not True:
            _fail("production-ready package needs promoted true contract")
        preliminary = payload.get("preliminary_candidate")
        final_candidate = payload.get("final_candidate")
        if not isinstance(preliminary, dict) or not isinstance(final_candidate, dict):
            _fail("production-ready package lacks preliminary/final binding")
        preliminary_target = _validated(
            str(preliminary.get("target_sha", "")),
            SHA40,
            "preliminary target SHA",
        )
        final_target = _validated(
            str(final_candidate.get("target_sha", "")),
            SHA40,
            "final candidate target SHA",
        )
        if final_target != target:
            _fail("final candidate target SHA mismatch")
        preliminary_binding = _binding_from_manifest(preliminary, "preliminary")
        final_binding = _binding_from_manifest(final_candidate, "final candidate")
        prelim_path = package / artifacts.get("preliminary_rehearsal", {}).get("path", "")
        final_path = package / artifacts.get("final_rehearsal", {}).get("path", "")
        preliminary_rehearsal = _validate_rehearsal(
            prelim_path,
            stage="preliminary",
            target_sha=preliminary_target,
            parent_sha=parent,
            binding=preliminary_binding,
        )
        final_rehearsal = _validate_rehearsal(
            final_path,
            stage="final",
            target_sha=target,
            parent_sha=parent,
            binding=final_binding,
        )
        if _upload_reference_binding(preliminary_rehearsal) != _upload_reference_binding(
            final_rehearsal
        ):
            _fail("preliminary/final historical upload gap binding mismatch")
    return package, payload


def verify(args: argparse.Namespace) -> int:
    _verify_package(args.package)
    return 0


def preflight(args: argparse.Namespace) -> int:
    _package, payload = _verify_package(args.package)
    if payload.get("production_ready") is not True:
        _fail("package is verified but not production-ready")
    return 0


def validate_historical_upload_gap(args: argparse.Namespace) -> int:
    approval = _validate_historical_gap_approval(
        Path(args.approval),
        parent_production_sha=args.parent_production_sha,
    )
    print(
        json.dumps(
            {
                "format": HISTORICAL_GAP_APPROVAL_FORMAT,
                "approved_missing_count": approval["approved_missing_count"],
                "historical_upload_gap_set_sha256": approval["gap_set_sha256"],
                "historical_upload_gap_approval_sha256": approval["approval_sha256"],
                "recovery_search_evidence_sha256": approval[
                    "recovery_search_evidence_sha256"
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


def audit_upload_references(args: argparse.Namespace) -> int:
    approval = (
        Path(args.historical_upload_gap_approval)
        if args.historical_upload_gap_approval is not None
        else None
    )
    payload = _audit_upload_references(
        uploads_root=Path(args.uploads_root),
        references_path=Path(args.references),
        parent_production_sha=args.parent_production_sha,
        approval_path=approval,
    )
    output = Path(args.output).absolute()
    if output.exists() or output.is_symlink():
        _fail("upload reference audit output already exists")
    output.parent.resolve(strict=True)
    with output.open("x", encoding="utf-8") as stream:
        stream.write(_canonical_bytes(payload).decode("utf-8"))
        stream.flush()
        os.fsync(stream.fileno())
    output.chmod(0o600)
    return 0


def finalize(args: argparse.Namespace) -> int:
    preliminary, preliminary_payload = _verify_package(args.preliminary_package)
    candidate, candidate_payload = _verify_package(args.final_candidate_package)
    if preliminary_payload.get("production_ready") is not False:
        _fail("preliminary package must not already be production-ready")
    if preliminary_payload["contract"]["production_apply_allowed"] is not False:
        _fail("preliminary contract must keep production_apply_allowed=false")
    if candidate_payload.get("production_ready") is not False:
        _fail("final candidate package must not already be production-ready")
    if candidate_payload["contract"]["production_apply_allowed"] is not True:
        _fail("final candidate contract must set production_apply_allowed=true")
    if preliminary_payload["target_sha"] == candidate_payload["target_sha"]:
        _fail("contract promotion must produce a distinct final target SHA")
    if preliminary_payload["parent_production_sha"] != candidate_payload["parent_production_sha"]:
        _fail("preliminary/final parent production SHA mismatch")
    preliminary_binding = _rehearsal_binding(preliminary, preliminary_payload)
    candidate_binding = _rehearsal_binding(candidate, candidate_payload)
    preliminary_rehearsal = _validate_rehearsal(
        Path(args.preliminary_rehearsal),
        stage="preliminary",
        target_sha=preliminary_payload["target_sha"],
        parent_sha=candidate_payload["parent_production_sha"],
        binding=preliminary_binding,
    )
    final_rehearsal = _validate_rehearsal(
        Path(args.final_rehearsal),
        stage="final",
        target_sha=candidate_payload["target_sha"],
        parent_sha=candidate_payload["parent_production_sha"],
        binding=candidate_binding,
    )
    if _upload_reference_binding(preliminary_rehearsal) != _upload_reference_binding(
        final_rehearsal
    ):
        _fail("preliminary/final historical upload gap binding mismatch")
    output = Path(args.output).absolute()
    staging = _new_staging(output)
    try:
        for entry in candidate.iterdir():
            if entry.name in {"manifest.json", "manifest.sha256"}:
                continue
            _copy_artifact(entry, staging / entry.name, executable=entry.name in PACKAGE_TOOLS)
        _copy_artifact(
            Path(args.preliminary_rehearsal),
            staging / "preliminary-rehearsal.json",
        )
        _copy_artifact(Path(args.final_rehearsal), staging / "final-rehearsal.json")
        artifacts = {
            key: dict(value) for key, value in candidate_payload["artifacts"].items()
        }
        artifacts["preliminary_rehearsal"] = _artifact_row(
            staging / "preliminary-rehearsal.json"
        )
        artifacts["final_rehearsal"] = _artifact_row(staging / "final-rehearsal.json")
        payload = dict(candidate_payload)
        payload["created_at"] = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
        payload["artifacts"] = artifacts
        payload["preliminary_candidate"] = {
            "target_sha": preliminary_payload["target_sha"],
            "manifest_sha256": preliminary_binding["package_manifest_sha256"],
            "contract_sha256": preliminary_binding["contract_sha256"],
            "candidate_compose_sha256": preliminary_binding["candidate_compose_sha256"],
            "database_image_id": preliminary_binding["database_image_id"],
            "app_image_id": preliminary_binding["app_image_id"],
            "frontend_image_id": preliminary_binding["frontend_image_id"],
        }
        payload["final_candidate"] = {
            "target_sha": candidate_payload["target_sha"],
            "manifest_sha256": candidate_binding["package_manifest_sha256"],
            "contract_sha256": candidate_binding["contract_sha256"],
            "candidate_compose_sha256": candidate_binding["candidate_compose_sha256"],
            "database_image_id": candidate_binding["database_image_id"],
            "app_image_id": candidate_binding["app_image_id"],
            "frontend_image_id": candidate_binding["frontend_image_id"],
        }
        payload["production_ready"] = True
        _write_manifest(staging, payload)
        _publish_directory(staging, output)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return 0


def _candidate_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--target-sha", required=True)
    parser.add_argument("--parent-production-sha", required=True)
    parser.add_argument("--app-image-id", required=True)
    parser.add_argument("--frontend-image-id", required=True)
    parser.add_argument("--database-image-id", required=True)
    parser.add_argument("--previous-app-image-id", required=True)
    parser.add_argument("--previous-frontend-image-id", required=True)
    parser.add_argument("--candidate-compose", required=True)
    parser.add_argument("--contract", required=True)
    parser.add_argument("--sbom", required=True)
    parser.add_argument("--build-evidence", required=True)
    parser.add_argument("--source-bundle", required=True)
    parser.add_argument("--image-bundle", required=True)
    parser.add_argument("--ci-evidence", required=True)
    parser.add_argument("--canary-project-id", required=True)
    parser.add_argument("--output", required=True)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    commands = root.add_subparsers(dest="command", required=True)
    build_parser = commands.add_parser("build")
    _candidate_arguments(build_parser)
    build_parser.set_defaults(func=build)
    for name, handler in (("verify", verify), ("preflight", preflight)):
        command = commands.add_parser(name)
        command.add_argument("package")
        command.set_defaults(func=handler)
    finalize_parser = commands.add_parser("finalize")
    finalize_parser.add_argument("preliminary_package")
    finalize_parser.add_argument("final_candidate_package")
    finalize_parser.add_argument("--preliminary-rehearsal", required=True)
    finalize_parser.add_argument("--final-rehearsal", required=True)
    finalize_parser.add_argument("--output", required=True)
    finalize_parser.set_defaults(func=finalize)
    approval_parser = commands.add_parser("validate-historical-upload-gap")
    approval_parser.add_argument("approval")
    approval_parser.add_argument("--parent-production-sha", required=True)
    approval_parser.set_defaults(func=validate_historical_upload_gap)
    audit_parser = commands.add_parser("audit-upload-references")
    audit_parser.add_argument("--uploads-root", required=True)
    audit_parser.add_argument("--references", required=True)
    audit_parser.add_argument("--parent-production-sha", required=True)
    audit_parser.add_argument("--historical-upload-gap-approval")
    audit_parser.add_argument("--output", required=True)
    audit_parser.set_defaults(func=audit_upload_references)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return int(args.func(args))
    except ManifestError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

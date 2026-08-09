"""Conservative, dry-run-by-default reconciliation for Artifact v2 state.

Only UUID object keys matching the server-owned layout are ever considered. The service is
deliberately independent of the public v2 kill switch so operators can reconcile while routes
remain disabled.
"""

from __future__ import annotations

import re
import stat
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.agent_artifact import AgentArtifact
from app.services import agent_files

MIN_GRACE = timedelta(minutes=5)
MAX_GRACE = timedelta(days=30)
DEFAULT_GRACE = timedelta(hours=1)

_EXTENSIONS = "(?:xlsx|docx|pdf|txt|csv|md|jpg|jpeg|png|webp|bmp)"
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_OBJECT_KEY = re.compile(rf"objects/(?P<id>{_UUID})\.(?P<ext>{_EXTENSIONS})\Z")
_TEMP_PART = re.compile(r"artifact-[A-Za-z0-9_-]{6,}\.part\Z")

_ACTIONS = (
    "recover_ready",
    "mark_failed",
    "expire_ready",
    "delete_failed_object",
    "delete_expired_object",
    "delete_orphan_object",
    "delete_temp_part",
)


def _result(*, apply: bool) -> dict:
    return {
        "dry_run": not apply,
        "outcome": "applied" if apply else "dry_run",
        "rows_scanned": 0,
        "objects_scanned": 0,
        "temp_scanned": 0,
        "planned": {action: 0 for action in _ACTIONS},
        "applied": {action: 0 for action in _ACTIONS},
        "skipped": 0,
        "errors": 0,
    }


def _finish(result: dict) -> dict:
    if result["errors"]:
        result["outcome"] = "partial"
    return result


def _is_regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def _old_regular_state(path: Path, *, cutoff: datetime):
    try:
        state = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(state.st_mode) or state.st_mtime >= cutoff.timestamp():
        return None
    return state


def _same_file_state(left, right) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_size,
        left.st_mtime_ns,
        left.st_ctime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_size,
        right.st_mtime_ns,
        right.st_ctime_ns,
    )


def _unlink_if_unchanged(path: Path, expected) -> bool:
    try:
        current = path.lstat()
        if not stat.S_ISREG(current.st_mode) or not _same_file_state(expected, current):
            return False
        path.unlink()
        return True
    except OSError:
        return False


def _owned_directory(root: Path, name: str) -> Path | None:
    path = root / name
    try:
        root_state = root.lstat()
        state = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RuntimeError("artifact directory unavailable") from exc
    if not stat.S_ISDIR(root_state.st_mode) or not stat.S_ISDIR(state.st_mode):
        raise RuntimeError("artifact directory is not a real directory")
    return path


def _object_matches(row: AgentArtifact) -> bool:
    matched = _OBJECT_KEY.fullmatch(str(row.storage_key or ""))
    if matched is None or matched.group("id") != str(row.id):
        return False
    if agent_files._ext_of(row.filename) != matched.group("ext"):
        return False
    try:
        store = agent_files.get_artifact_store()
        path = store.path_for(row.storage_key)
        if not _is_regular_file(path):
            return False
        stored = store.inspect(row.storage_key)
    except agent_files.FileError:
        return False
    return stored.size_bytes == row.size_bytes and stored.sha256 == row.sha256


def _row_object_state(row: AgentArtifact, *, cutoff: datetime | None = None):
    matched = _OBJECT_KEY.fullmatch(str(row.storage_key or ""))
    if (
        matched is None
        or matched.group("id") != str(row.id)
        or agent_files._ext_of(row.filename) != matched.group("ext")
    ):
        return None
    try:
        path = agent_files.get_artifact_store().path_for(row.storage_key)
        state = path.lstat()
    except (OSError, agent_files.FileError):
        return None
    if not stat.S_ISREG(state.st_mode):
        return None
    if cutoff is not None and state.st_mtime >= cutoff.timestamp():
        return None
    return path, state


def _conditional_status(
    session_factory: Callable[[], Session],
    *,
    artifact_id: str,
    expected: str,
    target: str,
) -> bool:
    with session_factory.begin() as db:
        changed = db.execute(
            update(AgentArtifact)
            .where(AgentArtifact.id == artifact_id, AgentArtifact.status == expected)
            .values(status=target)
        )
        return changed.rowcount == 1


_AUTHORIZED = "authorized"
_DENIED = "denied"
_UNKNOWN = "unknown"


def _recovery_decision(row: AgentArtifact) -> str:
    """Tri-state recovery proof: transient uncertainty must preserve retry state."""
    try:
        if not _object_matches(row):
            return _DENIED
        return (
            _AUTHORIZED
            if agent_files._reconcile_ready_authorized(row)
            else _DENIED
        )
    except Exception:  # noqa: BLE001 - unknown is neither allow nor permanent deny
        return _UNKNOWN


def reconcile_agent_artifacts(
    *,
    apply: bool = False,
    grace_period: timedelta = DEFAULT_GRACE,
    session_factory: Callable[[], Session] = SessionLocal,
    now: datetime | None = None,
    artifact_root: str | Path | None = None,
) -> dict:
    """Plan or apply narrowly proven state repairs; mutation requires ``apply=True``."""
    result = _result(apply=apply)
    if not MIN_GRACE <= grace_period <= MAX_GRACE:
        result["errors"] = 1
        return _finish(result)
    effective_now = now or datetime.now(timezone.utc)
    if effective_now.tzinfo is None:
        result["errors"] = 1
        return _finish(result)
    cutoff = effective_now - grace_period

    try:
        with session_factory() as db:
            rows = list(db.scalars(select(AgentArtifact)))
            for row in rows:
                db.expunge(row)
    except Exception:
        result["errors"] = 1
        return _finish(result)

    result["rows_scanned"] = len(rows)
    for row in rows:
        if row.status in {"prepared", "validating"} and row.created_at < cutoff:
            decision = _recovery_decision(row)
            if decision == _UNKNOWN:
                result["errors"] += 1
                result["skipped"] += 1
                continue
            if decision == _AUTHORIZED:
                action = "recover_ready"
                target = "ready"
            else:
                action = "mark_failed"
                target = "failed"
            result["planned"][action] += 1
            if apply:
                try:
                    # Planning and mutation are intentionally separate for dry-run
                    # visibility.  Repeat both object integrity and the exact publisher
                    # principal/scope contract immediately before the CAS.  A later
                    # account change is still enforced by every ready/download read;
                    # PostgreSQL authorization rows and filesystem objects cannot share
                    # one atomic lock domain.
                    if target == "ready":
                        current_decision = _recovery_decision(row)
                        if current_decision == _UNKNOWN:
                            result["errors"] += 1
                            result["skipped"] += 1
                            continue
                        if current_decision == _DENIED:
                            result["planned"]["recover_ready"] -= 1
                            result["planned"]["mark_failed"] += 1
                            action = "mark_failed"
                            target = "failed"
                    if _conditional_status(
                        session_factory,
                        artifact_id=row.id,
                        expected=row.status,
                        target=target,
                    ):
                        result["applied"][action] += 1
                    else:
                        result["skipped"] += 1
                except Exception:
                    result["errors"] += 1

        if row.status == "failed" and row.created_at < cutoff:
            located = _row_object_state(row, cutoff=cutoff)
            if located is not None:
                result["planned"]["delete_failed_object"] += 1
                if apply:
                    path, state = located
                    if _unlink_if_unchanged(path, state):
                        result["applied"]["delete_failed_object"] += 1
                    else:
                        result["errors"] += 1

        if row.status == "ready" and row.expires_at <= effective_now:
            located = _row_object_state(row)
            result["planned"]["expire_ready"] += 1
            if located is not None:
                result["planned"]["delete_expired_object"] += 1
            if apply:
                try:
                    changed = _conditional_status(
                        session_factory,
                        artifact_id=row.id,
                        expected="ready",
                        target="expired",
                    )
                except Exception:
                    result["errors"] += 1
                    changed = False
                if changed:
                    result["applied"]["expire_ready"] += 1
                    if located is not None:
                        path, state = located
                        if _unlink_if_unchanged(path, state):
                            result["applied"]["delete_expired_object"] += 1
                        else:
                            result["errors"] += 1
                else:
                    result["skipped"] += 1

        # The status transition and object deletion cannot be one atomic transaction.
        # Keep expired rows as durable retry markers when an unlink fails after commit.
        if row.status == "expired":
            located = _row_object_state(row)
            if located is not None:
                result["planned"]["delete_expired_object"] += 1
                if apply:
                    path, state = located
                    if _unlink_if_unchanged(path, state):
                        result["applied"]["delete_expired_object"] += 1
                    else:
                        result["errors"] += 1

    references = {
        row.storage_key for row in rows
        if isinstance(row.storage_key, str) and _OBJECT_KEY.fullmatch(row.storage_key)
    }
    root = Path(artifact_root) if artifact_root is not None else agent_files._dir()
    try:
        objects_dir = _owned_directory(root, "objects")
        if objects_dir is not None:
            for path in objects_dir.iterdir():
                result["objects_scanned"] += 1
                matched = _OBJECT_KEY.fullmatch(f"objects/{path.name}")
                if matched is None:
                    result["skipped"] += 1
                    continue
                key = f"objects/{path.name}"
                if key in references:
                    continue
                state = _old_regular_state(path, cutoff=cutoff)
                if state is None:
                    result["skipped"] += 1
                    continue
                result["planned"]["delete_orphan_object"] += 1
                if apply:
                    if _unlink_if_unchanged(path, state):
                        result["applied"]["delete_orphan_object"] += 1
                    else:
                        result["errors"] += 1

        temp_dir = _owned_directory(root, ".tmp")
        if temp_dir is not None:
            for path in temp_dir.iterdir():
                result["temp_scanned"] += 1
                if _TEMP_PART.fullmatch(path.name) is None:
                    result["skipped"] += 1
                    continue
                state = _old_regular_state(path, cutoff=cutoff)
                if state is None:
                    result["skipped"] += 1
                    continue
                result["planned"]["delete_temp_part"] += 1
                if apply:
                    if _unlink_if_unchanged(path, state):
                        result["applied"]["delete_temp_part"] += 1
                    else:
                        result["errors"] += 1
    except (OSError, RuntimeError):
        result["errors"] += 1
    return _finish(result)

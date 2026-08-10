"""Conservative, dry-run-by-default reconciliation for Artifact v2 state.

Only UUID object keys matching the server-owned layout are ever considered. The service is
deliberately independent of the public v2 kill switch so operators can reconcile while routes
remain disabled.
"""

from __future__ import annotations

import hashlib
import re
import stat
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.agent_artifact import AgentArtifact, AgentArtifactAudit
from app.services import agent_files

MIN_GRACE = timedelta(minutes=5)
MAX_GRACE = timedelta(days=30)
DEFAULT_GRACE = timedelta(hours=1)

_EXTENSIONS = "(?:xlsx|docx|pdf|txt|csv|md|jpg|jpeg|png|webp|bmp)"
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_OBJECT_KEY = re.compile(rf"objects/(?P<id>{_UUID})\.(?P<ext>{_EXTENSIONS})\Z")
_TEMP_PART = re.compile(r"artifact-[A-Za-z0-9_-]{6,}\.part\Z")

_ACTIONS = (
    "mark_failed",
    "expire_ready",
    "report_ready_binding_invalid",
    "report_ready_object_missing",
    "report_ready_object_invalid",
    "report_ready_integrity_mismatch",
    "report_nonready_without_receipt",
    "report_nonready_binding_invalid",
    "delete_failed_object",
    "delete_expired_object",
    "delete_orphan_object",
    "delete_temp_part",
)

_DECISION_REASONS = (
    "authorization_denied",
    "authorization_unknown",
    "binding_invalid",
    "integrity_mismatch",
    "object_invalid",
    "object_locator_invalid",
    "object_missing",
    "object_oversize",
    "publisher_completion_receipt_missing",
    "store_unknown",
)
_PHYSICAL_DELETE_ACTIONS = (
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
        "disabled": {action: 0 for action in _ACTIONS},
        "decisions": {reason: 0 for reason in _DECISION_REASONS},
        "unresolved": 0,
        "requires_operator": False,
        "skipped": 0,
        "errors": 0,
    }


def _finish(result: dict) -> dict:
    planned_delete_backlog = sum(
        int(result["planned"].get(action, 0))
        for action in _PHYSICAL_DELETE_ACTIONS
    )
    result["requires_operator"] = bool(
        result["errors"]
        or result["unresolved"]
        or any(result["disabled"].values())
        or (result["dry_run"] and planned_delete_backlog)
    )
    if result["errors"]:
        result["outcome"] = "partial"
    elif result["dry_run"] and result["requires_operator"]:
        result["outcome"] = "dry_run_requires_operator"
    elif result["unresolved"]:
        result["outcome"] = (
            "requires_operator"
        )
    elif not result["dry_run"] and any(result["disabled"].values()):
        result["outcome"] = "applied_with_disabled_actions"
    return result


def _old_regular_state(path: Path, *, cutoff: datetime):
    try:
        state = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(state.st_mode) or state.st_mtime >= cutoff.timestamp():
        return None
    return state


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


_AUTHORIZED = "authorized"
_DENIED = "denied"
_UNKNOWN = "unknown"


def _object_assessment(row: AgentArtifact) -> tuple[str, str]:
    """Classify immutable-object evidence without converting outages to denial."""
    matched = _OBJECT_KEY.fullmatch(str(row.storage_key or ""))
    if matched is None or matched.group("id") != str(row.id):
        return _DENIED, "object_locator_invalid"
    if agent_files._ext_of(row.filename) != matched.group("ext"):
        return _DENIED, "object_locator_invalid"
    try:
        stored = agent_files.get_artifact_store().read_bytes(
            row.storage_key,
            max_bytes=agent_files._MAX_DOWNLOAD_BYTES,
        )
    except agent_files.ArtifactObjectInvalid as exc:
        return _DENIED, exc.reason_code
    except (agent_files.ArtifactStoreUnavailable, agent_files.FileError):
        return _UNKNOWN, "store_unknown"
    except Exception:  # noqa: BLE001 - storage uncertainty cannot prove denial
        return _UNKNOWN, "store_unknown"
    if stored.size_bytes != row.size_bytes or stored.sha256 != row.sha256:
        return _DENIED, "integrity_mismatch"
    return _AUTHORIZED, "object_verified"


def _object_decision(row: AgentArtifact) -> str:
    return _object_assessment(row)[0]


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


def _locked_current(db: Session, artifact_id: str) -> AgentArtifact | None:
    return db.scalar(
        select(AgentArtifact)
        .where(AgentArtifact.id == artifact_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _record_disabled_delete(
    session_factory: Callable[[], Session],
    *,
    artifact_id: str | None,
    action: str,
    locator: str,
) -> int:
    """Idempotently persist delete intent + disabled outcome; never unlink."""
    locator_sha256 = hashlib.sha256(locator.encode("utf-8")).hexdigest()
    decision_key = hashlib.sha256(
        "\x00".join((
            "artifact-delete-decision/v1",
            action,
            artifact_id or "",
            locator_sha256,
        )).encode("utf-8")
    ).hexdigest()
    detail = {
        "locator_sha256": locator_sha256,
        "reason": "storage_conditional_delete_unavailable",
    }
    inserted = 0
    with session_factory.begin() as db:
        for outcome in ("intent", "disabled"):
            statement = (
                pg_insert(AgentArtifactAudit)
                .values(
                    artifact_id=artifact_id,
                    decision_key=decision_key,
                    action=action,
                    outcome=outcome,
                    actor="system:artifact-reconciler",
                    detail=detail,
                )
                .on_conflict_do_nothing(
                    index_elements=["decision_key", "outcome"]
                )
            )
            inserted += int(db.execute(statement).rowcount or 0)
    return inserted


def _stale_nonready_assessment(row: AgentArtifact) -> tuple[str, str]:
    """Classify a stale marker; AUTHORIZED is not a completion receipt."""
    try:
        agent_files._validated_artifact_metadata(row)
    except agent_files.FileError:
        # An invalid aggregate binding cannot safely be re-signed into any state.
        return _UNKNOWN, "binding_invalid"
    object_decision, object_reason = _object_assessment(row)
    if object_decision != _AUTHORIZED:
        return object_decision, object_reason
    try:
        if agent_files._reconcile_ready_authorized(row):
            return _AUTHORIZED, "publisher_completion_receipt_missing"
        return _DENIED, "authorization_denied"
    except Exception:  # noqa: BLE001 - unknown is neither allow nor permanent deny
        return _UNKNOWN, "authorization_unknown"


def _stale_nonready_decision(row: AgentArtifact) -> str:
    return _stale_nonready_assessment(row)[0]


def _apply_stale_nonready(
    session_factory: Callable[[], Session],
    *,
    artifact_id: str,
    cutoff: datetime,
) -> tuple[str, str, str, int]:
    """Re-read all mutable facts under the row lock before any terminal change."""
    with session_factory.begin() as db:
        row = _locked_current(db, artifact_id)
        if (
            row is None
            or row.status not in {"prepared", "validating"}
            or row.created_at >= cutoff
        ):
            return "skipped", _UNKNOWN, "not_current", 0
        decision, reason = _stale_nonready_assessment(row)
        if reason == "binding_invalid":
            action = "report_nonready_binding_invalid"
            inserted = _record_reconcile_observation(
                db,
                row=row,
                action=action,
                reason=reason,
            )
            return action, decision, reason, inserted
        if decision == _AUTHORIZED:
            action = "report_nonready_without_receipt"
            inserted = _record_reconcile_observation(
                db,
                row=row,
                action=action,
                reason="publisher_completion_receipt_missing",
            )
            return action, decision, reason, inserted
        if decision != _DENIED:
            return "retained", decision, reason, 0
        changed = agent_files._transition_locked_bound_status(
            db,
            row,
            expected=row.status,
            target="failed",
            actor="system:artifact-reconciler",
            reason=f"reconcile_{reason}",
        )
        return (
            ("mark_failed" if changed else "skipped"),
            decision,
            reason,
            int(changed),
        )


def _confirmed_terminal_locator(
    session_factory: Callable[[], Session],
    *,
    artifact_id: str,
    expected: str,
    cutoff: datetime | None = None,
) -> str | None:
    with session_factory.begin() as db:
        row = _locked_current(db, artifact_id)
        if row is None or row.status != expected:
            return None
        if cutoff is not None and row.created_at >= cutoff:
            return None
        agent_files._validated_artifact_metadata(row)
        return row.storage_key


def _ready_anomaly(row: AgentArtifact) -> tuple[str | None, str]:
    try:
        agent_files._validated_artifact_metadata(row)
    except agent_files.FileError:
        return "report_ready_binding_invalid", "binding_invalid"
    decision, reason = _object_assessment(row)
    if decision == _AUTHORIZED:
        return None, reason
    if decision == _UNKNOWN:
        return None, reason
    if reason == "integrity_mismatch":
        return "report_ready_integrity_mismatch", reason
    if reason == "object_missing":
        return "report_ready_object_missing", reason
    return "report_ready_object_invalid", reason


def _record_reconcile_observation(
    db: Session,
    *,
    row: AgentArtifact,
    action: str,
    reason: str,
) -> int:
    locator_sha256 = hashlib.sha256(
        str(row.storage_key or "").encode("utf-8")
    ).hexdigest()
    decision_key = hashlib.sha256(
        "\x00".join((
            "artifact-reconcile-observation/v1",
            action,
            str(row.id),
            str(row.sha256 or ""),
            locator_sha256,
        )).encode("utf-8")
    ).hexdigest()
    statement = (
        pg_insert(AgentArtifactAudit)
        .values(
            artifact_id=row.id,
            decision_key=decision_key,
            action=action,
            outcome="observed",
            actor="system:artifact-reconciler",
            detail={
                "reason": reason,
                "locator_sha256": locator_sha256,
            },
        )
        .on_conflict_do_nothing(
            index_elements=["decision_key", "outcome"]
        )
    )
    return int(db.execute(statement).rowcount or 0)


def _apply_ready_evaluation(
    session_factory: Callable[[], Session],
    *,
    artifact_id: str,
    effective_now: datetime,
) -> tuple[str | None, str, int, bool, str | None]:
    """Assess, audit, and optionally expire one locked current ready row."""
    with session_factory.begin() as db:
        row = _locked_current(db, artifact_id)
        if row is None or row.status != "ready":
            return None, "not_current", 0, False, None
        action, reason = _ready_anomaly(row)
        inserted = 0
        if action is not None:
            inserted = _record_reconcile_observation(
                db,
                row=row,
                action=action,
                reason=reason,
            )
        changed = False
        if (
            row.expires_at <= effective_now
            and reason not in {"binding_invalid", "store_unknown"}
        ):
            changed = agent_files._transition_locked_bound_status(
                db,
                row,
                expected="ready",
                target="expired",
                actor="system:artifact-reconciler",
                reason="retention_expired",
            )
        deletable_locator = (
            row.storage_key
            if changed
            and reason in {"object_verified", "integrity_mismatch", "object_oversize"}
            else None
        )
        return action, reason, inserted, changed, deletable_locator


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
        if row.status == "ready":
            planned_action, planned_reason = _ready_anomaly(row)
            if planned_action is not None:
                result["planned"][planned_action] += 1
            expiry_actionable = (
                row.expires_at <= effective_now
                and planned_reason not in {"binding_invalid", "store_unknown"}
            )
            located = _row_object_state(row) if expiry_actionable else None
            if expiry_actionable:
                result["planned"]["expire_ready"] += 1
                if located is not None:
                    result["planned"]["delete_expired_object"] += 1
            if apply:
                try:
                    (
                        current_action,
                        current_reason,
                        inserted,
                        expired_changed,
                        current_locator,
                    ) = _apply_ready_evaluation(
                        session_factory,
                        artifact_id=row.id,
                        effective_now=effective_now,
                    )
                    if current_action is not None:
                        if planned_action != current_action:
                            result["planned"][current_action] += 1
                        if inserted:
                            result["applied"][current_action] += 1
                        else:
                            result["skipped"] += 1
                        result["unresolved"] += 1
                        if current_reason in result["decisions"]:
                            result["decisions"][current_reason] += 1
                    elif current_reason == "store_unknown":
                        result["errors"] += 1
                        result["skipped"] += 1
                        result["decisions"]["store_unknown"] += 1
                    elif planned_action is not None or current_reason == "not_current":
                        # A healthy, unexpired row needs no action and is not a skip.
                        # Count only a stale plan or a row that changed concurrently.
                        result["skipped"] += 1
                    if expired_changed:
                        if not expiry_actionable:
                            result["planned"]["expire_ready"] += 1
                        result["applied"]["expire_ready"] += 1
                        if current_locator is not None:
                            if located is None:
                                result["planned"]["delete_expired_object"] += 1
                            _record_disabled_delete(
                                session_factory,
                                artifact_id=row.id,
                                action="delete_expired_object",
                                locator=current_locator,
                            )
                            result["disabled"]["delete_expired_object"] += 1
                except Exception:
                    result["errors"] += 1
            elif planned_reason == "store_unknown":
                result["errors"] += 1
                result["skipped"] += 1
                result["decisions"]["store_unknown"] += 1
            elif planned_action is not None:
                result["unresolved"] += 1
                if planned_reason in result["decisions"]:
                    result["decisions"][planned_reason] += 1

        if row.status in {"prepared", "validating"} and row.created_at < cutoff:
            planned_decision, planned_reason = _stale_nonready_assessment(row)
            if planned_decision == _DENIED:
                result["planned"]["mark_failed"] += 1
            elif planned_decision == _AUTHORIZED:
                result["planned"]["report_nonready_without_receipt"] += 1
            elif planned_reason == "binding_invalid":
                result["planned"]["report_nonready_binding_invalid"] += 1
            if apply:
                try:
                    outcome, current_decision, current_reason, inserted = (
                        _apply_stale_nonready(
                            session_factory,
                            artifact_id=row.id,
                            cutoff=cutoff,
                        )
                    )
                    if current_reason in result["decisions"]:
                        result["decisions"][current_reason] += 1
                    if outcome == "mark_failed":
                        if planned_decision != _DENIED:
                            result["planned"]["mark_failed"] += 1
                        result["applied"]["mark_failed"] += 1
                    elif outcome == "report_nonready_without_receipt":
                        if planned_decision != _AUTHORIZED:
                            result["planned"][outcome] += 1
                        if inserted:
                            result["applied"][outcome] += 1
                        else:
                            result["skipped"] += 1
                        result["unresolved"] += 1
                    elif outcome == "report_nonready_binding_invalid":
                        if planned_reason != "binding_invalid":
                            result["planned"][outcome] += 1
                        if inserted:
                            result["applied"][outcome] += 1
                        else:
                            result["skipped"] += 1
                        result["unresolved"] += 1
                    else:
                        result["skipped"] += 1
                        if current_decision == _UNKNOWN:
                            result["errors"] += 1
                except Exception:
                    result["errors"] += 1
            elif planned_reason == "binding_invalid":
                result["decisions"]["binding_invalid"] += 1
                result["unresolved"] += 1
            elif planned_decision == _UNKNOWN:
                result["errors"] += 1
                result["skipped"] += 1
                if planned_reason in result["decisions"]:
                    result["decisions"][planned_reason] += 1
            else:
                result["decisions"][planned_reason] += 1
                if planned_decision == _AUTHORIZED:
                    result["unresolved"] += 1

        if row.status == "failed" and row.created_at < cutoff:
            located = _row_object_state(row, cutoff=cutoff)
            if located is not None:
                result["planned"]["delete_failed_object"] += 1
                if apply:
                    try:
                        locator = _confirmed_terminal_locator(
                            session_factory,
                            artifact_id=row.id,
                            expected="failed",
                            cutoff=cutoff,
                        )
                        if locator is None:
                            result["skipped"] += 1
                        else:
                            _record_disabled_delete(
                                session_factory,
                                artifact_id=row.id,
                                action="delete_failed_object",
                                locator=locator,
                            )
                            result["disabled"]["delete_failed_object"] += 1
                    except Exception:
                        result["errors"] += 1

        # Physical deletion is intentionally unavailable. Expired rows remain durable
        # evidence while the reconciler records an idempotent disabled decision.
        if row.status == "expired":
            located = _row_object_state(row)
            if located is not None:
                result["planned"]["delete_expired_object"] += 1
                if apply:
                    try:
                        locator = _confirmed_terminal_locator(
                            session_factory,
                            artifact_id=row.id,
                            expected="expired",
                        )
                        if locator is None:
                            result["skipped"] += 1
                        else:
                            _record_disabled_delete(
                                session_factory,
                                artifact_id=row.id,
                                action="delete_expired_object",
                                locator=locator,
                            )
                            result["disabled"]["delete_expired_object"] += 1
                    except Exception:
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
                    try:
                        _record_disabled_delete(
                            session_factory,
                            artifact_id=None,
                            action="delete_orphan_object",
                            locator=key,
                        )
                        result["disabled"]["delete_orphan_object"] += 1
                    except Exception:
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
                    try:
                        _record_disabled_delete(
                            session_factory,
                            artifact_id=None,
                            action="delete_temp_part",
                            locator=f".tmp/{path.name}",
                        )
                        result["disabled"]["delete_temp_part"] += 1
                    except Exception:
                        result["errors"] += 1
    except (OSError, RuntimeError):
        result["errors"] += 1
    return _finish(result)

"""Crash-safe, dry-run-first Artifact v2 reconciliation."""

from __future__ import annotations

import copy
import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from app import permissions, security
from app.auth import hash_password
from app.db import SessionLocal
from app.models.agent_artifact import AgentArtifact, AgentArtifactAudit
from app.models.system import SysUser
from app.services import agent_artifact_reconcile, agent_files
from tests.artifact_test_support import force_artifact_state

_NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
_OLD = _NOW - timedelta(hours=2)


def _owner(db, username: str):
    db.add(SysUser(
        username=username, role="admin", password_hash=hash_password("pw123456"),
        permissions=permissions.effective("admin", None),
    ))
    db.commit()
    return agent_files.verified_artifact_owner(db, security.UserContext(
        user_id=username,
        role="admin",
        permissions=permissions.effective("admin", None),
        is_authenticated=True,
        authn="sys_user",
        has_stable_subject=True,
        token_version=0,
    ))


def _run(*, apply: bool = False, artifact_root=None):
    return agent_artifact_reconcile.reconcile_agent_artifacts(
        apply=apply,
        grace_period=timedelta(hours=1),
        session_factory=SessionLocal,
        now=_NOW,
        artifact_root=artifact_root,
    )


def test_crash_after_atomic_rename_never_manufactures_ready(db):
    result = agent_files.save_upload(b"durable", "crash.txt", _owner(db, "crash-owner"))
    artifact = db.get(AgentArtifact, result["file_id"])
    assert artifact is not None
    artifact = force_artifact_state(
        db,
        artifact,
        "validating",
        created_at=_OLD,
        expires_at=_NOW + timedelta(days=1),
    )
    path = agent_files.get_artifact_store().path_for(artifact.storage_key)

    preview = _run()
    db.expire_all()
    assert preview["dry_run"] is True
    assert "recover_ready" not in preview["planned"]
    assert preview["planned"]["mark_failed"] == 0
    assert preview["planned"]["report_nonready_without_receipt"] == 1
    assert db.get(AgentArtifact, artifact.id).status == "validating"
    assert path.read_bytes() == b"durable"

    applied = _run(apply=True)
    db.expire_all()
    assert applied["dry_run"] is False
    assert "recover_ready" not in applied["applied"]
    assert applied["applied"]["mark_failed"] == 0
    assert applied["applied"]["report_nonready_without_receipt"] == 1
    assert applied["outcome"] == "requires_operator"
    assert applied["unresolved"] == 1
    assert db.get(AgentArtifact, artifact.id).status == "validating"
    assert path.read_bytes() == b"durable"
    assert db.query(AgentArtifactAudit).filter_by(
        artifact_id=artifact.id,
        from_status="validating",
        to_status="ready",
        actor="system:artifact-reconciler",
    ).count() == 0
    assert db.query(AgentArtifactAudit).filter_by(
        artifact_id=artifact.id,
        action="report_nonready_without_receipt",
        outcome="observed",
    ).count() == 1

    replay = _run(apply=True)
    db.expire_all()
    assert replay["applied"]["report_nonready_without_receipt"] == 0
    assert db.query(AgentArtifactAudit).filter_by(
        artifact_id=artifact.id,
        action="report_nonready_without_receipt",
        outcome="observed",
    ).count() == 1


def test_crash_recovery_rejects_revoked_publisher_token_version(db):
    created = agent_files.save_upload(
        b"revoked durable bytes",
        "revoked-crash.txt",
        _owner(db, "revoked-crash-owner"),
    )
    artifact = db.get(AgentArtifact, created["file_id"])
    user = db.query(SysUser).filter_by(username="revoked-crash-owner").one()
    assert artifact is not None
    user.token_version = int(user.token_version or 0) + 1
    db.commit()
    artifact = force_artifact_state(
        db,
        artifact,
        "validating",
        created_at=_OLD,
        expires_at=_NOW + timedelta(days=1),
    )

    preview = _run()
    assert preview["planned"]["mark_failed"] == 1

    applied = _run(apply=True)
    db.expire_all()
    assert applied["applied"]["mark_failed"] == 1
    assert db.get(AgentArtifact, artifact.id).status == "failed"


def test_post_store_authorization_unknown_stays_hidden_after_auth_recovers(
    db, monkeypatch
):
    owner = _owner(db, "post-store-unknown-owner")
    real_require = agent_files._require_live_owner
    calls = 0

    def unknown_after_store(candidate):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise agent_files.AuthorizationUnavailable("transient auth outage")
        return real_require(candidate)

    monkeypatch.setattr(agent_files, "_require_live_owner", unknown_after_store)
    with pytest.raises(agent_files.FileError, match="待协调"):
        agent_files.save_upload(
            b"durable but not ready",
            "post-store-unknown.txt",
            owner,
        )

    db.expire_all()
    artifact = db.query(AgentArtifact).filter_by(
        filename="post-store-unknown.txt"
    ).one()
    assert calls == 3
    assert artifact.status == "validating"
    path = agent_files.get_artifact_store().path_for(artifact.storage_key)
    assert path.read_bytes() == b"durable but not ready"
    with pytest.raises(agent_files.ArtifactUnavailable):
        agent_files.get_download_info(artifact.id, owner)

    artifact = force_artifact_state(
        db,
        artifact,
        "validating",
        created_at=_OLD,
        expires_at=_NOW + timedelta(days=1),
    )
    monkeypatch.setattr(agent_files, "_require_live_owner", real_require)
    recovered = _run(apply=True)
    db.expire_all()
    assert "recover_ready" not in recovered["applied"]
    assert recovered["applied"]["mark_failed"] == 0
    assert db.get(AgentArtifact, artifact.id).status == "validating"


@pytest.mark.parametrize("fault", ["directory_fsync", "final_stat"])
def test_post_link_store_fault_preserves_validating_marker_without_auto_ready(
    db, monkeypatch, fault
):
    owner = _owner(db, f"post-link-{fault}-owner")
    filename = f"post-link-{fault}.txt"

    with monkeypatch.context() as scoped:
        if fault == "directory_fsync":
            real_fsync = agent_files.os.fsync
            fsync_calls = 0

            def fail_directory_fsync(fd):
                nonlocal fsync_calls
                fsync_calls += 1
                if fsync_calls == 2:
                    raise OSError("directory fsync unavailable")
                return real_fsync(fd)

            scoped.setattr(agent_files.os, "fsync", fail_directory_fsync)
        else:
            path_type = type(agent_files._dir())
            real_stat = path_type.stat
            real_link = agent_files.os.link
            linked = False
            failed = False

            def track_link(*args, **kwargs):
                nonlocal linked
                result = real_link(*args, **kwargs)
                linked = True
                return result

            def fail_final_stat(path, *args, **kwargs):
                nonlocal failed
                if linked and not failed:
                    failed = True
                    raise OSError("final stat unavailable")
                return real_stat(path, *args, **kwargs)

            scoped.setattr(agent_files.os, "link", track_link)
            scoped.setattr(path_type, "stat", fail_final_stat)

        with pytest.raises(agent_files.FileError, match="待协调"):
            agent_files.save_upload(b"post-link durable", filename, owner)

    db.expire_all()
    artifact = db.query(AgentArtifact).filter_by(filename=filename).one()
    assert artifact.status == "validating"
    path = agent_files.get_artifact_store().path_for(artifact.storage_key)
    assert path.read_bytes() == b"post-link durable"
    assert not list((agent_files._dir() / ".tmp").glob("*.part"))
    with pytest.raises(agent_files.ArtifactUnavailable):
        agent_files.get_download_info(artifact.id, owner)

    artifact = force_artifact_state(
        db,
        artifact,
        "validating",
        created_at=_OLD,
        expires_at=_NOW + timedelta(days=1),
    )
    recovered = _run(apply=True)
    db.expire_all()
    assert recovered["applied"]["mark_failed"] == 0
    assert db.get(AgentArtifact, artifact.id).status == "validating"


@pytest.mark.parametrize(
    "binding_case",
    [
        "missing_user_id",
        "non_int_user_id",
        "mismatched_user_id",
        "missing_token_version",
        "non_int_token_version",
    ],
)
def test_crash_recovery_rejects_invalid_publisher_identity_binding(
    db, binding_case
):
    created = agent_files.save_upload(
        b"bound durable bytes",
        "bound-crash.txt",
        _owner(db, "bound-crash-owner"),
    )
    artifact = db.get(AgentArtifact, created["file_id"])
    assert artifact is not None
    extra = copy.deepcopy(artifact.extra_meta)
    if binding_case == "missing_user_id":
        extra.pop("_publisher_user_id")
    elif binding_case == "non_int_user_id":
        extra["_publisher_user_id"] = "1"
    elif binding_case == "mismatched_user_id":
        extra["_publisher_user_id"] = -1
    elif binding_case == "missing_token_version":
        extra.pop("_publisher_token_version")
    else:
        extra["_publisher_token_version"] = "0"
    artifact = force_artifact_state(
        db,
        artifact,
        "validating",
        extra_meta=extra,
        created_at=_OLD,
        expires_at=_NOW + timedelta(days=1),
    )

    preview = _run()
    assert preview["planned"]["mark_failed"] == 1
    applied = _run(apply=True)
    db.expire_all()
    assert applied["applied"]["mark_failed"] == 1
    assert db.get(AgentArtifact, artifact.id).status == "failed"


def test_stale_nonready_binding_invalid_is_reported_without_resign(db):
    created = agent_files.save_upload(
        b"binding invalid",
        "binding-invalid.txt",
        _owner(db, "binding-invalid-owner"),
    )
    row = db.get(AgentArtifact, created["file_id"])
    assert row is not None
    row = force_artifact_state(
        db,
        row,
        "validating",
        created_at=_OLD,
        expires_at=_NOW + timedelta(days=1),
    )
    original_binding = copy.deepcopy(row.binding_envelope)
    row.extra_meta = {**row.extra_meta, "tampered": True}
    db.commit()

    preview = _run()
    assert preview["planned"]["report_nonready_binding_invalid"] == 1
    assert preview["outcome"] == "dry_run_requires_operator"
    assert preview["unresolved"] == 1

    first = _run(apply=True)
    db.expire_all()
    current = db.get(AgentArtifact, row.id)
    assert current is not None and current.status == "validating"
    assert current.binding_envelope == original_binding
    assert first["applied"]["report_nonready_binding_invalid"] == 1
    assert first["outcome"] == "requires_operator"
    assert db.query(AgentArtifactAudit).filter_by(
        artifact_id=row.id,
        action="report_nonready_binding_invalid",
        outcome="observed",
    ).count() == 1

    second = _run(apply=True)
    assert second["applied"]["report_nonready_binding_invalid"] == 0
    assert db.query(AgentArtifactAudit).filter_by(
        artifact_id=row.id,
        action="report_nonready_binding_invalid",
        outcome="observed",
    ).count() == 1


def test_recovery_authorization_exception_is_retryable_without_auto_ready(
    db, monkeypatch
):
    created = agent_files.save_upload(
        b"unknown authorization",
        "unknown-auth.txt",
        _owner(db, "unknown-auth-owner"),
    )
    artifact = db.get(AgentArtifact, created["file_id"])
    assert artifact is not None
    artifact = force_artifact_state(
        db,
        artifact,
        "validating",
        created_at=_OLD,
        expires_at=_NOW + timedelta(days=1),
    )

    real_authorize = agent_files._reconcile_ready_authorized
    monkeypatch.setattr(
        agent_files,
        "_reconcile_ready_authorized",
        lambda _row: (_ for _ in ()).throw(RuntimeError("auth db unavailable")),
    )
    first = _run(apply=True)
    db.expire_all()
    assert first["outcome"] == "partial"
    assert first["errors"] == 1
    assert first["skipped"] >= 1
    assert first["applied"]["mark_failed"] == 0
    assert db.get(AgentArtifact, artifact.id).status == "validating"

    monkeypatch.setattr(
        agent_files,
        "_reconcile_ready_authorized",
        real_authorize,
    )
    second = _run(apply=True)
    db.expire_all()
    assert second["outcome"] == "requires_operator"
    assert second["applied"]["mark_failed"] == 0
    assert db.get(AgentArtifact, artifact.id).status == "validating"


def test_orphan_and_temp_delete_intents_are_audited_but_physical_delete_is_disabled(
    db, tmp_path
):
    root = tmp_path / "agent_files"
    root.mkdir()
    objects = root / "objects"
    temp = root / ".tmp"
    objects.mkdir(parents=True, exist_ok=True)
    temp.mkdir(parents=True, exist_ok=True)

    orphan = objects / f"{uuid.uuid4()}.txt"
    recent_orphan = objects / f"{uuid.uuid4()}.txt"
    unexpected = objects / "not-an-artifact.txt"
    legacy = root / "abcdef123456.txt"
    stale_part = temp / "artifact-AbC_1234.part"
    unrelated_part = temp / "customer-contract.part"
    for path in (orphan, recent_orphan, unexpected, legacy, stale_part, unrelated_part):
        path.write_bytes(path.name.encode())
    old_ts = _OLD.timestamp()
    for path in (orphan, unexpected, legacy, stale_part, unrelated_part):
        os.utime(path, (old_ts, old_ts))
    os.utime(recent_orphan, (_NOW.timestamp(), _NOW.timestamp()))

    preview = _run(artifact_root=root)
    assert preview["planned"]["delete_orphan_object"] == 1
    assert preview["planned"]["delete_temp_part"] == 1
    assert preview["requires_operator"] is True
    assert preview["outcome"] == "dry_run_requires_operator"
    assert all(path.exists() for path in (
        orphan, recent_orphan, unexpected, legacy, stale_part, unrelated_part
    ))

    applied = _run(apply=True, artifact_root=root)
    assert applied["applied"]["delete_orphan_object"] == 0
    assert applied["applied"]["delete_temp_part"] == 0
    assert all(path.exists() for path in (
        orphan, recent_orphan, unexpected, legacy, stale_part, unrelated_part
    ))
    audits = db.query(AgentArtifactAudit).filter(
        AgentArtifactAudit.action.in_({"delete_orphan_object", "delete_temp_part"})
    ).all()
    assert {(audit.action, audit.outcome) for audit in audits} >= {
        ("delete_orphan_object", "intent"),
        ("delete_orphan_object", "disabled"),
        ("delete_temp_part", "intent"),
        ("delete_temp_part", "disabled"),
    }
    assert all("locator_sha256" in audit.detail for audit in audits)
    assert repr(audits) and orphan.name not in repr([audit.detail for audit in audits])

    first_audit_count = len(audits)
    replay = _run(apply=True, artifact_root=root)
    db.expire_all()
    replay_audits = db.query(AgentArtifactAudit).filter(
        AgentArtifactAudit.action.in_({"delete_orphan_object", "delete_temp_part"})
    ).all()
    assert replay["outcome"] == "applied_with_disabled_actions"
    assert len(replay_audits) == first_audit_count


def test_disabled_delete_decision_is_idempotent_under_concurrency(db):
    locator = f"objects/{uuid.uuid4()}.txt"

    def record_once():
        return agent_artifact_reconcile._record_disabled_delete(
            SessionLocal,
            artifact_id=None,
            action="delete_orphan_object",
            locator=locator,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        inserted = list(pool.map(lambda _index: record_once(), range(8)))

    db.expire_all()
    rows = db.query(AgentArtifactAudit).filter_by(
        artifact_id=None,
        action="delete_orphan_object",
    ).all()
    assert sum(inserted) == 2
    assert len(rows) == 2
    assert {row.outcome for row in rows} == {"intent", "disabled"}
    assert len({row.decision_key for row in rows}) == 1


def test_transient_store_outage_preserves_stale_validating_marker(db, monkeypatch):
    created = agent_files.save_upload(
        b"store outage",
        "store-outage.txt",
        _owner(db, "store-outage-owner"),
    )
    row = db.get(AgentArtifact, created["file_id"])
    assert row is not None
    row = force_artifact_state(
        db,
        row,
        "validating",
        created_at=_OLD,
        expires_at=_NOW + timedelta(days=1),
    )
    delegate = agent_files.get_artifact_store()

    class UnavailableStore:
        def read_bytes(self, *_args, **_kwargs):
            raise agent_files.ArtifactStoreUnavailable("mount unavailable")

        def path_for(self, storage_key):
            return delegate.path_for(storage_key)

    monkeypatch.setattr(agent_files, "get_artifact_store", UnavailableStore)
    first = _run(apply=True)
    db.expire_all()
    assert first["outcome"] == "partial"
    assert first["applied"]["mark_failed"] == 0
    assert db.get(AgentArtifact, row.id).status == "validating"

    monkeypatch.setattr(agent_files, "get_artifact_store", lambda: delegate)
    second = _run(apply=True)
    db.expire_all()
    assert second["errors"] == 0
    assert second["applied"]["mark_failed"] == 0
    assert db.get(AgentArtifact, row.id).status == "validating"


@pytest.mark.parametrize(
    ("fault", "expected_action"),
    [
        ("binding", "report_ready_binding_invalid"),
        ("missing", "report_ready_object_missing"),
        ("integrity", "report_ready_integrity_mismatch"),
        ("locator", "report_ready_object_invalid"),
    ],
)
@pytest.mark.parametrize("expired_ready", [False, True])
def test_ready_anomalies_are_reported_idempotently_before_any_expiry(
    db, fault, expected_action, expired_ready
):
    created = agent_files.save_upload(
        b"ready-anomaly-original",
        f"ready-{fault}.txt",
        _owner(db, f"ready-{fault}-owner"),
    )
    row = db.get(AgentArtifact, created["file_id"])
    assert row is not None
    row = force_artifact_state(
        db,
        row,
        "ready",
        expires_at=(
            _NOW - timedelta(minutes=1)
            if expired_ready
            else _NOW + timedelta(days=1)
        ),
    )
    object_path = agent_files.get_artifact_store().path_for(row.storage_key)
    if fault == "binding":
        row.extra_meta = {**row.extra_meta, "tampered": True}
        db.commit()
    elif fault == "missing":
        object_path.unlink()
    elif fault == "integrity":
        object_path.write_bytes(b"ready-anomaly-changed")
    else:
        row = force_artifact_state(
            db,
            row,
            "ready",
            storage_key=f"objects/{row.id}.pdf",
        )

    preview = _run()
    assert preview["planned"][expected_action] == 1
    assert db.get(AgentArtifact, row.id).status == "ready"

    first = _run(apply=True)
    db.expire_all()
    assert first["applied"][expected_action] == 1
    expected_status = (
        "ready"
        if not expired_ready or fault == "binding"
        else "expired"
    )
    assert db.get(AgentArtifact, row.id).status == expected_status
    assert db.query(AgentArtifactAudit).filter_by(
        artifact_id=row.id,
        action=expected_action,
        outcome="observed",
    ).count() == 1

    second = _run(apply=True)
    db.expire_all()
    assert second["applied"][expected_action] == 0
    assert second["unresolved"] == (1 if expected_status == "ready" else 0)
    assert db.query(AgentArtifactAudit).filter_by(
        artifact_id=row.id,
        action=expected_action,
        outcome="observed",
    ).count() == 1


def test_ready_anomaly_repair_between_plan_and_locked_apply_is_skipped(
    db, monkeypatch
):
    original = b"concurrent-ready-original"
    created = agent_files.save_upload(
        original,
        "concurrent-ready.txt",
        _owner(db, "concurrent-ready-owner"),
    )
    row = db.get(AgentArtifact, created["file_id"])
    assert row is not None
    row = force_artifact_state(
        db,
        row,
        "ready",
        expires_at=_NOW - timedelta(minutes=1),
    )
    path = agent_files.get_artifact_store().path_for(row.storage_key)
    path.write_bytes(b"concurrent-ready-tamper")
    real_apply = agent_artifact_reconcile._apply_ready_evaluation
    repaired = False

    def repair_then_apply(*args, **kwargs):
        nonlocal repaired
        if not repaired:
            repaired = True
            path.write_bytes(original)
        return real_apply(*args, **kwargs)

    monkeypatch.setattr(
        agent_artifact_reconcile,
        "_apply_ready_evaluation",
        repair_then_apply,
    )
    result = _run(apply=True)

    assert result["planned"]["report_ready_integrity_mismatch"] == 1
    assert result["applied"]["report_ready_integrity_mismatch"] == 0
    assert result["applied"]["expire_ready"] == 1
    assert result["skipped"] >= 1
    db.expire_all()
    assert db.get(AgentArtifact, row.id).status == "expired"
    assert db.query(AgentArtifactAudit).filter_by(
        artifact_id=row.id,
        action="report_ready_integrity_mismatch",
    ).count() == 0


def test_expired_ready_store_unknown_stays_ready_until_reassessment(
    db, monkeypatch
):
    created = agent_files.save_upload(
        b"expired unknown",
        "expired-unknown.txt",
        _owner(db, "expired-unknown-owner"),
    )
    row = db.get(AgentArtifact, created["file_id"])
    assert row is not None
    row = force_artifact_state(
        db,
        row,
        "ready",
        expires_at=_NOW - timedelta(minutes=1),
    )
    delegate = agent_files.get_artifact_store()

    class UnavailableStore:
        def read_bytes(self, *_args, **_kwargs):
            raise agent_files.ArtifactStoreUnavailable("mount unavailable")

        def path_for(self, storage_key):
            return delegate.path_for(storage_key)

    monkeypatch.setattr(agent_files, "get_artifact_store", UnavailableStore)
    first = _run(apply=True)
    db.expire_all()
    assert first["outcome"] == "partial"
    assert first["requires_operator"] is True
    assert first["applied"]["expire_ready"] == 0
    assert db.get(AgentArtifact, row.id).status == "ready"

    monkeypatch.setattr(agent_files, "get_artifact_store", lambda: delegate)
    second = _run(apply=True)
    db.expire_all()
    assert second["applied"]["expire_ready"] == 1
    assert db.get(AgentArtifact, row.id).status == "expired"

    second = _run(apply=True)
    db.expire_all()
    assert second["unresolved"] == 0
    assert db.query(AgentArtifactAudit).filter_by(
        artifact_id=row.id,
        action="report_ready_integrity_mismatch",
        outcome="observed",
    ).count() == 0


def test_reconciler_audits_status_repairs_and_retains_objects_without_conditional_delete(db):
    owner = _owner(db, "owner")
    missing = agent_files.save_upload(b"missing", "missing.txt", owner)
    failed = agent_files.save_upload(b"failed", "failed.txt", owner)
    expired = agent_files.save_upload(b"expired", "expired.txt", owner)
    missing_row = db.get(AgentArtifact, missing["file_id"])
    failed_row = db.get(AgentArtifact, failed["file_id"])
    expired_row = db.get(AgentArtifact, expired["file_id"])
    assert missing_row and failed_row and expired_row
    missing_row = force_artifact_state(
        db, missing_row, "prepared", created_at=_OLD,
        expires_at=_NOW + timedelta(days=1),
    )
    failed_row = force_artifact_state(
        db, failed_row, "failed", created_at=_OLD,
        expires_at=_NOW + timedelta(days=1),
    )
    expired_row = force_artifact_state(
        db, expired_row, "ready", created_at=_OLD,
        expires_at=_NOW - timedelta(minutes=30),
    )

    missing_path = agent_files.get_artifact_store().path_for(missing_row.storage_key)
    failed_path = agent_files.get_artifact_store().path_for(failed_row.storage_key)
    expired_path = agent_files.get_artifact_store().path_for(expired_row.storage_key)
    missing_path.unlink()

    preview = _run()
    assert preview["planned"]["mark_failed"] == 1
    assert preview["planned"]["delete_failed_object"] == 1
    assert preview["planned"]["expire_ready"] == 1
    assert preview["planned"]["delete_expired_object"] == 1
    db.expire_all()
    assert db.get(AgentArtifact, missing_row.id).status == "prepared"
    assert db.get(AgentArtifact, expired_row.id).status == "ready"
    assert failed_path.exists() and expired_path.exists()

    applied = _run(apply=True)
    assert applied["applied"]["mark_failed"] == 1
    assert applied["applied"]["delete_failed_object"] == 0
    assert applied["applied"]["expire_ready"] == 1
    assert applied["applied"]["delete_expired_object"] == 0
    assert applied["disabled"]["delete_failed_object"] == 1
    assert applied["disabled"]["delete_expired_object"] == 1
    assert applied["outcome"] == "applied_with_disabled_actions"
    db.expire_all()
    assert db.get(AgentArtifact, missing_row.id).status == "failed"
    assert db.get(AgentArtifact, expired_row.id).status == "expired"
    assert failed_path.exists()
    assert expired_path.exists()
    status_audits = db.query(AgentArtifactAudit).filter(
        AgentArtifactAudit.artifact_id.in_({missing_row.id, expired_row.id}),
        AgentArtifactAudit.action == "status_transition",
        AgentArtifactAudit.actor == "system:artifact-reconciler",
    ).all()
    assert {(audit.from_status, audit.to_status) for audit in status_audits} >= {
        ("prepared", "failed"), ("ready", "expired")
    }
    delete_audits = db.query(AgentArtifactAudit).filter(
        AgentArtifactAudit.action.in_({
            "delete_failed_object", "delete_expired_object"
        })
    ).all()
    assert {audit.outcome for audit in delete_audits} == {"intent", "disabled"}


def test_reconciler_never_calls_unlink_and_records_disabled_delete_outcome(
    db, monkeypatch
):
    created = agent_files.save_upload(b"retry", "retry.txt", _owner(db, "owner"))
    row = db.get(AgentArtifact, created["file_id"])
    assert row is not None
    row = force_artifact_state(
        db, row, "ready", created_at=_OLD,
        expires_at=_NOW - timedelta(minutes=30),
    )
    path = agent_files.get_artifact_store().path_for(row.storage_key)

    path_type = type(path)
    real_unlink = path_type.unlink

    def forbidden_unlink(candidate, *args, **kwargs):
        if candidate == path:
            raise AssertionError("reconciler must not physically unlink Artifact objects")
        return real_unlink(candidate, *args, **kwargs)

    monkeypatch.setattr(path_type, "unlink", forbidden_unlink)
    first = _run(apply=True)
    db.expire_all()
    assert first["outcome"] == "applied_with_disabled_actions"
    assert first["applied"]["expire_ready"] == 1
    assert first["applied"]["delete_expired_object"] == 0
    assert first["disabled"]["delete_expired_object"] == 1
    assert first["errors"] == 0
    assert db.get(AgentArtifact, row.id).status == "expired"
    assert path.exists()

    audits = db.query(AgentArtifactAudit).filter_by(
        artifact_id=row.id, action="delete_expired_object"
    ).all()
    assert [audit.outcome for audit in audits[-2:]] == ["intent", "disabled"]
    assert path.exists()


def test_reconcile_status_change_and_audit_are_one_atomic_transaction(db, monkeypatch):
    created = agent_files.save_upload(
        b"atomic audit", "atomic-audit.txt", _owner(db, "atomic-audit-owner")
    )
    row = db.get(AgentArtifact, created["file_id"])
    assert row is not None
    row = force_artifact_state(
        db,
        row,
        "validating",
        created_at=_OLD,
        expires_at=_NOW + timedelta(days=1),
    )
    agent_files.get_artifact_store().path_for(row.storage_key).unlink()

    monkeypatch.setattr(
        agent_files,
        "_add_artifact_audit",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("audit unavailable")),
    )
    monkeypatch.setattr(agent_artifact_reconcile, "_owned_directory", lambda *_args: None)
    result = _run(apply=True)

    db.expire_all()
    assert result["errors"] == 1
    assert result["applied"]["mark_failed"] == 0
    assert db.get(AgentArtifact, row.id).status == "validating"


def test_ready_anomaly_observation_and_expiry_roll_back_together(db, monkeypatch):
    created = agent_files.save_upload(
        b"ready atomic original",
        "ready-atomic.txt",
        _owner(db, "ready-atomic-owner"),
    )
    row = db.get(AgentArtifact, created["file_id"])
    assert row is not None
    row = force_artifact_state(
        db,
        row,
        "ready",
        expires_at=_NOW - timedelta(minutes=1),
    )
    agent_files.get_artifact_store().path_for(row.storage_key).write_bytes(
        b"ready atomic tamper"
    )
    real_transition = agent_files._transition_locked_bound_status

    def fail_after_transition(*args, **kwargs):
        changed = real_transition(*args, **kwargs)
        assert changed is True
        raise RuntimeError("transaction commit must not proceed")

    monkeypatch.setattr(
        agent_files,
        "_transition_locked_bound_status",
        fail_after_transition,
    )
    monkeypatch.setattr(agent_artifact_reconcile, "_owned_directory", lambda *_args: None)
    result = _run(apply=True)

    db.expire_all()
    assert result["errors"] == 1
    assert result["applied"]["report_ready_integrity_mismatch"] == 0
    assert result["applied"]["expire_ready"] == 0
    assert db.get(AgentArtifact, row.id).status == "ready"
    assert db.query(AgentArtifactAudit).filter_by(
        artifact_id=row.id,
        action="report_ready_integrity_mismatch",
        outcome="observed",
    ).count() == 0
    assert db.query(AgentArtifactAudit).filter_by(
        artifact_id=row.id,
        from_status="ready",
        to_status="expired",
    ).count() == 0


def test_reconcile_cli_is_dry_run_by_default_and_apply_is_explicit(monkeypatch, capsys):
    from scripts import agent_artifact_reconcile as cli

    calls = []

    def fake_reconcile(**kwargs):
        calls.append(kwargs)
        return agent_artifact_reconcile._result(apply=kwargs["apply"])

    monkeypatch.setattr(cli, "_load_reconciler", lambda: fake_reconcile)
    assert cli.main([]) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True
    assert calls[-1]["apply"] is False

    assert cli.main(["--apply", "--grace-minutes", "90"]) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is False
    assert calls[-1]["apply"] is True
    assert calls[-1]["grace_period"] == timedelta(minutes=90)

    def unresolved_reconcile(**kwargs):
        result = agent_artifact_reconcile._result(apply=kwargs["apply"])
        result["unresolved"] = 1
        return agent_artifact_reconcile._finish(result)

    monkeypatch.setattr(cli, "_load_reconciler", lambda: unresolved_reconcile)
    assert cli.main([]) == 2
    reported = json.loads(capsys.readouterr().out)
    assert reported["requires_operator"] is True
    assert reported["outcome"] == "dry_run_requires_operator"

    def delete_backlog_reconcile(**kwargs):
        result = agent_artifact_reconcile._result(apply=kwargs["apply"])
        result["planned"]["delete_orphan_object"] = 1
        return agent_artifact_reconcile._finish(result)

    monkeypatch.setattr(cli, "_load_reconciler", lambda: delete_backlog_reconcile)
    assert cli.main([]) == 2
    reported = json.loads(capsys.readouterr().out)
    assert reported["requires_operator"] is True
    assert reported["outcome"] == "dry_run_requires_operator"

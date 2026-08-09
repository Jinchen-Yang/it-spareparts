"""Crash-safe, dry-run-first Artifact v2 reconciliation."""

from __future__ import annotations

import os
import json
import uuid
from datetime import datetime, timedelta, timezone

from app import permissions, security
from app.db import SessionLocal
from app.models.agent_artifact import AgentArtifact
from app.services import agent_artifact_reconcile, agent_files

_NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
_OLD = _NOW - timedelta(hours=2)


def _owner(username: str):
    return agent_files.verified_artifact_owner(security.UserContext(
        user_id=username,
        role="admin",
        permissions=permissions.effective("admin", None),
        is_authenticated=True,
        authn="sys_user",
        has_stable_subject=True,
    ))


def _run(*, apply: bool = False, artifact_root=None):
    return agent_artifact_reconcile.reconcile_agent_artifacts(
        apply=apply,
        grace_period=timedelta(hours=1),
        session_factory=SessionLocal,
        now=_NOW,
        artifact_root=artifact_root,
    )


def test_crash_after_atomic_rename_is_dry_run_then_recovered_ready(db):
    result = agent_files.save_upload(b"durable", "crash.txt", _owner("crash-owner"))
    artifact = db.get(AgentArtifact, result["file_id"])
    assert artifact is not None
    artifact.status = "validating"
    artifact.created_at = _OLD
    artifact.expires_at = _NOW + timedelta(days=1)
    db.commit()
    path = agent_files.get_artifact_store().path_for(artifact.storage_key)

    preview = _run()
    db.expire_all()
    assert preview["dry_run"] is True
    assert preview["planned"]["recover_ready"] == 1
    assert db.get(AgentArtifact, artifact.id).status == "validating"
    assert path.read_bytes() == b"durable"

    applied = _run(apply=True)
    db.expire_all()
    assert applied["dry_run"] is False
    assert applied["applied"]["recover_ready"] == 1
    assert db.get(AgentArtifact, artifact.id).status == "ready"
    assert path.read_bytes() == b"durable"


def test_orphan_and_temp_cleanup_is_strict_dry_run_first_and_preserves_collateral(db, tmp_path):
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
    assert all(path.exists() for path in (
        orphan, recent_orphan, unexpected, legacy, stale_part, unrelated_part
    ))

    applied = _run(apply=True, artifact_root=root)
    assert applied["applied"]["delete_orphan_object"] == 1
    assert applied["applied"]["delete_temp_part"] == 1
    assert not orphan.exists()
    assert not stale_part.exists()
    assert all(path.exists() for path in (recent_orphan, unexpected, legacy, unrelated_part))


def test_reconciler_marks_missing_stale_rows_and_reaps_expired_failed_objects(db):
    missing = agent_files.save_upload(b"missing", "missing.txt", _owner("owner"))
    failed = agent_files.save_upload(b"failed", "failed.txt", _owner("owner"))
    expired = agent_files.save_upload(b"expired", "expired.txt", _owner("owner"))
    missing_row = db.get(AgentArtifact, missing["file_id"])
    failed_row = db.get(AgentArtifact, failed["file_id"])
    expired_row = db.get(AgentArtifact, expired["file_id"])
    assert missing_row and failed_row and expired_row
    missing_row.status = "prepared"
    failed_row.status = "failed"
    expired_row.status = "ready"
    for row in (missing_row, failed_row, expired_row):
        row.created_at = _OLD
    missing_row.expires_at = _NOW + timedelta(days=1)
    failed_row.expires_at = _NOW + timedelta(days=1)
    expired_row.expires_at = _NOW - timedelta(minutes=30)
    db.commit()

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
    assert applied["applied"]["delete_failed_object"] == 1
    assert applied["applied"]["expire_ready"] == 1
    assert applied["applied"]["delete_expired_object"] == 1
    db.expire_all()
    assert db.get(AgentArtifact, missing_row.id).status == "failed"
    assert db.get(AgentArtifact, expired_row.id).status == "expired"
    assert not failed_path.exists()
    assert not expired_path.exists()


def test_expired_object_delete_failure_is_retried_on_next_run(db, monkeypatch):
    created = agent_files.save_upload(b"retry", "retry.txt", _owner("owner"))
    row = db.get(AgentArtifact, created["file_id"])
    assert row is not None
    row.created_at = _OLD
    row.expires_at = _NOW - timedelta(minutes=30)
    db.commit()
    path = agent_files.get_artifact_store().path_for(row.storage_key)

    real_unlink = agent_artifact_reconcile._unlink_if_unchanged
    monkeypatch.setattr(
        agent_artifact_reconcile,
        "_unlink_if_unchanged",
        lambda candidate, state: (
            False if candidate == path else real_unlink(candidate, state)
        ),
    )
    first = _run(apply=True)
    db.expire_all()
    assert first["outcome"] == "partial"
    assert first["applied"]["expire_ready"] == 1
    assert first["applied"]["delete_expired_object"] == 0
    assert first["errors"] == 1
    assert db.get(AgentArtifact, row.id).status == "expired"
    assert path.exists()

    monkeypatch.setattr(agent_artifact_reconcile, "_unlink_if_unchanged", real_unlink)
    second = _run(apply=True)
    assert second["outcome"] == "applied"
    assert second["planned"]["expire_ready"] == 0
    assert second["applied"]["delete_expired_object"] == 1
    assert not path.exists()


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

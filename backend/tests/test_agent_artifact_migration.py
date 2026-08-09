"""Artifact provenance migration is conservative, non-destructive, and reversible."""

from __future__ import annotations

import copy
import json
import os
import threading
import time
import uuid
from io import BytesIO

import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from openpyxl import Workbook

from app import permissions, security
from app.auth import hash_password
from app.db import engine
from app.models.agent_artifact import AgentArtifact
from app.models.system import SysUser
from app.services import agent_files

_HEAD = "b1e7c9d4f2a8"
_PREV = "ad8f6c2e1b47"
_DROP_PREV = "c4e8a1d7f2b6"


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


def _cfg() -> AlembicConfig:
    cfg = AlembicConfig(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(os.path.dirname(__file__), "..", "alembic"))
    return cfg


def test_nonempty_artifact_table_blocks_destructive_downgrade_and_preserves_forward_path(db):
    artifact = agent_files.save_upload(
        b"preserve", "preserve.txt", _owner(db, "migration-owner")
    )
    db.close()

    try:
        # The provenance migration itself is a non-destructive constraint rollback.
        alembic_command.downgrade(_cfg(), _PREV)
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == _PREV
            assert connection.scalar(text(
                "SELECT count(*) FROM agent_artifact WHERE id=CAST(:id AS uuid)"
            ), {"id": artifact["file_id"]}) == 1

        # The older table-dropping migration still blocks non-empty rollback.
        with pytest.raises(DBAPIError, match="downgrade blocked"):
            alembic_command.downgrade(_cfg(), _DROP_PREV)
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == _PREV
            assert connection.scalar(text(
                "SELECT count(*) FROM agent_artifact WHERE id=CAST(:id AS uuid)"
            ), {"id": artifact["file_id"]}) == 1
        alembic_command.upgrade(_cfg(), "head")
        with engine.connect() as connection:
            assert connection.scalar(text(
                "SELECT count(*) FROM agent_artifact WHERE id=CAST(:id AS uuid)"
            ), {"id": artifact["file_id"]}) == 1
    finally:
        alembic_command.upgrade(_cfg(), "head")


def test_empty_artifact_table_allows_downgrade_and_forward_upgrade(db):
    cfg = _cfg()
    alembic_command.downgrade(cfg, _DROP_PREV)
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT to_regclass('agent_artifact')")) is None
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == _DROP_PREV
        alembic_command.upgrade(cfg, "head")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == _HEAD
            assert connection.scalar(text("SELECT to_regclass('agent_artifact')")) == "agent_artifact"
    finally:
        alembic_command.upgrade(cfg, "head")


def test_concurrent_insert_cannot_commit_between_empty_check_and_drop(db):
    """An in-flight insert must become visible before downgrade decides emptiness."""
    alembic_command.downgrade(_cfg(), _PREV)
    artifact_id = str(uuid.uuid4())
    inserter = engine.connect()
    transaction = inserter.begin()
    errors: list[BaseException] = []
    completed: list[bool] = []

    inserter.execute(text(
        """
        INSERT INTO agent_artifact
          (id, owner_sub, filename, media_type, size_bytes, sha256, status,
           storage_key, kind, sensitivity, source_ids, access_scope, extra_meta,
           created_at, expires_at)
        VALUES
          (CAST(:id AS uuid), 'migration-racer', 'race.txt', 'text/plain', 1,
           :sha, 'ready', :storage_key, 'upload', 'critical', '[]'::jsonb,
           '{"policy":"owner_only"}'::jsonb, '{}'::jsonb,
           now(), now() + interval '1 day')
        """
    ), {
        "id": artifact_id,
        "sha": "a" * 64,
        "storage_key": f"objects/{artifact_id}.txt",
    })

    def downgrade():
        try:
            alembic_command.downgrade(_cfg(), _DROP_PREV)
            completed.append(True)
        except BaseException as exc:  # noqa: BLE001 - asserted after thread join
            errors.append(exc)

    worker = threading.Thread(target=downgrade, daemon=True)
    worker.start()
    waiting = False
    try:
        for _ in range(100):
            with engine.connect() as observer:
                waiting = bool(observer.scalar(text(
                    """
                    SELECT EXISTS (
                      SELECT 1
                      FROM pg_stat_activity
                      WHERE datname = current_database()
                        AND pid <> pg_backend_pid()
                        AND wait_event_type = 'Lock'
                        AND query ILIKE '%agent_artifact%'
                    )
                    """
                )))
            if waiting:
                break
            time.sleep(0.05)
        assert waiting, "downgrade never reached the table-lock boundary"

        transaction.commit()
        worker.join(timeout=10)
        assert not worker.is_alive()
        assert not completed
        assert errors and isinstance(errors[0], DBAPIError)
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT to_regclass('agent_artifact')")) == "agent_artifact"
            assert connection.scalar(text(
                "SELECT count(*) FROM agent_artifact WHERE id=CAST(:id AS uuid)"
            ), {"id": artifact_id}) == 1
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == _PREV
    finally:
        if transaction.is_active:
            transaction.rollback()
        inserter.close()
        worker.join(timeout=10)
        alembic_command.upgrade(_cfg(), "head")


def test_v1_rows_migrate_fail_closed_and_survive_downgrade_reupgrade(db):
    cfg = _cfg()
    db.close()
    alembic_command.downgrade(cfg, _PREV)
    upload_id = str(uuid.uuid4())
    generated_id = str(uuid.uuid4())
    legacy_source = str(uuid.uuid4())
    try:
        with engine.begin() as connection:
            for artifact_id, kind, source_ids, scope in (
                (
                    upload_id,
                    "upload",
                    "[]",
                    '{"policy":"owner_only"}',
                ),
                (
                    generated_id,
                    "generated",
                    f'["{legacy_source}"]',
                    '{"version":1,"policy":"current_scope_dominates",'
                    '"required_permissions":["data_purchase_cost"]}',
                ),
            ):
                connection.execute(text(
                    """
                    INSERT INTO agent_artifact
                      (id, owner_sub, filename, media_type, size_bytes, sha256,
                       status, storage_key, kind, sensitivity, source_ids,
                       access_scope, extra_meta, created_at, expires_at)
                    VALUES
                      (CAST(:id AS uuid), 'migration-owner', :filename,
                       'text/plain', 1, :sha, 'ready', :storage_key, :kind,
                       'high', CAST(:source_ids AS jsonb), CAST(:scope AS jsonb),
                       '{}'::jsonb, now(), now() + interval '1 day')
                    """
                ), {
                    "id": artifact_id,
                    "filename": f"{kind}.txt",
                    "sha": "a" * 64,
                    "storage_key": f"objects/{artifact_id}.txt",
                    "kind": kind,
                    "source_ids": source_ids,
                    "scope": scope,
                })

        alembic_command.upgrade(cfg, _HEAD)
        with engine.connect() as connection:
            upload = connection.execute(text(
                "SELECT sensitivity, source_ids, access_scope, extra_meta "
                "FROM agent_artifact WHERE id=CAST(:id AS uuid)"
            ), {"id": upload_id}).mappings().one()
            generated = connection.execute(text(
                "SELECT sensitivity, source_ids, access_scope, extra_meta "
                "FROM agent_artifact WHERE id=CAST(:id AS uuid)"
            ), {"id": generated_id}).mappings().one()

        assert upload["sensitivity"] == "critical"
        assert upload["source_ids"] == []
        assert upload["access_scope"]["schema_version"] == "artifact-access/v2"
        assert upload["access_scope"]["policy"] == "owner_only"
        assert upload["access_scope"]["classification"] == "business_content"
        assert upload["access_scope"]["containment_status"] == "unclassified"
        assert upload["extra_meta"]["legacy_access_scope_v1"] == {
            "policy": "owner_only"
        }

        assert generated["sensitivity"] == "critical"
        assert generated["source_ids"] == []
        assert generated["access_scope"]["schema_version"] == "artifact-access/v2"
        assert generated["access_scope"]["policy"] == "unclassified_deny"
        assert generated["access_scope"]["classification"] == "unclassified"
        assert generated["extra_meta"]["legacy_unproven_source_ids"] == [legacy_source]
        assert generated["extra_meta"]["legacy_access_scope_v1"]["policy"] == (
            "current_scope_dominates"
        )

        # The new rollback removes only the constraint; no row or audit fact is lost.
        alembic_command.downgrade(cfg, _PREV)
        with engine.connect() as connection:
            before_reupgrade = connection.execute(text(
                "SELECT source_ids, access_scope, extra_meta FROM agent_artifact "
                "WHERE id=CAST(:id AS uuid)"
            ), {"id": generated_id}).mappings().one()
            assert before_reupgrade["access_scope"]["policy"] == "unclassified_deny"
            assert before_reupgrade["extra_meta"]["legacy_unproven_source_ids"] == [
                legacy_source
            ]
        alembic_command.upgrade(cfg, _HEAD)
        with engine.connect() as connection:
            after_reupgrade = connection.execute(text(
                "SELECT source_ids, access_scope, extra_meta FROM agent_artifact "
                "WHERE id=CAST(:id AS uuid)"
            ), {"id": generated_id}).mappings().one()
        assert after_reupgrade == before_reupgrade
    finally:
        alembic_command.upgrade(cfg, "head")


def test_postgres_constraint_rejects_missing_null_and_fake_v2_scopes(db):
    owner = _owner(db, "constraint-owner")
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "申请"
    sheet.append(["PN", "数量", "备注"])
    buffer = BytesIO()
    workbook.save(buffer)
    workbook.close()
    identity = agent_files.save_upload(
        buffer.getvalue(), "补库申请模板.xlsx", owner
    )
    evidence = agent_files._mint_report_provenance(
        owner,
        title="采购来源",
        headers=["PN", "成本"],
        rows=[["PN-1", 100]],
        output_name="采购来源.xlsx",
        money_cols=[1],
        contained_resources={"purchases"},
        contained_fields={"purchase_cost"},
    )
    generated = agent_files.write_report(
        "采购来源",
        ["PN", "成本"],
        [["PN-1", 100]],
        "采购来源.xlsx",
        owner,
        money_cols=[1],
        provenance=evidence,
    )
    identity_scope = copy.deepcopy(
        db.get(AgentArtifact, identity["file_id"]).access_scope
    )
    generated_scope = copy.deepcopy(
        db.get(AgentArtifact, generated["file_id"]).access_scope
    )
    db.close()

    invalid_scopes = []
    for missing in (
        "schema_version",
        "sensitivity",
        "condition",
        "source_access_snapshots",
    ):
        value = copy.deepcopy(generated_scope)
        value.pop(missing)
        invalid_scopes.append((generated["file_id"], value))
    invalid_scopes.extend([
        (generated["file_id"], None),
        (
            generated["file_id"],
            {**generated_scope, "source_access_snapshots": []},
        ),
    ])
    fake_identity = copy.deepcopy(identity_scope)
    fake_identity["template_proof"]["classifier_version"] = "model-asserted/v1"
    invalid_scopes.append((identity["file_id"], fake_identity))

    for artifact_id, invalid_scope in invalid_scopes:
        with pytest.raises(DBAPIError):
            with engine.begin() as connection:
                connection.execute(text(
                    "UPDATE agent_artifact SET access_scope=CAST(:scope AS jsonb) "
                    "WHERE id=CAST(:id AS uuid)"
                ), {
                    "id": artifact_id,
                    "scope": json.dumps(invalid_scope, ensure_ascii=False),
                })

"""Artifact provenance migration is conservative and evidence-preserving."""

from __future__ import annotations

import copy
import json
import os
import threading
import time
import uuid

import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app import permissions, security
from app.auth import hash_password
from app.db import engine
from app.models.agent_artifact import AgentArtifact
from app.models.system import SysUser
from app.services import agent_artifact_provenance, agent_files

_HEAD = "c2f8a4d6e9b1"
_PROVENANCE = "b1e7c9d4f2a8"
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


def _test_only_clear_artifact_audit(connection) -> None:
    """Evacuate isolated test evidence; production has no application bypass."""
    assert str(engine.url.database or "").startswith("spareparts_test_")
    connection.execute(text(
        "ALTER TABLE agent_artifact_audit DISABLE TRIGGER USER"
    ))
    try:
        connection.execute(text("TRUNCATE agent_artifact_audit"))
    finally:
        connection.execute(text(
            "ALTER TABLE agent_artifact_audit ENABLE TRIGGER USER"
        ))


def test_c2_downgrade_blocks_nonempty_durable_audit_until_explicitly_cleared(db):
    cfg = _cfg()
    db.close()
    marker = f"migration-audit-canary-{uuid.uuid4()}"
    with engine.begin() as connection:
        audit_id = connection.scalar(text(
            """
            INSERT INTO agent_artifact_audit
              (artifact_id, action, outcome, actor, detail)
            VALUES
              (NULL, 'downgrade_canary', 'success', :actor, '{}'::jsonb)
            RETURNING id
            """
        ), {"actor": marker})

    try:
        with pytest.raises(DBAPIError, match="audit history exists"):
            alembic_command.downgrade(cfg, _PROVENANCE)
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == _HEAD
            assert connection.scalar(text(
                "SELECT count(*) FROM agent_artifact_audit WHERE id=:id"
            ), {"id": audit_id}) == 1

        # Only the test explicitly removes its canary.  The migration must never
        # erase audit evidence on an operator's behalf.
        with engine.begin() as connection:
            _test_only_clear_artifact_audit(connection)
        alembic_command.downgrade(cfg, _PROVENANCE)
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == _PROVENANCE
            assert connection.scalar(text("SELECT to_regclass('agent_artifact_audit')")) is None
        alembic_command.upgrade(cfg, _HEAD)
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == _HEAD
            assert connection.scalar(text("SELECT to_regclass('agent_artifact_audit')")) == "agent_artifact_audit"
    finally:
        with engine.begin() as connection:
            if connection.scalar(text("SELECT to_regclass('agent_artifact_audit')")):
                _test_only_clear_artifact_audit(connection)
        alembic_command.upgrade(cfg, "head")


def test_nonempty_artifact_table_blocks_destructive_downgrade_and_preserves_forward_path(db):
    artifact = agent_files.save_upload(
        b"preserve", "preserve.txt", _owner(db, "migration-owner")
    )
    db.close()

    try:
        # This test isolates Artifact row preservation.  c2 separately proves that
        # operators must explicitly evacuate audit history before downgrading.
        with engine.begin() as connection:
            _test_only_clear_artifact_audit(connection)
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


def test_c2_repairs_already_applied_structural_identity_classifier(db):
    """An old b1 database must not retain non-canonical identity-only claims."""
    cfg = _cfg()
    db.close()
    alembic_command.downgrade(cfg, _PROVENANCE)
    canonical_sha = agent_artifact_provenance.canonical_identity_template_sha256(
        "pn-replenishment-request", 1
    )
    unsafe_sha = "b" * 64
    canonical_id = str(uuid.uuid4())
    unsafe_id = str(uuid.uuid4())

    def legacy_scope(sha256: str) -> dict:
        return {
            "schema_version": "artifact-access/v2",
            "policy": "owner_only",
            "classification": "identity_only",
            "proof_version": "identity-template-classifier/v1",
            "containment_status": "classified",
            "required_permissions": [],
            "contained_resources": [],
            "contained_fields": [],
            "sensitivity": "low",
            "row_subject": None,
            "predicate_version": "identity-top/v1",
            "condition": {"op": "top"},
            "source_access_snapshots": [],
            "template_proof": {
                "classifier_version": "identity-template-classifier/v1",
                "profile_id": "pn-replenishment-request/v1",
                "template_sha256": sha256,
                "sheet_headers": [
                    {"sheet": "申请", "headers": ["PN", "数量", "备注"]}
                ],
                "safe_style_profile": "default-style-only/v1",
                "pre_model": True,
            },
        }

    try:
        with engine.begin() as connection:
            for artifact_id, sha256 in (
                (canonical_id, canonical_sha),
                (unsafe_id, unsafe_sha),
            ):
                connection.execute(text(
                    """
                    INSERT INTO agent_artifact
                      (id, owner_sub, filename, media_type, size_bytes, sha256,
                       status, storage_key, kind, sensitivity, source_ids,
                       access_scope, extra_meta, created_at, expires_at)
                    VALUES
                      (CAST(:id AS uuid), 'legacy-identity', 'template.xlsx',
                       'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                       1, :sha, 'ready', :storage_key, 'upload', 'low', '[]'::jsonb,
                       CAST(:scope AS jsonb), '{}'::jsonb,
                       now(), now() + interval '1 day')
                    """
                ), {
                    "id": artifact_id,
                    "sha": sha256,
                    "storage_key": f"objects/{artifact_id}.xlsx",
                    "scope": json.dumps(legacy_scope(sha256), ensure_ascii=False),
                })

        alembic_command.upgrade(cfg, _HEAD)
        with engine.connect() as connection:
            canonical = connection.execute(text(
                "SELECT sensitivity, access_scope, extra_meta FROM agent_artifact "
                "WHERE id=CAST(:id AS uuid)"
            ), {"id": canonical_id}).mappings().one()
            unsafe = connection.execute(text(
                "SELECT sensitivity, access_scope, extra_meta FROM agent_artifact "
                "WHERE id=CAST(:id AS uuid)"
            ), {"id": unsafe_id}).mappings().one()

        assert canonical["sensitivity"] == "low"
        assert canonical["access_scope"]["proof_version"] == (
            "identity-template-classifier/v2"
        )
        assert canonical["access_scope"]["template_proof"]["template_sha256"] == (
            canonical_sha
        )
        assert canonical["extra_meta"]["legacy_identity_scope_before_c2"][
            "proof_version"
        ] == "identity-template-classifier/v1"

        assert unsafe["sensitivity"] == "critical"
        assert unsafe["access_scope"]["classification"] == "business_content"
        assert unsafe["access_scope"]["containment_status"] == "unclassified"
        assert unsafe["access_scope"]["template_proof"] is None
        assert unsafe["extra_meta"]["legacy_identity_scope_before_c2"][
            "proof_version"
        ] == "identity-template-classifier/v1"

        alembic_command.downgrade(cfg, _PROVENANCE)
        with engine.connect() as connection:
            downgraded = connection.execute(text(
                "SELECT access_scope FROM agent_artifact "
                "WHERE id=CAST(:id AS uuid)"
            ), {"id": canonical_id}).mappings().one()
        assert downgraded["access_scope"]["proof_version"] == (
            "identity-template-classifier/v1"
        )
        alembic_command.upgrade(cfg, _HEAD)
        with engine.connect() as connection:
            reupgraded = connection.execute(text(
                "SELECT access_scope FROM agent_artifact "
                "WHERE id=CAST(:id AS uuid)"
            ), {"id": canonical_id}).mappings().one()
        assert reupgraded["access_scope"]["proof_version"] == (
            "identity-template-classifier/v2"
        )
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
    identity = agent_files.save_upload(
        agent_artifact_provenance.canonical_identity_template_bytes(
            "pn-replenishment-request", 1
        ),
        "补库申请模板.xlsx",
        owner,
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
        required_positive_keys={"page_purchases", "data_purchase_cost"},
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


def test_postgres_rejects_non_object_artifact_and_audit_json_columns(db):
    created = agent_files.save_upload(
        b"shape", "shape.txt", _owner(db, "json-shape-owner")
    )
    with pytest.raises(DBAPIError):
        with engine.begin() as connection:
            connection.execute(text(
                "UPDATE agent_artifact SET extra_meta='[]'::jsonb "
                "WHERE id=CAST(:id AS uuid)"
            ), {"id": created["file_id"]})
    with pytest.raises(DBAPIError):
        with engine.begin() as connection:
            connection.execute(text(
                "UPDATE agent_artifact SET source_ids='[{}]'::jsonb "
                "WHERE id=CAST(:id AS uuid)"
            ), {"id": created["file_id"]})
    with pytest.raises(DBAPIError):
        with engine.begin() as connection:
            connection.execute(text(
                "UPDATE agent_artifact SET access_scope=jsonb_set("
                "access_scope, '{required_permissions}', '[{}]'::jsonb) "
                "WHERE id=CAST(:id AS uuid)"
            ), {"id": created["file_id"]})
    with pytest.raises(DBAPIError):
        with engine.begin() as connection:
            connection.execute(text(
                """
                INSERT INTO agent_artifact_audit
                  (artifact_id, action, outcome, actor, detail)
                VALUES
                  (CAST(:id AS uuid), 'shape_canary', 'denied', 'test', '[]'::jsonb)
                """
            ), {"id": created["file_id"]})


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE agent_artifact_audit SET outcome='mutated' WHERE id=(SELECT min(id) FROM agent_artifact_audit)",
        "DELETE FROM agent_artifact_audit WHERE id=(SELECT min(id) FROM agent_artifact_audit)",
        "TRUNCATE agent_artifact_audit",
    ],
    ids=["update", "delete", "truncate"],
)
def test_artifact_audit_is_append_only_at_database_layer(db, statement):
    agent_files.save_upload(
        b"append-only",
        "append-only.txt",
        _owner(db, f"append-only-{statement.split()[0].lower()}"),
    )

    with pytest.raises(DBAPIError, match="append-only"):
        with engine.begin() as connection:
            connection.execute(text(statement))


def test_postgres_rejects_noncanonical_delete_decision_key(db):
    with pytest.raises(DBAPIError):
        with engine.begin() as connection:
            connection.execute(text(
                """
                INSERT INTO agent_artifact_audit
                  (artifact_id, decision_key, action, outcome, actor, detail)
                VALUES
                  (NULL, :decision_key, 'delete_orphan_object', 'disabled',
                   'test', '{}'::jsonb)
                """
            ), {"decision_key": "g" * 64})


def test_postgres_status_guard_rejects_illegal_edge_and_missing_resign(db):
    created = agent_files.save_upload(
        b"status graph",
        "status-graph.txt",
        _owner(db, "status-graph-owner"),
    )
    with pytest.raises(DBAPIError, match="illegal agent_artifact status transition"):
        with engine.begin() as connection:
            connection.execute(text(
                "UPDATE agent_artifact SET status='prepared' "
                "WHERE id=CAST(:id AS uuid)"
            ), {"id": created["file_id"]})
    with pytest.raises(DBAPIError, match="requires a new aggregate binding"):
        with engine.begin() as connection:
            connection.execute(text(
                "UPDATE agent_artifact SET status='expired' "
                "WHERE id=CAST(:id AS uuid)"
            ), {"id": created["file_id"]})

    row = db.get(AgentArtifact, created["file_id"])
    assert row is not None
    with pytest.raises(agent_files.FileError, match="迁移边无效"):
        agent_files._transition_locked_bound_status(
            db,
            row,
            expected="ready",
            target="validating",
            actor="test",
            reason="illegal_test_edge",
        )
    db.rollback()


def test_json_shape_migration_blocks_malformed_rows_without_coercion(db):
    created = agent_files.save_upload(
        b"preserve malformed", "preserve-malformed.txt",
        _owner(db, "malformed-migration-owner"),
    )
    db.close()
    cfg = _cfg()
    # This test needs a b1 fixture; its explicit audit removal is test-only and the
    # dedicated downgrade test covers the production fail-closed contract.
    with engine.begin() as connection:
        _test_only_clear_artifact_audit(connection)
    alembic_command.downgrade(cfg, _PROVENANCE)
    try:
        with engine.begin() as connection:
            connection.execute(text(
                "UPDATE agent_artifact SET extra_meta='[]'::jsonb "
                "WHERE id=CAST(:id AS uuid)"
            ), {"id": created["file_id"]})
        with pytest.raises(DBAPIError, match="contains non-object JSON"):
            alembic_command.upgrade(cfg, _HEAD)
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == _PROVENANCE
            assert connection.scalar(text(
                "SELECT jsonb_typeof(extra_meta) FROM agent_artifact "
                "WHERE id=CAST(:id AS uuid)"
            ), {"id": created["file_id"]}) == "array"
        with engine.begin() as connection:
            connection.execute(text(
                "UPDATE agent_artifact SET extra_meta='{}'::jsonb "
                "WHERE id=CAST(:id AS uuid)"
            ), {"id": created["file_id"]})
        alembic_command.upgrade(cfg, _HEAD)
    finally:
        with engine.begin() as connection:
            if connection.scalar(text("SELECT to_regclass('agent_artifact')")):
                connection.execute(text(
                    "UPDATE agent_artifact SET extra_meta='{}'::jsonb "
                    "WHERE jsonb_typeof(extra_meta) IS DISTINCT FROM 'object'"
                ))
        alembic_command.upgrade(cfg, "head")

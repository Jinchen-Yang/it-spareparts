"""Migration contracts for project-bound replenishment submissions (#260)."""

import json
import os

import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.auth import hash_password
from app.db import engine
from app.models.dimensions import DimPart
from app.models.maintenance_project import MaintenanceProject
from app.models.system import SysUser


_PREVIOUS = "f3b5d7c9e2a4"
_HEAD = "a4c6e8f1b2d3"


def _valid_screening_payload() -> dict:
    return {
        "schema_version": 1,
        "as_of": "2026-08-17",
        "lookback_days": 182,
        "checks": [
            {
                "key": "pool_membership",
                "passed": False,
                "detail": {
                    "in_pool": None,
                    "pool_name": None,
                    "pool_status": None,
                },
            },
            {
                "key": "recent_activity",
                "passed": False,
                "detail": {
                    "window": {"from": "2026-02-17", "to": "2026-08-17"},
                    "purchase_samples": 0,
                    "sales_samples": 0,
                },
            },
            {
                "key": "niche_pn",
                "passed": False,
                "detail": {
                    "is_niche": True,
                    "purchase_samples": 0,
                    "sales_samples": 0,
                    "rule": "零样本边界",
                },
            },
        ],
        "anomaly_count": 3,
        "latest_sales": {},
        "pool_floor_ex_tax": None,
    }


def _cfg() -> AlembicConfig:
    cfg = AlembicConfig(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option(
        "script_location",
        os.path.join(os.path.dirname(__file__), "..", "alembic"),
    )
    return cfg


def _insert_atomic_draft_line(db, *, token: str, quantity: str) -> tuple[str, str]:
    owner = f"replenishment_{token}"
    project = MaintenanceProject(
        project_id=f"{token}-project",
        project_code=f"REPL-{token.upper()}",
        display_name=f"补库 {token} 项目",
        lifecycle_status="ongoing",
        is_active=True,
    )
    part = DimPart(pn_std=f"REPL-{token.upper()}-PN", status="active")
    db.add_all(
        [
            project,
            part,
            SysUser(
                username=owner,
                password_hash=hash_password("synthetic-password-123"),
                role="sales",
                display_name=f"补库 {token} 用户",
                is_active=True,
            ),
        ]
    )
    db.commit()
    application_id = f"{token}-app"
    version_id = f"{token}-version"
    digest = "9" * 64
    screening = json.dumps(_valid_screening_payload())
    db.execute(
        text(
            "INSERT INTO replenishment_application "
            "(application_id, application_no, owner_username, project_id, "
            "project_code_snapshot, project_name_snapshot, client_request_id, "
            "request_digest, is_legacy_project_unbound, status) VALUES "
            "(:application_id, :application_no, :owner, :project_id, :project_code, "
            ":project_name, :request_key, :digest, false, 'draft')"
        ),
        {
            "application_id": application_id,
            "application_no": f"BLK-{token.upper()}",
            "owner": owner,
            "project_id": project.project_id,
            "project_code": project.project_code,
            "project_name": project.display_name,
            "request_key": f"{token}-request-key",
            "digest": digest,
        },
    )
    db.execute(
        text(
            "INSERT INTO replenishment_application_version "
            "(version_id, application_id, version_no, status, created_by) VALUES "
            "(:version_id, :application_id, 1, 'draft', :owner)"
        ),
        {
            "version_id": version_id,
            "application_id": application_id,
            "owner": owner,
        },
    )
    db.execute(
        text(
            "INSERT INTO replenishment_application_line "
            "(line_id, request_line_id, version_id, line_no, part_id, pn_std, quantity, "
            "price_window_from, price_window_to, price_as_of, purchase_stats_json, "
            "sales_stats_json, evidence_digest, screening_json) VALUES "
            "(:line_id, :request_line_id, :version_id, 1, :part_id, :pn, :quantity, "
            "'2026-08-17', '2026-08-17', '2026-08-17', '{}'::jsonb, '{}'::jsonb, "
            ":digest, CAST(:screening AS jsonb))"
        ),
        {
            "line_id": f"{token}-line",
            "request_line_id": f"{token}-request-line",
            "version_id": version_id,
            "part_id": part.id,
            "pn": part.pn_std,
            "quantity": quantity,
            "digest": digest,
            "screening": screening,
        },
    )
    return owner, version_id


def test_upgrade_preserves_submitted_history_as_explicit_unbound_legacy(db):
    """Existing production history is labelled, never guessed or rewritten."""
    owner = "replenishment_migration_owner"
    digest = "a" * 64
    db.add(
        SysUser(
            username=owner,
            password_hash=hash_password("synthetic-password-123"),
            role="sales",
            display_name="补库迁移历史用户",
            is_active=True,
        )
    )
    db.commit()
    db.close()

    cfg = _cfg()
    alembic_command.downgrade(cfg, _PREVIOUS)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO replenishment_application "
                    "(application_id, application_no, owner_username, status) "
                    "VALUES ('legacy-submitted-app', 'BLK-LEGACY-001', :owner, "
                    "'submitted')"
                ),
                {"owner": owner},
            )
            connection.execute(
                text(
                    "INSERT INTO replenishment_application_version "
                    "(version_id, application_id, version_no, status, warehouse, "
                    "content_digest, created_by, submitted_by, submitted_at) "
                    "VALUES ('legacy-submitted-version', 'legacy-submitted-app', 1, "
                    "'submitted', '历史仓', :digest, :owner, :owner, now())"
                ),
                {"digest": digest, "owner": owner},
            )

        alembic_command.upgrade(cfg, _HEAD)

        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT a.project_id, a.client_request_id, "
                    "a.is_legacy_project_unbound, a.status, a.latest_version_no, "
                    "a.version, v.status AS version_status, v.version_no, "
                    "v.content_digest "
                    "FROM replenishment_application a "
                    "JOIN replenishment_application_version v USING (application_id) "
                    "WHERE a.application_id = 'legacy-submitted-app'"
                )
            ).mappings().one()

        assert dict(row) == {
            "project_id": None,
            "client_request_id": None,
            "is_legacy_project_unbound": True,
            "status": "submitted",
            "latest_version_no": 1,
            "version": 1,
            "version_status": "submitted",
            "version_no": 1,
            "content_digest": digest,
        }
    finally:
        alembic_command.upgrade(cfg, "head")


def test_database_rejects_new_application_forged_as_legacy(db):
    owner = "replenishment_forged_legacy"
    db.add(
        SysUser(
            username=owner,
            password_hash=hash_password("synthetic-password-123"),
            role="sales",
            display_name="补库伪造历史用户",
            is_active=True,
        )
    )
    db.commit()

    with pytest.raises(DBAPIError, match="new replenishment application"):
        db.execute(
            text(
                "INSERT INTO replenishment_application "
                "(application_id, application_no, owner_username, client_request_id, "
                "is_legacy_project_unbound, status) "
                "VALUES ('forged-legacy-app', 'BLK-FORGED-LEGACY', :owner, "
                "'forged-request-key', true, 'draft')"
            ),
            {"owner": owner},
        )
        db.flush()


def test_database_requires_new_atomic_application_to_start_draft(db):
    owner = "replenishment_insert_state"
    project = MaintenanceProject(
        project_id="replenishment-insert-state-project",
        project_code="REPL-INSERT-STATE",
        display_name="补库新单初始状态项目",
        lifecycle_status="ongoing",
        is_active=True,
    )
    db.add_all(
        [
            project,
            SysUser(
                username=owner,
                password_hash=hash_password("synthetic-password-123"),
                role="sales",
                display_name="补库新单初始状态用户",
                is_active=True,
            ),
        ]
    )
    db.commit()

    with pytest.raises(DBAPIError, match="new replenishment application must start draft"):
        db.execute(
            text(
                "INSERT INTO replenishment_application "
                "(application_id, application_no, owner_username, project_id, "
                "project_code_snapshot, project_name_snapshot, client_request_id, "
                "request_digest, is_legacy_project_unbound, status) VALUES "
                "('invalid-insert-state-app', 'BLK-INVALID-STATE', :owner, :project_id, "
                "'REPL-INSERT-STATE', '补库新单初始状态项目', 'insert-state-key', "
                ":digest, false, 'submitted')"
            ),
            {
                "owner": owner,
                "project_id": project.project_id,
                "digest": "d" * 64,
            },
        )
        db.flush()


def test_atomic_application_allows_only_draft_to_submitted(db):
    owner, version_id = _insert_atomic_draft_line(
        db, token="atomicstate", quantity="1"
    )
    db.execute(
        text(
            "UPDATE replenishment_application_version "
            "SET status = 'submitted', content_digest = :digest, "
            "submitted_by = :owner, submitted_at = now() "
            "WHERE version_id = :version_id"
        ),
        {
            "owner": owner,
            "digest": "e" * 64,
            "version_id": version_id,
        },
    )
    db.execute(
        text(
            "UPDATE replenishment_application "
            "SET status = 'submitted', version = version + 1 "
            "WHERE application_id = 'atomicstate-app'"
        )
    )
    db.commit()

    with pytest.raises(DBAPIError, match="submitted replenishment status is immutable"):
        db.execute(
            text(
                "UPDATE replenishment_application "
                "SET status = 'approved', version = version + 1 "
                "WHERE application_id = 'atomicstate-app'"
            )
        )
        db.flush()


def test_atomic_application_cannot_submit_before_its_version(db):
    _insert_atomic_draft_line(db, token="appversion", quantity="1")

    db.execute(
        text(
            "UPDATE replenishment_application "
            "SET status = 'submitted', version = version + 1 "
            "WHERE application_id = 'appversion-app'"
        )
    )
    with pytest.raises(DBAPIError, match="requires submitted latest version"):
        db.commit()


def test_atomic_version_rejects_submission_without_frozen_screening(db):
    owner = "replenishment_missing_screening"
    project = MaintenanceProject(
        project_id="missing-screening-project",
        project_code="REPL-MISSING-SCREENING",
        display_name="补库缺失冻结证据项目",
        lifecycle_status="ongoing",
        is_active=True,
    )
    part = DimPart(pn_std="REPL-MISSING-SCREENING-PN", status="active")
    db.add_all(
        [
            project,
            part,
            SysUser(
                username=owner,
                password_hash=hash_password("synthetic-password-123"),
                role="sales",
                display_name="补库缺失冻结证据用户",
                is_active=True,
            ),
        ]
    )
    db.commit()
    db.execute(
        text(
            "INSERT INTO replenishment_application "
            "(application_id, application_no, owner_username, project_id, "
            "project_code_snapshot, project_name_snapshot, client_request_id, "
            "request_digest, is_legacy_project_unbound, status) VALUES "
            "('missing-screening-app', 'BLK-MISSING-SCREENING', :owner, :project_id, "
            "'REPL-MISSING-SCREENING', '补库缺失冻结证据项目', "
            "'missing-screening-key', :digest, false, 'draft')"
        ),
        {
            "owner": owner,
            "project_id": project.project_id,
            "digest": "f" * 64,
        },
    )
    db.execute(
        text(
            "INSERT INTO replenishment_application_version "
            "(version_id, application_id, version_no, status, created_by) VALUES "
            "('missing-screening-version', 'missing-screening-app', 1, 'draft', :owner)"
        ),
        {"owner": owner},
    )
    db.execute(
        text(
            "INSERT INTO replenishment_application_line "
            "(line_id, request_line_id, version_id, line_no, part_id, pn_std, quantity, "
            "price_window_from, price_window_to, price_as_of, purchase_stats_json, "
            "sales_stats_json, evidence_digest, screening_json) VALUES "
            "('missing-screening-line', 'missing-screening-request', "
            "'missing-screening-version', 1, :part_id, :pn, 1, '2026-08-17', "
            "'2026-08-17', '2026-08-17', '{}'::jsonb, '{}'::jsonb, :digest, NULL)"
        ),
        {"part_id": part.id, "pn": part.pn_std, "digest": "f" * 64},
    )

    with pytest.raises(DBAPIError, match="complete frozen screening_json"):
        db.execute(
            text(
                "UPDATE replenishment_application_version SET status = 'submitted', "
                "content_digest = :digest, submitted_by = :owner, submitted_at = now() "
                "WHERE version_id = 'missing-screening-version'"
            ),
            {"digest": "f" * 64, "owner": owner},
        )
        db.flush()


def test_atomic_version_rejects_fractional_line_quantity(db):
    owner, version_id = _insert_atomic_draft_line(
        db, token="fractional", quantity="1.500"
    )

    with pytest.raises(DBAPIError, match="positive integer quantity"):
        db.execute(
            text(
                "UPDATE replenishment_application_version SET status = 'submitted', "
                "content_digest = :digest, submitted_by = :owner, submitted_at = now() "
                "WHERE version_id = :version_id"
            ),
            {"digest": "9" * 64, "owner": owner, "version_id": version_id},
        )
        db.flush()


@pytest.mark.parametrize(
    "invalid_kind",
    [
        "duplicate_key",
        "passed_not_boolean",
        "detail_not_object",
        "pool_detail_missing",
        "activity_sample_not_integer",
        "niche_flag_not_boolean",
    ],
)
def test_atomic_version_rejects_malformed_screening_shape(db, invalid_kind):
    owner, version_id = _insert_atomic_draft_line(
        db, token=f"badshape{invalid_kind[:5]}", quantity="1"
    )
    payload = _valid_screening_payload()
    if invalid_kind == "duplicate_key":
        payload["checks"][1]["key"] = "pool_membership"
    elif invalid_kind == "passed_not_boolean":
        payload["checks"][0]["passed"] = "false"
    elif invalid_kind == "detail_not_object":
        payload["checks"][0]["detail"] = "not-an-object"
    elif invalid_kind == "pool_detail_missing":
        del payload["checks"][0]["detail"]["pool_name"]
    elif invalid_kind == "activity_sample_not_integer":
        payload["checks"][1]["detail"]["purchase_samples"] = "zero"
    else:
        payload["checks"][2]["detail"]["is_niche"] = "true"
    db.execute(
        text(
            "UPDATE replenishment_application_line SET screening_json = "
            "CAST(:screening AS jsonb) WHERE version_id = :version_id"
        ),
        {"screening": json.dumps(payload), "version_id": version_id},
    )

    with pytest.raises(DBAPIError, match="complete frozen screening_json"):
        db.execute(
            text(
                "UPDATE replenishment_application_version SET status = 'submitted', "
                "content_digest = :digest, submitted_by = :owner, submitted_at = now() "
                "WHERE version_id = :version_id"
            ),
            {"digest": "7" * 64, "owner": owner, "version_id": version_id},
        )
        db.commit()


def test_legacy_history_rejects_project_binding(db):
    owner = "replenishment_legacy_binding"
    project = MaintenanceProject(
        project_id="legacy-binding-project",
        project_code="REPL-LEGACY",
        display_name="补库历史绑定项目",
        lifecycle_status="ongoing",
        is_active=True,
    )
    db.add_all(
        [
            project,
            SysUser(
                username=owner,
                password_hash=hash_password("synthetic-password-123"),
                role="sales",
                display_name="补库历史绑定用户",
                is_active=True,
            ),
        ]
    )
    db.commit()
    project_snapshot = {
        "project_id": project.project_id,
        "project_code": project.project_code,
        "project_name": project.display_name,
    }
    db.close()

    cfg = _cfg()
    alembic_command.downgrade(cfg, _PREVIOUS)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO replenishment_application "
                    "(application_id, application_no, owner_username, status) "
                    "VALUES ('legacy-binding-app', 'BLK-LEGACY-BINDING', :owner, "
                    "'submitted')"
                ),
                {"owner": owner},
            )
        alembic_command.upgrade(cfg, _HEAD)

        with pytest.raises(DBAPIError, match="legacy replenishment history is read-only"):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE replenishment_application "
                        "SET project_id = :project_id, "
                        "project_code_snapshot = :project_code, "
                        "project_name_snapshot = :project_name, "
                        "is_legacy_project_unbound = false, version = version + 1 "
                        "WHERE application_id = 'legacy-binding-app'"
                    ),
                    project_snapshot,
                )
    finally:
        alembic_command.upgrade(cfg, "head")


def test_legacy_history_rejects_status_changes(db):
    owner = "replenishment_legacy_status"
    db.add(
        SysUser(
            username=owner,
            password_hash=hash_password("synthetic-password-123"),
            role="sales",
            display_name="补库历史状态用户",
            is_active=True,
        )
    )
    db.commit()
    db.close()

    cfg = _cfg()
    alembic_command.downgrade(cfg, _PREVIOUS)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO replenishment_application "
                    "(application_id, application_no, owner_username, status) "
                    "VALUES ('legacy-status-app', 'BLK-LEGACY-STATUS', :owner, "
                    "'submitted')"
                ),
                {"owner": owner},
            )
        alembic_command.upgrade(cfg, _HEAD)

        with pytest.raises(DBAPIError, match="legacy replenishment history is read-only"):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE replenishment_application "
                        "SET status = 'approved', version = version + 1 "
                        "WHERE application_id = 'legacy-status-app'"
                    )
                )
    finally:
        alembic_command.upgrade(cfg, "head")


@pytest.mark.parametrize("mutation", ["version", "latest_version_no", "updated_at"])
def test_legacy_history_rejects_nonidentity_updates(db, mutation):
    owner = "replenishment_legacy_pointer"
    db.add(
        SysUser(
            username=owner,
            password_hash=hash_password("synthetic-password-123"),
            role="sales",
            display_name="补库历史只读用户",
            is_active=True,
        )
    )
    db.commit()
    db.close()

    cfg = _cfg()
    alembic_command.downgrade(cfg, _PREVIOUS)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO replenishment_application "
                    "(application_id, application_no, owner_username, status) "
                    "VALUES ('legacy-pointer-app', 'BLK-LEGACY-POINTER', :owner, "
                    "'submitted')"
                ),
                {"owner": owner},
            )
        alembic_command.upgrade(cfg, _HEAD)

        statements = {
            "version": text(
                "UPDATE replenishment_application SET version = version + 1 "
                "WHERE application_id = 'legacy-pointer-app'"
            ),
            "latest_version_no": text(
                "UPDATE replenishment_application SET latest_version_no = 999 "
                "WHERE application_id = 'legacy-pointer-app'"
            ),
            "updated_at": text(
                "UPDATE replenishment_application "
                "SET updated_at = updated_at + interval '1 second' "
                "WHERE application_id = 'legacy-pointer-app'"
            ),
        }
        with pytest.raises(DBAPIError):
            with engine.begin() as connection:
                connection.execute(statements[mutation])
    finally:
        alembic_command.upgrade(cfg, "head")


def test_client_request_and_digest_must_be_present_as_a_pair(db):
    owner = "replenishment_client_pair"
    project = MaintenanceProject(
        project_id="replenishment-client-pair-project",
        project_code="REPL-CLIENT-PAIR",
        display_name="补库幂等键约束项目",
        lifecycle_status="ongoing",
        is_active=True,
    )
    db.add_all(
        [
            project,
            SysUser(
                username=owner,
                password_hash=hash_password("synthetic-password-123"),
                role="sales",
                display_name="补库幂等键约束用户",
                is_active=True,
            ),
        ]
    )
    db.commit()

    connection = engine.connect()
    transaction = connection.begin()
    try:
        connection.execute(
            text(
                "ALTER TABLE replenishment_application "
                "DISABLE TRIGGER trg_replenishment_project_binding"
            )
        )
        with pytest.raises(DBAPIError):
            connection.execute(
                text(
                    "INSERT INTO replenishment_application "
                    "(application_id, application_no, owner_username, project_id, "
                    "project_code_snapshot, project_name_snapshot, client_request_id, "
                    "request_digest, is_legacy_project_unbound, status) VALUES "
                    "('client-pair-app', 'BLK-CLIENT-PAIR', :owner, :project_id, "
                    "'REPL-CLIENT-PAIR', '补库幂等键约束项目', NULL, :digest, false, "
                    "'draft')"
                ),
                {
                    "owner": owner,
                    "project_id": project.project_id,
                    "digest": "c" * 64,
                },
            )
    finally:
        transaction.rollback()
        connection.close()


@pytest.mark.parametrize(
    ("project_code_snapshot", "project_name_snapshot"),
    [(None, "补库快照约束项目"), ("REPL-SNAPSHOT-CHECK", None)],
)
def test_bound_project_snapshots_cannot_be_null_when_trigger_is_disabled(
    db, project_code_snapshot, project_name_snapshot
):
    owner, _version_id = _insert_atomic_draft_line(
        db, token="snapshotcheck", quantity="1"
    )
    db.commit()
    db.execute(
        text(
            "ALTER TABLE replenishment_application "
            "DISABLE TRIGGER trg_replenishment_project_binding"
        )
    )

    with pytest.raises(DBAPIError):
        db.execute(
            text(
                "INSERT INTO replenishment_application "
                "(application_id, application_no, owner_username, project_id, "
                "project_code_snapshot, project_name_snapshot, client_request_id, "
                "request_digest, is_legacy_project_unbound, status) VALUES "
                "('snapshot-null-app', 'BLK-SNAPSHOT-NULL', :owner, "
                "'snapshotcheck-project', :project_code, :project_name, "
                "'snapshot-null-key', :digest, false, 'draft')"
            ),
            {
                "owner": owner,
                "project_code": project_code_snapshot,
                "project_name": project_name_snapshot,
                "digest": "8" * 64,
            },
        )
        db.flush()


def test_downgrade_fails_closed_when_project_bound_atomic_rows_exist(db):
    _insert_atomic_draft_line(db, token="downgrade", quantity="1")
    db.commit()
    db.close()
    cfg = _cfg()

    with pytest.raises(DBAPIError, match="downgrade blocked"):
        alembic_command.downgrade(cfg, _PREVIOUS)

    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == _HEAD

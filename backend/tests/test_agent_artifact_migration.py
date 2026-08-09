"""Artifact v2 migration rollback is empty-only and otherwise forward-fix."""

from __future__ import annotations

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
from app.models.system import SysUser
from app.services import agent_files

_HEAD = "ad8f6c2e1b47"
_PREV = "c4e8a1d7f2b6"


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
        with pytest.raises(DBAPIError, match="downgrade blocked"):
            alembic_command.downgrade(_cfg(), _PREV)
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == _HEAD
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
    alembic_command.downgrade(cfg, _PREV)
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT to_regclass('agent_artifact')")) is None
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == _PREV
        alembic_command.upgrade(cfg, "head")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == _HEAD
            assert connection.scalar(text("SELECT to_regclass('agent_artifact')")) == "agent_artifact"
    finally:
        alembic_command.upgrade(cfg, "head")


def test_concurrent_insert_cannot_commit_between_empty_check_and_drop(db):
    """An in-flight insert must become visible before downgrade decides emptiness."""
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
            alembic_command.downgrade(_cfg(), _PREV)
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
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == _HEAD
    finally:
        if transaction.is_active:
            transaction.rollback()
        inserter.close()
        worker.join(timeout=10)
        alembic_command.upgrade(_cfg(), "head")

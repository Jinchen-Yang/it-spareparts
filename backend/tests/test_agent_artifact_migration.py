"""Artifact v2 migration rollback is empty-only and otherwise forward-fix."""

from __future__ import annotations

import os

import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app import permissions, security
from app.db import engine
from app.services import agent_files

_HEAD = "ad8f6c2e1b47"
_PREV = "c4e8a1d7f2b6"


def _owner(username: str):
    return agent_files.verified_artifact_owner(security.UserContext(
        user_id=username,
        role="admin",
        permissions=permissions.effective("admin", None),
        is_authenticated=True,
        authn="sys_user",
        has_stable_subject=True,
    ))


def _cfg() -> AlembicConfig:
    cfg = AlembicConfig(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(os.path.dirname(__file__), "..", "alembic"))
    return cfg


def test_nonempty_artifact_table_blocks_destructive_downgrade_and_preserves_forward_path(db):
    artifact = agent_files.save_upload(
        b"preserve", "preserve.txt", _owner("migration-owner")
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

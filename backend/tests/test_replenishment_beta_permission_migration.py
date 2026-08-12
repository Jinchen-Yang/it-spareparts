"""Replenishment Beta migration keeps the page closed for every existing account."""

import os

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import text

from app import permissions
from app.auth import hash_password
from app.db import engine
from app.models.system import SysUser


_PREV = "d3e5f7a9b1c2"
_HEAD = "c7e2a9f4b6d1"
_PAGE = "page_replenishment_beta"
_CREATE = "action_replenishment_create"
_REVIEW = "action_replenishment_review"


def _cfg() -> AlembicConfig:
    cfg = AlembicConfig(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option(
        "script_location",
        os.path.join(os.path.dirname(__file__), "..", "alembic"),
    )
    return cfg


def test_upgrade_closes_page_for_existing_admin_but_keeps_admin_actions(db):
    base = permissions.effective("admin", None)
    db.add(
        SysUser(
            username="replenishment-beta-migration-admin",
            role="admin",
            display_name="migration admin",
            password_hash=hash_password("synthetic-migration-password"),
            is_active=True,
            template_code="admin",
            template_version=1,
            template_perms=base,
            perm_overrides={_PAGE: True},
            permissions=base,
        )
    )
    db.commit()
    db.close()
    cfg = _cfg()
    try:
        alembic_command.downgrade(cfg, _PREV)
        alembic_command.upgrade(cfg, _HEAD)
        with engine.connect() as connection:
            role = connection.execute(
                text(
                    "SELECT permissions ->> :page AS page, "
                    "permissions ->> :create AS can_create, "
                    "permissions ->> :review AS can_review "
                    "FROM sys_role_template WHERE code = 'admin'"
                ),
                {"page": _PAGE, "create": _CREATE, "review": _REVIEW},
            ).mappings().one()
            user = connection.execute(
                text(
                    "SELECT template_perms ->> :page AS page, "
                    "template_perms ->> :create AS can_create, "
                    "template_perms ->> :review AS can_review, "
                    "permissions ->> :page AS legacy_page, "
                    "perm_overrides ? :page AS page_overridden "
                    "FROM sys_user WHERE username = 'replenishment-beta-migration-admin'"
                ),
                {"page": _PAGE, "create": _CREATE, "review": _REVIEW},
            ).mappings().one()

        assert role == {"page": "false", "can_create": "true", "can_review": "true"}
        assert user.page == "false"
        assert user.can_create == "true"
        assert user.can_review == "true"
        assert user.legacy_page == "false"
        assert user.page_overridden is False
    finally:
        alembic_command.upgrade(cfg, "head")

"""维保 Beta 白名单权限迁移：包括管理员在内的存量账号默认失败关闭。"""

import os

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import text

from app import permissions
from app.auth import hash_password
from app.db import engine
from app.models.system import SysUser


_PREV = "c7e2a9f4b6d1"
_HEAD = "d9f1a3c7e5b2"
_KEY = "page_maintenance_beta"


def _cfg() -> AlembicConfig:
    cfg = AlembicConfig(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option(
        "script_location",
        os.path.join(os.path.dirname(__file__), "..", "alembic"),
    )
    return cfg


def test_upgrade_backfills_every_account_closed_and_downgrade_removes_key(db):
    for role in ("admin", "readonly"):
        base = permissions.effective(role, None)
        db.add(
            SysUser(
                username=f"maintenance-beta-migration-{role}",
                role=role,
                display_name=role,
                password_hash=hash_password("synthetic-migration-password"),
                is_active=True,
                template_code=role,
                template_version=1,
                template_perms=base,
                perm_overrides={_KEY: True},
                permissions=base,
            )
        )
    db.commit()
    db.close()
    cfg = _cfg()
    try:
        alembic_command.downgrade(cfg, _PREV)
        with engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT count(*) FROM sys_role_template "
                    f"WHERE permissions ? '{_KEY}'"
                )
            ).scalar_one() == 0
            assert connection.execute(
                text(
                    "SELECT count(*) FROM sys_user "
                    f"WHERE template_perms ? '{_KEY}' "
                    f"OR perm_overrides ? '{_KEY}' OR permissions ? '{_KEY}'"
                )
            ).scalar_one() == 0

        alembic_command.upgrade(cfg, _HEAD)
        with engine.connect() as connection:
            role_values = dict(
                connection.execute(
                    text(
                        "SELECT code, permissions ->> :key AS enabled "
                        "FROM sys_role_template WHERE code IN ('admin', 'readonly')"
                    ),
                    {"key": _KEY},
                ).all()
            )
            assert role_values == {"admin": "false", "readonly": "false"}
            user_values = {
                row.username: row
                for row in connection.execute(
                    text(
                        "SELECT username, template_perms ->> :key AS template_enabled, "
                        "permissions ->> :key AS legacy_enabled, perm_overrides ? :key AS overridden "
                        "FROM sys_user WHERE username LIKE 'maintenance-beta-migration-%'"
                    ),
                    {"key": _KEY},
                ).mappings()
            }
            assert user_values["maintenance-beta-migration-admin"].template_enabled == "false"
            assert user_values["maintenance-beta-migration-admin"].legacy_enabled == "false"
            assert user_values["maintenance-beta-migration-readonly"].template_enabled == "false"
            assert user_values["maintenance-beta-migration-readonly"].legacy_enabled == "false"
            assert all(not row.overridden for row in user_values.values())
    finally:
        alembic_command.upgrade(cfg, "head")

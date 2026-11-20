"""仓库分析权限迁移保留存量账号显式值、旧快照和回退语义。"""
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app import permissions
from app.db import engine

PAGE = "page_inventory_analytics"


def test_warehouse_permission_defaults():
    assert PAGE in permissions.PAGE_KEYS and PAGE in permissions.PERMISSION_META
    for role in ("admin", "boss", "sales", "purchaser", "readonly", "guest"):
        assert permissions.effective(role, None)[PAGE] is (role in ("admin", "boss"))


def test_warehouse_permissions_roundtrip(db):
    root = Path(__file__).resolve().parents[1]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    db.close()
    try:
        command.downgrade(cfg, "b6285fa3c918-1")
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO sys_user (username, role, password_hash, template_perms, permissions, perm_overrides)
                VALUES ('wh-new', 'boss', 'unused', '{}', '{}', '{"page_inventory_analytics": false}'),
                       ('wh-explicit', 'boss', 'unused', '{"page_inventory_analytics": false}', null, null),
                       ('wh-old', 'readonly', 'unused', null, '{"page_inventory": true}', null)
            """))
        command.upgrade(cfg, "b6285fa3c918")
        with engine.connect() as conn:
            rows = {r.username: r for r in conn.execute(text("""
                SELECT username, template_perms, permissions, perm_overrides
                FROM sys_user WHERE username IN ('wh-new','wh-explicit','wh-old')
            """))}
            assert rows["wh-new"].template_perms[PAGE] is True
            assert rows["wh-new"].permissions[PAGE] is True
            assert rows["wh-new"].perm_overrides[PAGE] is False
            assert rows["wh-explicit"].template_perms[PAGE] is False
            assert rows["wh-explicit"].permissions is None
            assert rows["wh-old"].template_perms is None
            assert rows["wh-old"].permissions == {"page_inventory": True, PAGE: False}
            roles = dict(conn.execute(text("SELECT code, permissions ->> :key FROM sys_role_template"), {"key": PAGE}).all())
            assert roles["boss"] == roles["admin"] == "true"
            assert roles["readonly"] == roles["sales"] == "false"
    finally:
        command.upgrade(cfg, "head")

"""采购分析页面键迁移（D-39，a5e8c3f7b2d9）：page_purchase_analytics。

口径与 test_maintenance_boss_permissions_migration 同款：键登记完整；模板仅 boss
（及随 _full() 同步的 admin）为 true、其余角色 fail-closed；真实 downgrade→upgrade
往返——boss 存量账号补 true 并清覆盖，其余账号回填 false 不动覆盖层。
"""
from sqlalchemy import text

from app import permissions

_KEY = "page_purchase_analytics"
_PREV, _REV = "7f73424a7f65", "a5e8c3f7b2d9"


def test_key_registered_everywhere():
    assert _KEY in permissions.PAGE_KEYS
    assert _KEY in permissions.LABELS
    assert _KEY in permissions.PERMISSION_META
    # UI 矩阵可见（页面组）
    page_group = next(g for g in permissions.UI_GROUPS if g["key"] == "page")
    assert _KEY in page_group["keys"]
    # 非账号白名单键：admin 运行时恒放行（page_permission_allowed 短路）
    assert _KEY not in permissions.ACCOUNT_SCOPED_BETA_PAGE_KEYS


def test_role_template_defaults():
    """boss 开、admin 模板随 _full() 同步 true（运行时另有短路）、其余显式 False。"""
    assert permissions.effective("admin", None)[_KEY] is True
    assert permissions.effective("boss", None)[_KEY] is True
    for role in ("sales", "purchaser", "readonly", "guest"):
        eff = permissions.effective(role, None)
        assert eff[_KEY] is False, role


def test_page_permission_allowed_admin_short_circuit():
    assert permissions.page_permission_allowed(
        role="admin", permission_map=None, page_key=_KEY) is True


def test_upgrade_backfills_and_downgrade_removes_key(db):
    """真实 downgrade→（以存量形态播种）→upgrade：boss 覆盖层让位（False 覆盖被清、
    补 true）；非 boss 回填 false 且**不动覆盖层**（管理员先前的显式 True 授予保留，
    之后仍可在权限中心按账号收紧/放开）；legacy permissions 列双写。

    播种放在 downgrade 之后：覆盖层的去留才能归因于 upgrade 本身（先种后降会把
    覆盖层一起抹掉，断言就失真了）。
    """
    import os

    from alembic import command as alembic_command
    from alembic.config import Config as AlembicConfig

    from app.auth import hash_password
    from app.db import engine
    from app.models.system import SysUser

    cfg = AlembicConfig(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option(
        "script_location", os.path.join(os.path.dirname(__file__), "..", "alembic"))
    try:
        alembic_command.downgrade(cfg, _PREV)
        # 存量账号形态：键尚不存在（pre-migration 模板快照不含该键）
        specs = [("boss", False), ("boss", True), ("readonly", True)]
        for i, (role, override_val) in enumerate(specs):
            base = permissions.effective(role, None)
            base.pop(_KEY, None)                  # 模拟迁移前的快照
            db.add(SysUser(
                username=f"analytics-migration-{i}", role=role, display_name=role,
                password_hash=hash_password("synthetic-migration-password"),
                is_active=True, template_code=role, template_version=1,
                template_perms=base,
                perm_overrides={_KEY: override_val},
                permissions=base,
            ))
        db.commit()
        db.close()
        with engine.connect() as conn:
            # 降级后：模板表与账号快照（template_perms/permissions）均无该键；
            # 覆盖层是播种出来的"管理员先前的显式授予"，带着键进 upgrade。
            for column in ("template_perms", "permissions"):
                assert conn.execute(text(
                    f"SELECT count(*) FROM sys_user WHERE {column} ? :k "
                    "AND username LIKE 'analytics-migration-%'"),
                    {"k": _KEY}).scalar_one() == 0, column
            assert conn.execute(text(
                "SELECT count(*) FROM sys_user WHERE perm_overrides ? :k "
                "AND username LIKE 'analytics-migration-%'"),
                {"k": _KEY}).scalar_one() == 3
            assert conn.execute(text(
                "SELECT count(*) FROM sys_role_template WHERE permissions ? :k"),
                {"k": _KEY}).scalar_one() == 0
        alembic_command.upgrade(cfg, _REV)
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT username, role, template_perms ->> :k AS enabled, "
                "perm_overrides ->> :k AS override_val, permissions ->> :k AS legacy "
                "FROM sys_user WHERE username LIKE 'analytics-migration-%'"),
                {"k": _KEY}).mappings().all()
            assert rows
            for row in rows:
                if row.role == "boss":
                    # boss：补 true 且覆盖层让位（False/True 覆盖一律被清）
                    assert row.enabled == "true", row.username
                    assert row.override_val is None, row.username
                    assert row.legacy == "true", row.username
                else:
                    # 非 boss：模板回填 false，覆盖层原样保留（显式 True 不被吞）
                    assert row.enabled == "false", row.username
                    assert row.override_val == "true", row.username
                    assert row.legacy == "false", row.username
            tmpl = dict(conn.execute(text(
                "SELECT code, permissions ->> :k FROM sys_role_template"),
                {"k": _KEY}).all())
            assert tmpl["boss"] == "true" and tmpl["admin"] == "true"
            assert tmpl["sales"] == "false" and tmpl["readonly"] == "false"
            assert tmpl["purchaser"] == "false"
    finally:
        alembic_command.upgrade(cfg, "head")

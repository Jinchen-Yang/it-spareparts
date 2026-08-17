"""M1-6 权限键回填（plan v1.3）：page_maintenance_boss + action_maintenance_wbdd_import。

模仿 test_maintenance_beta_access_migration 的口径：存量模板与账号（含 admin 快照）
两键一律回填 false；Python 模板对非 admin 角色显式 False；依赖表登记完整。
"""
from sqlalchemy import text

from app import permissions

_KEYS = ("page_maintenance_boss", "action_maintenance_wbdd_import")


def test_keys_registered_everywhere():
    assert "page_maintenance_boss" in permissions.PAGE_KEYS
    assert "action_maintenance_wbdd_import" in permissions.ACTION_KEYS
    for key in _KEYS:
        assert key in permissions.LABELS
        assert key in permissions.PERMISSION_META
        assert key in permissions.HIGH_RISK_KEYS
    assert permissions.ACTION_PAGE_DEPENDENCIES[
        "action_maintenance_wbdd_import"] == "page_maintenance"
    # WBDD 导出无价格列：动作不挂数据组依赖（plan M1-6 与 doc_import 的差异点）
    assert "action_maintenance_wbdd_import" not in permissions.ACTION_DATA_DEPENDENCIES
    # 非账号白名单键：admin 常规 bypass 生效
    assert "action_maintenance_wbdd_import" not in permissions.ACCOUNT_SCOPED_ACTION_KEYS
    assert "page_maintenance_boss" not in permissions.ACCOUNT_SCOPED_BETA_PAGE_KEYS
    # UI 矩阵可见（高风险组）
    admin_group = next(g for g in permissions.UI_GROUPS if g["key"] == "admin")
    assert "action_maintenance_wbdd_import" in admin_group["keys"]
    page_group = next(g for g in permissions.UI_GROUPS if g["key"] == "page")
    assert "page_maintenance_boss" in page_group["keys"]


def test_role_template_defaults_fail_closed():
    assert permissions.effective("admin", None)["page_maintenance_boss"] is True
    assert permissions.effective("admin", None)["action_maintenance_wbdd_import"] is True
    for role in ("boss", "sales", "purchaser", "readonly", "guest"):
        eff = permissions.effective(role, None)
        for key in _KEYS:
            assert eff[key] is False, (role, key)


def test_combo_requires_page_for_action():
    invalid = permissions.effective("readonly", {
        "page_maintenance": False,
        "action_maintenance_wbdd_import": True,
    })
    errors = permissions.combo_errors(invalid)
    # combo_errors 输出业务中文文案（键的 LABELS），按标签断言
    assert any(permissions.LABELS["action_maintenance_wbdd_import"] in e
               for e in errors)


def test_upgrade_backfills_accounts_closed_and_downgrade_removes_keys(db):
    """真实 downgrade→upgrade 往返（模仿 test_maintenance_beta_access_migration）：
    存量账号即使 override 为 True，回填后也一律 false 且 override 被清除。"""
    import os

    from alembic import command as alembic_command
    from alembic.config import Config as AlembicConfig

    from app.auth import hash_password
    from app.db import engine
    from app.models.system import SysUser

    prev, head_rev = "b4c8d2e6f1a3", "c5d9e3f7a2b4"
    for role in ("admin", "readonly"):
        base = permissions.effective(role, None)
        db.add(SysUser(
            username=f"boss-perm-migration-{role}", role=role, display_name=role,
            password_hash=hash_password("synthetic-migration-password"),
            is_active=True, template_code=role, template_version=1,
            template_perms=base,
            perm_overrides={k: True for k in _KEYS},
            permissions=base,
        ))
    db.commit()
    db.close()
    cfg = AlembicConfig(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option(
        "script_location", os.path.join(os.path.dirname(__file__), "..", "alembic"))
    try:
        alembic_command.downgrade(cfg, prev)
        with engine.connect() as conn:
            for key in _KEYS:
                assert conn.execute(text(
                    "SELECT count(*) FROM sys_user WHERE template_perms ? :k "
                    "OR perm_overrides ? :k"), {"k": key}).scalar_one() == 0
        alembic_command.upgrade(cfg, head_rev)
        with engine.connect() as conn:
            for key in _KEYS:
                rows = conn.execute(text(
                    "SELECT username, template_perms ->> :k AS enabled, "
                    "perm_overrides ? :k AS overridden FROM sys_user "
                    "WHERE username LIKE 'boss-perm-migration-%'"),
                    {"k": key}).mappings().all()
                assert rows
                for row in rows:
                    assert row.enabled == "false", (row.username, key)
                    assert not row.overridden, (row.username, key)
    finally:
        alembic_command.upgrade(cfg, "head")

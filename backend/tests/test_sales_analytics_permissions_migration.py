"""销售分析页权限键迁移（e3a5c7d9b1f4）：模板/账号回填与 downgrade→upgrade 往返。

模仿 test_maintenance_boss_permissions_migration 的口径：全部模板与存量账号
fail-closed 后仅 boss（及 admin 模板行）开启；Python 模板对 readonly 显式 False；
覆盖层让位，升级后管理员仍可逐账号重新授予。
"""
from sqlalchemy import text

from app import permissions

_KEYS = ("page_sales_analysis", "data_reconciliation")
_PREV, _REV = "b8f2d6c4a1e9", "e3a5c7d9b1f4"


def test_upgrade_backfills_templates_and_accounts_roundtrip(db):
    """真实 downgrade→upgrade 往返：boss 账号开启、readonly 账号失败关闭、覆盖层让位。"""
    import os

    from alembic import command as alembic_command
    from alembic.config import Config as AlembicConfig

    from app.auth import hash_password
    from app.db import engine
    from app.models.system import SysUser

    for role in ("boss", "readonly", "sales"):
        base = permissions.effective(role, None)
        db.add(SysUser(
            username=f"sa-perm-migration-{role}", role=role, display_name=role,
            password_hash=hash_password("synthetic-migration-password"),
            is_active=True, template_code=role, template_version=1,
            template_perms=base,
            # 升级前不存在这两个键；预置 override 模拟同批误写，升级应剥离
            perm_overrides={k: not (role == "boss") for k in _KEYS},
            permissions=base,
        ))
    db.commit()
    db.close()
    cfg = AlembicConfig(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option(
        "script_location", os.path.join(os.path.dirname(__file__), "..", "alembic"))
    try:
        alembic_command.downgrade(cfg, _PREV)
        with engine.connect() as conn:
            for key in _KEYS:
                assert conn.execute(text(
                    "SELECT count(*) FROM sys_role_template WHERE permissions ? :k "
                    "OR permissions IS NULL"), {"k": key}).scalar_one() == 0
                assert conn.execute(text(
                    "SELECT count(*) FROM sys_user WHERE template_perms ? :k "
                    "OR perm_overrides ? :k"), {"k": key}).scalar_one() == 0
                # legacy permissions 图同样剥离（无快照老账号读这里）
                assert conn.execute(text(
                    "SELECT count(*) FROM sys_user WHERE permissions ? :k"),
                    {"k": key}).scalar_one() == 0
        alembic_command.upgrade(cfg, _REV)
        with engine.connect() as conn:
            # 模板：boss/admin 开，其余模板关
            for key in _KEYS:
                rows = conn.execute(text(
                    "SELECT code, permissions ->> :k AS enabled FROM sys_role_template "
                    "ORDER BY code"), {"k": key}).mappings().all()
                assert rows
                for row in rows:
                    expected = "true" if row["code"] in ("boss", "admin") else "false"
                    assert row["enabled"] == expected, (row["code"], key)
            # 账号：boss 快照 true + override 剥离；readonly/sales 快照 false + override 剥离；
            # legacy permissions 双写同口径（无快照老账号读这里，boss 不能被步骤 3 的
            # fail-closed 困死）
            for key in _KEYS:
                rows = conn.execute(text(
                    "SELECT username, role, template_perms ->> :k AS enabled, "
                    "permissions ->> :k AS legacy_enabled, "
                    "perm_overrides ? :k AS overridden FROM sys_user "
                    "WHERE username LIKE 'sa-perm-migration-%'"),
                    {"k": key}).mappings().all()
                assert len(rows) == 3
                for row in rows:
                    assert not row["overridden"], (row["username"], key)
                    expected = "true" if row["role"] == "boss" else "false"
                    assert row["enabled"] == expected, (row["username"], key)
                    assert row["legacy_enabled"] == expected, (row["username"], key)
    finally:
        alembic_command.upgrade(cfg, "head")


def test_migration_is_single_head():
    """版本目录单头，且本迁移位于该 head 的祖先链上（后续 PR 接在其后时本测试不应失效）。"""
    import os

    from alembic.config import Config as AlembicConfig
    from alembic.script import ScriptDirectory

    cfg = AlembicConfig(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option(
        "script_location", os.path.join(os.path.dirname(__file__), "..", "alembic"))
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert len(heads) == 1
    chain = {rev.revision for rev in script.walk_revisions(base="base", head=heads[0])}
    assert _REV in chain

"""测试库寿命守卫：边界、重建后迁移链，以及三类破坏性安全门。"""
import os
import uuid

import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from app.config import get_settings
from tests.db_guard import ensure_test_database


def _maintenance_engine(url):
    return create_engine(
        url.set(database="postgres"), poolclass=NullPool,
        isolation_level="AUTOCOMMIT",
    )


def _unique_url(prefix: str = "guard"):
    base = make_url(os.environ["DATABASE_URL"])
    name = f"spareparts_test_{prefix}_{uuid.uuid4().hex[:10]}"
    return base, name, base.set(database=name)


def _render(url) -> str:
    return url.render_as_string(hide_password=False)


def _burn_columns(url, count: int, table: str = "burn") -> None:
    probe = create_engine(url, poolclass=NullPool)
    try:
        columns = ", ".join(f'"c{i}" integer' for i in range(count))
        drops = ", ".join(f'DROP COLUMN "c{i}"' for i in range(count))
        with probe.begin() as conn:
            conn.execute(text(f'CREATE TABLE "{table}" ({columns})'))
            conn.execute(text(f'ALTER TABLE "{table}" {drops}'))
    finally:
        probe.dispose()


def _drop_database(maint, name: str) -> None:
    with maint.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))


def _upgrade_to_head(database_url: str) -> str:
    """对刚重建的隔离库真实执行项目 Alembic 链，并返回脚本 head。"""
    old = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    get_settings.cache_clear()
    cfg = AlembicConfig(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(os.path.dirname(__file__), "..", "alembic"))
    expected = ScriptDirectory.from_config(cfg).get_current_head()
    try:
        alembic_command.upgrade(cfg, "head")
    finally:
        if old is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = old
        get_settings.cache_clear()
    return expected


def test_guard_keeps_exactly_800_rebuilds_801_then_upgrades_head():
    _, name, url = _unique_url("boundary")
    rendered = _render(url)
    maint = _maintenance_engine(url)
    try:
        assert ensure_test_database(rendered) == "created"
        with maint.connect() as conn:
            oid_before = conn.execute(
                text("SELECT oid FROM pg_database WHERE datname = :db"), {"db": name}
            ).scalar_one()

        _burn_columns(url, 800)
        assert ensure_test_database(rendered) == "healthy"       # 恰好 800 不重建
        with maint.connect() as conn:
            assert conn.execute(
                text("SELECT oid FROM pg_database WHERE datname = :db"), {"db": name}
            ).scalar_one() == oid_before

        # 在同一表累计第 801 个 dropped attnum，必须跨过阈值重建。
        probe = create_engine(url, poolclass=NullPool)
        try:
            with probe.begin() as conn:
                conn.execute(text('ALTER TABLE "burn" ADD COLUMN "c800" integer'))
                conn.execute(text('ALTER TABLE "burn" DROP COLUMN "c800"'))
        finally:
            probe.dispose()
        assert ensure_test_database(rendered) == "recreated"

        with maint.connect() as conn:
            oid_after = conn.execute(
                text("SELECT oid FROM pg_database WHERE datname = :db"), {"db": name}
            ).scalar_one()
        assert oid_after != oid_before

        # Spec 门槛：不是“空库建回来了”就算成功，必须在这个刚重建的库上
        # 重放整条迁移链。
        expected_head = _upgrade_to_head(rendered)
        check = create_engine(url, poolclass=NullPool)
        try:
            with check.connect() as conn:
                assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() \
                    == expected_head
                tables = {row[0] for row in conn.execute(text(
                    "SELECT table_name FROM information_schema.tables"
                    " WHERE table_schema='public' AND table_name IN"
                    " ('dim_part','sys_user','part_pool','sys_role_template')"
                ))}
            assert tables == {"dim_part", "sys_user", "part_pool", "sys_role_template"}
        finally:
            check.dispose()
    finally:
        _drop_database(maint, name)
        maint.dispose()


def test_guard_refuses_remote_host_before_connect(monkeypatch):
    base = make_url(os.environ["DATABASE_URL"])
    remote = base.set(host="db.example.invalid", database="spareparts_test_remote_guard")
    monkeypatch.delenv("ALLOW_REMOTE_TEST_DB_REBUILD", raising=False)
    with pytest.raises(RuntimeError, match="拒绝连接或重建非本地测试数据库主机"):
        ensure_test_database(_render(remote))


@pytest.mark.parametrize(("key", "value"), [
    ("host", "db.example.invalid"),
    ("hostaddr", "203.0.113.10"),
    ("dbname", "production"),
])
def test_guard_refuses_libpq_target_query_override_before_connect(
    monkeypatch, key, value,
):
    """URL authority 看似 localhost 也不够；libpq query 可改写真正连接目标。"""
    base = make_url(os.environ["DATABASE_URL"]).set(
        database="spareparts_test_query_override",
    ).update_query_dict({key: value})
    monkeypatch.delenv("ALLOW_REMOTE_TEST_DB_REBUILD", raising=False)
    monkeypatch.setattr(
        "tests.db_guard.create_engine",
        lambda *_args, **_kwargs: pytest.fail("目标校验必须发生在任何连接之前"),
    )
    with pytest.raises(RuntimeError, match="覆盖连接目标的 libpq query 参数"):
        ensure_test_database(_render(base))


def test_guard_refuses_non_test_database_name_before_connect():
    url = make_url(os.environ["DATABASE_URL"]).set(database="spareparts_dev")
    with pytest.raises(RuntimeError, match="拒绝管理非测试数据库"):
        ensure_test_database(_render(url))


def test_guard_refuses_rebuild_while_another_client_is_connected():
    _, name, url = _unique_url("busy")
    rendered = _render(url)
    maint = _maintenance_engine(url)
    blocker = None
    blocker_conn = None
    blocker_tx = None
    try:
        assert ensure_test_database(rendered) == "created"
        _burn_columns(url, 801)
        blocker = create_engine(url, poolclass=NullPool)
        blocker_conn = blocker.connect()
        blocker_tx = blocker_conn.begin()
        blocker_conn.execute(text("SELECT 1"))  # 保持一个 idle-in-transaction 客户端
        with pytest.raises(RuntimeError, match="仍有 1 个其他客户端连接"):
            ensure_test_database(rendered)
        with maint.connect() as conn:
            assert conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname=:db"), {"db": name}
            ).scalar_one() == 1
    finally:
        if blocker_tx is not None:
            blocker_tx.rollback()
        if blocker_conn is not None:
            blocker_conn.close()
        if blocker is not None:
            blocker.dispose()
        _drop_database(maint, name)
        maint.dispose()


def test_guard_refuses_when_another_maintenance_session_holds_guard_lock():
    base, name, url = _unique_url("lock")
    maint = _maintenance_engine(base)
    lock_key = f"spareparts-test-db-guard:{name}"
    try:
        with maint.connect() as blocker:
            blocker.execute(
                text("SELECT pg_advisory_lock(hashtext(:key))"), {"key": lock_key},
            )
            with pytest.raises(RuntimeError, match="正被另一个寿命守卫处理"):
                ensure_test_database(_render(url))
            blocker.execute(
                text("SELECT pg_advisory_unlock(hashtext(:key))"), {"key": lock_key},
            )
    finally:
        _drop_database(maint, name)
        maint.dispose()


def test_guard_checks_recreate_privilege_before_drop():
    base, name, owner_url = _unique_url("priv")
    maint = _maintenance_engine(base)
    role = f"guard_nocreatedb_{uuid.uuid4().hex[:10]}"
    password = "guardpw123456"
    low_url = owner_url.set(username=role, password=password)
    try:
        with maint.connect() as conn:
            conn.execute(text(f'CREATE ROLE "{role}" LOGIN PASSWORD \'{password}\' NOCREATEDB'))
            conn.execute(text(f'CREATE DATABASE "{name}" OWNER "{role}"'))
        _burn_columns(low_url, 801)
        with pytest.raises(RuntimeError, match="为避免先删后建失败，未执行任何破坏动作"):
            ensure_test_database(_render(low_url))
        with maint.connect() as conn:
            assert conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname=:db"), {"db": name}
            ).scalar_one() == 1
    finally:
        _drop_database(maint, name)
        with maint.connect() as conn:
            conn.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
        maint.dispose()


def test_guard_allows_exact_owner_with_createdb_to_recreate():
    base, name, owner_url = _unique_url("exact_owner")
    maint = _maintenance_engine(base)
    owner = f"guard_exact_owner_{uuid.uuid4().hex[:10]}"
    password = "guardpw123456"
    role_url = owner_url.set(username=owner, password=password)
    try:
        with maint.connect() as conn:
            conn.execute(text(
                f'CREATE ROLE "{owner}" LOGIN PASSWORD \'{password}\' CREATEDB'
            ))
            conn.execute(text(f'CREATE DATABASE "{name}" OWNER "{owner}"'))
        _burn_columns(role_url, 801)
        assert ensure_test_database(_render(role_url)) == "recreated"
        with maint.connect() as conn:
            actual_owner = conn.execute(text(
                "SELECT r.rolname FROM pg_database d"
                " JOIN pg_roles r ON r.oid=d.datdba WHERE d.datname=:db"
            ), {"db": name}).scalar_one()
        assert actual_owner == owner
    finally:
        _drop_database(maint, name)
        with maint.connect() as conn:
            conn.execute(text(f'DROP ROLE IF EXISTS "{owner}"'))
        maint.dispose()


def test_guard_requires_exact_owner_not_membership_in_owner_role():
    base, name, url = _unique_url("owner_membership")
    maint = _maintenance_engine(base)
    owner = f"guard_owner_{uuid.uuid4().hex[:10]}"
    member = f"guard_member_{uuid.uuid4().hex[:10]}"
    password = "guardpw123456"
    member_url = url.set(username=member, password=password)
    try:
        with maint.connect() as conn:
            conn.execute(text(f'CREATE ROLE "{owner}" NOLOGIN'))
            conn.execute(text(
                f'CREATE ROLE "{member}" LOGIN PASSWORD \'{password}\' CREATEDB'
            ))
            conn.execute(text(f'GRANT "{owner}" TO "{member}"'))
            conn.execute(text(f'CREATE DATABASE "{name}" OWNER "{owner}"'))
        # 用维护角色制造寿命耗尽；被测 member 具备 CREATEDB 且属于 owner role，
        # 但当前会话没有 SET ROLE，不能被预检误判成精确 owner。
        _burn_columns(url, 801)
        with pytest.raises(RuntimeError, match="DROP_DATABASE=False"):
            ensure_test_database(_render(member_url))
        with maint.connect() as conn:
            assert conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname=:db"), {"db": name}
            ).scalar_one() == 1
    finally:
        _drop_database(maint, name)
        with maint.connect() as conn:
            conn.execute(text(f'DROP ROLE IF EXISTS "{member}"'))
            conn.execute(text(f'DROP ROLE IF EXISTS "{owner}"'))
        maint.dispose()

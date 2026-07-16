"""测试库寿命守卫：不存在可创建、健康库不动、dropped 列超限会重建。"""
import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from tests.db_guard import ensure_test_database


def _maintenance_engine(url):
    return create_engine(
        url.set(database="postgres"), poolclass=NullPool,
        isolation_level="AUTOCOMMIT",
    )


def test_database_guard_rebuilds_only_exhausted_test_database():
    base = make_url(os.environ["DATABASE_URL"])
    name = f"spareparts_test_guard_{uuid.uuid4().hex[:10]}"
    url = base.set(database=name)
    rendered = url.render_as_string(hide_password=False)
    maint = _maintenance_engine(url)
    probe = None
    try:
        assert ensure_test_database(rendered) == "created"
        with maint.connect() as conn:
            oid_before = conn.execute(
                text("SELECT oid FROM pg_database WHERE datname = :db"), {"db": name}
            ).scalar_one()

        # 空库是健康分支，不能无故重建。
        assert ensure_test_database(rendered) == "healthy"
        with maint.connect() as conn:
            assert conn.execute(
                text("SELECT oid FROM pg_database WHERE datname = :db"), {"db": name}
            ).scalar_one() == oid_before

        # 一次性制造 801 个 dropped 列，确定性跨过默认阈值 800。
        probe = create_engine(url, poolclass=NullPool)
        columns = ", ".join(f'"c{i}" integer' for i in range(801))
        drops = ", ".join(f'DROP COLUMN "c{i}"' for i in range(801))
        with probe.begin() as conn:
            conn.execute(text(f"CREATE TABLE burn ({columns})"))
            conn.execute(text(f"ALTER TABLE burn {drops}"))
        probe.dispose()
        probe = None

        assert ensure_test_database(rendered) == "recreated"
        with maint.connect() as conn:
            oid_after = conn.execute(
                text("SELECT oid FROM pg_database WHERE datname = :db"), {"db": name}
            ).scalar_one()
        assert oid_after != oid_before
        check = create_engine(url, poolclass=NullPool)
        try:
            with check.connect() as conn:
                assert conn.execute(text("SELECT to_regclass('burn')")).scalar() is None
        finally:
            check.dispose()
    finally:
        if probe is not None:
            probe.dispose()
        with maint.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        maint.dispose()


def test_database_guard_refuses_non_test_database_name():
    url = make_url(os.environ["DATABASE_URL"]).set(database="spareparts_dev")
    with pytest.raises(RuntimeError, match="拒绝管理非测试数据库"):
        ensure_test_database(url.render_as_string(hide_password=False))

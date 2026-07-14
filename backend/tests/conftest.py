"""测试夹具：独立 Postgres（127.0.0.1:5433/spareparts_test），绝不连 dev 库。

会话级先确保测试库健康（不存在则创建；迁移循环测试烧掉的 dropped 列逼近
PostgreSQL 1600 attnum 上限时整库重建，见 _ensure_test_database），再跑一次
alembic upgrade head（顺带验证迁移链）；每个用例前 TRUNCATE 全部业务表。
"""
import os

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://spareparts:spareparts@127.0.0.1:5433/spareparts_test",
)
assert "spareparts_test" in os.environ["DATABASE_URL"], "测试必须使用独立测试库"

import pytest  # noqa: E402
from alembic import command as alembic_command  # noqa: E402
from alembic.config import Config as AlembicConfig  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.db import SessionLocal, engine  # noqa: E402

# PostgreSQL 每表 1600 列上限按「曾经分配过的 attnum」计数：DROP COLUMN 只把
# pg_attribute.attisdropped 置真，attnum 永不复用（VACUUM FULL 也不回收）。
# 迁移循环测试（test_migration_manual_pool / test_migration_pool_seq）每轮全量
# pytest 在 part_pool 上实测烧掉 120 个 attnum，十几轮就会撞 TooManyColumns
# （2026-07-14 实际发生过）。阈值 800：重建约每 6 轮全量一次，且 800 +
# 单轮消耗(120) + 活列数 仍远低于 1600，会话中途不可能撞限。
_DROPPED_COLS_REBUILD_THRESHOLD = 800


def _ensure_test_database() -> None:
    """确保测试库存在，且任一表的 dropped 列都未逼近 1600 attnum 上限。

    超阈值时 DROP DATABASE 重建：测试库无持久数据（每用例 TRUNCATE），重建
    零成本，随后的 alembic upgrade head 会从零重放整条迁移链。CI 每次都是
    全新库，此守卫恒为 no-op。必须在 app.db.engine 发出第一个连接前调用。
    """
    url = make_url(os.environ["DATABASE_URL"])
    test_db = url.database
    # DROP/CREATE DATABASE 不能在事务里跑，须经维护库 postgres + AUTOCOMMIT
    maint = create_engine(url.set(database="postgres"), poolclass=NullPool,
                          isolation_level="AUTOCOMMIT")
    try:
        with maint.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :db"),
                {"db": test_db}).scalar()
        if exists:
            probe = create_engine(url, poolclass=NullPool)
            try:
                with probe.connect() as conn:
                    worst = conn.execute(text(
                        "SELECT COALESCE(MAX(n), 0) FROM ("
                        "  SELECT COUNT(*) AS n FROM pg_attribute"
                        "  WHERE attisdropped GROUP BY attrelid) per_table"
                    )).scalar()
            finally:
                probe.dispose()
            if worst <= _DROPPED_COLS_REBUILD_THRESHOLD:
                return
            print(f"[conftest] {test_db} 单表 dropped 列已达 {worst}"
                  f"（阈值 {_DROPPED_COLS_REBUILD_THRESHOLD}），整库重建以避开"
                  " PostgreSQL 1600 attnum 上限")
            with maint.connect() as conn:
                conn.execute(text(f'DROP DATABASE "{test_db}" WITH (FORCE)'))
        with maint.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{test_db}"'))
    finally:
        maint.dispose()

_TABLES = [
    "chat_message", "chat_session",
    "product_data_quality_issues", "product_merge_logs", "product_match_candidates",
    "product_specs", "product_categories", "brands",
    # 池三表：TRUNCATE 不会重置独立序列 part_pool_group_id_seq（非 owned），
    # 「退役 ID 永不复用」语义跨用例保持
    "part_pool_price_policy", "part_pool_member", "part_pool",
    "f_part_inquiry", "part_substitute", "inventory",
    "f_project_expense",
    "f_maintenance_line", "f_maintenance_order",
    "f_sales_line", "f_sales_order", "f_purchase_line", "f_purchase_order",
    "part_alias", "dim_part", "dim_supplier", "dim_customer",
    "sys_audit_log", "sys_access_log", "sys_raw_file", "sys_import_error", "sys_import_batch",
    "sys_import_job", "sys_user",
]


@pytest.fixture(scope="session", autouse=True)
def migrated():
    _ensure_test_database()
    cfg = AlembicConfig(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option("script_location",
                        os.path.join(os.path.dirname(__file__), "..", "alembic"))
    alembic_command.upgrade(cfg, "head")
    yield


@pytest.fixture()
def db(migrated):
    with engine.connect() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(_TABLES)} RESTART IDENTITY CASCADE"))
        conn.commit()
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()

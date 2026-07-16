"""长寿命 PostgreSQL 测试库的 dropped-column 寿命守卫。"""
import os
import re

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

# PostgreSQL 每表 1600 列上限按「曾经分配过的 attnum」计数：DROP COLUMN 只把
# pg_attribute.attisdropped 置真，attnum 永不复用。迁移循环测试每轮会在
# part_pool 等表上反复 DROP/ADD COLUMN，长期复用同一测试库最终会撞
# psycopg.errors.TooManyColumns。800 给下一轮完整迁移循环保留了充足余量。
DROPPED_COLS_REBUILD_THRESHOLD = 800
_TEST_DATABASE_NAME = re.compile(r"^spareparts_test(?:_[A-Za-z0-9_]+)?$")


def ensure_test_database(
    database_url: str | None = None,
    *,
    threshold: int = DROPPED_COLS_REBUILD_THRESHOLD,
) -> str:
    """确保测试库存在，且任一表的 dropped 列未逼近 1600 attnum 上限。

    返回 ``created`` / ``recreated`` / ``healthy``，便于回归测试验证三条分支。
    数据库名必须是 ``spareparts_test`` 或其带下划线后缀的隔离测试库，避免配置
    错误时对开发库执行 DROP DATABASE。
    """
    url = make_url(database_url or os.environ["DATABASE_URL"])
    test_db = url.database or ""
    if not _TEST_DATABASE_NAME.fullmatch(test_db):
        raise RuntimeError(
            f"拒绝管理非测试数据库 {test_db!r}；名称必须是 spareparts_test[_后缀]"
        )

    maint = create_engine(
        url.set(database="postgres"),
        poolclass=NullPool,
        isolation_level="AUTOCOMMIT",
    )
    try:
        with maint.connect() as conn:
            exists = bool(conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :db"),
                {"db": test_db},
            ).scalar())
        action = "created"
        if exists:
            probe = create_engine(url, poolclass=NullPool)
            try:
                with probe.connect() as conn:
                    worst = int(conn.execute(text(
                        "SELECT COALESCE(MAX(n), 0) FROM ("
                        "  SELECT COUNT(*) AS n FROM pg_attribute"
                        "  WHERE attisdropped GROUP BY attrelid"
                        ") per_table"
                    )).scalar() or 0)
            finally:
                probe.dispose()
            if worst <= threshold:
                return "healthy"
            print(
                f"[conftest] {test_db} 单表 dropped 列已达 {worst}（阈值 {threshold}），"
                "整库重建以避开 PostgreSQL 1600 attnum 上限"
            )
            with maint.connect() as conn:
                conn.execute(text(f'DROP DATABASE "{test_db}" WITH (FORCE)'))
            action = "recreated"
        with maint.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{test_db}"'))
        return action
    finally:
        maint.dispose()

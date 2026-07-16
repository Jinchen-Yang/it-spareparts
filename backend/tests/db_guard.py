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
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_REMOTE_OPT_IN = "ALLOW_REMOTE_TEST_DB_REBUILD"


def _allow_target(url, test_db: str) -> None:
    """所有连接和破坏动作之前完成目标白名单校验。"""
    if not _TEST_DATABASE_NAME.fullmatch(test_db):
        raise RuntimeError(
            f"拒绝管理非测试数据库 {test_db!r}；名称必须是 spareparts_test[_后缀]"
        )
    remote_opt_in = os.environ.get(_REMOTE_OPT_IN, "").strip().lower() in {
        "1", "true", "yes",
    }
    if url.host not in _LOCAL_HOSTS and not remote_opt_in:
        raise RuntimeError(
            f"拒绝连接或重建非本地测试数据库主机 {url.host!r}；默认只允许"
            f" {sorted(_LOCAL_HOSTS)}，确需远端隔离测试库时显式设置 {_REMOTE_OPT_IN}=1"
        )


def _assert_recreate_privileges(conn, test_db: str, *, exists: bool) -> None:
    """DROP 前一次性证明当前角色既能删除目标库，也能重新 CREATE DATABASE。"""
    row = conn.execute(text(
        "SELECT r.rolsuper, r.rolcreatedb,"
        "       CASE WHEN d.datdba IS NULL THEN false"
        "            ELSE pg_has_role(current_user, d.datdba, 'USAGE') END AS owns_db"
        " FROM pg_roles r LEFT JOIN pg_database d ON d.datname = :db"
        " WHERE r.rolname = current_user"
    ), {"db": test_db}).one()
    can_create = bool(row.rolsuper or row.rolcreatedb)
    can_drop = bool(row.rolsuper or row.owns_db)
    if not can_create or (exists and not can_drop):
        raise RuntimeError(
            f"当前数据库角色无法安全重建 {test_db!r}："
            f"CREATE_DATABASE={can_create}, DROP_DATABASE={can_drop}；"
            "为避免先删后建失败，未执行任何破坏动作"
        )


def ensure_test_database(
    database_url: str | None = None,
    *,
    threshold: int = DROPPED_COLS_REBUILD_THRESHOLD,
) -> str:
    """确保测试库存在，且任一表的 dropped 列未逼近 1600 attnum 上限。

    返回 ``created`` / ``recreated`` / ``healthy``，便于回归测试验证三条分支。
    数据库名和主机在发起连接前双重校验；重建过程还会串行化、拒绝占用中的数据库，
    并在 DROP 前证明当前角色能重新 CREATE，避免任何“删掉却建不回来”的窗口。
    """
    url = make_url(database_url or os.environ["DATABASE_URL"])
    test_db = url.database or ""
    _allow_target(url, test_db)

    maint = create_engine(
        url.set(database="postgres"),
        poolclass=NullPool,
        isolation_level="AUTOCOMMIT",
    )
    try:
        with maint.connect() as conn:
            lock_key = f"spareparts-test-db-guard:{test_db}"
            locked = bool(conn.execute(
                text("SELECT pg_try_advisory_lock(hashtext(:key))"), {"key": lock_key}
            ).scalar())
            if not locked:
                raise RuntimeError(
                    f"测试数据库 {test_db!r} 正被另一个寿命守卫处理，拒绝并行重建"
                )
            try:
                exists = bool(conn.execute(
                    text("SELECT 1 FROM pg_database WHERE datname = :db"),
                    {"db": test_db},
                ).scalar())
                action = "created"
                if exists:
                    probe = create_engine(url, poolclass=NullPool)
                    try:
                        with probe.connect() as probe_conn:
                            worst = int(probe_conn.execute(text(
                                "SELECT COALESCE(MAX(n), 0) FROM ("
                                "  SELECT COUNT(*) AS n FROM pg_attribute"
                                "  WHERE attisdropped GROUP BY attrelid"
                                ") per_table"
                            )).scalar() or 0)
                    finally:
                        probe.dispose()
                    if worst <= threshold:
                        return "healthy"

                    # 绝不使用 WITH (FORCE)：另一个 pytest 即便没拿本守卫的 advisory
                    # lock，只要仍连着目标库就拒绝重建，不能杀掉它的会话。
                    sessions = int(conn.execute(text(
                        "SELECT COUNT(*) FROM pg_stat_activity"
                        " WHERE datname = :db AND pid <> pg_backend_pid()"
                        "   AND backend_type = 'client backend'"
                    ), {"db": test_db}).scalar() or 0)
                    if sessions:
                        raise RuntimeError(
                            f"测试数据库 {test_db!r} 仍有 {sessions} 个其他客户端连接；"
                            "拒绝重建，避免终止并行 pytest"
                        )
                    _assert_recreate_privileges(conn, test_db, exists=True)
                    print(
                        f"[conftest] {test_db} 单表 dropped 列已达 {worst}"
                        f"（阈值 {threshold}），整库重建以避开 PostgreSQL 1600 attnum 上限"
                    )
                    conn.execute(text(f'DROP DATABASE "{test_db}"'))
                    action = "recreated"
                else:
                    _assert_recreate_privileges(conn, test_db, exists=False)
                conn.execute(text(f'CREATE DATABASE "{test_db}"'))
                return action
            finally:
                conn.execute(
                    text("SELECT pg_advisory_unlock(hashtext(:key))"), {"key": lock_key}
                )
    finally:
        maint.dispose()

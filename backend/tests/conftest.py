"""测试夹具：独立 Postgres（127.0.0.1:5433/spareparts_test），绝不连 dev 库。

会话级先确保测试库健康（不存在则创建；迁移循环测试烧掉的 dropped 列逼近
PostgreSQL 1600 attnum 上限时整库重建，见 ``ensure_test_database``），再跑一次
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
from sqlalchemy import text  # noqa: E402

from app.db import SessionLocal, engine  # noqa: E402
from tests.db_guard import ensure_test_database  # noqa: E402

_TABLES = [
    "chat_message", "chat_session",
    "fact_data_quality_issue",
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
    # 职位模板（权限中心 v2）：清掉用例改过/新建的模板后重播内置 5 条（下方 _reseed_templates），
    # 否则前一个用例编辑 sales 模板会污染后一个用例新建的账号快照
    "sys_role_template",
]

_BUILTIN_TEMPLATE_ROLES = ["admin", "boss", "sales", "purchaser", "readonly"]
_BUILTIN_TEMPLATE_NAMES = {"admin": "管理员", "boss": "老板", "sales": "销售",
                           "purchaser": "采购", "readonly": "只读"}


def _reseed_templates(conn) -> None:
    """重播内置模板（等价迁移 seed；值=当前代码 effective(role, None)，
    与迁移冻结值的一致性由 test_frozen_templates_match_current_code 看守）。"""
    import json

    from app import permissions as _perms
    for role in _BUILTIN_TEMPLATE_ROLES:
        conn.execute(text(
            "INSERT INTO sys_role_template"
            " (code, name, base_role, permissions, is_system, is_active, version, created_by)"
            " VALUES (:c, :n, :c, CAST(:p AS jsonb), true, true, 1, 'conftest')"),
            {"c": role, "n": _BUILTIN_TEMPLATE_NAMES[role],
             "p": json.dumps(_perms.effective(role, None))})


@pytest.fixture(scope="session", autouse=True)
def migrated():
    # app.db.engine 在模块导入时已创建；重建前清空它可能缓存的旧连接，避免 DROP 后
    # 复用指向旧数据库实例的连接。
    engine.dispose()
    ensure_test_database()
    cfg = AlembicConfig(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option("script_location",
                        os.path.join(os.path.dirname(__file__), "..", "alembic"))
    alembic_command.upgrade(cfg, "head")
    yield


@pytest.fixture()
def db(migrated):
    with engine.connect() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(_TABLES)} RESTART IDENTITY CASCADE"))
        _reseed_templates(conn)
        conn.commit()
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()

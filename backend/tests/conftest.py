"""测试夹具：独立 Postgres（127.0.0.1:5433/spareparts_test），绝不连 dev 库。

会话级跑一次 alembic upgrade head（顺带验证迁移链）；每个用例前 TRUNCATE 全部业务表。
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

_TABLES = [
    "chat_message", "chat_session",
    "product_data_quality_issues", "product_merge_logs", "product_match_candidates",
    "product_specs", "product_categories", "brands",
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

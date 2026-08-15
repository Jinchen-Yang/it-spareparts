"""测试夹具：每个 pytest 进程独占新建的数据库和原始文件目录。"""

import atexit
import os
import signal
import sys
from pathlib import Path

from tests.run_isolation import (
    CONTROLLED_RAW_BASE,
    RunLifecycle,
    cleanup_database_run,
    cleanup_raw_run,
    create_database_run,
    create_raw_run,
    validate_platform_capabilities,
    validate_database_base,
    validate_pytest_invocation,
    validate_raw_base,
)

validate_platform_capabilities()

_DEFAULT_DATABASE_BASE_URL = (
    "postgresql+psycopg://spareparts:spareparts@127.0.0.1:5433/spareparts_test"
)
_database_run = None
_raw_run = None
_app_engine = None


def _dispose_engine() -> None:
    if _app_engine is not None:
        _app_engine.dispose()


def _cleanup_database() -> None:
    if _database_run is not None:
        cleanup_database_run(_database_run)


def _cleanup_raw() -> None:
    if _raw_run is not None:
        cleanup_raw_run(_raw_run)


_lifecycle = RunLifecycle(
    engine_dispose=_dispose_engine,
    database_cleanup=_cleanup_database,
    raw_cleanup=_cleanup_raw,
)


def _cleanup_run() -> None:
    _lifecycle.cleanup()


def _atexit_cleanup() -> None:
    try:
        _cleanup_run()
    except BaseException:
        print("[conftest] pytest run cleanup failed safely", file=sys.stderr)


def _handle_sigterm(_signum, _frame) -> None:
    raise KeyboardInterrupt("pytest received SIGTERM")


atexit.register(_atexit_cleanup)
if hasattr(signal, "SIGTERM"):
    signal.signal(signal.SIGTERM, _handle_sigterm)

validate_pytest_invocation(sys.argv[1:], os.environ)
_checkout_root = Path(__file__).resolve().parents[2]
_raw_base = os.environ.get("PYTEST_RAW_FILE_BASE_DIR", str(CONTROLLED_RAW_BASE))
_raw_plan = validate_raw_base(_raw_base, checkout_root=_checkout_root)
_database_base_url = os.environ.get(
    "PYTEST_DATABASE_BASE_URL",
    os.environ.get("DATABASE_URL", _DEFAULT_DATABASE_BASE_URL),
)
validate_database_base(_database_base_url)

try:

    def _record_raw_run(handle) -> None:
        global _raw_run
        _raw_run = handle

    _raw_run = create_raw_run(_raw_plan, on_owned=_record_raw_run)

    def _record_database_run(handle) -> None:
        global _database_run
        _database_run = handle

    _database_run = create_database_run(
        _database_base_url,
        on_owned=_record_database_run,
    )
except BaseException:
    _cleanup_run()
    raise

os.environ["PYTEST_DATABASE_BASE_URL"] = _database_run.base_url
os.environ["DATABASE_URL"] = _database_run.database_url
os.environ["PYTEST_RAW_FILE_BASE_DIR"] = str(_raw_run.root)
os.environ["RAW_FILE_DIR"] = str(_raw_run.run_dir)
# 新维保接口在生产默认关闭；业务测试需显式处于 Beta 已开启环境。
# 总闸关闭及逐账号白名单边界由 test_maintenance_beta_gate 单独覆盖。
os.environ.setdefault("MAINTENANCE_BETA_ENABLED", "true")

try:
    import pytest  # noqa: E402
    from alembic import command as alembic_command  # noqa: E402
    from alembic.config import Config as AlembicConfig  # noqa: E402
    from sqlalchemy import text  # noqa: E402

    from app.db import SessionLocal, engine  # noqa: E402
except BaseException:
    _cleanup_run()
    raise
_app_engine = engine

_TABLES = [
    "chat_message", "chat_session",
    "replenishment_review_line",
    "replenishment_review",
    "replenishment_audit_event",
    "replenishment_application_line",
    "replenishment_application_version",
    "replenishment_application",
    "fact_data_quality_issue",
    "maintenance_warehouse_audit_event",
    "maintenance_warehouse_ambiguity",
    "maintenance_warehouse_document_link",
    "maintenance_warehouse_document_line",
    "maintenance_warehouse_document",
    "maintenance_warehouse_import_batch",
    "product_data_quality_issues", "product_merge_logs", "product_match_candidates",
    "product_specs", "product_categories", "brands",
    # 池三表：TRUNCATE 不会重置独立序列 part_pool_group_id_seq（非 owned），
    # 「退役 ID 永不复用」语义跨用例保持
    "part_pool_price_policy", "part_pool_member", "part_pool",
    "f_part_inquiry", "part_substitute", "inventory",
    "maintenance_manual_cost_override",
    "maintenance_roundtrip_operation",
    "maintenance_demand_delete_event",
    "maintenance_demand_tombstone",
    "maintenance_demand_delete_intent_item",
    "maintenance_demand_delete_intent",
    "f_project_expense",
    "maintenance_source_order_assignment",
    "f_maintenance_line", "f_maintenance_order",
    "f_sales_line", "f_sales_order", "f_purchase_line", "f_purchase_order",
    "part_alias", "dim_part", "dim_supplier", "dim_customer",
    "maintenance_contract_workbook_state",
    "maintenance_migration_event",
    "maintenance_migration_discrepancy",
    "maintenance_inventory_opening_balance",
    "maintenance_historical_cost_baseline",
    "maintenance_project_cutover_plan",
    "maintenance_migration_run",
    "maintenance_project_audit_log",
    "business_file_download_audit", "maintenance_acceptance_operation",
    "business_file_link", "business_file", "maintenance_acceptance_deliverable",
    "maintenance_collection_milestone", "maintenance_collection_milestone_operation",
    "maintenance_collection_plan_import_batch", "maintenance_collection_plan_source_binding",
    "maintenance_ledger_expense_row", "maintenance_ledger_plan_row",
    "maintenance_ledger_contract_row", "maintenance_ledger_import_batch",
    "maintenance_front_stock_ledger", "maintenance_front_stock",
    "maintenance_service_period",
    "maintenance_manager_upload_batch_project", "maintenance_manager_upload_batch",
    "maintenance_project_user_assignment",
    "maintenance_project_contract", "maintenance_project",
    "sys_audit_log", "sys_access_log", "sys_raw_file", "sys_import_error", "sys_import_batch",
    "sys_import_job", "sys_user",
    "sys_business_setting",
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


def _reseed_business_setting(conn) -> None:
    conn.execute(text(
        "INSERT INTO sys_business_setting"
        " (id, maintenance_project_profit_default_basis,"
        " purchase_display_basis, sales_display_basis, version)"
        " VALUES (1, 'both', 'both', 'ex', 1)"))


@pytest.fixture(scope="session", autouse=True)
def migrated():
    cfg = AlembicConfig(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option("script_location",
                        os.path.join(os.path.dirname(__file__), "..", "alembic"))
    try:
        alembic_command.upgrade(cfg, "head")
        yield
    finally:
        _cleanup_run()


def pytest_sessionfinish(session, exitstatus):
    try:
        _cleanup_run()
    except BaseException:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        print("[conftest] pytest run cleanup failed safely", file=sys.stderr)


@pytest.fixture()
def db(migrated):
    with engine.connect() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(_TABLES)} RESTART IDENTITY CASCADE"))
        _reseed_templates(conn)
        _reseed_business_setting(conn)
        conn.commit()
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()

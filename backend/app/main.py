"""FastAPI 入口。"""
import logging

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app import auth
from app.api import (
    accounts,
    agent,
    chat_sessions,
    dashboard,
    data_quality,
    data_quality_calibration,
    governance,
    imports,
    inventory,
    maintenance,
    maintenance_acceptance,
    maintenance_audit,
    maintenance_ai_fallback,
    maintenance_bad_returns,
    maintenance_bad_salvage,
    maintenance_boss_board,
    maintenance_ckd_import,
    maintenance_collection_plan_imports,
    maintenance_collection_reminders,
    maintenance_collection_evidence,
    maintenance_demands,
    maintenance_doc_import,
    maintenance_expense_reconcile,
    maintenance_front_stock,
    maintenance_ledger,
    maintenance_manager_workbooks,
    maintenance_project_assignments,
    maintenance_migration,
    maintenance_project_operations,
    maintenance_source_assignments,
    maintenance_project_workbooks,
    maintenance_project_workbook_v3,
    maintenance_projects,
    maintenance_recovery,
    maintenance_warehouse,
    maintenance_expense_collection_workbook,
    maintenance_project_master_workbook,
    maintenance_wbdd_import,
    parts,
    pool_analysis,
    pools,
    profit,
    purchases,
    replenishment,
    role_templates,
    substitutes,
    system_settings,
)
from app.config import check_security, get_settings
from app.db import engine
from app.http_controls import MigrationHttpControlsMiddleware
from app.maintenance_beta import require_maintenance_beta
from app.maintenance_boss import require_maintenance_boss

_log = logging.getLogger("startup")
settings = get_settings()

# 安全自检：prod 下默认弱口令/密钥直接拒绝启动；dev 下仅醒目告警（不打断本地开发）
_sec_warns = check_security(settings)
if _sec_warns:
    if settings.environment == "prod":
        raise RuntimeError("生产环境禁止使用默认口令/密钥：" + "；".join(_sec_warns))
    for w in _sec_warns:
        _log.warning("[安全告警] %s（部署到生产前务必在 .env 覆盖，并设 ENVIRONMENT=prod）", w)

app = FastAPI(title=settings.app_name)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(
    MigrationHttpControlsMiddleware,
    path_prefix=f"{settings.api_prefix}/maintenance/migration-runs",
    max_body_bytes=settings.maintenance_migration_max_body_bytes,
)

app.include_router(auth.router, prefix=settings.api_prefix)
app.include_router(accounts.router, prefix=settings.api_prefix)
app.include_router(role_templates.router, prefix=settings.api_prefix)
app.include_router(imports.router, prefix=settings.api_prefix)
app.include_router(parts.router, prefix=settings.api_prefix)
app.include_router(profit.router, prefix=settings.api_prefix)
app.include_router(inventory.router, prefix=settings.api_prefix)
app.include_router(substitutes.router, prefix=settings.api_prefix)
app.include_router(governance.router, prefix=settings.api_prefix)
app.include_router(data_quality.router, prefix=settings.api_prefix)
app.include_router(data_quality_calibration.router, prefix=settings.api_prefix)
app.include_router(agent.router, prefix=settings.api_prefix)
app.include_router(chat_sessions.router, prefix=settings.api_prefix)
app.include_router(purchases.router, prefix=settings.api_prefix)
app.include_router(replenishment.router, prefix=settings.api_prefix)
app.include_router(maintenance.router, prefix=settings.api_prefix)
# 维保展示板（plan v1.3）：独立 flag 闸（router 自带 require_maintenance_boss），
# 不挂 Beta 依赖——回滚=关 maintenance_boss_dashboard_enabled。
app.include_router(maintenance_wbdd_import.router, prefix=settings.api_prefix)
app.include_router(maintenance_expense_collection_workbook.router, prefix=settings.api_prefix)
app.include_router(maintenance_project_master_workbook.router, prefix=settings.api_prefix)
app.include_router(maintenance_boss_board.router, prefix=settings.api_prefix)
maintenance_beta_dependencies = [Depends(require_maintenance_beta)]
# 新 2 页（卡墙/项目面板，plan v1.3）依赖的 router 随 boss 总闸走：beta 总闸在 v1.23
# 发布后恒 false（审计 §2 2a），若面板的基础信息/归属挂靠仍挂 beta 闸会整组 404
# ——2026-08-17 生产实发（「页面不存在」）。回滚口径统一＝关 boss 闸。
maintenance_boss_dependencies = [Depends(require_maintenance_boss)]
app.include_router(
    maintenance_acceptance.router,
    prefix=settings.api_prefix,
    dependencies=maintenance_beta_dependencies,
)
app.include_router(
    maintenance_project_assignments.router,
    prefix=settings.api_prefix,
    dependencies=maintenance_beta_dependencies,
)
app.include_router(
    maintenance_demands.router,
    prefix=settings.api_prefix,
    dependencies=maintenance_beta_dependencies,
)
app.include_router(
    maintenance_manager_workbooks.router,
    prefix=settings.api_prefix,
    dependencies=maintenance_beta_dependencies,
)
app.include_router(
    maintenance_source_assignments.router,
    prefix=settings.api_prefix,
    # 归属挂靠是项目面板「基础信息」tab 的功能（#45/#48）——随 boss 总闸，不随 beta
    dependencies=maintenance_boss_dependencies,
)
app.include_router(
    maintenance_migration.router,
    prefix=settings.api_prefix,
    dependencies=maintenance_beta_dependencies,
)
# The stable operations router must precede the project-master ``/{project_id}``
# route so literal paths such as ``/operations`` cannot be captured as an id.
app.include_router(
    maintenance_project_operations.router,
    prefix=settings.api_prefix,
    dependencies=maintenance_beta_dependencies,
)
app.include_router(
    maintenance_project_operations.site_issue_router,
    prefix=settings.api_prefix,
    dependencies=maintenance_beta_dependencies,
)
app.include_router(
    maintenance_bad_returns.router,
    prefix=settings.api_prefix,
    dependencies=maintenance_beta_dependencies,
)
app.include_router(
    maintenance_project_workbooks.router,
    prefix=settings.api_prefix,
    dependencies=maintenance_beta_dependencies,
)
app.include_router(
    maintenance_project_workbook_v3.router,
    prefix=settings.api_prefix,
    dependencies=maintenance_beta_dependencies,
)
app.include_router(
    maintenance_warehouse.router,
    prefix=settings.api_prefix,
    dependencies=maintenance_beta_dependencies,
)
app.include_router(
    maintenance_collection_evidence.router,
    prefix=settings.api_prefix,
    dependencies=maintenance_beta_dependencies,
)
app.include_router(
    maintenance_collection_reminders.router,
    prefix=settings.api_prefix,
    dependencies=maintenance_beta_dependencies,
)
app.include_router(
    maintenance_collection_plan_imports.router,
    prefix=settings.api_prefix,
    dependencies=maintenance_beta_dependencies,
)
app.include_router(
    maintenance_ledger.router,
    prefix=settings.api_prefix,
    dependencies=maintenance_beta_dependencies,
)
app.include_router(
    maintenance_front_stock.router,
    prefix=settings.api_prefix,
    dependencies=maintenance_beta_dependencies,
)
app.include_router(
    maintenance_recovery.router,
    prefix=settings.api_prefix,
    dependencies=maintenance_beta_dependencies,
)
app.include_router(
    maintenance_bad_salvage.router,
    prefix=settings.api_prefix,
    dependencies=maintenance_beta_dependencies,
)
app.include_router(
    maintenance_ckd_import.router,
    prefix=settings.api_prefix,
    dependencies=maintenance_beta_dependencies,
)
app.include_router(
    maintenance_doc_import.router,
    prefix=settings.api_prefix,
    dependencies=maintenance_beta_dependencies,
)
app.include_router(
    maintenance_ai_fallback.router,
    prefix=settings.api_prefix,
    dependencies=maintenance_beta_dependencies,
)
app.include_router(
    maintenance_expense_reconcile.router,
    prefix=settings.api_prefix,
    dependencies=maintenance_beta_dependencies,
)
app.include_router(
    maintenance_projects.router,
    prefix=settings.api_prefix,
    # 项目基础信息 GET/PATCH 是面板「基础信息 tab／编辑基本信息」的落点（各端点
    # 另有 page/action 级权限）——随 boss 总闸，不随 beta
    dependencies=maintenance_boss_dependencies,
)
app.include_router(maintenance_audit.router, prefix=settings.api_prefix)
app.include_router(dashboard.router, prefix=settings.api_prefix)
app.include_router(pools.router, prefix=settings.api_prefix)
app.include_router(pool_analysis.router, prefix=settings.api_prefix)
app.include_router(system_settings.router, prefix=settings.api_prefix)


@app.get("/health")
def health() -> dict:
    """存活探针：不依赖数据库。"""
    return {"status": "ok", "app": settings.app_name}


@app.get("/health/db")
def health_db() -> dict:
    """数据库连通性探针。"""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok", "db": "reachable"}
    except Exception as exc:  # noqa: BLE001
        # 不向客户端泄露连接串/主机/驱动等细节，详细错误写服务端日志
        _log.error("DB health check failed: %s", exc)
        return {"status": "error", "db": "unreachable"}

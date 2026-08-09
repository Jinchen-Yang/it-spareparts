"""FastAPI 入口。"""
import logging

from fastapi import FastAPI
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
    maintenance_bad_returns,
    maintenance_demands,
    maintenance_manager_workbooks,
    maintenance_project_assignments,
    maintenance_project_operations,
    maintenance_project_workbooks,
    maintenance_projects,
    maintenance_warehouse,
    parts,
    pool_analysis,
    pools,
    profit,
    purchases,
    role_templates,
    substitutes,
    system_settings,
)
from app.config import check_security, get_settings
from app.db import engine

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
app.include_router(maintenance.router, prefix=settings.api_prefix)
app.include_router(maintenance_acceptance.router, prefix=settings.api_prefix)
app.include_router(maintenance_project_assignments.router, prefix=settings.api_prefix)
app.include_router(maintenance_demands.router, prefix=settings.api_prefix)
app.include_router(maintenance_manager_workbooks.router, prefix=settings.api_prefix)
# The stable operations router must precede the project-master ``/{project_id}``
# route so literal paths such as ``/operations`` cannot be captured as an id.
app.include_router(maintenance_project_operations.router, prefix=settings.api_prefix)
app.include_router(
    maintenance_project_operations.site_issue_router,
    prefix=settings.api_prefix,
)
app.include_router(maintenance_bad_returns.router, prefix=settings.api_prefix)
app.include_router(maintenance_project_workbooks.router, prefix=settings.api_prefix)
app.include_router(maintenance_warehouse.router, prefix=settings.api_prefix)
app.include_router(maintenance_projects.router, prefix=settings.api_prefix)
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

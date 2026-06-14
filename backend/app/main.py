"""FastAPI 入口。"""
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app import auth
from app.api import accounts, agent, chat_sessions, governance, imports, inventory, parts, profit, purchases, substitutes
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
app.include_router(imports.router, prefix=settings.api_prefix)
app.include_router(parts.router, prefix=settings.api_prefix)
app.include_router(profit.router, prefix=settings.api_prefix)
app.include_router(inventory.router, prefix=settings.api_prefix)
app.include_router(substitutes.router, prefix=settings.api_prefix)
app.include_router(governance.router, prefix=settings.api_prefix)
app.include_router(agent.router, prefix=settings.api_prefix)
app.include_router(chat_sessions.router, prefix=settings.api_prefix)
app.include_router(purchases.router, prefix=settings.api_prefix)


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

"""FastAPI 入口。"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app import auth
from app.api import imports, inventory, parts, profit, substitutes
from app.config import get_settings
from app.db import engine

settings = get_settings()

app = FastAPI(title=settings.app_name)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix=settings.api_prefix)
app.include_router(imports.router, prefix=settings.api_prefix)
app.include_router(parts.router, prefix=settings.api_prefix)
app.include_router(profit.router, prefix=settings.api_prefix)
app.include_router(inventory.router, prefix=settings.api_prefix)
app.include_router(substitutes.router, prefix=settings.api_prefix)


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
    except Exception as exc:  # noqa: BLE001 —— 探针需返回错误信息而非抛 500
        return {"status": "error", "db": "unreachable", "detail": str(exc)}

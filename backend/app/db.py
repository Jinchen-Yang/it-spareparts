"""数据库 engine / session / Base（SQLAlchemy 2.0 声明式）。"""
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

_settings = get_settings()

# engine 是惰性连接：创建时不会真正连库，服务可在无 DB 时启动。
# 池显式配置：AI 对话流式期间存在 worker 线程并行连接（见 api/chat_sessions），
# 默认 5+10 在多路对话下会被打满、拖垮其他端点。
engine = create_engine(_settings.database_url, pool_pre_ping=True, future=True,
                       pool_size=10, max_overflow=20, hide_parameters=True)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖：每个请求一个会话。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

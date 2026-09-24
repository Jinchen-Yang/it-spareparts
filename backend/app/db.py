"""数据库 engine / session / Base（SQLAlchemy 2.0 声明式）。"""
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

_settings = get_settings()

# 连接级超时（PG options）：锁等待 10s、单语句 10min、事务内空闲 15min 即断开。
# 兜底 2026-09-24 生产事故——行锁互等 + 对端事务停摆且 PG 无任何超时，API 冻死
# 1.5 小时；有超时后最坏结果是单请求报错回滚，不再拖垮全站。任一项设 0 即禁用。
def _pg_timeout_options(s) -> str:  # noqa: ANN001 - Settings 循环导入风险，鸭子类型即可
    parts = []
    if s.db_lock_timeout_ms:
        parts.append(f"-c lock_timeout={s.db_lock_timeout_ms}")
    if s.db_statement_timeout_ms:
        parts.append(f"-c statement_timeout={s.db_statement_timeout_ms}")
    if s.db_idle_in_transaction_timeout_ms:
        parts.append(f"-c idle_in_transaction_session_timeout={s.db_idle_in_transaction_timeout_ms}")
    return " ".join(parts)


# engine 是惰性连接：创建时不会真正连库，服务可在无 DB 时启动。
# 池显式配置：AI 对话流式期间存在 worker 线程并行连接（见 api/chat_sessions），
# 默认 5+10 在多路对话下会被打满、拖垮其他端点。
engine = create_engine(_settings.database_url, pool_pre_ping=True, future=True,
                       pool_size=10, max_overflow=20, hide_parameters=True,
                       connect_args={"options": _pg_timeout_options(_settings)})

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

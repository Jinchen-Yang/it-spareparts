"""数据库 engine / session / Base（SQLAlchemy 2.0 声明式）。"""
from collections.abc import Generator
from contextlib import contextmanager

from fastapi import HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError, TimeoutError as PoolTimeoutError
from sqlalchemy.orm import DeclarativeBase, Session, SessionTransaction, sessionmaker

from app.config import get_settings

_settings = get_settings()

# 这是单条 SQL（包括锁等待）的预算，不是整个请求的总时长预算。
# 仅在 HTTP 请求/短鉴权会话生效，不影响显式 SessionLocal 创建的导入/后台会话。
REQUEST_STATEMENT_TIMEOUT_MS = 30_000
POOL_TIMEOUT_SECONDS = 5
AUTH_IDLE_TRANSACTION_TIMEOUT_MS = 60_000

# engine 是惰性连接：创建时不会真正连库，服务可在无 DB 时启动。
# 池显式配置：AI 对话流式期间存在 worker 线程并行连接（见 api/chat_sessions），
# 默认 5+10 在多路对话下会被打满、拖垮其他端点。
# 保持连接数上限；连接耗尽时尽快返回 503，避免默认 30 秒排队叠加慢 SQL。
engine = create_engine(_settings.database_url, pool_pre_ping=True, future=True,
                       pool_size=10, max_overflow=20,
                       pool_timeout=POOL_TIMEOUT_SECONDS, hide_parameters=True)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


def _set_request_statement_timeout(
    _session: Session, transaction: SessionTransaction, connection: Connection,
) -> None:
    # 请求内 commit/rollback 后的下一事务也必须有预算。用 SET LOCAL 防止设置
    # 泄漏到池中复用该连接的其他请求/后台会话；不在 SAVEPOINT 重设，以保留
    # 规则校准等服务主动设置的局部预算。after_begin 中须用 connection，非 Session。
    if not transaction.nested:
        connection.exec_driver_sql(
            f"SET LOCAL statement_timeout = '{REQUEST_STATEMENT_TIMEOUT_MS}ms'"
        )


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖：请求会话惰性连接，逐事务限时，失败先回滚再归还连接。"""
    db = SessionLocal()
    event.listen(db, "after_begin", _set_request_statement_timeout)
    try:
        yield db
    except DBAPIError as exc:
        # PostgreSQL statement_timeout 取消会让事务进入失败态。HTTP 错误必须
        # 在 rollback 之后抛出，连接回池前消除失败事务与事务级锁。
        db.rollback()
        sqlstate = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
        if sqlstate == "57014":
            raise HTTPException(
                status_code=504,
                detail="数据库查询超时，请缩小查询范围后重试",
            ) from exc
        raise
    except PoolTimeoutError as exc:
        db.rollback()
        raise HTTPException(
            status_code=503,
            detail="数据库连接繁忙，请稍后重试",
        ) from exc
    finally:
        db.close()
        event.remove(db, "after_begin", _set_request_statement_timeout)


def _set_auth_timeouts(
    session: Session, transaction: SessionTransaction, connection: Connection,
) -> None:
    _set_request_statement_timeout(session, transaction, connection)
    if not transaction.nested:
        connection.exec_driver_sql(
            f"SET LOCAL idle_in_transaction_session_timeout = '{AUTH_IDLE_TRANSACTION_TIMEOUT_MS}ms'"
        )


@contextmanager
def auth_db_session() -> Generator[Session, None, None]:
    """只包围身份快照读取，不跨越业务处理/外部等待；不提交或回滚业务 Session。"""
    db = SessionLocal()
    event.listen(db, "after_begin", _set_auth_timeouts)
    try:
        yield db
    except DBAPIError as exc:
        sqlstate = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
        if sqlstate == "25P03" or exc.connection_invalidated:
            # PostgreSQL 已终止此连接；即使驱动未识别断连也不得放回池里复用。
            db.invalidate()
        else:
            try:
                db.rollback()
            except DBAPIError:
                db.invalidate()
        if sqlstate == "57014":
            raise HTTPException(status_code=504, detail="数据库查询超时，请稍后重试") from exc
        raise HTTPException(status_code=503, detail="数据库暂时不可用，请稍后重试") from exc
    except PoolTimeoutError as exc:
        raise HTTPException(status_code=503, detail="数据库连接繁忙，请稍后重试") from exc
    finally:
        try:
            db.close()
        except DBAPIError as exc:
            db.invalidate()
            raise HTTPException(status_code=503, detail="数据库暂时不可用，请稍后重试") from exc
        finally:
            event.remove(db, "after_begin", _set_auth_timeouts)

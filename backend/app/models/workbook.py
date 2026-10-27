"""通用工作簿两步提交记录；不引用业务表。"""
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._types import TZDateTime


class WorkbookOperation(Base):
    __tablename__ = "workbook_operation"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    schema_name: Mapped[str] = mapped_column(String(64))
    subject_type: Mapped[str | None] = mapped_column(String(32))
    subject_id: Mapped[int | None] = mapped_column(BigInteger)
    export_id: Mapped[str] = mapped_column(String(128))
    file_sha256: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="previewed", server_default="previewed")
    plan: Mapped[dict] = mapped_column(JSONB)
    response: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    applied_at: Mapped[datetime | None] = mapped_column(TZDateTime)

    __table_args__ = (
        UniqueConstraint("export_id", "file_sha256", "actor", name="uq_workbook_operation_file_actor"),
        CheckConstraint("status IN ('previewed', 'applied', 'failed')", name="ck_workbook_operation_status"),
    )

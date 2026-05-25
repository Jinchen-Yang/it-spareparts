"""系统表：导入批次 / 导入错误 / 原始文件归档 / 审计日志（§5）。"""
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._types import TZDateTime


class SysImportBatch(Base):
    __tablename__ = "sys_import_batch"

    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(256))
    file_type: Mapped[str] = mapped_column(String(16))  # purchase/sales/inventory/inquiry
    file_hash: Mapped[str] = mapped_column(String(64))
    uploaded_by: Mapped[str | None] = mapped_column(String(64))
    uploaded_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    rows_total: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    rows_inserted: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    rows_skipped: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    rows_error: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    rows_inactive: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    status: Mapped[str] = mapped_column(
        String(16), default="processing", server_default="processing"
    )
    report_json: Mapped[dict | None] = mapped_column(JSONB)

    # 仅对 success 批次的 file_hash 唯一 → 失败可重试、重复成功文件被挡（§5/§6.1）
    __table_args__ = (
        Index(
            "ux_batch_success_hash",
            "file_hash",
            unique=True,
            postgresql_where=text("status = 'success'"),
        ),
    )


class SysImportError(Base):
    __tablename__ = "sys_import_error"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("sys_import_batch.id"))
    row_no: Mapped[int | None] = mapped_column(Integer)
    error_type: Mapped[str | None] = mapped_column(String(32))
    error_detail: Mapped[str | None] = mapped_column(Text)
    raw_row: Mapped[dict | None] = mapped_column(JSONB)


class SysRawFile(Base):
    __tablename__ = "sys_raw_file"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("sys_import_batch.id"))
    filename: Mapped[str | None] = mapped_column(String(256))     # 原始文件名（仅记录）
    file_hash: Mapped[str | None] = mapped_column(String(64))
    storage_path: Mapped[str | None] = mapped_column(Text)        # 实际存储：{hash}.xlsx
    uploaded_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())


class SysAuditLog(Base):
    """库存/替代料人工修改留痕（§5/§7.4）。"""

    __tablename__ = "sys_audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(64))   # inventory/substitute
    entity_id: Mapped[int] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(32))        # update/create/delete
    before_json: Mapped[dict | None] = mapped_column(JSONB)
    after_json: Mapped[dict | None] = mapped_column(JSONB)
    reason: Mapped[str | None] = mapped_column(Text)
    operated_by: Mapped[str | None] = mapped_column(String(64))
    operated_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())

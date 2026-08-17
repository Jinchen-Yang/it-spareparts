"""WBDD 专用上传回执（plan v1.3 §4.1）。

一次成功的 /api/maintenance/wbdd-imports 上传对应一条回执：
- (uploaded_by, idempotency_key) 唯一 → 同键重放直接返回原报告，不重复写；
- report_json 保存完整对账报告（布局/计数/快照差异/成本重算统计），
  与 sys_import_batch.report_json（通用导入报告）互补；
- 事实写入仍走通用 f_maintenance_order/f_maintenance_line（sys_import_batch 审计链），
  本表只做维保入口的幂等与报告存档，不承载业务事实。
"""
from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._types import TZDateTime


class MaintenanceWbddImportReceipt(Base):
    __tablename__ = "maintenance_wbdd_import_receipt"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("sys_import_batch.id"), unique=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(128))
    uploaded_by: Mapped[str] = mapped_column(String(64))
    file_hash: Mapped[str] = mapped_column(String(64))
    layout: Mapped[str | None] = mapped_column(String(4))  # "91" / "90"
    report_json: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "uploaded_by", "idempotency_key",
            name="uq_maintenance_wbdd_import_idempotency",
        ),
        Index("ix_maintenance_wbdd_receipt_hash", "file_hash"),
    )

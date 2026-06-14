"""系统表：导入批次 / 导入错误 / 原始文件归档 / 审计日志 / 用户（§5）。"""
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
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


class SysUser(Base):
    """系统用户（三期 RBAC 身份层）。role 决定可见范围；salesperson_name 对齐
    f_sales_order.salesperson 用于"只看自己客户"的行级过滤；ding_user_id 预留钉钉 SSO。"""

    __tablename__ = "sys_user"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(256))   # pbkdf2$iter$salt$hash
    role: Mapped[str] = mapped_column(String(16))             # admin/boss/sales/purchaser/readonly
    display_name: Mapped[str | None] = mapped_column(String(64))
    salesperson_name: Mapped[str | None] = mapped_column(String(64))  # 对齐销售数据，行级过滤用
    ding_user_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    # 按用户细粒度权限（账号管理页勾选）；为空 → 回退 role 模板。见 app/permissions.py
    permissions: Mapped[dict | None] = mapped_column(JSONB)
    last_login_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    # 登录暴力破解防护：连续失败计数 + 锁定到期时间（达阈值锁定一段时间）
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    locked_until: Mapped[datetime | None] = mapped_column(TZDateTime)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())


class SysImportJob(Base):
    """批量导入作业（一次「批量上传」聚合的 N 个 batch，§9）。

    后台 worker 逐文件跑 run_import，每文件一条 batch（job_id 关联）；前端轮询本表看进度。
    同时承载导入人与时间，是审计/归组单元。
    """

    __tablename__ = "sys_import_job"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    # processing=进行中 / done=全成 / partial=部分成 / failed=全失败
    status: Mapped[str] = mapped_column(String(16), default="processing", server_default="processing")
    mode: Mapped[str] = mapped_column(String(16), default="skip", server_default="skip")
    total_files: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    done_files: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error_files: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    note: Mapped[str | None] = mapped_column(Text)


class SysImportBatch(Base):
    __tablename__ = "sys_import_batch"

    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(256))
    file_type: Mapped[str] = mapped_column(String(16))  # purchase/sales/inventory/inquiry
    file_hash: Mapped[str] = mapped_column(String(64))
    uploaded_by: Mapped[str | None] = mapped_column(String(64))
    import_job_id: Mapped[int | None] = mapped_column(ForeignKey("sys_import_job.id"))  # 批量作业归组
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


class SysAccessLog(Base):
    """访问活动日志（谁、何时、查了什么）——账号管理页看子账号活动用。"""

    __tablename__ = "sys_access_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64))
    role: Mapped[str | None] = mapped_column(String(16))
    action: Mapped[str] = mapped_column(String(32))        # search/overview/profit/export/chat/import…
    resource: Mapped[str | None] = mapped_column(Text)     # 查的型号/客户/维度
    detail: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())

    __table_args__ = (Index("ix_access_user_time", "username", "created_at"),)

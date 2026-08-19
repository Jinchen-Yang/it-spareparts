"""系统表：导入批次 / 导入错误 / 原始文件归档 / 审计日志 / 用户（§5）。"""
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
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
    # 【旧列，权限中心 v2 起只做回滚保险】v1 语义：自定义权限图，为空回退 role 模板。
    # v2 写路径每次保存都把**完整有效权限图**双写进来——旧代码 effective(role, 完整图)
    # 逐键等于该图，因此 downgrade 掉 v2 后有效权限零漂移。见 docs/权限中心v2 设计 §1.6
    permissions: Mapped[dict | None] = mapped_column(JSONB)
    # 权限中心 v2：模板快照三件套。有效权限 = template_perms（套用模板时的快照）⊕
    # perm_overrides（与快照不同的键，稀疏 diff）。模板后续被编辑**不**影响这里的快照，
    # 直到管理员显式「保存并同步账号」。template_version 记录套用时模板版本，用于
    # 显示「模板已更新到 vN，此账号仍在 vM」。见 app/permissions.py:effective_for_user
    template_code: Mapped[str | None] = mapped_column(String(64))
    template_version: Mapped[int | None] = mapped_column(Integer)
    template_perms: Mapped[dict | None] = mapped_column(JSONB)
    perm_overrides: Mapped[dict | None] = mapped_column(JSONB)
    last_login_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    # 登录暴力破解防护：连续失败计数 + 锁定到期时间（达阈值锁定一段时间）
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    locked_until: Mapped[datetime | None] = mapped_column(TZDateTime)
    # token 版本：改密/停用/改权限时递增 → 旧 token 的 tv 不匹配即失效（即时吊销）
    token_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # 软删除：删除账号置 is_active=false + deleted_at=now；历史外键引用保留
    deleted_at: Mapped[datetime | None] = mapped_column(TZDateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())


class SysRoleTemplate(Base):
    """职位模板（权限中心 v2）：可编辑的权限预设，持久化替代 permissions.ROLE_TEMPLATES 硬编码。

    语义=复制快照：套用模板把 permissions 快照进 sys_user.template_perms；之后编辑模板
    不影响已套用账号，除非显式「保存并同步账号」。内置 5 条（code=角色名）is_system=True
    不可删；其中 admin 模板锁定（不可编辑/停用/套用——升管理员走单账号改 role）。
    version 是乐观锁：PUT 必须带当前 version，不匹配 409（防两管理员互相覆盖）。"""

    __tablename__ = "sys_role_template"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str | None] = mapped_column(Text)
    # 套用该模板的账号获得的基础角色 ∈ boss/sales/purchaser/readonly（内置 admin 模板=admin）。
    # role 挂着行级语义/文件 ACL/replace require_roles 等硬编码点，跟模板走避免"勾了权限仍 403"
    base_role: Mapped[str] = mapped_column(String(16))
    permissions: Mapped[dict] = mapped_column(JSONB)   # 完整键→bool 图（保存时 normalize 补全）
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    updated_by: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime | None] = mapped_column(TZDateTime)


class SysBusinessSetting(Base):
    """类型化业务设置单例。

    不使用自由 key/value：每个设置都必须在模型和迁移中声明类型、合法值、默认值与
    回滚行为。id 固定为 1；version 供管理员界面的乐观锁使用。
    """

    __tablename__ = "sys_business_setting"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_sys_business_setting_singleton"),
        CheckConstraint(
            "maintenance_project_profit_default_basis IN ('inc', 'ex', 'both')",
            name="ck_sys_business_setting_maintenance_profit_basis",
        ),
        CheckConstraint(
            "purchase_display_basis IN ('inc', 'ex', 'both')",
            name="ck_sys_business_setting_purchase_display_basis",
        ),
        CheckConstraint(
            "sales_display_basis IN ('inc', 'ex', 'both')",
            name="ck_sys_business_setting_sales_display_basis",
        ),
        CheckConstraint("version >= 1", name="ck_sys_business_setting_version"),
    )

    id: Mapped[int] = mapped_column(
        SmallInteger,
        primary_key=True,
        default=1,
        server_default="1",
    )
    maintenance_project_profit_default_basis: Mapped[str] = mapped_column(
        String(8),
        default="both",
        server_default="both",
    )
    purchase_display_basis: Mapped[str] = mapped_column(
        String(8),
        default="both",
        server_default="both",
    )
    sales_display_basis: Mapped[str] = mapped_column(
        String(8),
        default="ex",
        server_default="ex",
    )
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    updated_by: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime | None] = mapped_column(TZDateTime)


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

    # 仅对同一 file_type 的 success file_hash 唯一。固定维保回填与通用导入是
    # 不同协议命名空间，错入口的历史记录不能污染正确入口的幂等判断。
    __table_args__ = (
        Index(
            "ux_batch_success_hash",
            "file_type",
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

    __table_args__ = (Index("ix_import_error_batch_id_id", "batch_id", "id"),)


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
    action: Mapped[str] = mapped_column(String(32))        # search/overview/.../login_success/login_failed…
    resource: Mapped[str | None] = mapped_column(Text)     # 查的型号/客户/维度
    detail: Mapped[dict | None] = mapped_column(JSONB)
    ip_address: Mapped[str | None] = mapped_column(String(64))     # 登录源(暴力破解排查);X-Forwarded-For 优先
    user_agent: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())

    __table_args__ = (Index("ix_access_user_time", "username", "created_at"),)


class SysDshScript(Base):
    """DSH agent 白名单脚本（企业定制 P4）。

    由管理员在权限面板维护；agent 通过 /api/agent/scripts/{name}/run 触发，
    服务端子进程执行（独立于业务 ORM），执行前按 required_action 做动作级准入，
    每次执行写 sys_access_log 审计。写库语义由脚本内容与专用 PG 角色约束。
    """

    __tablename__ = "sys_dsh_script"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    description: Mapped[str | None] = mapped_column(String(256))
    content: Mapped[str] = mapped_column(Text)             # Python 源码
    required_action: Mapped[str | None] = mapped_column(String(64))  # ACTION_KEYS 之一；空=仅需 page_chat
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=60, server_default="60")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    created_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())

"""PN 主数据对外写接口（shop 集成）：dim_part 加厂商/简介、请求幂等回执表。

设计口径（.task/方案.md 第 4 节）：
- manufacturer/summary 是采购可人工维护的基础资料，与 description/brand 同级；
- integration_request_log 以 request_id 唯一锚定幂等回执，request_fingerprint 绑定
  method+path+规范化请求体，response_json 原样保存首次响应；
- part_alias.source / dim_part.master_source 在当前 schema 没有 CHECK 约束（已核对
  pg_constraint），故无需放宽，只登记 shop 值；
- sys_audit_log.operated_by 原为 varchar(64)，而对外审计口径是 ``shop:<用户名>``
  且用户名允许 64 字符，64+5 放不下。这里同步扩到 96，否则合法用户名会在审计写入时
  被 PostgreSQL 拒绝；降级若遇到超长历史行会明确拒绝而不是静默截断。

Revision ID: b2f4a6c8d1e3
Revises: d8f6a2c4b9e7
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b2f4a6c8d1e3"
down_revision: str | None = "d8f6a2c4b9e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("dim_part", sa.Column("manufacturer", sa.String(128), nullable=True))
    op.add_column("dim_part", sa.Column("summary", sa.Text(), nullable=True))

    op.create_table(
        "integration_request_log",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("actor_system", sa.String(32), nullable=False),
        sa.Column("actor_user", sa.String(64), nullable=False),
        sa.Column("method", sa.String(8), nullable=False),
        sa.Column("path", sa.String(255), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("response_json", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.UniqueConstraint(
            "request_id", name="uq_integration_request_log_request_id"
        ),
    )

    op.alter_column(
        "sys_audit_log", "operated_by",
        existing_type=sa.String(64), type_=sa.String(96),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # 不静默截断操作人：存在 >64 字符记录时先让运维处理，再降级。
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM sys_audit_log WHERE length(operated_by) > 64
            ) THEN
                RAISE EXCEPTION
                    'downgrade blocked: sys_audit_log.operated_by has rows longer than 64';
            END IF;
        END
        $$;
        """
    )
    op.alter_column(
        "sys_audit_log", "operated_by",
        existing_type=sa.String(96), type_=sa.String(64),
        existing_nullable=True,
    )

    op.drop_table("integration_request_log")
    op.drop_column("dim_part", "summary")
    op.drop_column("dim_part", "manufacturer")

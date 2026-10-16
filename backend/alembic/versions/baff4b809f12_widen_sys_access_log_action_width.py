"""sys_access_log.action 扩宽 varchar(32)→varchar(64)：审计动作名超长被拒。

全仓 156 个 action 名最长 43 字符（maintenance_warehouse_integration_reconcile，
app/api/maintenance_warehouse.py），29 个超 32 → INSERT 报 value too long、
审计行静默丢失（best-effort 写入只留 warning）。64 = 最长 43 + 余量，覆盖
全部动态名（agent_tool:<注册键> 最长 35）。

Revision ID: baff4b809f12
Revises: a40c9e1d7b52
"""

from alembic import op
import sqlalchemy as sa

revision = "baff4b809f12"
down_revision = "a40c9e1d7b52"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # varchar 加宽在 PG 侧是元数据变更（不重写表、瞬间完成），仍按仓库惯例
    # 带上 lock_timeout，避免极端情况下等锁堵住迁移事务。
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.alter_column(
        "sys_access_log",
        "action",
        existing_type=sa.String(32),
        type_=sa.String(64),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # 检查与收窄同事务持锁，阻止检查后的并发写入造成隐式失败。
    op.execute("LOCK TABLE sys_access_log IN ACCESS EXCLUSIVE MODE")
    # 不静默截断：存在超 32 字符的行时明确拒绝，由运维决定导出/清理后再降。
    op.execute("""
        DO $migration$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM sys_access_log WHERE char_length(action) > 32
            ) THEN
                RAISE EXCEPTION
                    'downgrade blocked: % rows in sys_access_log have action longer than 32 chars; export or clean them before narrowing',
                    (SELECT count(*) FROM sys_access_log WHERE char_length(action) > 32);
            END IF;
        END
        $migration$;
    """)
    op.alter_column(
        "sys_access_log",
        "action",
        existing_type=sa.String(64),
        type_=sa.String(32),
        existing_nullable=False,
    )

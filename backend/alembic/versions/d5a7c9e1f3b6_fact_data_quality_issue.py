"""add fact data quality issue foundation

Revision ID: d5a7c9e1f3b6
Revises: a9c5e2f7d4b1
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "d5a7c9e1f3b6"
down_revision: str | None = "a9c5e2f7d4b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACTION = "action_data_quality_review"


def upgrade() -> None:
    op.create_table(
        "fact_data_quality_issue",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("side", sa.String(16), nullable=False),
        sa.Column("line_id", sa.Integer(), nullable=False),
        sa.Column("part_id", sa.Integer(), sa.ForeignKey("dim_part.id"), nullable=False),
        sa.Column("import_batch_id", sa.Integer(), sa.ForeignKey("sys_import_batch.id")),
        sa.Column("rule_code", sa.String(64), nullable=False),
        sa.Column("rule_version", sa.String(64), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("source_fingerprint", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="open"),
        sa.Column("detected_by", sa.String(64), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("reviewed_by", sa.String(64)),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("review_note", sa.Text()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("side", "line_id", "rule_code", name="uq_fact_dq_issue_current"),
        sa.CheckConstraint("side IN ('purchase','sales')", name="ck_fact_dq_issue_side"),
        sa.CheckConstraint(
            "status IN ('open','confirmed_valid','confirmed_source_error','source_changed')",
            name="ck_fact_dq_issue_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_fact_dq_issue_version"),
    )
    op.create_index("ix_fact_dq_issue_status_updated", "fact_data_quality_issue",
                    ["status", "updated_at"])
    op.create_index("ix_fact_dq_issue_part_status", "fact_data_quality_issue",
                    ["part_id", "status"])
    op.create_index("ix_fact_dq_issue_batch", "fact_data_quality_issue", ["import_batch_id"])

    # 持久化职位模板补新动作键：系统没有稳定的“数据维护”内置角色，故只给 admin，
    # 其余内置/自定义模板均失败关闭。已有账号快照缺键也按 False 解释。
    op.execute(sa.text(
        "UPDATE sys_role_template SET permissions = COALESCE(permissions, '{}'::jsonb) || "
        "jsonb_build_object(:key, code = 'admin')"
    ).bindparams(key=_ACTION))


def downgrade() -> None:
    # 一旦产生人工结论，不允许用 schema downgrade 删除问题历史；生产回滚应回退应用。
    op.execute(sa.text("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM fact_data_quality_issue LIMIT 1) THEN
            RAISE EXCEPTION 'fact_data_quality_issue is not empty; downgrade would lose audit history';
          END IF;
        END $$;
    """))
    op.execute(sa.text(
        "UPDATE sys_role_template SET permissions = permissions - :key"
    ).bindparams(key=_ACTION))
    op.drop_index("ix_fact_dq_issue_batch", table_name="fact_data_quality_issue")
    op.drop_index("ix_fact_dq_issue_part_status", table_name="fact_data_quality_issue")
    op.drop_index("ix_fact_dq_issue_status_updated", table_name="fact_data_quality_issue")
    op.drop_table("fact_data_quality_issue")

"""新增通用字段池和受众可见性结构。

Revision ID: e7a9c1d3f5b7
Revises: d6f8a0b2c4e6
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e7a9c1d3f5b7"
down_revision: str | None = "d6f8a0b2c4e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audience_group",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("owner_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("archived", sa.Boolean(), nullable=False, server_default="false"),
        sa.CheckConstraint("scope IN ('personal', 'public')", name="ck_audience_group_scope"),
    )
    op.create_index("uq_audience_group_active_name", "audience_group", ["scope", "owner_id", "name"],
                    unique=True, postgresql_where=sa.text("NOT archived"))
    op.create_table(
        "audience_group_member",
        sa.Column("group_id", sa.Integer(), sa.ForeignKey("audience_group.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_id", sa.String(64), primary_key=True),
    )
    op.create_table(
        "audience_rule",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("owner_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("mode IN ('public', 'private', 'include', 'exclude')", name="ck_audience_rule_mode"),
    )
    op.create_table(
        "audience_rule_member",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("rule_id", sa.Integer(), sa.ForeignKey("audience_rule.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=True),
        sa.Column("group_id", sa.Integer(), sa.ForeignKey("audience_group.id"), nullable=True),
        sa.CheckConstraint("(user_id IS NOT NULL) <> (group_id IS NOT NULL)", name="ck_audience_rule_member_target"),
    )
    op.create_index("uq_audience_rule_member_user", "audience_rule_member", ["rule_id", "user_id"],
                    unique=True, postgresql_where=sa.text("user_id IS NOT NULL"))
    op.create_index("uq_audience_rule_member_group", "audience_rule_member", ["rule_id", "group_id"],
                    unique=True, postgresql_where=sa.text("group_id IS NOT NULL"))
    op.create_table(
        "field_def",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner_scope", sa.String(16), nullable=False),
        sa.Column("owner_id", sa.String(64), nullable=True),
        sa.Column("subject_type", sa.String(64), nullable=False),
        sa.Column("level", sa.String(16), nullable=False),
        sa.Column("name", sa.String(32), nullable=False),
        sa.Column("value_type", sa.String(16), nullable=False),
        sa.Column("choices", postgresql.JSONB(), nullable=True),
        sa.Column("default_rule_id", sa.Integer(), sa.ForeignKey("audience_rule.id"), nullable=True),
        sa.Column("auto_include", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("sort", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("archived", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("owner_scope IN ('personal', 'team')", name="ck_field_def_owner_scope"),
        sa.CheckConstraint("(owner_scope = 'personal' AND owner_id IS NOT NULL) OR "
                           "(owner_scope = 'team' AND owner_id IS NULL)", name="ck_field_def_owner"),
        sa.CheckConstraint("level IN ('header', 'line')", name="ck_field_def_level"),
        sa.CheckConstraint("value_type IN ('text', 'number', 'date', 'bool', 'choice')", name="ck_field_def_value_type"),
    )
    # 团队池的 owner_id 为空；NULLS NOT DISTINCT 保证同名团队字段仍然唯一。
    op.create_index("uq_field_def_active_name", "field_def", ["owner_scope", "owner_id", "subject_type", "level", "name"],
                    unique=True, postgresql_where=sa.text("NOT archived"), postgresql_nulls_not_distinct=True)
    op.create_table(
        "field_value",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subject_type", sa.String(64), nullable=False),
        sa.Column("subject_id", sa.String(128), nullable=False),
        sa.Column("field_def_id", sa.Integer(), sa.ForeignKey("field_def.id"), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("rule_id", sa.Integer(), sa.ForeignKey("audience_rule.id"), nullable=True),
        sa.Column("updated_by", sa.String(64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("subject_type", "subject_id", "field_def_id", name="uq_field_value_subject"),
    )
    op.create_table(
        "field_anchor_usage",
        sa.Column("field_def_id", sa.Integer(), sa.ForeignKey("field_def.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("anchor", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), primary_key=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("uses", sa.Integer(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    op.drop_table("field_anchor_usage")
    op.drop_table("field_value")
    op.drop_table("field_def")
    op.drop_table("audience_rule_member")
    op.drop_table("audience_rule")
    op.drop_table("audience_group_member")
    op.drop_table("audience_group")

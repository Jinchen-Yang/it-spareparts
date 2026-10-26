"""字段池和受众规则：只保存通用身份，不依赖任何业务表。"""
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._types import TZDateTime


class AudienceGroup(Base):
    __tablename__ = "audience_group"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    scope: Mapped[str] = mapped_column(String(16))
    owner_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    archived: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    __table_args__ = (
        CheckConstraint("scope IN ('personal', 'public')", name="ck_audience_group_scope"),
        Index("uq_audience_group_active_name", "scope", "owner_id", "name", unique=True,
              postgresql_where=text("NOT archived")),
    )


class AudienceGroupMember(Base):
    __tablename__ = "audience_group_member"

    group_id: Mapped[int] = mapped_column(ForeignKey("audience_group.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)


class AudienceRule(Base):
    __tablename__ = "audience_rule"

    id: Mapped[int] = mapped_column(primary_key=True)
    mode: Mapped[str] = mapped_column(String(16))
    owner_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())

    __table_args__ = (
        CheckConstraint("mode IN ('public', 'private', 'include', 'exclude')", name="ck_audience_rule_mode"),
    )


class AudienceRuleMember(Base):
    __tablename__ = "audience_rule_member"

    id: Mapped[int] = mapped_column(primary_key=True)
    rule_id: Mapped[int] = mapped_column(ForeignKey("audience_rule.id", ondelete="CASCADE"))
    user_id: Mapped[str | None] = mapped_column(String(64))
    group_id: Mapped[int | None] = mapped_column(ForeignKey("audience_group.id"))

    __table_args__ = (
        CheckConstraint("(user_id IS NOT NULL) <> (group_id IS NOT NULL)", name="ck_audience_rule_member_target"),
        Index("uq_audience_rule_member_user", "rule_id", "user_id", unique=True,
              postgresql_where=text("user_id IS NOT NULL")),
        Index("uq_audience_rule_member_group", "rule_id", "group_id", unique=True,
              postgresql_where=text("group_id IS NOT NULL")),
    )


class FieldDef(Base):
    __tablename__ = "field_def"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_scope: Mapped[str] = mapped_column(String(16))
    owner_id: Mapped[str | None] = mapped_column(String(64))
    subject_type: Mapped[str] = mapped_column(String(64))
    level: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(32))
    value_type: Mapped[str] = mapped_column(String(16))
    choices: Mapped[list[str] | None] = mapped_column(JSONB)
    default_rule_id: Mapped[int | None] = mapped_column(ForeignKey("audience_rule.id"))
    auto_include: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    sort: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    archived: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())

    __table_args__ = (
        CheckConstraint("owner_scope IN ('personal', 'team')", name="ck_field_def_owner_scope"),
        CheckConstraint("(owner_scope = 'personal' AND owner_id IS NOT NULL) OR "
                        "(owner_scope = 'team' AND owner_id IS NULL)", name="ck_field_def_owner"),
        CheckConstraint("level IN ('header', 'line')", name="ck_field_def_level"),
        CheckConstraint("value_type IN ('text', 'number', 'date', 'bool', 'choice')", name="ck_field_def_value_type"),
        Index("uq_field_def_active_name", "owner_scope", "owner_id", "subject_type", "level", "name",
              unique=True, postgresql_where=text("NOT archived"), postgresql_nulls_not_distinct=True),
    )


class FieldValue(Base):
    __tablename__ = "field_value"

    id: Mapped[int] = mapped_column(primary_key=True)
    subject_type: Mapped[str] = mapped_column(String(64))
    subject_id: Mapped[str] = mapped_column(String(128))
    field_def_id: Mapped[int] = mapped_column(ForeignKey("field_def.id"))
    value: Mapped[str] = mapped_column(Text)
    rule_id: Mapped[int | None] = mapped_column(ForeignKey("audience_rule.id"))
    updated_by: Mapped[str] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("subject_type", "subject_id", "field_def_id", name="uq_field_value_subject"),
    )


class FieldAnchorUsage(Base):
    __tablename__ = "field_anchor_usage"

    field_def_id: Mapped[int] = mapped_column(ForeignKey("field_def.id", ondelete="CASCADE"), primary_key=True)
    anchor: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_used_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    uses: Mapped[int] = mapped_column(Integer, default=1, server_default="1")

"""Independent PN JSON attributes: no inferred units or coercion to standard specs."""
from sqlalchemy import BigInteger, Boolean, CheckConstraint, ForeignKey, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AttributeState(Base):
    __tablename__ = "pn_catalog_attribute_state"
    part_id: Mapped[int] = mapped_column(ForeignKey("dim_part.id"), primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    __table_args__ = (CheckConstraint("revision >= 0", name="ck_pn_attribute_revision"),)


class AttributeValue(Base):
    __tablename__ = "pn_catalog_attribute"
    part_id: Mapped[int] = mapped_column(ForeignKey("pn_catalog_attribute_state.part_id"), primary_key=True)
    key: Mapped[str] = mapped_column(String(256), primary_key=True)
    value: Mapped[object] = mapped_column(JSONB(none_as_null=False), nullable=False)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    version: Mapped[int] = mapped_column(BigInteger, default=1, server_default="1")
    protected: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    provenance: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    __table_args__ = (CheckConstraint("version >= 1", name="ck_pn_attribute_version"),)

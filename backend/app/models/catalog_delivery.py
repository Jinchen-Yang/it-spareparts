"""Technical delivery state only; no business facts or credentials."""
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._types import TZDateTime


class CatalogDelivery(Base):
    __tablename__ = "catalog_delivery"

    delivery_id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    part_id: Mapped[int] = mapped_column(ForeignKey("dim_part.id"), unique=True)
    state: Mapped[str] = mapped_column(String(16), server_default="pending", default="pending")
    attempts: Mapped[int] = mapped_column(server_default="0", default=0)
    available_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    lease_until: Mapped[datetime | None] = mapped_column(TZDateTime)
    lease_token: Mapped[UUID | None]
    last_error_code: Mapped[str | None] = mapped_column(String(40))
    receipt_status: Mapped[str | None] = mapped_column(String(16))
    completed_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())

    __table_args__ = (
        CheckConstraint("state IN ('pending','delivering','succeeded','conflict')",
                        name="ck_catalog_delivery_state"),
        CheckConstraint("attempts >= 0", name="ck_catalog_delivery_attempts"),
        CheckConstraint("(state = 'delivering') = (lease_token IS NOT NULL AND lease_until IS NOT NULL)",
                        name="ck_catalog_delivery_lease"),
        CheckConstraint("receipt_status IS NULL OR receipt_status IN ('applied','unchanged','conflict')",
                        name="ck_catalog_delivery_receipt"),
        Index("ix_catalog_delivery_pending", "available_at", "created_at",
              postgresql_where=text("state = 'pending'")),
        Index("ix_catalog_delivery_lease", "lease_until",
              postgresql_where=text("state = 'delivering'")),
    )

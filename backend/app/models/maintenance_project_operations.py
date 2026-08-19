"""Stable-project operating facts independent from the legacy WBDD read model."""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    LargeBinary,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._types import Money, Qty, Rate, TZDateTime


class MaintenanceCollectionSnapshot(Base):
    """One contract's cumulative collection value for one reporting month."""

    __tablename__ = "maintenance_collection_snapshot"

    collection_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_project.project_id"), nullable=False
    )
    project_contract_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_project_contract.project_contract_id"), nullable=False
    )
    report_month: Mapped[date] = mapped_column(Date, nullable=False)
    cumulative_amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    receipt_reference: Mapped[str | None] = mapped_column(String(128))
    remark: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(
        String(24), nullable=False, default="legacy", server_default="legacy"
    )
    import_batch_id: Mapped[str | None] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('confirmed', 'unconfirmed', 'void')",
            name="ck_maintenance_collection_status",
        ),
        CheckConstraint(
            "source IN ('legacy', 'direct_api', 'workbook')",
            name="ck_maintenance_collection_source",
        ),
        CheckConstraint(
            "(source = 'workbook' AND import_batch_id IS NOT NULL) OR "
            "(source IN ('legacy', 'direct_api') AND import_batch_id IS NULL)",
            name="ck_maintenance_collection_import_batch",
        ),
        CheckConstraint(
            "cumulative_amount >= 0 AND cumulative_amount < 1000000000000",
            name="ck_maintenance_collection_amount",
        ),
        CheckConstraint("version >= 1", name="ck_maintenance_collection_version"),
        CheckConstraint(
            "report_month = date_trunc('month', report_month)::date",
            name="ck_maintenance_collection_month_start",
        ),
        UniqueConstraint(
            "project_contract_id",
            "report_month",
            name="uq_maintenance_collection_contract_month",
        ),
        Index(
            "ix_maintenance_collection_project_month",
            "project_id",
            "report_month",
        ),
    )


class MaintenanceProjectOperationAudit(Base):
    """Audit log for project-scoped operating facts with string identifiers."""

    __tablename__ = "maintenance_project_operation_audit"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_project.project_id"), nullable=False
    )
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    before_json: Mapped[dict | None] = mapped_column(JSONB)
    after_json: Mapped[dict | None] = mapped_column(JSONB)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    operated_by: Mapped[str] = mapped_column(String(64), nullable=False)
    operated_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "char_length(btrim(reason)) > 0",
            name="ck_maintenance_project_operation_audit_reason",
        ),
        CheckConstraint(
            "char_length(btrim(operated_by)) > 0",
            name="ck_maintenance_project_operation_audit_operator",
        ),
        Index(
            "ix_maintenance_project_operation_audit_project_time",
            "project_id",
            "operated_at",
            "id",
        ),
    )


class MaintenanceSiteIssueDeliverySource(Base):
    """Stable delivery-line contract supplied by a warehouse adapter.

    The first implementation deliberately accepts only explicit synthetic rows.
    An empty table means the real WBDD/warehouse adapter is unavailable; no project
    or part identity is inferred from names.
    """

    __tablename__ = "maintenance_site_issue_delivery_source"

    delivery_line_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    adapter_key: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_project.project_id"), nullable=False
    )
    source_order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_line_id: Mapped[str] = mapped_column(String(128), nullable=False)
    delivery_no: Mapped[str] = mapped_column(String(128), nullable=False)
    delivery_date: Mapped[date] = mapped_column(Date, nullable=False)
    part_id: Mapped[int] = mapped_column(ForeignKey("dim_part.id"), nullable=False)
    pn: Mapped[str] = mapped_column(String(128), nullable=False)
    serial_number: Mapped[str | None] = mapped_column(Text)
    delivered_quantity: Mapped[Decimal] = mapped_column(Qty, nullable=False)
    linked_purchase_line_id: Mapped[int | None] = mapped_column(
        ForeignKey("f_purchase_line.id")
    )
    mapping_state: Mapped[str] = mapped_column(String(16), nullable=False)
    mapping_version: Mapped[str] = mapped_column(String(64), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "adapter_key IN ('synthetic_delivery_v1', 'warehouse_shipment_v1')",
            name="ck_maintenance_site_issue_delivery_adapter",
        ),
        CheckConstraint(
            "mapping_state IN ('ready', 'unavailable')",
            name="ck_maintenance_site_issue_delivery_mapping_state",
        ),
        CheckConstraint(
            "delivered_quantity > 0 AND delivered_quantity < 1000000000000",
            name="ck_maintenance_site_issue_delivery_quantity",
        ),
        UniqueConstraint(
            "adapter_key",
            "source_order_id",
            "source_line_id",
            name="uq_maintenance_site_issue_delivery_source_identity",
        ),
        Index(
            "ix_maintenance_site_issue_delivery_project_date",
            "project_id",
            "delivery_date",
            "delivery_line_id",
        ),
    )


class MaintenanceSiteIssue(Base):
    """Canonical, status-mapped site issue generated by the monthly workbook."""

    __tablename__ = "maintenance_site_issue"

    issue_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_project.project_id"), nullable=False
    )
    issue_no: Mapped[str] = mapped_column(String(64), nullable=False)
    issue_date: Mapped[date] = mapped_column(Date, nullable=False)
    raw_status: Mapped[str] = mapped_column(String(64), nullable=False)
    status_mapping_state: Mapped[str] = mapped_column(String(16), nullable=False)
    normalized_status: Mapped[str] = mapped_column(String(16), nullable=False)
    status_mapping_version: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(
        String(24), nullable=False, default="legacy", server_default="legacy"
    )
    import_batch_id: Mapped[str | None] = mapped_column(String(64))
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    request_fingerprint: Mapped[str | None] = mapped_column(String(64))
    receiver: Mapped[str | None] = mapped_column(String(128))
    issued_by: Mapped[str | None] = mapped_column(String(128))
    site_location: Mapped[str | None] = mapped_column(String(256))
    created_by: Mapped[str | None] = mapped_column(String(64))
    confirmed_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    corrected_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    voided_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status_mapping_state IN ('mapped', 'unmapped')",
            name="ck_maintenance_site_issue_mapping_state",
        ),
        CheckConstraint(
            "normalized_status IN ('draft', 'confirmed', 'corrected', 'void', 'unknown')",
            name="ck_maintenance_site_issue_normalized_status",
        ),
        CheckConstraint(
            "source IN ('legacy', 'direct_api', 'workbook', 'site_issue_v2')",
            name="ck_maintenance_site_issue_source",
        ),
        CheckConstraint(
            "(source = 'workbook' AND import_batch_id IS NOT NULL) OR "
            "(source IN ('legacy', 'direct_api', 'site_issue_v2') AND import_batch_id IS NULL)",
            name="ck_maintenance_site_issue_import_batch",
        ),
        CheckConstraint(
            "(status_mapping_state = 'mapped') = (normalized_status <> 'unknown')",
            name="ck_maintenance_site_issue_unmapped_unknown",
        ),
        CheckConstraint("version >= 1", name="ck_maintenance_site_issue_version"),
        UniqueConstraint("project_id", "issue_no", name="uq_maintenance_site_issue_project_no"),
        Index(
            "uq_maintenance_site_issue_v2_no",
            "issue_no",
            unique=True,
            postgresql_where=text("source = 'site_issue_v2'"),
        ),
        Index(
            "uq_maintenance_site_issue_idempotency",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        Index("ix_maintenance_site_issue_project_date", "project_id", "issue_date"),
    )


class MaintenanceSiteIssueLine(Base):
    """One actual site-consumption line and its reproducible pricing evidence."""

    __tablename__ = "maintenance_site_issue_line"

    issue_line_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    issue_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_site_issue.issue_id"), nullable=False
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    part_id: Mapped[int] = mapped_column(ForeignKey("dim_part.id"), nullable=False)
    pn: Mapped[str] = mapped_column(String(128), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Qty, nullable=False)
    delivery_line_id: Mapped[str | None] = mapped_column(
        ForeignKey("maintenance_site_issue_delivery_source.delivery_line_id")
    )
    source_order_id: Mapped[str | None] = mapped_column(String(64))
    source_line_id: Mapped[str | None] = mapped_column(String(128))
    serial_number: Mapped[str | None] = mapped_column(Text)
    # 用户回填的现场说明；领用/返还事实仍来自源单。
    remark: Mapped[str | None] = mapped_column(Text)
    # 行级不返还覆盖：True=不返还 / False=必须返还 / None=继承项目默认
    no_return: Mapped[bool | None] = mapped_column(Boolean)
    linked_purchase_line_id: Mapped[int | None] = mapped_column(
        ForeignKey("f_purchase_line.id")
    )
    # Legacy ``manual_unit_cost`` remains the ex-tax input/alias.  Its
    # inc-tax counterpart and every resolved amount are server-owned facts.
    manual_unit_cost: Mapped[Decimal | None] = mapped_column(Money)
    manual_unit_cost_inc_tax: Mapped[Decimal | None] = mapped_column(Money)
    manual_evidence: Mapped[str | None] = mapped_column(Text)
    unit_cost: Mapped[Decimal | None] = mapped_column(Money)
    cost_amount: Mapped[Decimal | None] = mapped_column(Money)
    unit_cost_ex_tax: Mapped[Decimal | None] = mapped_column(Money)
    unit_cost_inc_tax: Mapped[Decimal | None] = mapped_column(Money)
    cost_amount_ex_tax: Mapped[Decimal | None] = mapped_column(Money)
    cost_amount_inc_tax: Mapped[Decimal | None] = mapped_column(Money)
    tax_rate_used: Mapped[Decimal] = mapped_column(
        Rate,
        nullable=False,
        default=Decimal("0.13"),
        server_default="0.13",
    )
    cost_source: Mapped[str | None] = mapped_column(String(24))
    price_basis: Mapped[str] = mapped_column(
        String(16), nullable=False, default="ex_tax", server_default="ex_tax"
    )
    reference_side: Mapped[str | None] = mapped_column(String(16))
    reference_sample_ids: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    reference_sample_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    reference_samples: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    reference_window_from: Mapped[date | None] = mapped_column(Date)
    reference_window_to: Mapped[date | None] = mapped_column(Date)
    algorithm_version: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    # 06 行随 03 备件行级联作废；读侧过滤 is_active=false（#264/#266）。
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "quantity > 0 AND quantity < 1000000000000",
            name="ck_maintenance_site_issue_line_quantity",
        ),
        CheckConstraint(
            "manual_unit_cost IS NULL OR (manual_unit_cost >= 0 AND manual_unit_cost < 1000000000000)",
            name="ck_maintenance_site_issue_line_manual_cost",
        ),
        CheckConstraint(
            "manual_unit_cost_inc_tax IS NULL OR (manual_unit_cost_inc_tax >= 0 AND manual_unit_cost_inc_tax < 1000000000000)",
            name="ck_maintenance_site_issue_line_manual_cost_inc_tax",
        ),
        CheckConstraint(
            "unit_cost IS NULL OR (unit_cost >= 0 AND unit_cost < 1000000000000)",
            name="ck_maintenance_site_issue_line_unit_cost",
        ),
        CheckConstraint(
            "cost_source IS NULL OR cost_source IN ('direct_purchase', 'purchase_window', 'sales_window', 'manual')",
            name="ck_maintenance_site_issue_line_cost_source",
        ),
        CheckConstraint(
            "(manual_unit_cost IS NULL) = (manual_evidence IS NULL)",
            name="ck_maintenance_site_issue_line_manual_evidence_pair",
        ),
        CheckConstraint(
            "(manual_unit_cost IS NULL AND manual_unit_cost_inc_tax IS NULL) OR "
            "(manual_unit_cost IS NOT NULL AND manual_unit_cost_inc_tax = round(manual_unit_cost * NUMERIC '1.13', 2))",
            name="ck_maintenance_site_issue_line_manual_tax_pair",
        ),
        CheckConstraint(
            "(cost_source IS NULL AND unit_cost IS NULL AND cost_amount IS NULL "
            "AND unit_cost_ex_tax IS NULL AND unit_cost_inc_tax IS NULL "
            "AND cost_amount_ex_tax IS NULL AND cost_amount_inc_tax IS NULL) OR "
            "(cost_source IS NOT NULL AND unit_cost IS NOT NULL AND cost_amount IS NOT NULL "
            "AND unit_cost_ex_tax IS NOT NULL AND unit_cost_inc_tax IS NOT NULL "
            "AND cost_amount_ex_tax IS NOT NULL AND cost_amount_inc_tax IS NOT NULL)",
            name="ck_maintenance_site_issue_line_cost_result_pair",
        ),
        CheckConstraint(
            "unit_cost IS NULL OR (unit_cost = unit_cost_ex_tax AND cost_amount = cost_amount_ex_tax)",
            name="ck_maintenance_site_issue_line_legacy_ex_tax_aliases",
        ),
        CheckConstraint(
            "unit_cost_ex_tax IS NULL OR ("
            "unit_cost_inc_tax = round(unit_cost_ex_tax * NUMERIC '1.13', 2) "
            "AND cost_amount_ex_tax = round(quantity * unit_cost_ex_tax, 2) "
            "AND cost_amount_inc_tax = round(quantity * unit_cost_inc_tax, 2))",
            name="ck_maintenance_site_issue_line_dual_tax_amounts",
        ),
        CheckConstraint(
            "tax_rate_used = 0.13",
            name="ck_maintenance_site_issue_line_tax_rate_used",
        ),
        CheckConstraint(
            "price_basis = 'ex_tax'",
            name="ck_maintenance_site_issue_line_price_basis",
        ),
        CheckConstraint(
            "reference_sample_count = jsonb_array_length(reference_samples)",
            name="ck_maintenance_site_issue_line_sample_evidence_count",
        ),
        CheckConstraint(
            "char_length(btrim(algorithm_version)) > 0",
            name="ck_maintenance_site_issue_line_algorithm_version",
        ),
        CheckConstraint("reference_sample_count >= 0", name="ck_maintenance_site_issue_line_sample_count"),
        CheckConstraint("version >= 1", name="ck_maintenance_site_issue_line_version"),
        UniqueConstraint("issue_id", "line_no", name="uq_maintenance_site_issue_line_no"),
        Index("ix_maintenance_site_issue_line_issue", "issue_id", "line_no"),
        Index("ix_maintenance_site_issue_line_part", "part_id"),
        Index("ix_msil_active", "is_active"),
    )


class MaintenanceSiteIssueCommand(Base):
    """Idempotency receipt for a state-changing site-issue command."""

    __tablename__ = "maintenance_site_issue_command"

    command_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_project.project_id"), nullable=False
    )
    issue_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_site_issue.issue_id"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    response_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "action IN ('update', 'confirm', 'void', 'correct')",
            name="ck_maintenance_site_issue_command_action",
        ),
        Index(
            "ix_maintenance_site_issue_command_issue_time",
            "issue_id",
            "created_at",
        ),
    )


class MaintenanceSiteIssueReturnEvent(Base):
    """Transactional interface event for the later return-obligation module."""

    __tablename__ = "maintenance_site_issue_return_event"

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_project.project_id"), nullable=False
    )
    issue_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_site_issue.issue_id"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    issue_version: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    downstream_reference: Mapped[str | None] = mapped_column(String(128))
    consumed_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "event_type IN ('return_obligation_created', "
            "'return_obligation_corrected', 'return_obligation_voided')",
            name="ck_maintenance_site_issue_return_event_type",
        ),
        CheckConstraint(
            "issue_version >= 1",
            name="ck_maintenance_site_issue_return_event_version",
        ),
        CheckConstraint(
            "(downstream_reference IS NULL) = (consumed_at IS NULL)",
            name="ck_maintenance_site_issue_return_event_consumed_pair",
        ),
        UniqueConstraint(
            "issue_id",
            "event_type",
            "issue_version",
            name="uq_maintenance_site_issue_return_event_version",
        ),
        Index(
            "ix_maintenance_site_issue_return_event_issue_time",
            "issue_id",
            "created_at",
        ),
    )


class MaintenanceProjectExpenseAttribution(Base):
    """Canonical expense attribution; only mapped+approved facts count."""

    __tablename__ = "maintenance_project_expense_attribution"

    expense_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_project.project_id"), nullable=False
    )
    project_contract_id: Mapped[str | None] = mapped_column(
        ForeignKey("maintenance_project_contract.project_contract_id")
    )
    expense_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    expense_date: Mapped[date] = mapped_column(Date, nullable=False)
    applicant: Mapped[str | None] = mapped_column(String(64))
    category: Mapped[str | None] = mapped_column(String(64))
    expense_reason: Mapped[str | None] = mapped_column(Text)
    amount_ex_tax: Mapped[Decimal] = mapped_column(Money, nullable=False)
    amount_inc_tax: Mapped[Decimal] = mapped_column(Money, nullable=False)
    tax_rate_used: Mapped[Decimal] = mapped_column(
        Rate,
        nullable=False,
        default=Decimal("0.13"),
        server_default="0.13",
    )
    raw_status: Mapped[str] = mapped_column(String(64), nullable=False)
    status_mapping_state: Mapped[str] = mapped_column(String(16), nullable=False)
    normalized_status: Mapped[str] = mapped_column(String(16), nullable=False)
    status_mapping_version: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status_mapping_state IN ('mapped', 'unmapped')",
            name="ck_maintenance_project_expense_mapping_state",
        ),
        CheckConstraint(
            "normalized_status IN ('approved', 'rejected', 'void', 'unknown')",
            name="ck_maintenance_project_expense_status",
        ),
        CheckConstraint(
            "(status_mapping_state = 'mapped') = (normalized_status <> 'unknown')",
            name="ck_maintenance_project_expense_unmapped_unknown",
        ),
        CheckConstraint(
            "amount_ex_tax >= 0 AND amount_ex_tax < 1000000000000",
            name="ck_maintenance_project_expense_amount",
        ),
        CheckConstraint(
            "amount_inc_tax >= 0 AND amount_inc_tax < 1000000000000",
            name="ck_maintenance_project_expense_amount_inc_tax",
        ),
        CheckConstraint(
            "amount_inc_tax = round(amount_ex_tax * NUMERIC '1.13', 2)",
            name="ck_maintenance_project_expense_dual_tax_amounts",
        ),
        CheckConstraint(
            "tax_rate_used = 0.13",
            name="ck_maintenance_project_expense_tax_rate_used",
        ),
        CheckConstraint("version >= 1", name="ck_maintenance_project_expense_version"),
        UniqueConstraint("project_id", "expense_ref", name="uq_maintenance_project_expense_ref"),
        Index(
            "ix_maintenance_project_expense_project_date",
            "project_id",
            "expense_date",
        ),
    )


class MaintenanceProjectWorkbookState(Base):
    """Project-level concurrency token for the generated four-sheet workbook."""

    __tablename__ = "maintenance_project_workbook_state"

    project_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_project.project_id"), primary_key=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_export_id: Mapped[str | None] = mapped_column(String(64))
    last_exported_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    last_applied_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    expense_ready_through: Mapped[date | None] = mapped_column(Date)
    data_version: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint("revision >= 0", name="ck_maintenance_project_workbook_state_revision"),
        CheckConstraint(
            "expense_ready_through IS NULL OR "
            "expense_ready_through = date_trunc('month', expense_ready_through)::date",
            name="ck_maintenance_project_expense_ready_month",
        ),
    )


class MaintenanceProjectWorkbookOperation(Base):
    """Idempotency/audit record for project-workbook exports and applies."""

    __tablename__ = "maintenance_project_workbook_operation"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_project.project_id"), nullable=False
    )
    export_id: Mapped[str | None] = mapped_column(String(64))
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_key: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_type: Mapped[str] = mapped_column(String(24), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(
        ForeignKey("maintenance_collection_snapshot.collection_id")
    )
    operated_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "operation_type IN ('collection_create', 'file_export', 'file_apply')",
            name="ck_maintenance_project_workbook_operation_type",
        ),
        UniqueConstraint(
            "operation_key",
            name="uq_maintenance_project_workbook_operation_key",
        ),
        Index(
            "ix_maintenance_project_workbook_operation_project_file",
            "project_id",
            "file_sha256",
        ),
        Index(
            "ix_maintenance_project_workbook_operation_entity",
            "entity_id",
        ),
    )


class MaintenanceProjectWorkbookValidation(Base):
    """Server-owned, expiring validation plan used by atomic workbook apply."""

    __tablename__ = "maintenance_project_workbook_validation"

    validation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_project.project_id"), nullable=False
    )
    export_id: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_json: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    issues_json: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    error_workbook: Mapped[bytes | None] = mapped_column(LargeBinary)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    applied_at: Mapped[datetime | None] = mapped_column(TZDateTime)

    __table_args__ = (
        CheckConstraint("expected_revision >= 0", name="ck_maintenance_project_workbook_validation_revision"),
        CheckConstraint(
            "status IN ('valid', 'error', 'applied', 'expired')",
            name="ck_maintenance_project_workbook_validation_status",
        ),
        CheckConstraint(
            "error_workbook IS NULL OR status = 'error'",
            name="ck_maintenance_project_workbook_validation_error_file_status",
        ),
        CheckConstraint(
            "plan_json IS NULL OR status IN ('valid', 'applied')",
            name="ck_maintenance_project_workbook_validation_plan_status",
        ),
        CheckConstraint(
            "error_workbook IS NULL OR octet_length(error_workbook) <= 5242880",
            name="ck_maintenance_project_workbook_validation_error_file_size",
        ),
        Index("ix_maintenance_project_workbook_validation_expires", "expires_at"),
        Index(
            "ix_maintenance_project_workbook_validation_status_applied",
            "status",
            "applied_at",
        ),
        Index(
            "ix_maintenance_project_workbook_validation_project_file",
            "project_id",
            "file_sha256",
        ),
    )

"""Project-manager monthly workbook facts, acceptance state, and file metadata."""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._types import Money, TZDateTime


class MaintenanceManagerUploadBatch(Base):
    """Server-owned validation plan and idempotency ledger for workbook v3."""

    __tablename__ = "maintenance_manager_upload_batch"

    batch_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[int] = mapped_column(ForeignKey("sys_user.id"), nullable=False)
    report_month: Mapped[date] = mapped_column(Date, nullable=False)
    protocol_version: Mapped[str] = mapped_column(String(16), nullable=False)
    template_version: Mapped[str] = mapped_column(String(32), nullable=False)
    export_id: Mapped[str] = mapped_column(String(64), nullable=False)
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    operation_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    semantic_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_version: Mapped[str] = mapped_column(String(64), nullable=False)
    data_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    plan_json: Mapped[dict | None] = mapped_column(JSONB)
    issues_json: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    error_workbook: Mapped[bytes | None] = mapped_column(LargeBinary)
    result_json: Mapped[dict | None] = mapped_column(JSONB)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    applied_by: Mapped[str | None] = mapped_column(String(64))
    applied_at: Mapped[datetime | None] = mapped_column(TZDateTime)

    __table_args__ = (
        CheckConstraint(
            "report_month = date_trunc('month', report_month)::date",
            name="ck_maintenance_manager_batch_month_start",
        ),
        CheckConstraint(
            "file_size > 0 AND file_size <= 67108864",
            name="ck_maintenance_manager_batch_file_size",
        ),
        CheckConstraint(
            "status IN ('valid', 'error', 'applied', 'expired')",
            name="ck_maintenance_manager_batch_status",
        ),
        CheckConstraint(
            "(status IN ('valid', 'applied') AND plan_json IS NOT NULL) OR "
            "(status IN ('error', 'expired'))",
            name="ck_maintenance_manager_batch_plan_status",
        ),
        CheckConstraint(
            "error_workbook IS NULL OR (status = 'error' AND octet_length(error_workbook) <= 5242880)",
            name="ck_maintenance_manager_batch_error_workbook",
        ),
        CheckConstraint(
            "(status = 'applied' AND applied_at IS NOT NULL AND applied_by IS NOT NULL "
            "AND result_json IS NOT NULL) OR "
            "(status <> 'applied' AND applied_at IS NULL AND applied_by IS NULL)",
            name="ck_maintenance_manager_batch_applied_state",
        ),
        Index(
            "ix_maintenance_manager_batch_owner_month",
            "owner_user_id",
            "report_month",
            "created_at",
        ),
        Index(
            "ix_maintenance_manager_batch_status_expiry",
            "status",
            "expires_at",
        ),
        Index(
            "ix_maintenance_manager_batch_semantic",
            "owner_user_id",
            "report_month",
            "semantic_hash",
        ),
    )


class MaintenanceServicePeriod(Base):
    """Manager-maintained service dates; absent endpoints remain explicit."""

    __tablename__ = "maintenance_service_period"

    project_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_project.project_id"), primary_key=True
    )
    service_start: Mapped[date | None] = mapped_column(Date)
    service_end: Mapped[date | None] = mapped_column(Date)
    completeness_state: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_batch_id: Mapped[str | None] = mapped_column(
        ForeignKey("maintenance_manager_upload_batch.batch_id")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "completeness_state IN ('complete', 'start_only', 'end_only', 'empty')",
            name="ck_maintenance_service_period_completeness",
        ),
        CheckConstraint(
            "(completeness_state = 'complete' AND service_start IS NOT NULL AND service_end IS NOT NULL) OR "
            "(completeness_state = 'start_only' AND service_start IS NOT NULL AND service_end IS NULL) OR "
            "(completeness_state = 'end_only' AND service_start IS NULL AND service_end IS NOT NULL) OR "
            "(completeness_state = 'empty' AND service_start IS NULL AND service_end IS NULL)",
            name="ck_maintenance_service_period_state_fields",
        ),
        CheckConstraint(
            "service_start IS NULL OR service_end IS NULL OR service_end >= service_start",
            name="ck_maintenance_service_period_date_order",
        ),
        CheckConstraint(
            "source IN ('direct_api', 'manager_workbook_v3')",
            name="ck_maintenance_service_period_source",
        ),
        CheckConstraint(
            "(source = 'manager_workbook_v3' AND source_batch_id IS NOT NULL) OR "
            "(source = 'direct_api' AND source_batch_id IS NULL)",
            name="ck_maintenance_service_period_batch_source",
        ),
        CheckConstraint("version >= 1", name="ck_maintenance_service_period_version"),
    )


class MaintenanceCollectionMilestone(Base):
    """One planned collection node; never a financially confirmed receipt."""

    __tablename__ = "maintenance_collection_milestone"

    milestone_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_project.project_id"), nullable=False
    )
    project_contract_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_project_contract.project_contract_id"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    planned_date: Mapped[date | None] = mapped_column(Date)
    planned_amount: Mapped[Decimal | None] = mapped_column(Money)
    completeness_state: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_batch_id: Mapped[str | None] = mapped_column(
        ForeignKey("maintenance_manager_upload_batch.batch_id")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "sequence BETWEEN 1 AND 24",
            name="ck_maintenance_collection_milestone_sequence",
        ),
        CheckConstraint(
            "planned_amount IS NULL OR (planned_amount > 0 AND planned_amount < 1000000000000)",
            name="ck_maintenance_collection_milestone_amount",
        ),
        CheckConstraint(
            "completeness_state IN ('complete', 'date_only', 'amount_only')",
            name="ck_maintenance_collection_milestone_completeness",
        ),
        CheckConstraint(
            "(completeness_state = 'complete' AND planned_date IS NOT NULL AND planned_amount IS NOT NULL) OR "
            "(completeness_state = 'date_only' AND planned_date IS NOT NULL AND planned_amount IS NULL) OR "
            "(completeness_state = 'amount_only' AND planned_date IS NULL AND planned_amount IS NOT NULL)",
            name="ck_maintenance_collection_milestone_state_fields",
        ),
        CheckConstraint(
            "source IN ('direct_api', 'manager_workbook_v3')",
            name="ck_maintenance_collection_milestone_source",
        ),
        CheckConstraint(
            "(source = 'manager_workbook_v3' AND source_batch_id IS NOT NULL) OR "
            "(source = 'direct_api' AND source_batch_id IS NULL)",
            name="ck_maintenance_collection_milestone_batch_source",
        ),
        CheckConstraint("version >= 1", name="ck_maintenance_collection_milestone_version"),
        UniqueConstraint(
            "project_contract_id",
            "sequence",
            name="uq_maintenance_collection_milestone_contract_sequence",
        ),
        Index(
            "ix_maintenance_collection_milestone_project_date",
            "project_id",
            "planned_date",
            "sequence",
        ),
    )


class MaintenanceAcceptanceDeliverable(Base):
    """Submission and approval are stored as separate, non-self-approvable facts."""

    __tablename__ = "maintenance_acceptance_deliverable"

    deliverable_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_project.project_id"), nullable=False
    )
    deliverable_type: Mapped[str] = mapped_column(String(32), nullable=False)
    due_date: Mapped[date | None] = mapped_column(Date)
    submission_status: Mapped[str] = mapped_column(String(16), nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    submitted_by: Mapped[str | None] = mapped_column(String(64))
    approval_status: Mapped[str] = mapped_column(String(16), nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    approved_by: Mapped[str | None] = mapped_column(String(64))
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    configuration_state: Mapped[str] = mapped_column(String(40), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "deliverable_type IN ('acceptance_report', 'inspection_report')",
            name="ck_maintenance_acceptance_deliverable_type",
        ),
        CheckConstraint(
            "submission_status IN ('not_submitted', 'submitted')",
            name="ck_maintenance_acceptance_submission_status",
        ),
        CheckConstraint(
            "approval_status IN ('not_reviewed', 'approved', 'rejected')",
            name="ck_maintenance_acceptance_approval_status",
        ),
        CheckConstraint(
            "configuration_state IN ('configured', 'pending_business_configuration')",
            name="ck_maintenance_acceptance_configuration",
        ),
        CheckConstraint(
            "(submission_status = 'not_submitted' AND submitted_at IS NULL AND submitted_by IS NULL) OR "
            "(submission_status = 'submitted' AND submitted_at IS NOT NULL AND submitted_by IS NOT NULL)",
            name="ck_maintenance_acceptance_submission_fields",
        ),
        CheckConstraint(
            "(approval_status = 'not_reviewed' AND approved_at IS NULL AND approved_by IS NULL AND rejection_reason IS NULL) OR "
            "(approval_status = 'approved' AND approved_at IS NOT NULL AND approved_by IS NOT NULL AND rejection_reason IS NULL) OR "
            "(approval_status = 'rejected' AND approved_at IS NOT NULL AND approved_by IS NOT NULL "
            "AND rejection_reason IS NOT NULL AND char_length(btrim(rejection_reason)) > 0)",
            name="ck_maintenance_acceptance_approval_fields",
        ),
        CheckConstraint(
            "approval_status = 'not_reviewed' OR submission_status = 'submitted'",
            name="ck_maintenance_acceptance_approval_requires_submission",
        ),
        CheckConstraint(
            "submitted_by IS NULL OR approved_by IS NULL OR submitted_by <> approved_by",
            name="ck_maintenance_acceptance_no_self_approval",
        ),
        CheckConstraint("version >= 1", name="ck_maintenance_acceptance_version"),
        UniqueConstraint(
            "project_id",
            "deliverable_type",
            name="uq_maintenance_acceptance_project_type",
        ),
        Index(
            "ix_maintenance_acceptance_project_due",
            "project_id",
            "due_date",
            "deliverable_type",
        ),
    )


class BusinessFile(Base):
    """Carrier-neutral file metadata; an external URL is never a file object."""

    __tablename__ = "business_file"

    file_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    storage_provider: Mapped[str] = mapped_column(String(32), nullable=False)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(256), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    security_state: Mapped[str] = mapped_column(String(16), nullable=False)
    uploaded_by: Mapped[str] = mapped_column(String(64), nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    __table_args__ = (
        CheckConstraint(
            "storage_provider IN ('local', 'object_storage')",
            name="ck_business_file_storage_provider",
        ),
        CheckConstraint(
            "char_length(btrim(object_key)) > 0 AND "
            "btrim(object_key) !~* '^(https?|ftp|file)://' AND "
            "btrim(object_key) !~ '(^/|(^|/)\\.\\.(/|$))' AND "
            "strpos(object_key, chr(92)) = 0",
            name="ck_business_file_object_key_not_external_url",
        ),
        CheckConstraint(
            "char_length(btrim(original_filename)) BETWEEN 1 AND 256 AND "
            "strpos(original_filename, '/') = 0 AND "
            "strpos(original_filename, chr(92)) = 0 AND "
            "original_filename !~ '[[:cntrl:]]'",
            name="ck_business_file_original_filename_safe",
        ),
        CheckConstraint(
            "size_bytes > 0 AND size_bytes <= 52428800",
            name="ck_business_file_size",
        ),
        CheckConstraint(
            "sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_business_file_sha256",
        ),
        CheckConstraint(
            "security_state IN ('quarantined', 'active', 'blocked')",
            name="ck_business_file_security_state",
        ),
        CheckConstraint("version >= 1", name="ck_business_file_version"),
        UniqueConstraint("storage_provider", "object_key", name="uq_business_file_storage_object"),
        Index("ix_business_file_sha256", "sha256"),
    )


class BusinessFileLink(Base):
    """ACL-scoped attachment link to a controlled business entity."""

    __tablename__ = "business_file_link"

    link_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("business_file.file_id"), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(48), nullable=False)
    entity_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("maintenance_acceptance_deliverable.deliverable_id"),
        nullable=False,
    )
    relation_type: Mapped[str] = mapped_column(String(24), nullable=False)
    acl_scope: Mapped[str] = mapped_column(String(32), nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )
    archived_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    archived_by: Mapped[str | None] = mapped_column(String(64))
    archive_reason: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "entity_type = 'maintenance_acceptance_deliverable'",
            name="ck_business_file_link_entity_type",
        ),
        CheckConstraint(
            "relation_type = 'evidence'",
            name="ck_business_file_link_relation_type",
        ),
        CheckConstraint(
            "acl_scope = 'project_members'",
            name="ck_business_file_link_acl_scope",
        ),
        CheckConstraint(
            "(archived_at IS NULL AND archived_by IS NULL AND archive_reason IS NULL) OR "
            "(archived_at IS NOT NULL AND archived_by IS NOT NULL AND archive_reason IS NOT NULL "
            "AND char_length(btrim(archive_reason)) > 0)",
            name="ck_business_file_link_archive",
        ),
        UniqueConstraint(
            "file_id",
            "entity_type",
            "entity_id",
            "relation_type",
            name="uq_business_file_link_identity",
        ),
        Index(
            "ix_business_file_link_entity_active",
            "entity_type",
            "entity_id",
            "archived_at",
        ),
    )


class MaintenanceAcceptanceOperation(Base):
    """Append-only idempotency ledger for acceptance writes."""

    __tablename__ = "maintenance_acceptance_operation"

    operation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    operation_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_type: Mapped[str] = mapped_column(String(24), nullable=False)
    deliverable_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_acceptance_deliverable.deliverable_id"), nullable=False
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_project.project_id"), nullable=False
    )
    result_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    operated_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "operation_type IN ('attachment_upload', 'submit', 'approve', 'reject')",
            name="ck_maintenance_acceptance_operation_type",
        ),
        CheckConstraint(
            "payload_hash ~ '^[0-9a-f]{64}$'",
            name="ck_maintenance_acceptance_operation_payload_present",
        ),
        CheckConstraint(
            "char_length(btrim(operated_by)) > 0",
            name="ck_maintenance_acceptance_operation_operator",
        ),
        Index(
            "ix_maintenance_acceptance_operation_deliverable_time",
            "deliverable_id",
            "created_at",
        ),
    )


class BusinessFileDownloadAudit(Base):
    """Append-only proof of every controlled acceptance-file download."""

    __tablename__ = "business_file_download_audit"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    file_id: Mapped[str] = mapped_column(
        ForeignKey("business_file.file_id"), nullable=False
    )
    link_id: Mapped[str] = mapped_column(
        ForeignKey("business_file_link.link_id"), nullable=False
    )
    deliverable_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_acceptance_deliverable.deliverable_id"), nullable=False
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_project.project_id"), nullable=False
    )
    downloaded_by: Mapped[str] = mapped_column(String(64), nullable=False)
    sha256_at_download: Mapped[str] = mapped_column(String(64), nullable=False)
    downloaded_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "char_length(btrim(downloaded_by)) > 0",
            name="ck_business_file_download_audit_operator",
        ),
        CheckConstraint(
            "sha256_at_download ~ '^[0-9a-f]{64}$'",
            name="ck_business_file_download_audit_sha256",
        ),
        Index(
            "ix_business_file_download_audit_file_time",
            "file_id",
            "downloaded_at",
            "id",
        ),
        Index(
            "ix_business_file_download_audit_project_time",
            "project_id",
            "downloaded_at",
            "id",
        ),
    )


class MaintenanceManagerUploadBatchProject(Base):
    """Projects successfully covered by one applied monthly batch."""

    __tablename__ = "maintenance_manager_upload_batch_project"

    batch_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_manager_upload_batch.batch_id"), primary_key=True
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_project.project_id"), primary_key=True
    )
    assignment_id: Mapped[str] = mapped_column(
        ForeignKey("maintenance_project_user_assignment.assignment_id"), nullable=False
    )
    assignment_version: Mapped[int] = mapped_column(Integer, nullable=False)
    project_version: Mapped[int] = mapped_column(Integer, nullable=False)
    applied_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "assignment_version >= 1 AND project_version >= 1",
            name="ck_maintenance_manager_batch_project_versions",
        ),
        Index(
            "ix_maintenance_manager_batch_project_monthly_task",
            "project_id",
            "applied_at",
        ),
    )

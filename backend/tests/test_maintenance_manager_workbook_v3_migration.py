"""Storage invariants for the manager workbook v3 foundation (#206)."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError


TABLES = {
    "maintenance_manager_upload_batch",
    "maintenance_manager_upload_batch_project",
    "maintenance_service_period",
    "maintenance_collection_milestone",
    "maintenance_acceptance_deliverable",
    "business_file",
    "business_file_link",
}


def test_manager_workbook_v3_schema_has_longitudinal_and_attachment_foundations(db):
    inspector = inspect(db.get_bind())
    assert TABLES <= set(inspector.get_table_names())
    milestone_column_rows = inspector.get_columns("maintenance_collection_milestone")
    milestone_columns = {column["name"] for column in milestone_column_rows}
    assert {
        "project_contract_id",
        "sequence",
        "planned_date",
        "planned_amount",
        "completeness_state",
        "source_batch_id",
        "version",
    } <= milestone_columns
    planned_amount_type = next(
        column["type"]
        for column in milestone_column_rows
        if column["name"] == "planned_amount"
    )
    assert planned_amount_type.precision == 14
    assert planned_amount_type.scale == 2
    deliverable_columns = {
        column["name"]
        for column in inspector.get_columns("maintenance_acceptance_deliverable")
    }
    assert {
        "submission_status",
        "submitted_at",
        "submitted_by",
        "approval_status",
        "approved_at",
        "approved_by",
        "configuration_state",
    } <= deliverable_columns


def test_storage_rejects_self_approval_and_external_url_as_attachment(db):
    db.execute(
        text(
            "INSERT INTO maintenance_project "
            "(project_id, project_code, display_name, lifecycle_status) VALUES "
            "('manager-v3-storage-project', 'MANAGER-V3-STORAGE', "
            "'合成存储项目', 'ongoing')"
        )
    )
    db.commit()

    submitted_at = datetime.now(UTC)
    with pytest.raises(DBAPIError):
        db.execute(
            text(
                "INSERT INTO maintenance_acceptance_deliverable "
                "(deliverable_id, project_id, deliverable_type, submission_status, "
                "submitted_at, submitted_by, approval_status, approved_at, approved_by, "
                "configuration_state) VALUES "
                "('self-approved', 'manager-v3-storage-project', 'acceptance_report', "
                "'submitted', :submitted_at, 'same-user', 'approved', :submitted_at, "
                "'same-user', 'configured')"
            ),
            {"submitted_at": submitted_at},
        )
        db.flush()
    db.rollback()

    with pytest.raises(DBAPIError):
        db.execute(
            text(
                "INSERT INTO business_file "
                "(file_id, storage_provider, object_key, original_filename, mime_type, "
                "size_bytes, sha256, security_state, uploaded_by) VALUES "
                "('external-url-file', 'object_storage', '  https://example.invalid/file', "
                "'synthetic.pdf', 'application/pdf', 10, :sha256, 'active', 'synthetic')"
            ),
            {"sha256": "a" * 64},
        )
        db.flush()
    db.rollback()

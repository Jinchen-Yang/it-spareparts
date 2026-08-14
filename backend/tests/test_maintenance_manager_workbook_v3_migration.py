"""Storage invariants for the manager workbook and acceptance closure (#206)."""

from datetime import UTC, datetime
import os

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError

from app import permissions
from app.db import engine


TABLES = {
    "maintenance_manager_upload_batch",
    "maintenance_manager_upload_batch_project",
    "maintenance_service_period",
    "maintenance_collection_milestone",
    "maintenance_acceptance_deliverable",
    "business_file",
    "business_file_link",
    "maintenance_acceptance_operation",
    "business_file_download_audit",
}


def _cfg() -> AlembicConfig:
    cfg = AlembicConfig(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option(
        "script_location",
        os.path.join(os.path.dirname(__file__), "..", "alembic"),
    )
    return cfg


def _current_head() -> str:
    return ScriptDirectory.from_config(_cfg()).get_current_head()


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
    link_fks = {
        tuple(constraint["constrained_columns"]): constraint["referred_table"]
        for constraint in inspector.get_foreign_keys("business_file_link")
    }
    assert link_fks[("entity_id",)] == "maintenance_acceptance_deliverable"


def test_acceptance_permissions_default_fail_closed_and_review_is_high_risk():
    for key in (
        "action_maintenance_manager_workbook_apply",
        "action_maintenance_acceptance_submit",
        "action_maintenance_acceptance_review",
    ):
        assert key in permissions.ACTION_KEYS
        assert permissions.ACTION_PAGE_DEPENDENCIES[key] == "page_maintenance"
        assert permissions.effective("admin", None)[key] is True
        for role in ("boss", "sales", "purchaser", "readonly", "guest"):
            assert permissions.effective(role, None)[key] is False
    assert "action_maintenance_manager_workbook_apply" in permissions.HIGH_RISK_KEYS
    assert "action_maintenance_acceptance_review" in permissions.HIGH_RISK_KEYS


def test_acceptance_permission_upgrade_backfills_only_real_admin_role(db):
    db.close()
    config = _cfg()
    alembic_command.downgrade(config, "b7e1c3a9d5f2")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO sys_user
                        (username, role, password_hash, template_code,
                         template_perms, permissions, perm_overrides)
                    VALUES
                        ('issue206-migration-boss', 'boss', 'unused', 'admin',
                         '{"sentinel": true}'::jsonb,
                         '{"sentinel": true}'::jsonb,
                         '{"action_maintenance_acceptance_review": true}'::jsonb),
                        ('issue206-migration-admin', 'admin', 'unused', 'boss',
                         '{"sentinel": true}'::jsonb,
                         '{"sentinel": true}'::jsonb,
                         '{"action_maintenance_acceptance_review": false}'::jsonb)
                    """
                )
            )
        alembic_command.upgrade(config, "head")
        with engine.connect() as connection:
            templates = dict(
                connection.execute(
                    text(
                        """
                        SELECT code,
                               (permissions->>'action_maintenance_acceptance_review')::boolean
                        FROM sys_role_template
                        WHERE code IN ('admin', 'boss')
                        """
                    )
                ).all()
            )
            users = {
                username: (template_value, permission_value, has_override)
                for username, template_value, permission_value, has_override
                in connection.execute(
                    text(
                        """
                        SELECT username,
                               (template_perms->>'action_maintenance_acceptance_review')::boolean,
                               (permissions->>'action_maintenance_acceptance_review')::boolean,
                               perm_overrides ? 'action_maintenance_acceptance_review'
                        FROM sys_user
                        WHERE username LIKE 'issue206-migration-%'
                        """
                    )
                )
            }
        assert templates == {"admin": True, "boss": False}
        assert users == {
            "issue206-migration-admin": (True, True, False),
            "issue206-migration-boss": (False, False, False),
        }
    finally:
        alembic_command.upgrade(config, "head")


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


def _seed_acceptance_audit(db, *, operation_id: str) -> None:
    db.execute(
        text(
            "INSERT INTO maintenance_project "
            "(project_id, project_code, display_name, lifecycle_status) VALUES "
            "('issue206-audit-project', 'ISSUE206-AUDIT', '合成审计项目', 'ongoing')"
        )
    )
    db.execute(
        text(
            "INSERT INTO maintenance_acceptance_deliverable "
            "(deliverable_id, project_id, deliverable_type, due_date, submission_status, "
            "approval_status, configuration_state, version) VALUES "
            "('issue206-audit-deliverable', 'issue206-audit-project', 'acceptance_report', "
            "'2026-08-31', 'not_submitted', 'not_reviewed', 'configured', 1)"
        )
    )
    db.execute(
        text(
            "INSERT INTO maintenance_acceptance_operation "
            "(operation_id, operation_key, payload_hash, operation_type, deliverable_id, "
            "project_id, result_json, operated_by) VALUES "
            "(:operation_id, :operation_key, :payload_hash, 'submit', "
            "'issue206-audit-deliverable', 'issue206-audit-project', '{}'::jsonb, 'audit-user')"
        ),
        {
            "operation_id": operation_id,
            "operation_key": f"operation-key-{operation_id}",
            "payload_hash": "a" * 64,
        },
    )
    db.commit()


def test_acceptance_operation_is_database_append_only(db):
    _seed_acceptance_audit(db, operation_id="issue206-append-only")
    with pytest.raises(DBAPIError, match="append-only"):
        db.execute(
            text(
                "UPDATE maintenance_acceptance_operation SET operated_by = 'tampered' "
                "WHERE operation_id = 'issue206-append-only'"
            )
        )
        db.flush()
    db.rollback()
    with pytest.raises(DBAPIError, match="append-only"):
        db.execute(
            text(
                "DELETE FROM maintenance_acceptance_operation "
                "WHERE operation_id = 'issue206-append-only'"
            )
        )
        db.flush()
    db.rollback()


def test_acceptance_downgrade_blocks_when_append_only_history_exists(db):
    _seed_acceptance_audit(db, operation_id="issue206-downgrade-guard")
    db.close()
    with pytest.raises(Exception, match="downgrade blocked"):
        alembic_command.downgrade(_cfg(), "b7e1c3a9d5f2")
    with engine.connect() as connection:
        current = connection.scalar(text("SELECT version_num FROM alembic_version"))
    # The integration merge preflights every maintenance branch before Alembic
    # can remove the merge marker or partially traverse a sibling branch.
    assert current == _current_head()


def test_v3_milestone_orm_exposes_new_follow_up_columns():
    """回款提醒迁移后，ORM 模型必须映射七列新增字段（设计 §4.1）。"""
    from app.models.maintenance_manager import MaintenanceCollectionMilestone

    mapped = {column.name for column in MaintenanceCollectionMilestone.__table__.columns}
    for column in (
        "date_precision",
        "collection_plan_import_batch_id",
        "follow_up_status",
        "follow_up_review_required",
        "follow_up_note",
        "followed_up_by",
        "followed_up_at",
    ):
        assert column in mapped, f"MaintenanceCollectionMilestone 缺少映射列 {column}"


def test_existing_manager_workbook_v3_rows_backfill_new_columns(db):
    """新 revision 之后，存量 manager_workbook_v3 节点回填 day/pending/false。"""
    db.close()
    config = _cfg()
    alembic_command.downgrade(config, "d9f1a3c7e5b2")
    try:
        with engine.begin() as connection:
            user_id = connection.execute(
                text(
                    "INSERT INTO sys_user (username, password_hash, role) "
                    "VALUES ('v3-backfill-user', 'unused', 'readonly') RETURNING id"
                )
            ).scalar_one()
            connection.execute(
                text(
                    "INSERT INTO maintenance_project "
                    "(project_id, project_code, display_name, lifecycle_status) VALUES "
                    "('v3-backfill-project', 'V3-BACKFILL-PROJECT', '合成v3回填项目', 'ongoing')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO maintenance_project_contract "
                    "(project_contract_id, project_id, contract_id, contract_no, "
                    " contract_amount, contract_status, status_mapping_state, "
                    " status_mapping_version, included_in_total, effective_from, "
                    " source, version) "
                    "VALUES ('v3-backfill-contract', 'v3-backfill-project', "
                    " 'v3-backfill-contract-id', 'XS-V3-BACKFILL', 100000, 'active', "
                    " 'mapped', 'synthetic-v1', true, '2026-01-01', "
                    " 'synthetic-test', 1)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO maintenance_manager_upload_batch "
                    "(batch_id, owner_user_id, report_month, protocol_version, "
                    " template_version, export_id, file_sha256, file_size, "
                    " operation_key, semantic_hash, scope_version, data_version, "
                    " status, plan_json, issues_json, created_by, created_at, expires_at) "
                    "VALUES ('v3-backfill-batch', :user_id, '2026-08-01', 'v3', 'tpl', "
                    " 'export-1', repeat('a', 64), 100, 'v3-backfill-op-key', "
                    " repeat('b', 64), repeat('c', 64), repeat('d', 64), 'valid', "
                    " '{}'::jsonb, '[]'::jsonb, 'synthetic-test', now(), "
                    " now() + interval '24 hours')"
                ),
                {"user_id": user_id},
            )
            connection.execute(
                text(
                    "INSERT INTO maintenance_collection_milestone "
                    "(milestone_id, project_id, project_contract_id, sequence, "
                    " planned_date, planned_amount, completeness_state, source, "
                    " source_batch_id, version) "
                    "VALUES ('v3-backfill-milestone', 'v3-backfill-project', "
                    " 'v3-backfill-contract', 1, '2026-09-01', 25000.00, 'complete', "
                    " 'manager_workbook_v3', 'v3-backfill-batch', 1)"
                )
            )
        alembic_command.upgrade(config, "head")
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT date_precision, follow_up_status, follow_up_review_required, "
                    "followed_up_by, followed_up_at, collection_plan_import_batch_id "
                    "FROM maintenance_collection_milestone "
                    "WHERE milestone_id = 'v3-backfill-milestone'"
                )
            ).one()
        assert row.date_precision == "day"
        assert row.follow_up_status == "pending"
        assert row.follow_up_review_required is False
        assert row.followed_up_by is None
        assert row.followed_up_at is None
        assert row.collection_plan_import_batch_id is None
    finally:
        alembic_command.upgrade(config, "head")


def test_combined_downgrade_keeps_both_branches_when_assignment_history_exists(db):
    user_id = db.execute(
        text(
            "INSERT INTO sys_user (username, password_hash, role) "
            "VALUES ('issue206-merge-guard', 'unused', 'readonly') RETURNING id"
        )
    ).scalar_one()
    db.execute(
        text(
            "INSERT INTO maintenance_project "
            "(project_id, project_code, display_name, lifecycle_status) VALUES "
            "('issue206-merge-project', 'ISSUE206-MERGE', '合成合流项目', 'ongoing')"
        )
    )
    db.execute(
        text(
            "INSERT INTO maintenance_project_user_assignment "
            "(assignment_id, project_id, responsibility_type, user_id, version, "
            "assigned_by, assignment_reason) VALUES "
            "('issue206-merge-assignment', 'issue206-merge-project', "
            "'primary_manager', :user_id, 1, 'migration-test', '验证跨分支降级保护')"
        ),
        {"user_id": user_id},
    )
    db.commit()
    db.close()

    with pytest.raises(Exception, match="downgrade blocked"):
        alembic_command.downgrade(_cfg(), "b7e1c3a9d5f2")

    with engine.connect() as connection:
        current = connection.scalar(text("SELECT version_num FROM alembic_version"))
        assignment_table = connection.scalar(
            text("SELECT to_regclass('maintenance_project_user_assignment')")
        )
        manager_table = connection.scalar(
            text("SELECT to_regclass('maintenance_manager_upload_batch')")
        )
    assert current == _current_head()
    assert assignment_table == "maintenance_project_user_assignment"
    assert manager_table == "maintenance_manager_upload_batch"

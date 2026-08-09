"""Storage invariants for explicit project-manager assignments (#205)."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.auth import hash_password
from app.models.maintenance_project import (
    MaintenanceProject,
    MaintenanceProjectUserAssignment,
)
from app.models.system import SysUser


def test_manager_assignment_schema_has_history_fields_fks_and_one_active_index(db):
    inspector = inspect(db.get_bind())
    columns = {
        column["name"]
        for column in inspector.get_columns("maintenance_project_user_assignment")
    }
    assert {
        "assignment_id",
        "project_id",
        "responsibility_type",
        "user_id",
        "source_manager_text",
        "version",
        "assigned_at",
        "assigned_by",
        "assignment_reason",
        "archived_at",
        "archived_by",
        "archive_reason",
    } <= columns
    foreign_targets = {
        tuple(foreign_key["referred_columns"]): foreign_key["referred_table"]
        for foreign_key in inspector.get_foreign_keys(
            "maintenance_project_user_assignment"
        )
    }
    assert foreign_targets[("project_id",)] == "maintenance_project"
    assert foreign_targets[("id",)] == "sys_user"
    indexes = {
        index["name"]: index
        for index in inspector.get_indexes("maintenance_project_user_assignment")
    }
    assert indexes["ux_maintenance_project_primary_manager_active"]["unique"] is True


def test_storage_rejects_two_active_primary_managers_but_keeps_archived_history(db):
    project = MaintenanceProject(
        project_id="project-assignment-storage",
        project_code="PM-STORAGE",
        display_name="合成存储约束项目",
        lifecycle_status="ongoing",
    )
    first_user = SysUser(
        username="assignment_storage_first",
        role="purchaser",
        display_name="合成存储负责人一",
        password_hash=hash_password("synthetic-password-123"),
    )
    second_user = SysUser(
        username="assignment_storage_second",
        role="purchaser",
        display_name="合成存储负责人二",
        password_hash=hash_password("synthetic-password-123"),
    )
    db.add_all([project, first_user, second_user])
    db.commit()
    first = MaintenanceProjectUserAssignment(
        assignment_id="assignment-storage-first",
        project_id=project.project_id,
        responsibility_type="primary_manager",
        user_id=first_user.id,
        source_manager_text="来源负责人原文",
        version=1,
        assigned_at=datetime.now(UTC),
        assigned_by="synthetic-admin",
        assignment_reason="合成首次映射",
    )
    db.add(first)
    db.commit()

    with pytest.raises(DBAPIError):
        db.execute(
            text(
                "UPDATE maintenance_project_user_assignment "
                "SET assigned_by = 'tampered-operator' "
                "WHERE assignment_id = 'assignment-storage-first'"
            )
        )
    db.rollback()
    with pytest.raises(DBAPIError):
        db.execute(
            text(
                "DELETE FROM maintenance_project_user_assignment "
                "WHERE assignment_id = 'assignment-storage-first'"
            )
        )
    db.rollback()

    db.add(
        MaintenanceProjectUserAssignment(
            assignment_id="assignment-storage-conflict",
            project_id=project.project_id,
            responsibility_type="primary_manager",
            user_id=second_user.id,
            source_manager_text="来源负责人原文",
            version=1,
            assigned_at=datetime.now(UTC),
            assigned_by="synthetic-admin",
            assignment_reason="合成冲突映射",
        )
    )
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()

    first = db.get(MaintenanceProjectUserAssignment, first.assignment_id)
    archived_at = datetime.now(UTC)
    first.archived_at = archived_at
    first.archived_by = "synthetic-admin"
    first.archive_reason = "合成项目交接"
    first.version += 1
    db.commit()
    with pytest.raises(DBAPIError):
        db.execute(
            text(
                "UPDATE maintenance_project_user_assignment "
                "SET archive_reason = 'tampered-archive-reason' "
                "WHERE assignment_id = 'assignment-storage-first'"
            )
        )
    db.rollback()
    second = MaintenanceProjectUserAssignment(
        assignment_id="assignment-storage-second",
        project_id=project.project_id,
        responsibility_type="primary_manager",
        user_id=second_user.id,
        source_manager_text="来源负责人原文",
        version=1,
        assigned_at=datetime.now(UTC),
        assigned_by="synthetic-admin",
        assignment_reason="合成交接后改派",
    )
    db.add(second)
    db.commit()

    assert db.get(MaintenanceProjectUserAssignment, first.assignment_id) is not None
    assert db.get(MaintenanceProjectUserAssignment, second.assignment_id) is not None

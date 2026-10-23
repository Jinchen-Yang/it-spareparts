"""State consumers depend on the primitive; workflows retain their transactions."""

import ast
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.models.maintenance_project import (
    MaintenanceProject,
    MaintenanceProjectAuditLog,
    MaintenanceProjectUserAssignment,
)
from app.models.maintenance_project_operations import MaintenanceProjectWorkbookState
from app.models.system import SysUser
from app.services import maintenance_project_assignments as assignments
from app.services import maintenance_project_catalog as catalog
from app.services import maintenance_project_operations as legacy_operations
from app.services import maintenance_workbook_state as workbook_state


APP = Path(__file__).parents[1] / "app"
LEGACY = "app.services.maintenance_project_operations"
STATE_HELPERS = {
    "get_or_create_workbook_state", "lock_workbook_states",
    "bump_workbook_revision", "bump_locked_workbook_revision",
    "_workbook_data_version", "_lock_project_for_fact_write",
}
# These consumers had no other direct operations capability. This is a boundary
# assertion, not an exception list: the global helper rule scans all app files.
STATE_ONLY_CONSUMERS = (
    "api/maintenance_projects.py",
    "services/maintenance_collection_milestone_restore.py",
    "services/maintenance_collection_plan_imports.py",
    "services/maintenance_collection_reminders.py",
    "services/maintenance_demand_manual.py",
    "services/maintenance_demands.py",
    "services/maintenance_manager_workbook_adapter.py",
    "services/maintenance_migration_source.py",
    "services/maintenance_project_assignments.py",
    "services/maintenance_project_catalog.py",
    "services/maintenance_project_identity.py",
    "services/maintenance_project_workbook_v3.py",
    "services/maintenance_return_receipt_import.py",
    "services/maintenance_source_assignments.py",
    "services/maintenance_source_cancel_cascade.py",
    "services/maintenance_wbdd_sync.py",
)


def _legacy_state_references(tree):
    bindings = {}
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                qualified = f"{node.module}.{alias.name}"
                bindings[alias.asname or alias.name] = qualified
                if node.module == LEGACY and (
                    alias.name in STATE_HELPERS or alias.name == "*"
                ):
                    violations.append((node.lineno, qualified))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    bindings[alias.asname] = alias.name

    def qualified(node):
        if isinstance(node, ast.Name):
            return bindings.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            return f"{qualified(node.value)}.{node.attr}"
        return ""

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in STATE_HELPERS:
            if qualified(node.value) == LEGACY:
                violations.append((node.lineno, qualified(node)))
    return violations


def test_every_application_consumer_uses_the_state_module_directly():
    violations = {
        str(path.relative_to(APP)): references
        for path in sorted(APP.rglob("*.py"))
        if (references := _legacy_state_references(ast.parse(path.read_text())))
    }
    assert violations == {}


@pytest.mark.parametrize("source", [
    "from app.services import maintenance_project_operations as old\nold.lock_workbook_states(db)",
    "from app.services.maintenance_project_operations import bump_workbook_revision as bump",
    "import app.services.maintenance_project_operations as old\nold.bump_locked_workbook_revision(db)",
    "import app.services.maintenance_project_operations\napp.services.maintenance_project_operations.get_or_create_workbook_state(db)",
    "from app.services.maintenance_project_operations import *",
])
def test_boundary_guard_recognizes_different_legacy_import_spellings(source):
    assert _legacy_state_references(ast.parse(source))


@pytest.mark.parametrize("relative_path", STATE_ONLY_CONSUMERS)
def test_state_only_consumer_has_no_direct_operations_import(relative_path):
    for node in ast.walk(ast.parse((APP / relative_path).read_text())):
        if isinstance(node, ast.ImportFrom):
            assert node.module != LEGACY
            assert not (node.module == "app.services" and any(
                alias.name == "maintenance_project_operations" for alias in node.names
            ))
        elif isinstance(node, ast.Import):
            assert all(alias.name != LEGACY for alias in node.names)


@pytest.mark.parametrize("rollback", [False, True], ids=["commit", "rollback"])
def test_distinct_workflows_keep_audits_and_one_caller_owned_revision(db, monkeypatch, rollback):
    """Project edit + manager assignment share state without using old exports."""
    project = MaintenanceProject(
        project_id="r3b-workflows", project_code="R3B-WORKFLOWS",
        display_name="R3b 隔离测试项目", lifecycle_status="ongoing",
    )
    manager = SysUser(
        username="r3b-manager", display_name="R3b 合成负责人", role="purchaser",
        password_hash="unused-in-service-test",
    )
    db.add_all([project, manager])
    db.commit()

    def forbidden_legacy_helper(*_args, **_kwargs):
        pytest.fail("a migrated workflow reached an operations state helper")

    for name in STATE_HELPERS:
        monkeypatch.setattr(legacy_operations, name, forbidden_legacy_helper)

    # A caller may already own the state row; both real workflows must join the
    # same root transaction, preserve both audit entries and leave commit to it.
    state = workbook_state.get_or_create_workbook_state(
        db, project_id=project.project_id, lock=True,
    )
    catalog.update_project(
        db, project_id=project.project_id, version=1,
        updates={"project_note": "事务中的项目备注"},
        reason="验证跨工作流状态边界", operated_by="r3b-test",
    )
    assignments.assign_primary_manager(
        db, project_id=project.project_id, user_id=manager.id,
        expected_assignment_id=None, expected_assignment_version=None,
        reason="验证独立负责人审计", operated_by="r3b-test",
    )
    assert state.revision == 1
    assert db.scalar(select(func.count()).select_from(MaintenanceProjectAuditLog)) == 2
    assert db.scalar(select(func.count()).select_from(MaintenanceProjectUserAssignment)) == 1

    if rollback:
        db.rollback()
    else:
        db.commit()
    db.expire_all()
    persisted = db.get(MaintenanceProject, "r3b-workflows")
    assert persisted.project_note == (None if rollback else "事务中的项目备注")
    assert persisted.version == (1 if rollback else 2)
    persisted_state = db.get(MaintenanceProjectWorkbookState, "r3b-workflows")
    if rollback:
        assert persisted_state is None
    else:
        assert persisted_state.revision == 1
    assert db.scalar(select(func.count()).select_from(MaintenanceProjectAuditLog)) == (0 if rollback else 2)
    assert db.scalar(select(func.count()).select_from(MaintenanceProjectUserAssignment)) == (0 if rollback else 1)

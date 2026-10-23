"""Architecture contracts: new adapters must not depend on transport/workbooks."""
import ast
from pathlib import Path

from sqlalchemy import func, select

from app.models.maintenance_project_operations import MaintenanceProjectOperationAudit
from app.services.maintenance_project_audit import write_project_audit
from tests.boss_board_helpers import make_project

APP = Path(__file__).resolve().parents[1] / "app"


def imports(path):
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            yield node.module or ""
            yield from (f"{node.module}.{alias.name}" for alias in node.names)


def test_maintenance_services_do_not_import_http_routes():
    violations = [(path.name, name) for path in (APP / "services").glob("maintenance*.py")
                  for name in imports(path) if name == "app.api" or name.startswith("app.api.")]
    assert violations == []


def test_shared_read_and_audit_modules_do_not_depend_on_workbook_adapters():
    for filename in ("maintenance_assigned_lines.py", "maintenance_project_access.py",
                     "maintenance_project_audit.py", "maintenance_row_workbench.py",
                     "maintenance_wbdd_sync.py", "maintenance_errors.py",
                     "maintenance_site_return_requirements.py",
                     "maintenance_site_issue_merge_references.py"):
        assert not any("workbook" in name for name in imports(APP / "services" / filename)), filename


def test_shared_queries_and_source_sync_have_no_indirect_http_or_excel_dependency():
    # A domain helper used to import WorkbookError from the full Excel writer.
    # Direct-import rules alone cannot catch that backward dependency.
    modules = {
        "app." + ".".join(path.relative_to(APP).with_suffix("").parts): path
        for path in APP.rglob("*.py") if path.name != "__init__.py"
    }
    edges = {name: set(imports(path)) & modules.keys()
             for name, path in modules.items()}
    workbook_adapters = {
        "app.services.maintenance_project_master_workbook",
        "app.services.maintenance_expense_collection_workbook",
        "app.services.maintenance_project_workbook_adapter",
        "app.services.maintenance_manager_workbook_adapter",
        "app.services.maintenance_project_workbook_v2",
        "app.services.maintenance_project_workbook_v3",
        "app.services.maintenance_manager_workbook_v3",
        "app.services.maintenance_roundtrip",
    }
    for root in ("maintenance_assigned_lines", "maintenance_project_access",
                 "maintenance_row_workbench", "maintenance_wbdd_sync"):
        pending = [(f"app.services.{root}",)]
        seen = set()
        for path in pending:
            current = path[-1]
            assert current not in workbook_adapters and not current.startswith("app.api."), (
                " -> ".join(path)
            )
            if current in seen:
                continue
            seen.add(current)
            pending.extend((*path, target) for target in sorted(edges[current]) if target not in seen)


def test_project_audit_joins_callers_transaction_and_preserves_payload(db):
    project = make_project(db)
    db.commit()
    count = select(func.count()).select_from(MaintenanceProjectOperationAudit)
    before = db.scalar(count)
    transaction = db.begin_nested()
    write_project_audit(db, project_id=project.project_id, entity_type="test_extension",
                        entity_id=42, action="UPDATE", operated_by="synthetic",
                        reason="", before={"qty": "1"}, after={"qty": "2"})
    assert transaction.is_active
    db.flush()
    row = db.scalar(select(MaintenanceProjectOperationAudit).where(
        MaintenanceProjectOperationAudit.entity_type == "test_extension"))
    assert row.entity_id == "42"
    assert row.reason == "工作簿编辑"
    assert row.before_json == {"qty": "1"} and row.after_json == {"qty": "2"}
    transaction.rollback()
    assert db.scalar(count) == before

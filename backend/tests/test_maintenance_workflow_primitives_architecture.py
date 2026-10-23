"""Keep workflow primitives independent of the commands that consume them."""

import ast
import subprocess
import sys
from pathlib import Path

import pytest


SERVICES = Path(__file__).parents[1] / "app" / "services"
MODULES = ("maintenance_workbook_state", "maintenance_site_issue_command_identity")


@pytest.mark.parametrize("name", MODULES)
def test_primitive_imports_only_models_from_the_application(name):
    tree = ast.parse((SERVICES / f"{name}.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative imports need an explicit boundary review"
            imports = [node.module or ""]
        elif isinstance(node, ast.Import):
            imports = [alias.name for alias in node.names]
        else:
            continue
        for module in imports:
            if module == "app" or module.startswith("app."):
                assert module.startswith("app.models."), (name, module)


@pytest.mark.parametrize("name", MODULES)
def test_fresh_process_import_does_not_load_application_workflows(name):
    code = f"""
import importlib, sys
importlib.import_module('app.services.{name}')
loaded = {{key for key in sys.modules if key.startswith(('app.services.', 'app.api.'))}}
assert loaded == {{'app.services.{name}'}}, sorted(loaded)
"""
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=SERVICES.parents[1],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_correction_has_no_direct_dependency_on_operations():
    tree = ast.parse((SERVICES / "maintenance_site_issue_line_correction.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "app.services.maintenance_project_operations"
            assert not (node.module == "app.services" and any(
                alias.name == "maintenance_project_operations" for alias in node.names
            ))
        elif isinstance(node, ast.Import):
            assert all(alias.name != "app.services.maintenance_project_operations" for alias in node.names)


def test_legacy_exports_keep_the_same_implementation_objects():
    from app.services import maintenance_project_operations as old
    from app.services import maintenance_workbook_state as states
    from app.services import maintenance_site_issue_command_identity as identity

    for name in ("get_or_create_workbook_state", "lock_workbook_states",
                 "bump_workbook_revision", "bump_locked_workbook_revision", "_project"):
        assert getattr(old, name) is getattr(states, name)
    assert old._workbook_data_version is states.workbook_data_version
    assert old._lock_project_for_fact_write is states.lock_project_for_fact_write
    assert old._site_issue_request_fingerprint is identity.site_issue_request_fingerprint
    assert old._lock_idempotency_key is identity.lock_site_issue_idempotency_key

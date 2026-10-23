"""R1/R2 shared modules must not load their former application adapters.

Static traversal includes function-local imports and package initializers;
fresh-process imports also check the actual startup path. This is deliberately
limited to these four seams, not a general dependency framework or a claim that
the legacy maintenance services have no remaining cycles.
"""

import ast
from collections import deque
from functools import lru_cache
import json
from pathlib import Path
import subprocess
import sys

import pytest


_BACKEND = Path(__file__).resolve().parents[1]
_SEAMS = (
    "app.services.maintenance_contract_reference",
    "app.services.maintenance_demand_project_reference",
    "app.services.maintenance_site_issue_representation",
    "app.services.maintenance_operation_errors",
)
_MODULES = {}
for _path in (_BACKEND / "app").rglob("*.py"):
    _parts = _path.relative_to(_BACKEND).with_suffix("").parts
    if _parts[-1] == "__init__":
        _parts = _parts[:-1]
    _MODULES[".".join(_parts)] = _path


def _forbidden(module: str) -> bool:
    if module == "app.api" or module.startswith("app.api."):
        return True
    if not module.startswith("app.services."):
        return False
    name = module.rsplit(".", 1)[-1]
    return (
        "workbook" in name
        or "manual" in name
        or name in {
            "maintenance_boss_board",
            "maintenance_cost",
            "maintenance_project_operations",
        }
    )


@lru_cache(maxsize=None)
def _dependencies(module: str) -> frozenset[str]:
    path = _MODULES[module]
    parts = module.split(".")
    # Importing app.models.foo also executes app and app.models initializers.
    dependencies = {".".join(parts[:end]) for end in range(1, len(parts))}
    package = parts if path.name == "__init__.py" else parts[:-1]
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            dependencies.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                prefix = package[:len(package) - node.level + 1]
                base = ".".join([*prefix, *([base] if base else [])])
            dependencies.add(base)
            dependencies.update(f"{base}.{alias.name}" for alias in node.names)
    return frozenset(dependencies.intersection(_MODULES))


def _forbidden_paths(root: str) -> list[str]:
    queue = deque([(root, (root,))])
    seen = {root}
    violations = []
    while queue:
        module, path = queue.popleft()
        for dependency in sorted(_dependencies(module)):
            if dependency in seen:
                continue
            seen.add(dependency)
            next_path = (*path, dependency)
            if _forbidden(dependency):
                violations.append(" -> ".join(next_path))
            else:
                queue.append((dependency, next_path))
    return violations


@pytest.mark.parametrize("module", _SEAMS)
def test_shared_seam_has_no_transitive_adapter_dependency(module):
    assert module in _MODULES, f"Missing shared seam: {module}"
    violations = _forbidden_paths(module)
    assert not violations, "Forbidden dependency paths:\n" + "\n".join(violations)


@pytest.mark.parametrize("module", _SEAMS)
def test_shared_seam_import_does_not_load_application_adapters(module):
    # The parent pytest process may already have imported every application
    # adapter through unrelated tests, so inspect each seam in a fresh process.
    blocked = sorted(name for name in _MODULES if _forbidden(name))
    script = (
        "import importlib, json, sys\n"
        "importlib.import_module(sys.argv[1])\n"
        "print(json.dumps(sorted(set(json.loads(sys.argv[2])) & sys.modules.keys())))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, module, json.dumps(blocked)],
        cwd=_BACKEND,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [], f"{module} loaded: {result.stdout}"


def test_fact_edits_do_not_import_manual_or_operations_directly():
    # R2 removes these two direct reverse edges. The existing legacy_event
    # path facts -> site_return_requirements -> project_operations (and its
    # WorkbookError adapter) is outside this extraction; no transitive exemption
    # is granted to any of the four new modules above.
    dependencies = _dependencies("app.services.maintenance_site_issue_facts")
    assert not dependencies.intersection({
        "app.services.maintenance_manual_site_issue",
        "app.services.maintenance_project_operations",
    })

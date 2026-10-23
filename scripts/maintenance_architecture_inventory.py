"""Report conservative Python import reachability; this is not a runtime profiler."""

import argparse
import ast
import json
import subprocess
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "--baseline",
    required=True,
    help="Verified Git ref to compare with the working tree",
)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
baseline = subprocess.check_output(
    ["git", "rev-parse", "--verify", args.baseline + "^{commit}"], cwd=root, text=True
).strip()
paths = [
    p
    for p in subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", baseline, "backend/app"],
        cwd=root,
        text=True,
    ).splitlines()
    if p.endswith(".py")
]


def module(p):
    name = p.removeprefix("backend/").removesuffix(".py").replace("/", ".")
    return name.removesuffix(".__init__")


def graph(current):
    ps = set(paths)
    if current:
        ps = {p for p in ps if (root / p).exists()}
        ps.update(
            str(p.relative_to(root)) for p in (root / "backend/app").rglob("*.py")
        )
    modules = {module(p): p for p in sorted(ps)}
    edges = {m: set() for m in modules}
    sizes = {}
    for m, p in modules.items():
        source = (
            (root / p).read_text()
            if current
            else subprocess.check_output(
                ["git", "show", f"{baseline}:{p}"], cwd=root, text=True
            )
        )
        sizes[m] = len(source.splitlines())
        for n in ast.walk(ast.parse(source)):
            if isinstance(n, ast.Import):
                candidates = [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom):
                base = n.module or ""
                if n.level:
                    parent = (
                        m.split(".")
                        if p.endswith("/__init__.py")
                        else m.split(".")[:-1]
                    )
                    base = ".".join(
                        parent[: len(parent) - n.level + 1] + ([base] if base else [])
                    )
                candidates = [base] + [base + "." + a.name for a in n.names]
            else:
                continue
            for item in candidates:
                if item in modules and item != m:
                    edges[m].add(item)

    def closure(m):
        found = set()
        todo = list(edges.get(m, ()))
        while todo:
            x = todo.pop()
            if x not in found:
                found.add(x)
                todo.extend(edges[x] - found)
        return found

    index = {}
    low = {}
    stack = []
    active = set()
    scc = []

    def visit(v):
        index[v] = low[v] = len(index)
        stack.append(v)
        active.add(v)
        for w in sorted(edges[v]):
            if w not in index:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in active:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            group = []
            while True:
                w = stack.pop()
                active.remove(w)
                group.append(w)
                if w == v:
                    break
            if len(group) > 1:
                scc.append(sorted(group))

    for v in edges:
        if v not in index:
            visit(v)
    maintenance = {m for m in modules if "maintenance" in m}
    interesting = [
        "app.services.maintenance_row_workbench",
        "app.services.maintenance_wbdd_sync",
        "app.agent.tools",
        "app.services.maintenance_assigned_lines",
        "app.services.maintenance_project_access",
        "app.services.maintenance_project_audit",
    ]
    return {
        "module_count": len(modules),
        "maintenance_module_count": len(maintenance),
        "maintenance_cycles": sorted(
            [g for g in scc if any(x in maintenance for x in g)],
            key=lambda group: (-len(group), group),
        ),
        "footprints": {
            m: {
                "direct": len(edges[m]),
                "transitive": len(closure(m)),
                "transitive_api_modules": sorted(
                    x for x in closure(m) if x.startswith("app.api.")
                ),
                "transitive_workbook_modules": sorted(
                    x for x in closure(m) if "workbook" in x
                ),
            }
            for m in interesting
            if m in edges
        },
        "edges": {
            k: sorted(v)
            for k, v in sorted(edges.items())
            if k in maintenance or k.startswith("app.agent.")
        },
        "sizes": {k: v for k, v in sizes.items() if k in maintenance},
    }


result = {
    "caveat": "Static upper bound, includes function-local and conditional imports; not runtime load or measured performance",
    "baseline_sha": baseline,
    "baseline": graph(False),
    "candidate": graph(True),
}
args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
for stage, data in result.items():
    if not isinstance(data, dict):
        continue
    print(
        stage,
        "modules",
        data["module_count"],
        "maintenance",
        data["maintenance_module_count"],
        "cycles",
        [(len(g), g[:5]) for g in data["maintenance_cycles"]],
    )
    print(json.dumps(data["footprints"], indent=2))

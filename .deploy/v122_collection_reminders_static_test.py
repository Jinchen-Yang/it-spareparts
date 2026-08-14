#!/usr/bin/env python3
"""Portable static self-test for v1.22 collection-reminders release controls."""

from __future__ import annotations

import ast
import importlib.util
import re
import subprocess
import sys
import tarfile
from pathlib import Path


HERE = Path(__file__).resolve().parent
PACKAGED = (HERE / "manifest.json").is_file()
ROOT = HERE if PACKAGED else HERE.parent
MANIFEST = HERE / "v122_collection_reminders_manifest.py"
BUILD = HERE / "v122_collection_reminders_build.sh"
REHEARSE = HERE / "v122_collection_reminders_rehearse.sh"
RELEASE = HERE / "v122_collection_reminders_release.sh"


def _script(path: Path) -> str:
    assert path.is_file(), path
    subprocess.run(["bash", "-n", str(path)], cwd=ROOT, check=True)
    return path.read_text(encoding="utf-8")


def _manifest_module():
    spec = importlib.util.spec_from_file_location("v122_collection_manifest", MANIFEST)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _assignment(tree: ast.Module, name: str):
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id == name for target in targets):
                return ast.literal_eval(node.value)
    raise AssertionError(f"missing Alembic assignment: {name}")


def _migration_sources() -> list[tuple[str, bytes]]:
    if not PACKAGED:
        directory = ROOT / "backend" / "alembic" / "versions"
        return [(path.name, path.read_bytes()) for path in directory.glob("*.py")]
    source_bundle = HERE / "source.tar"
    assert source_bundle.is_file(), source_bundle
    rows: list[tuple[str, bytes]] = []
    with tarfile.open(source_bundle, "r:*") as archive:
        for member in archive.getmembers():
            if (
                member.isfile()
                and member.name.startswith("source/backend/alembic/versions/")
                and member.name.endswith(".py")
            ):
                stream = archive.extractfile(member)
                assert stream is not None
                rows.append((Path(member.name).name, stream.read()))
    return rows


def _validate_migration_graph() -> None:
    graph: dict[str, str | tuple[str, ...] | None] = {}
    for name, raw in _migration_sources():
        tree = ast.parse(raw.decode("utf-8"), filename=name)
        revision = _assignment(tree, "revision")
        down_revision = _assignment(tree, "down_revision")
        assert isinstance(revision, str) and revision not in graph, revision
        assert down_revision is None or isinstance(down_revision, (str, tuple))
        graph[revision] = down_revision
    assert graph, "no Alembic migrations found"
    referenced: set[str] = set()
    for parent in graph.values():
        if isinstance(parent, str):
            referenced.add(parent)
        elif isinstance(parent, tuple):
            referenced.update(parent)
    assert sorted(set(graph) - referenced) == ["c8e2a4f6b1d3"]
    assert graph["c8e2a4f6b1d3"] == "d9f1a3c7e5b2"
    cursor = "c8e2a4f6b1d3"
    visited: set[str] = set()
    while cursor != "d9f1a3c7e5b2":
        assert cursor not in visited, "Alembic cycle"
        visited.add(cursor)
        parent = graph.get(cursor)
        assert isinstance(parent, str), "d9 is not an ancestor of c8"
        cursor = parent


def main() -> None:
    module = _manifest_module()
    assert module.DB_FROM == "d9f1a3c7e5b2"
    assert module.DB_TO == "c8e2a4f6b1d3"
    assert module.FORMAT == "v122-collection-reminders-2"
    if PACKAGED:
        subprocess.run(
            [sys.executable, str(MANIFEST), "verify", str(HERE)],
            cwd=HERE,
            check=True,
        )
    for script in (BUILD, REHEARSE, RELEASE):
        text = _script(script)
        assert ("f9b2" + "d4e7c1a6") not in text
        assert ("v121" + "_beta") not in text
    _validate_migration_graph()
    release = RELEASE.read_text(encoding="utf-8")
    for forbidden in (
        r"\balembic\s+downgrade\b",
        r"\bdocker\s+compose\s+down\b",
        r"\bcompose\s+down\b",
        r"\bdocker\s+volume\s+(rm|prune)\b",
    ):
        assert re.search(forbidden, release) is None, forbidden


if __name__ == "__main__":
    main()

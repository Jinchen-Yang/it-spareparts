"""Explicitly loaded CI plugin; no automatic pytest discovery or global env hook."""
from collections import Counter
from fnmatch import fnmatch
import os
from pathlib import Path

import pytest

from pytest_shard import make_manifest, read_json, validate_manifest, write_json

# This repository uses pytest's default Python discovery. A future intentional
# discovery change must update this small CI contract and its behavior tests.
DISCOVERY = {"python_files": ["test_*.py", "*_test.py"], "python_classes": ["Test"],
             "python_functions": ["test"], "testpaths": [],
             "norecursedirs": ["*.egg", ".*", "_darcs", "build", "CVS", "dist",
                               "node_modules", "venv", "{arch}"]}


def expected_test_files(root):
    found = set()
    for directory, names, files in os.walk(root):
        names[:] = [name for name in names if name != "__pycache__"
                    and not any(fnmatch(name, pattern) for pattern in DISCOVERY["norecursedirs"])
                    and not (Path(directory) / name / "pyvenv.cfg").is_file()
                    and not (Path(directory) / name / "conda-meta" / "history").is_file()]
        for name in files:
            if any(fnmatch(name, pattern) for pattern in DISCOVERY["python_files"]):
                found.add((Path(directory) / name).resolve())
    return found


def pytest_addoption(parser):
    group = parser.getgroup("ci-shard")
    group.addoption("--ci-shard-mode", choices=("plan", "run"), required=True)
    group.addoption("--ci-shard-output", required=True)
    group.addoption("--ci-shard-run-key", required=True)
    group.addoption("--ci-shard-commit", required=True)
    group.addoption("--ci-shard-manifest")
    group.addoption("--ci-shard-index", type=int)


def pytest_configure(config):
    if config.args != ["."] or config.rootpath != config.invocation_params.dir:
        raise pytest.UsageError("CI collection requires the fixed rootdir and sole '.' start path")
    if any(config.getini(name) != value for name, value in DISCOVERY.items()):
        raise pytest.UsageError("partial pytest discovery configuration is forbidden in sharded CI")
    # No implicit -k/-m/last-failed selection may redefine the full denominator.
    for option in ("keyword", "markexpr", "lf", "deselect", "ignore", "ignore_glob",
                   "stepwise", "stepwise_skip", "stepwise_reset"):
        if getattr(config.option, option, None):
            raise pytest.UsageError("partial pytest selection is forbidden in sharded CI")
    config.pluginmanager.register(Recorder(config), "ci-shard-recorder")


class Recorder:
    def __init__(self, config):
        self.config = config
        self.collected = []
        self.selected = []
        self.reports = []
        self.collection_errors = []
        self.manifest = None
        self.foreign_deselected = []
        self.selecting = False
        self.partitioned = False
        self.raw_items = []
        self.modules = set()
        self.expected_files = expected_test_files(config.rootpath)

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, session, config, items):
        if self.foreign_deselected:
            raise pytest.UsageError("another plugin deselected tests before CI partitioning")
        if Counter(item.nodeid for item in items) != Counter(self.raw_items):
            raise pytest.UsageError("collected items changed before CI partitioning")
        self.collected = [item.nodeid for item in items]
        run_key = config.getoption("--ci-shard-run-key")
        commit = config.getoption("--ci-shard-commit")
        if config.getoption("--ci-shard-mode") == "plan":
            self.manifest = make_manifest(self.collected, run_key, commit)
            self.partitioned = True
            return
        self.manifest = read_json(config.getoption("--ci-shard-manifest"))
        validate_manifest(self.manifest, run_key, commit)
        if self.collected != self.manifest["nodeids"]:
            raise pytest.UsageError("complete collection differs from CI manifest")
        shard = config.getoption("--ci-shard-index")
        if type(shard) is not int or not 0 <= shard < len(self.manifest["shards"]):
            raise pytest.UsageError("invalid CI shard index")
        self.selected = self.manifest["shards"][shard]
        selected = set(self.selected)
        deselected = [item for item in items if item.nodeid not in selected]
        items[:] = [item for item in items if item.nodeid in selected]
        self.selecting = True
        try:
            config.hook.pytest_deselected(items=deselected)
        finally:
            self.selecting = False
        if not items:
            raise pytest.UsageError("empty CI shard")
        self.partitioned = True

    def pytest_collectreport(self, report):
        if report.failed or report.skipped:
            self.collection_errors.append(report.nodeid)
        self.raw_items.extend(item.nodeid for item in report.result if isinstance(item, pytest.Item))

    def pytest_collectstart(self, collector):
        if isinstance(collector, pytest.Module):
            self.modules.add(collector.path.resolve())

    def pytest_deselected(self, items):
        if not self.selecting:
            self.foreign_deselected.extend(item.nodeid for item in items)

    def pytest_collection_finish(self, session):
        if self.collection_errors or self.foreign_deselected:
            raise pytest.UsageError("failed/skipped collection or external deselection is forbidden in CI")
        if not self.partitioned:
            return  # Preserve the original error raised during partitioning.
        if self.expected_files != self.modules:
            raise pytest.UsageError("default Python test files differ from collected modules")
        expected = self.selected if self.config.getoption("--ci-shard-mode") == "run" else self.collected
        if [item.nodeid for item in session.items] != expected:
            raise pytest.UsageError("collected items changed after CI partitioning")

    def pytest_runtest_logreport(self, report):
        reason = None
        if report.skipped:
            reason = str(report.longrepr[2]) if isinstance(report.longrepr, tuple) else str(report.longrepr)
            reason = reason.removeprefix("Skipped: ")
        self.reports.append({"nodeid": report.nodeid, "when": report.when,
                             "outcome": report.outcome, "reason": reason,
                             "wasxfail": getattr(report, "wasxfail", None),
                             "duration": report.duration})

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_sessionfinish(self, session, exitstatus):
        yield
        # Outer wrapper observes isolation cleanup changing session.exitstatus.
        config = self.config
        if config.getoption("--ci-shard-mode") == "plan":
            if session.exitstatus == 0 and not self.collection_errors and self.manifest:
                write_json(config.getoption("--ci-shard-output"), self.manifest)
            return
        if self.manifest is None:
            return
        write_json(config.getoption("--ci-shard-output"), {
            "schema": self.manifest["schema"], "run_key": self.manifest["run_key"],
            "commit": self.manifest["commit"], "manifest_digest": self.manifest["digest"],
            "shard": config.getoption("--ci-shard-index"), "collected": self.collected,
            "selected": self.selected, "reports": self.reports,
            "collection_errors": self.collection_errors, "pytest_exit": int(session.exitstatus),
        })

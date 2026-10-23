"""The required CI result must cover the full collection exactly once."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("ci_pytest_shard_test_support", ROOT / "scripts/pytest_shard.py")
shards = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(shards)
NEEDS = {"backend_prepare": {"result": "success"}, "backend_shards": {"result": "success"}}


def manifest():
    return shards.make_manifest([f"tests/test_{letter}.py::test_value[{i}]"
                                 for letter in "abcde" for i in range(3)], "run:1:sha", "sha")


def reports(plan):
    return [{"schema": 1, "run_key": plan["run_key"], "commit": plan["commit"],
             "manifest_digest": plan["digest"], "shard": index,
             "selected": selected.copy(), "collected": plan["nodeids"].copy(),
             "pytest_exit": 0, "command_exit": 0, "collection_errors": [],
             "reports": [{"nodeid": nodeid, "when": phase, "outcome": "passed",
                           "wasxfail": None, "reason": None, "duration": 0.01}
                          for nodeid in selected for phase in ("setup", "call", "teardown")]}
            for index, selected in enumerate(plan["shards"])]


def verify(plan, evidence, needs=NEEDS):
    return shards.verify_all(plan, evidence, "run:1:sha", "sha", needs)


def test_partition_is_deterministic_and_keeps_files_together():
    plan = manifest()
    assert plan == manifest()
    assert verify(plan, reports(plan)) == {"tests": 15, "shards": 4, "skipped": {}, "passed": 15}
    assert all(set(selected) <= set(plan["nodeids"]) for selected in plan["shards"])
    owners = {}
    for index, selected in enumerate(plan["shards"]):
        assert selected
        assert selected == [item for item in plan["nodeids"] if item in selected]
        for nodeid in selected:
            file = nodeid.split("::")[0]
            assert owners.setdefault(file, index) == index


@pytest.mark.parametrize("nodeids", [[], ["a::test"] * 4, ["a::test", "b::test"], ["invalid"] * 5])
def test_empty_duplicate_and_insufficient_collections_are_rejected(nodeids):
    with pytest.raises(ValueError):
        shards.partition(nodeids)


@pytest.mark.parametrize("field,value", [("run_key", "other-attempt"), ("commit", "other-sha"),
                                           ("digest", "wrong"), ("allowed_skips", {}),
                                           ("shards", [[]] * 4)])
def test_manifest_rejects_modified_identity_policy_or_partition(field, value):
    plan = manifest()
    plan[field] = value
    with pytest.raises(ValueError):
        verify(plan, reports(plan))


@pytest.mark.parametrize("state", ["failure", "cancelled", "skipped", "", "neutral"])
@pytest.mark.parametrize("job", ["backend_prepare", "backend_shards"])
def test_any_unsuccessful_upstream_blocks_required_job(job, state):
    needs = copy.deepcopy(NEEDS)
    needs[job]["result"] = state
    plan = manifest()
    with pytest.raises(ValueError):
        verify(plan, reports(plan), needs)


@pytest.mark.parametrize("mutation", ["missing_shard", "duplicate_shard", "missing_test", "extra_test",
                                      "duplicate_phase", "missing_phase", "failed_setup", "failed_call",
                                      "failed_teardown", "xfail", "xpass", "unexpected_skip",
                                      "old_attempt", "wrong_hash", "collection_drift", "collection_error",
                                      "exit_nonzero", "cleanup_failure", "missing_exit", "missing_event_field",
                                      "skip_without_reason"])
def test_incomplete_or_unsuccessful_evidence_never_passes(mutation):
    plan = manifest()
    evidence = reports(plan)
    first = evidence[0]
    if mutation == "missing_shard": evidence.pop()
    elif mutation == "duplicate_shard": evidence[-1] = copy.deepcopy(first)
    elif mutation == "missing_test": first["reports"] = first["reports"][3:]
    elif mutation == "extra_test": first["reports"].append({**first["reports"][0], "nodeid": "other::test"})
    elif mutation == "duplicate_phase": first["reports"].append(first["reports"][0])
    elif mutation == "missing_phase": first["reports"].pop()
    elif mutation.startswith("failed_"):
        phase = mutation.removeprefix("failed_")
        next(event for event in first["reports"] if event["when"] == phase)["outcome"] = "failed"
    elif mutation in ("xfail", "xpass"):
        first["reports"][1].update(wasxfail="not approved", outcome="skipped" if mutation == "xfail" else "passed")
    elif mutation == "unexpected_skip": first["reports"][1].update(outcome="skipped", reason="missing dependency")
    elif mutation == "old_attempt": first["run_key"] = "run:0:sha"
    elif mutation == "wrong_hash": first["manifest_digest"] = "wrong"
    elif mutation == "collection_drift": first["collected"] = first["collected"][:-1]
    elif mutation == "collection_error": first["collection_errors"] = ["broken.py"]
    elif mutation == "exit_nonzero": first["command_exit"] = 1
    elif mutation == "cleanup_failure": first["pytest_exit"] = 1
    elif mutation == "missing_exit": first.pop("command_exit")
    elif mutation == "missing_event_field": first["reports"][0].pop("wasxfail")
    elif mutation == "skip_without_reason": first["reports"][1].update(outcome="skipped", reason=None)
    with pytest.raises(ValueError):
        verify(plan, evidence)


def test_only_exact_existing_skip_nodeid_and_reason_are_allowed():
    allowed = shards.read_json(shards.SKIP_POLICY)
    nodeid, reason = next(iter(allowed.items()))
    plan = shards.make_manifest([nodeid, "b::test", "c::test", "d::test"], "run:1:sha", "sha")
    evidence = reports(plan)
    target = next(report for report in evidence if nodeid in report["selected"])
    event = next(event for event in target["reports"] if event["nodeid"] == nodeid and event["when"] == "call")
    event.update(outcome="skipped", reason=reason)
    assert verify(plan, evidence)["skipped"] == {nodeid: reason}
    event["reason"] = "new reason"
    with pytest.raises(ValueError): verify(plan, evidence)
    assert not any("authz" in name for name in allowed)


def run_plugin(directory, mode, output, *extra):
    env = os.environ.copy()
    env.pop("PYTEST_ADDOPTS", None)
    env.pop("PYTEST_PLUGINS", None)
    env["PYTHONPATH"] = str(ROOT / "scripts")
    cmd = [sys.executable, "-m", "pytest", "-q", "-c", "/dev/null", "-p", "pytest_shard_plugin",
           "--rootdir", str(directory), ".",
           "--ci-shard-mode", mode, "--ci-shard-output", str(output),
           "--ci-shard-run-key", "run:1:sha", "--ci-shard-commit", "sha", *extra]
    if mode == "plan": cmd.append("--collect-only")
    return subprocess.run(cmd, cwd=directory, env=env, capture_output=True, text=True, timeout=45)


def test_real_plugin_collects_all_tests_and_executes_each_exactly_once(tmp_path):
    for letter in "abcd":
        (tmp_path / f"test_{letter}.py").write_text(
            "import pytest\n@pytest.mark.parametrize('n', [1,2])\ndef test_value(n):\n    assert n > 0\n")
    output = tmp_path / "manifest.json"
    result = run_plugin(tmp_path, "plan", output)
    assert result.returncode == 0, result.stdout + result.stderr
    plan = shards.read_json(output)
    assert len(plan["nodeids"]) == 8
    evidence = []
    for index in range(4):
        output = tmp_path / f"shard-{index}.json"
        result = run_plugin(tmp_path, "run", output, "--ci-shard-manifest", str(tmp_path / "manifest.json"),
                            "--ci-shard-index", str(index))
        assert result.returncode == 0, result.stdout + result.stderr
        report = shards.read_json(output)
        report["command_exit"] = result.returncode
        evidence.append(report)
    assert verify(plan, evidence)["passed"] == 8


def test_real_plugin_reports_teardown_and_session_cleanup_failures(tmp_path):
    for letter in "abcd":
        (tmp_path / f"test_{letter}.py").write_text("def test_value():\n    assert True\n")
    (tmp_path / "conftest.py").write_text(
        "import pytest\n@pytest.fixture(autouse=True)\ndef cleanup():\n    yield\n    assert False, 'teardown failed'\n"
        "def pytest_sessionfinish(session):\n    if not session.config.option.collectonly:\n        session.exitstatus = 1\n")
    output = tmp_path / "manifest.json"
    result = run_plugin(tmp_path, "plan", output)
    assert result.returncode == 0, result.stdout + result.stderr
    plan = shards.read_json(output)
    result = run_plugin(tmp_path, "run", tmp_path / "shard.json", "--ci-shard-manifest", str(output),
                        "--ci-shard-index", "0")
    assert result.returncode != 0
    report = shards.read_json(tmp_path / "shard.json")
    report["command_exit"] = result.returncode
    assert report["pytest_exit"] == 1
    assert any(event["when"] == "teardown" and event["outcome"] == "failed" for event in report["reports"])
    with pytest.raises(ValueError): shards.verify_report(plan, report, 0)


def test_real_plugin_rejects_partial_collection_and_drift(tmp_path):
    for letter in "abcd":
        (tmp_path / f"test_{letter}.py").write_text("def test_value():\n    assert True\n")
    output = tmp_path / "manifest.json"
    result = run_plugin(tmp_path, "plan", output, "-k", "value")
    assert result.returncode != 0 and not output.exists()
    assert "partial pytest selection" in result.stderr
    assert run_plugin(tmp_path, "plan", output).returncode == 0
    (tmp_path / "test_a.py").unlink()
    result = run_plugin(tmp_path, "run", tmp_path / "shard.json", "--ci-shard-manifest", str(output),
                        "--ci-shard-index", "0")
    assert result.returncode != 0
    assert "collection differs" in result.stderr


def test_verify_cli_fails_for_missing_artifacts_and_failed_upstream(tmp_path):
    plan = shards.make_manifest(manifest()["nodeids"], "run:1:sha", shards.commit())
    shards.write_json(tmp_path / "manifest.json", plan)
    env = {**os.environ, "CI_TEST_NEEDS": json.dumps(NEEDS)}
    command = [sys.executable, str(ROOT / "scripts/pytest_shard.py"), "verify",
               "--manifest", str(tmp_path / "manifest.json"), "--reports", str(tmp_path),
               "--run-key", "run:1:sha", "--needs-env", "CI_TEST_NEEDS"]
    missing = subprocess.run(command, env=env, capture_output=True, text=True, timeout=15)
    assert missing.returncode != 0 and "missing or duplicate shard" in missing.stderr
    for report in reports(plan):
        shards.write_json(tmp_path / f"shard-{report['shard']}.json", report)
    good = subprocess.run(command, env=env, capture_output=True, text=True, timeout=15)
    assert good.returncode == 0, good.stderr
    env["CI_TEST_NEEDS"] = json.dumps({"backend_prepare": {"result": "failure"},
                                      "backend_shards": {"result": "skipped"}})
    failed = subprocess.run(command, env=env, capture_output=True, text=True, timeout=15)
    assert failed.returncode != 0 and "must succeed" in failed.stderr


@pytest.mark.parametrize("statement", ["pytest.skip('module missing', allow_module_level=True)",
                                        "pytest.importorskip('ci_nonexistent_test_dependency')"])
def test_real_plugin_rejects_collection_skips(tmp_path, statement):
    for letter in "abcde":
        (tmp_path / f"test_{letter}.py").write_text("def test_value():\n    assert True\n")
    (tmp_path / "test_a.py").write_text("import pytest\n" + statement + "\n")
    output = tmp_path / "manifest.json"
    result = run_plugin(tmp_path, "plan", output)
    assert result.returncode != 0 and not output.exists()
    assert "failed/skipped collection" in result.stderr


@pytest.mark.parametrize("addopts", ["--ignore=test_a.py", "--ignore-glob=*a.py", "--lf",
                                      "--stepwise", "--deselect=test_a.py::test_value", "-m invisible"])
def test_real_plugin_rejects_ini_selection(tmp_path, addopts):
    for letter in "abcde":
        (tmp_path / f"test_{letter}.py").write_text("def test_value():\n    assert True\n")
    output = tmp_path / "manifest.json"
    result = run_plugin(tmp_path, "plan", output, "-o", "addopts=" + addopts)
    assert result.returncode != 0 and not output.exists()
    assert "partial pytest selection" in result.stderr


@pytest.mark.parametrize("after", [False, True])
def test_real_plugin_rejects_other_plugin_deselection(tmp_path, after):
    for letter in "abcde":
        (tmp_path / f"test_{letter}.py").write_text("def test_value():\n    assert True\n")
    decorator = "@pytest.hookimpl(wrapper=True)" if after else "@pytest.hookimpl(tryfirst=True)"
    body = "    yield\n" if after else ""
    (tmp_path / "conftest.py").write_text(
        "import pytest\n" + decorator + "\ndef pytest_collection_modifyitems(config, items):\n" + body
        + "    removed = items.pop()\n    config.hook.pytest_deselected(items=[removed])\n")
    output = tmp_path / "manifest.json"
    result = run_plugin(tmp_path, "plan", output)
    assert result.returncode != 0 and not output.exists()
    assert "deselected" in result.stderr or "deselection" in result.stderr


@pytest.mark.parametrize("case", ["collect_ignore", "ignore_hook", "silent_before", "silent_after",
                                  "python_files", "python_classes", "python_functions",
                                  "testpaths", "norecursedirs", "positional"])
def test_real_plugin_rejects_accidentally_narrowed_discovery(tmp_path, case):
    for letter in "abcde":
        (tmp_path / f"test_{letter}.py").write_text("def test_value():\n    assert True\n")
    (tmp_path / "test_e.py").write_text("def test_value():\n    assert False, 'omitted poison'\n")
    hooks = {
        "collect_ignore": "collect_ignore = ['test_e.py']\n",
        "ignore_hook": "def pytest_ignore_collect(collection_path):\n    return collection_path.name == 'test_e.py' or None\n",
        "silent_before": "import pytest\n@pytest.hookimpl(tryfirst=True)\ndef pytest_collection_modifyitems(items):\n    items[:] = [i for i in items if 'test_e.py' not in i.nodeid]\n",
        "silent_after": "import pytest\n@pytest.hookimpl(wrapper=True)\ndef pytest_collection_modifyitems(items):\n    yield\n    items[:] = [i for i in items if 'test_e.py' not in i.nodeid]\n",
    }
    options = {"python_files": "python_files=test_[abcd].py", "python_classes": "python_classes=Only",
               "python_functions": "python_functions=other", "testpaths": "testpaths=subset",
               "norecursedirs": "norecursedirs=tests", "positional": "addopts=test_a.py"}
    if case in hooks:
        (tmp_path / "conftest.py").write_text(hooks[case])
    extra = ["-o", options[case]] if case in options else []
    output = tmp_path / "manifest.json"
    result = run_plugin(tmp_path, "plan", output, *extra)
    assert result.returncode != 0 and not output.exists(), result.stdout + result.stderr


@pytest.mark.parametrize("config_at", ["root", "backend"])
def test_real_cli_uses_same_root_with_external_manifest_and_junit(tmp_path, config_at):
    repository = tmp_path / "repo"
    backend = repository / "backend"
    scripts = repository / "scripts"
    (backend / "tests").mkdir(parents=True)
    scripts.mkdir()
    for name in ("pytest_shard.py", "pytest_shard_plugin.py", "pytest_shard_skips.json"):
        shutil.copyfile(ROOT / "scripts" / name, scripts / name)
    (repository if config_at == "root" else backend).joinpath("pytest.ini").write_text("[pytest]\n")
    for letter in "abcde":
        (backend / "tests" / f"test_{letter}.py").write_text("def test_value():\n    assert True\n")
    (backend / "tests" / "test_empty.py").write_text("# Empty modules are still collected.\n")
    for excluded in (".venv", "__pycache__", "build", "node_modules", "venv", "custom_env", "conda_env"):
        directory = backend / excluded
        directory.mkdir()
        (directory / "test_poison.py").write_text("def test_poison():\n    assert False\n")
        if excluded == "custom_env":
            (directory / "pyvenv.cfg").write_text("home = /unused\n")
        elif excluded == "conda_env":
            (directory / "conda-meta").mkdir()
            (directory / "conda-meta" / "history").write_text("environment\n")
    for arguments in (["init", "-q"], ["add", "."], ["-c", "user.name=CI test", "-c",
                      "user.email=ci@example.invalid", "commit", "-qm", "fixture"]):
        subprocess.run(["git", *arguments], cwd=repository, check=True, capture_output=True)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    env = os.environ.copy()
    for name in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTHONPATH"):
        env.pop(name, None)
    def invoke(*arguments):
        result = subprocess.run([sys.executable, str(scripts / "pytest_shard.py"), *arguments],
                                cwd=backend, env=env, capture_output=True, text=True, timeout=45)
        assert result.returncode == 0, result.stdout + result.stderr
    plan_path = artifacts / "manifest.json"
    invoke("plan", "--output", str(plan_path), "--run-key", "run:1:sha")
    plan = shards.read_json(plan_path)
    assert all(nodeid.startswith("tests/test_") for nodeid in plan["nodeids"])
    evidence = []
    for index in range(4):
        output = artifacts / f"shard-{index}.json"
        invoke("run", "--manifest", str(plan_path), "--output", str(output),
               "--run-key", "run:1:sha", "--shard", str(index))
        assert output.with_suffix(".xml").is_file()
        evidence.append(shards.read_json(output))
    assert shards.verify_all(plan, evidence, "run:1:sha", plan["commit"], NEEDS)["passed"] == 5


def test_workflow_keeps_required_gate_and_all_existing_checks():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    assert workflow.count("name: 后端测试（pytest + 迁移链验证）") == 1
    assert workflow.count("name: 前端类型检查 + 构建") == 1
    gate = workflow.split("\n  backend:\n")[1].split("\n  frontend:\n")[0]
    assert "needs: [backend_prepare, backend_shards]" in gate and "if: always()" in gate
    assert "pytest_shard.py verify" in gate and "toJSON(needs)" in gate
    matrix = workflow.split("\n  backend_shards:\n")[1].split("\n  backend:\n")[0]
    assert "shard: [0, 1, 2, 3]" in matrix and "fail-fast: false" in matrix
    assert "postgres:" in matrix and "install_test_cerbos.sh" in matrix
    assert "pytest_shard.py run" in matrix and "if-no-files-found: error" in matrix
    for token in ("alembic upgrade head", "alembic check", "alembic heads", "uvx pip-audit",
                  "npm run build", "npm run test", "npm run audit:prod"):
        assert token in workflow
    assert "continue-on-error" not in workflow and "|| true" not in workflow
    source = (ROOT / "scripts/pytest_shard.py").read_text()
    assert '"-m", "pytest", "-q"' in source
    assert '"--numprocesses"' not in source
    # Plugin is opt-in: normal pytest/conftest do not load it or consume its flags.
    assert "pytest_shard" not in (ROOT / "backend/tests/conftest.py").read_text()

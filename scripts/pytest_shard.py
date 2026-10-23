#!/usr/bin/env python3
"""Dependency-free pytest CI partitioning and fail-closed result verification.

Only the explicit plan/run commands load pytest_shard_plugin; ordinary pytest
and subprocess isolation probes keep their existing invocation and lifecycle.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = 1
SHARDS = 4
SKIP_POLICY = Path(__file__).with_name("pytest_shard_skips.json")


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def read_json(path):
    return json.loads(Path(path).read_text())


def commit():
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()


def partition(nodeids):
    if not nodeids or len(nodeids) != len(set(nodeids)):
        raise ValueError("collection is empty or contains duplicate nodeids")
    files = defaultdict(list)
    for nodeid in nodeids:
        if not isinstance(nodeid, str) or "::" not in nodeid:
            raise ValueError("invalid collected nodeid")
        files[nodeid.split("::", 1)[0]].append(nodeid)
    loads = [0] * SHARDS
    owners = {}
    # Largest file first, deterministic ties. Preserve original collection order
    # below, including module-scoped fixtures and parametrized test identities.
    for file in sorted(files, key=lambda file: (-len(files[file]), file)):
        shard = min(range(SHARDS), key=lambda index: (loads[index], index))
        owners[file] = shard
        loads[shard] += len(files[file])
    if not all(loads):
        raise ValueError("all four shards must contain tests")
    return [[nodeid for nodeid in nodeids if owners[nodeid.split("::", 1)[0]] == index]
            for index in range(SHARDS)]


def make_manifest(nodeids, run_key, source_commit):
    payload = {"schema": SCHEMA, "run_key": run_key, "commit": source_commit,
               "nodeids": nodeids, "shards": partition(nodeids),
               "allowed_skips": read_json(SKIP_POLICY)}
    return {**payload, "digest": digest(payload)}


def validate_manifest(manifest, run_key, source_commit):
    payload = {key: value for key, value in manifest.items() if key != "digest"}
    if manifest != make_manifest(manifest["nodeids"], run_key, source_commit):
        raise ValueError("manifest identity, partition, skip policy or checksum mismatch")
    if digest(payload) != manifest["digest"]:
        raise ValueError("manifest checksum mismatch")


def verify_needs(needs):
    expected = {"backend_prepare", "backend_shards"}
    if set(needs) != expected or any(needs[name].get("result") != "success" for name in expected):
        raise ValueError("every backend prerequisite and matrix shard must succeed")


def verify_report(manifest, report, shard):
    if type(shard) is not int or not 0 <= shard < SHARDS:
        raise ValueError("invalid shard index")
    for key, expected in (("schema", SCHEMA), ("run_key", manifest["run_key"]),
                          ("commit", manifest["commit"]), ("manifest_digest", manifest["digest"]),
                          ("shard", shard), ("selected", manifest["shards"][shard]),
                          ("collected", manifest["nodeids"]), ("pytest_exit", 0),
                          ("command_exit", 0), ("collection_errors", [])):
        if key not in report or report[key] != expected:
            raise ValueError(f"shard {shard}: missing or invalid {key}")
    reports = report.get("reports", [])
    by_test = defaultdict(list)
    for event in reports:
        if set(event) != {"nodeid", "when", "outcome", "wasxfail", "reason", "duration"}:
            raise ValueError("incomplete pytest report event")
        by_test[event["nodeid"]].append(event)
    if set(by_test) != set(report["selected"]):
        raise ValueError(f"shard {shard}: missing or extra executed tests")
    skipped = {}
    for nodeid, events in by_test.items():
        phases = [event["when"] for event in events]
        if phases not in (["setup", "call", "teardown"], ["setup", "teardown"]):
            raise ValueError(f"{nodeid}: missing, repeated or unordered pytest phases")
        if any(event.get("wasxfail") is not None for event in events):
            raise ValueError(f"{nodeid}: unexpected xfail/xpass")
        if any(event["outcome"] not in ("passed", "skipped") for event in events):
            raise ValueError(f"{nodeid}: failed pytest phase")
        skips = [event for event in events if event["outcome"] == "skipped"]
        if skips:
            if (len(skips) != 1 or skips[0]["when"] == "teardown"
                    or nodeid not in manifest["allowed_skips"]
                    or manifest["allowed_skips"].get(nodeid) != skips[0].get("reason")):
                raise ValueError(f"{nodeid}: unapproved skip or skip reason")
            if skips[0]["when"] == "setup" and phases != ["setup", "teardown"]:
                raise ValueError(f"{nodeid}: call after skipped setup")
            skipped[nodeid] = skips[0]["reason"]
        elif phases != ["setup", "call", "teardown"]:
            raise ValueError(f"{nodeid}: no successful test call")
    return skipped


def verify_all(manifest, reports, run_key, source_commit, needs):
    verify_needs(needs)
    validate_manifest(manifest, run_key, source_commit)
    if len(reports) != SHARDS or Counter(report.get("shard") for report in reports) != Counter(range(SHARDS)):
        raise ValueError("missing or duplicate shard report")
    skipped = {}
    executed = []
    for report in reports:
        skipped.update(verify_report(manifest, report, report["shard"]))
        executed.extend(report["selected"])
    if Counter(executed) != Counter(manifest["nodeids"]):
        raise ValueError("tests were not executed exactly once")
    return {"tests": len(executed), "shards": SHARDS, "skipped": skipped,
            "passed": len(executed) - len(skipped)}


def invoke(args):
    output = Path(args.output).resolve()
    if output.exists():
        raise ValueError("refusing to reuse existing output evidence")
    command = [sys.executable, "-m", "pytest", "-q", "-ra", "-p", "pytest_shard_plugin",
               "--rootdir", str(ROOT / "backend"), ".",
               "--ci-shard-mode", args.command, "--ci-shard-output", str(output),
               "--ci-shard-run-key", args.run_key, "--ci-shard-commit", commit()]
    if args.command == "plan":
        command.append("--collect-only")
    else:
        manifest_path = Path(args.manifest).resolve()
        manifest = read_json(manifest_path)
        validate_manifest(manifest, args.run_key, commit())
        command += ["--ci-shard-manifest", str(manifest_path), "--ci-shard-index", str(args.shard)]
        command += ["--junitxml", str(output.with_suffix(".xml")), "--durations=30"]
    env = os.environ.copy()
    if env.get("PYTEST_ADDOPTS") or env.get("PYTEST_PLUGINS"):
        raise ValueError("implicit pytest selection/plugins are not allowed in sharded CI")
    env["PYTHONPATH"] = os.pathsep.join([str(Path(__file__).parent), env.get("PYTHONPATH", "")])
    result = subprocess.run(command, cwd=ROOT / "backend", env=env, check=False)
    if not output.is_file():
        raise ValueError("pytest did not produce complete evidence")
    evidence = read_json(output)
    if args.command == "run":
        evidence["command_exit"] = result.returncode
        write_json(output, evidence)
        verify_report(manifest, evidence, args.shard)
    else:
        validate_manifest(evidence, args.run_key, commit())
    if result.returncode:
        raise ValueError(f"pytest exited {result.returncode}")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "run"):
        sub = commands.add_parser(name)
        sub.add_argument("--output", required=True)
        sub.add_argument("--run-key", required=True)
        if name == "run":
            sub.add_argument("--manifest", required=True)
            sub.add_argument("--shard", type=int, choices=range(SHARDS), required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--manifest", required=True)
    verify.add_argument("--reports", required=True)
    verify.add_argument("--run-key", required=True)
    verify.add_argument("--needs-env", required=True)
    args = parser.parse_args()
    try:
        if args.command in ("plan", "run"):
            return invoke(args)
        manifest = read_json(args.manifest)
        paths = sorted(Path(args.reports).rglob("shard-*.json"))
        summary = verify_all(manifest, [read_json(path) for path in paths], args.run_key,
                             commit(), json.loads(os.environ[args.needs_env]))
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as exc:
        print(f"CI shard verification failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Offline freeze gate for the cycle-pool cutover.

Replays one live snapshot (no production writes) as 19 checks, then mutates
that snapshot and the controller text. Every mutation must fail closed.
The controller's dry-run stage refuses to record PASS unless this report is
19/19 and the negative suite is ALL_OK. This file does not open SSH, Docker,
or Caddy.
"""
import copy
import hashlib
import json
from pathlib import Path
import sys

CHECKS = (
    "snapshot_metadata",
    "controller_digest",
    "manifest_validate",
    "route_shape",
    "route_terminal",
    "route_unknown_key",
    "conditional_patch_if_match",
    "patch_retries_412_without_load",
    "cutover_exposure_before_caddy",
    "adapt_same_origin",
    "curl_exchange",
    "etag_required",
    "mounts_normalized",
    "alembic_current_mergepoint",
    "alembic_heads_and_upgrade_head",
    "nginx_digest",
    "predecessor_owner_intact",
    "daily_backup_path",
    "receipt_chain",
)
DAILY_BACKUP_DIR = "/data/backups/spareparts"
LEGACY_BACKUP_DIR = "/var/backups/spareparts"


def _ops():
    import importlib.util
    path = Path(__file__).with_name("cycle_pool_ops.py")
    name = "cycle_pool_ops"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _cutover_source(controller_bytes):
    text = controller_bytes.decode()
    start = text.index("    def cutover(")
    end = text.index("\n    def ", start + 1)
    return text[start:end]


def _fail(errors, name, detail):
    errors.append({"check": name, "detail": detail})


def evaluate(snapshot, controller_bytes):
    """Return the 19-check report. Never raises for a bad snapshot."""
    ops = _ops()
    errors = []
    if not isinstance(snapshot, dict) or not isinstance(controller_bytes, (bytes, bytearray)):
        return {"checks": len(CHECKS), "passed": 0, "failed": len(CHECKS),
                "errors": [{"check": "snapshot_metadata", "detail": "snapshot or controller bytes missing"}]}
    controller_bytes = bytes(controller_bytes)
    digest = hashlib.sha256(controller_bytes).hexdigest()
    manifest = snapshot.get("manifest") if isinstance(snapshot.get("manifest"), dict) else {}
    files = snapshot.get("files") if isinstance(snapshot.get("files"), dict) else {}

    def check_snapshot_metadata():
        required = ("release", "captured_at", "controller_sha256", "manifest", "manifest_sha256", "files")
        if any(key not in snapshot for key in required):
            return "snapshot keys missing"
        if snapshot.get("release") != ops.APP_SHA or manifest.get("release") != ops.APP_SHA:
            return "snapshot release is not the frozen itsp commit"
        captured = snapshot.get("captured_at")
        if not isinstance(captured, str) or "T" not in captured:
            return "snapshot captured_at missing"
        if not isinstance(files, dict) or files.get("manifest.json") != snapshot.get("manifest_sha256"):
            return "manifest file hash is not bound"
        if not isinstance(snapshot.get("manifest_sha256"), str):
            return "manifest hash missing"
        return None

    def check_controller_digest():
        if digest != snapshot.get("controller_sha256") or digest != manifest.get("controller_sha256"):
            return "controller digest is not pinned to this snapshot"
        if digest != hashlib.sha256(Path(ops.__file__).read_bytes()).hexdigest():
            return "controller bytes are not the reviewed module"
        return None

    def check_manifest_validate():
        try:
            ops.validate(manifest)
        except Exception as exc:
            return "manifest rejected: " + str(exc)
        return None

    route = snapshot.get("route")
    dial = snapshot.get("route_dial")

    def check_route_shape():
        if not ops.route_is_main(route, dial):
            return "live route is not the singular main route"
        return None

    def check_route_terminal():
        if not isinstance(route, dict) or route.get("terminal") is not True:
            return "live route is missing terminal:true"
        if not ops.route_is_main(route, dial):
            return "terminal:true route was rejected"
        return None

    def check_route_unknown_key():
        extra = dict(route) if isinstance(route, dict) else {}
        extra["group"] = "g1"
        if ops.route_is_main(extra, dial):
            return "unknown route key was accepted"
        false_terminal = dict(route) if isinstance(route, dict) else {}
        false_terminal["terminal"] = False
        if ops.route_is_main(false_terminal, dial):
            return "terminal:false was accepted"
        return None

    def check_conditional_patch_if_match():
        body = _cutover_source(controller_bytes)
        if "If-Match" not in body or "conditional_dial_patch" not in body:
            return "cutover does not patch with the route ETag"
        return None

    def check_patch_retries_412_without_load():
        body = _cutover_source(controller_bytes)
        if '"/load"' in body or "load_active(" in body:
            return "cutover contains a full load"
        config = {"apps": {"http": {"servers": {"main": {"routes": [route]}}}}}
        calls = {"patch": 0, "load": 0}

        def request(method, path, payload=None, headers=None):
            if path == "/load" or method == "POST":
                calls["load"] += 1
                return 500, {}, b""
            if method == "GET" and path == "/config/":
                return 200, {}, json.dumps(config).encode()
            if method == "GET":
                etag = '"first"' if calls["patch"] == 0 else '"second"'
                return 200, {"etag": etag}, json.dumps(route).encode()
            if method == "PATCH":
                calls["patch"] += 1
                if headers.get("If-Match") != ('"first"' if calls["patch"] == 1 else '"second"'):
                    return 409, {}, b""
                if calls["patch"] == 1:
                    return 412, {}, b""
                updated = copy.deepcopy(route)
                ops.replace_one_dial(updated, dial, dial.replace("successor", "cycle"))
                config["apps"]["http"]["servers"]["main"]["routes"][0] = updated
                return 200, {}, b""
            return 500, {}, b""

        try:
            ops.conditional_dial_patch(request, dial, dial.replace("successor", "cycle"))
        except Exception as exc:
            return "412 retry failed: " + str(exc)
        if calls["load"] or calls["patch"] != 2:
            return "412 was not retried or a load was issued"
        fresh = {"apps": {"http": {"servers": {"main": {"routes": [copy.deepcopy(route)]}}}}}

        def always_412(method, path, payload=None, headers=None):
            if method == "GET" and path == "/config/":
                return 200, {}, json.dumps(fresh).encode()
            if method == "GET":
                return 200, {"etag": '"stuck"'}, json.dumps(route).encode()
            if method == "PATCH":
                return 412, {}, b""
            return 500, {}, b""

        try:
            ops.conditional_dial_patch(always_412, dial, dial.replace("successor", "cycle"))
        except RuntimeError as exc:
            if "full load refused" not in str(exc):
                return "exhausted 412 did not refuse full load"
        else:
            return "exhausted 412 was accepted"
        return None

    def check_cutover_exposure_before_caddy():
        body = _cutover_source(controller_bytes)
        try:
            if body.index("exposure-started.json") > body.index("commit_caddyfile_dial"):
                return "exposure marker is not written before the Caddy edit"
        except ValueError:
            return "cutover is missing the exposure marker or the inode write"
        if "exposure-started.json" not in body:
            return "exposure marker missing"
        return None

    def check_adapt_same_origin():
        body = _cutover_source(controller_bytes)
        if "replace_active(baseline_adapted, old_dial, new_dial)" not in body:
            return "cutover does not compare same-origin adaptations"
        if "adapt_caddyfile_bytes(updated) == applied" in body:
            return "cutover mixes stdin adapt with the live config export"
        return None

    def check_curl_exchange():
        raw = b"HTTP/1.1 412 Precondition Failed\r\nETag: \"abc\"\r\n\r\n{}\n__STATUS__412"
        try:
            status, headers, body = ops.parse_curl_exchange(raw)
        except Exception as exc:
            return "412 exchange rejected: " + str(exc)
        if status != 412 or headers.get("etag") != '"abc"' or body != b"{}":
            return "412 exchange parsed incorrectly"
        return None

    def check_etag_required():
        try:
            ops.header_etag({})
        except RuntimeError:
            return None
        return "missing ETag was accepted"

    def check_mounts_normalized():
        left = snapshot.get("mounts_a")
        right = snapshot.get("mounts_b")
        if not isinstance(left, list) or not isinstance(right, list) or len(left) < 2 or left == right:
            return "snapshot did not capture two mount orders"
        base = {"Config": {"Image": "sha256:abc"}, "HostConfig": {"NetworkMode": "it-spareparts_default"}}
        if ops.Release.config({**base, "Mounts": left}) != ops.Release.config({**base, "Mounts": right}):
            return "mount order was treated as drift"
        drifted = copy.deepcopy(left)
        drifted[0] = dict(drifted[0], Source=drifted[0].get("Source", "") + "-drift")
        if ops.Release.config({**base, "Mounts": left}) == ops.Release.config({**base, "Mounts": drifted}):
            return "mount source drift was ignored"
        return None

    def check_alembic_current_mergepoint():
        current = snapshot.get("alembic_current") if isinstance(snapshot.get("alembic_current"), dict) else {}
        if current.get("code") != 0:
            return "alembic current exit code is not 0"
        allowed = {ops.OLD_SCHEMA + " (head)", ops.OLD_SCHEMA + " (head) (mergepoint)"}
        lines = [line.strip() for line in str(current.get("stdout", "")).splitlines() if line.strip()]
        if len(lines) != 1 or lines[0] not in allowed:
            return "alembic current is not 98af with an optional mergepoint"
        return None

    def check_alembic_heads_and_upgrade_head():
        heads = snapshot.get("alembic_heads") if isinstance(snapshot.get("alembic_heads"), dict) else {}
        if heads.get("code") != 0:
            return "alembic heads exit code is not 0"
        lines = [line.strip() for line in str(heads.get("stdout", "")).splitlines() if line.strip()]
        if lines != [ops.TARGET + " (head)"]:
            return "candidate heads are not exactly c3e8a1d94b27 (head)"
        if snapshot.get("planned_upgrade") != ["upgrade", "head"]:
            return "planned command is not alembic upgrade head"
        if snapshot.get("upgrade_path") != [ops.OLD_SCHEMA, ops.MID_SCHEMA, ops.TARGET]:
            return "upgrade path is not 98af -> 86f7 -> c3e8"
        if "downgrade" in json.dumps(snapshot.get("planned_upgrade")):
            return "downgrade is planned"
        return None

    def check_nginx_digest():
        if snapshot.get("nginx_sha256") != manifest.get("nginx_sha256"):
            return "nginx digest drifted"
        if files.get("nginx") != manifest.get("nginx_sha256"):
            return "nginx file hash drifted"
        return None

    def check_predecessor_owner_intact():
        owner = snapshot.get("predecessor") if isinstance(snapshot.get("predecessor"), dict) else {}
        if owner.get("path") != ".requirements-successor-active-owner.json":
            return "predecessor owner path changed"
        if owner.get("release") != ops.OLD_SHA or owner.get("rewritten") is not False:
            return "successor owner was rewritten"
        if files.get("predecessor_owner") != manifest.get("predecessor_owner_sha256"):
            return "predecessor owner hash drifted"
        return None

    def check_daily_backup_path():
        if snapshot.get("daily_backup_dir") != DAILY_BACKUP_DIR:
            return "daily backup dir is not /data/backups/spareparts"
        if snapshot.get("daily_backup_dir") == LEGACY_BACKUP_DIR or snapshot.get("legacy_backup_dir_used") is True:
            return "legacy /var/backups/spareparts path was used"
        return None

    def check_receipt_chain():
        receipts = snapshot.get("receipts") if isinstance(snapshot.get("receipts"), dict) else {}
        for phase in ("preflight", "images"):
            row = receipts.get(phase) if isinstance(receipts.get(phase), dict) else {}
            if row.get("status") != "PASS" or row.get("release") != ops.APP_SHA:
                return phase + " receipt is not bound to this release"
            if row.get("manifest_sha256") != snapshot.get("manifest_sha256"):
                return phase + " receipt manifest hash drifted"
        if receipts.get("preflight", {}).get("schema") != ops.OLD_SCHEMA:
            return "preflight schema is not 98af"
        if snapshot.get("environment_file_labels") != []:
            return "environment_file label is present"
        return None

    runners = (
        check_snapshot_metadata, check_controller_digest, check_manifest_validate, check_route_shape,
        check_route_terminal, check_route_unknown_key, check_conditional_patch_if_match,
        check_patch_retries_412_without_load, check_cutover_exposure_before_caddy, check_adapt_same_origin,
        check_curl_exchange, check_etag_required, check_mounts_normalized, check_alembic_current_mergepoint,
        check_alembic_heads_and_upgrade_head, check_nginx_digest, check_predecessor_owner_intact,
        check_daily_backup_path, check_receipt_chain,
    )
    if len(runners) != len(CHECKS):
        return {"checks": len(CHECKS), "passed": 0, "failed": len(CHECKS),
                "errors": [{"check": "snapshot_metadata", "detail": "check table drifted"}]}
    for name, runner in zip(CHECKS, runners):
        try:
            detail = runner()
        except Exception as exc:
            detail = str(exc) or exc.__class__.__name__
        if detail:
            _fail(errors, name, detail)
    passed = len(CHECKS) - len(errors)
    return {"checks": len(CHECKS), "passed": passed, "failed": len(errors), "errors": errors}


def _mutate(snapshot, path, value):
    cloned = copy.deepcopy(snapshot)
    cursor = cloned
    for key in path[:-1]:
        cursor = cursor[key]
    cursor[path[-1]] = value
    return cloned


def negative_report(snapshot, controller_bytes):
    """Each case must make evaluate fail closed. ALL_OK only when every one does."""
    cases = []

    def add(name, mutated, text=None):
        report = evaluate(mutated, controller_bytes if text is None else text)
        cases.append({"name": name, "refused": report["passed"] != len(CHECKS) or report["failed"] != 0})

    add("controller_digest", _mutate(snapshot, ("controller_sha256",), "0" * 64))
    add("manifest_release", _mutate(snapshot, ("manifest", "release"), "b2650202c2adb9c40c8fef4663e3472963ef534a"))
    add("route_terminal_false", _mutate(snapshot, ("route", "terminal"), False))
    add("route_unknown_key", _mutate(snapshot, ("route", "group"), "g1"))
    bad_match = copy.deepcopy(snapshot)
    bad_match["route"]["match"] = [{"path": ["/"]}]
    add("route_without_host", bad_match)
    add("heads_exit", _mutate(snapshot, ("alembic_heads", "code"), 2))
    add("heads_still_98af", _mutate(snapshot, ("alembic_heads", "stdout"), "98afc0d2157b (head)"))
    add("heads_multiple", _mutate(snapshot, ("alembic_heads", "stdout"),
                                  "c3e8a1d94b27 (head)\n86f7486b9ed5 (head)"))
    add("current_already_target", _mutate(snapshot, ("alembic_current", "stdout"), "c3e8a1d94b27 (head)"))
    add("current_malformed", _mutate(snapshot, ("alembic_current", "stdout"),
                                      "98afc0d2157b (head) (mergepoint) extra"))
    drifted = copy.deepcopy(snapshot)
    drifted["mounts_b"] = [dict(item) for item in drifted["mounts_b"]]
    drifted["mounts_b"][0] = dict(drifted["mounts_b"][0], Source="/drift")
    add("mount_source_drift", drifted)
    add("nginx_mismatch", _mutate(snapshot, ("nginx_sha256",), "ab" * 32))
    add("predecessor_release", _mutate(snapshot, ("predecessor", "release"), "7e5f15e5" + "a" * 32))
    add("predecessor_path", _mutate(snapshot, ("predecessor", "path"), ".cycle-pool-active-owner.json"))
    add("legacy_backup_dir", _mutate(snapshot, ("daily_backup_dir",), LEGACY_BACKUP_DIR))
    add("planned_downgrade", _mutate(snapshot, ("planned_upgrade",), ["downgrade", "98afc0d2157b"]))
    add("planned_upgrade_revision", _mutate(snapshot, ("planned_upgrade",), ["upgrade", "c3e8a1d94b27"]))
    add("environment_file_label", _mutate(snapshot, ("environment_file_labels",), ["environment_file"]))
    add("preflight_schema", _mutate(snapshot, ("receipts", "preflight", "schema"), "c3e8a1d94b27"))
    add("missing_captured_at", _mutate(snapshot, ("captured_at",), ""))
    add("manifest_file_hash", _mutate(snapshot, ("files", "manifest.json"), "cd" * 32))
    tampered = controller_bytes.replace(
        b"h.save(self.root / \"exposure-started.json\"",
        b"self.load_active({})\n        h.save(self.root / \"exposure-started.json\"",
        1,
    )
    add("cutover_load_before_exposure", snapshot, tampered)
    refused = sum(1 for case in cases if case["refused"])
    return {
        "count": len(cases),
        "refused": refused,
        "status": "ALL_OK" if refused == len(cases) and cases else "FAILED",
        "cases": cases,
    }


def main(argv):
    if len(argv) != 3:
        print("usage: cycle_pool_dry_run.py SNAPSHOT.json CONTROLLER.py", file=sys.stderr)
        return 2
    snapshot = json.loads(Path(argv[1]).read_text())
    controller = Path(argv[2]).read_bytes()
    report = evaluate(snapshot, controller)
    negatives = negative_report(snapshot, controller)
    print(json.dumps({"checks_passed": report["passed"], "checks_failed": report["failed"],
                      "errors": report["errors"], "negatives": negatives["status"],
                      "negative_count": negatives["count"]}, ensure_ascii=False, sort_keys=True))
    return 0 if report["passed"] == len(CHECKS) and report["failed"] == 0 and negatives["status"] == "ALL_OK" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

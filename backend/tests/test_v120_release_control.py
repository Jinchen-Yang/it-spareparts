from __future__ import annotations

import base64
import grp
import hashlib
import importlib.util
import io
import json
import os
import pwd
import re
import shutil
import signal
import stat
import subprocess
import threading
import time
import textwrap
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / ".deploy" / "build_v120.sh"
RELEASE = ROOT / ".deploy" / "release_v120.sh"
ROLLBACK = ROOT / ".deploy" / "rollback_v120.sh"
OBSERVE = ROOT / ".deploy" / "observe_v120.sh"
STATE_LIBRARY = ROOT / ".deploy" / "v120_state.sh"
ROOT_SYNC = ROOT / ".deploy" / "sync_v120_root_state.sh"
INSTALL_CONTROL = ROOT / ".deploy" / "install_v120_control.sh"
PACKAGE_CONTROL = ROOT / ".deploy" / "package_v120_control.sh"
CRON_SPEC = ROOT / ".deploy" / "it-spareparts.cron"
DEPLOY_GUIDE = ROOT / "docs" / "DEPLOY.md"
RELEASE_RUNBOOK = ROOT / "docs" / "releases" / "v1.20-release-runbook.md"
BACKEND_DOCKERFILE = ROOT / "backend" / "Dockerfile"
FRONTEND_DOCKERFILE = ROOT / "frontend" / "Dockerfile"
BACKEND_REQUIREMENTS_LOCK = ROOT / "backend" / "requirements.lock"
BACKEND_UV_LOCK = ROOT / "backend" / "uv.lock"
FRONTEND_PACKAGE_LOCK = ROOT / "frontend" / "package-lock.json"
SBOM_GENERATOR = ROOT / ".deploy" / "generate_dependency_sbom.py"
BACKEND_SBOM = ROOT / "backend" / "dependency-sbom.cdx.json"
FRONTEND_SBOM = ROOT / "frontend" / "dependency-sbom.cdx.json"
ARTIFACT_VALIDATOR = ROOT / ".deploy" / "validate_release_artifacts.py"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
MOBILE_RELEASE_PROBE = ROOT / ".deploy" / "mobile_release_probe.mjs"
DEFAULT_TARGET = "a" * 40
DEFAULT_RELEASE_ID = "v120-aaaaaaaaaaaa-20260730160000"
ZERO_HASH = "0" * 64
MOBILE_TOKEN_SENTINEL = "fixture-token-must-not-reach-any-output"


def _test_node() -> str:
    node = shutil.which("node")
    assert node is not None, (
        "Node.js must be available on PATH for mobile probe tests"
    )
    return node


def _script(path: Path) -> str:
    assert path.is_file()
    assert path.stat().st_mode & stat.S_IXUSR
    subprocess.run(["bash", "-n", str(path)], check=True)
    return path.read_text(encoding="utf-8")


def _built_values(
    *,
    target: str = DEFAULT_TARGET,
    release_id: str | None = None,
    attempt_no: int = 1,
    parent_release_id: str | None = None,
    parent_state_hash: str | None = None,
    rollback_policy: str = "old_allowed",
    old_running_source_commit: str = (
        "a1cf00910f08da7f27a9e6e0faaacc3a3cce9bab"
    ),
    old_app_image_id: str = "sha256:" + "b" * 64,
    old_frontend_image_id: str = "sha256:" + "c" * 64,
) -> dict[str, str]:
    release_id = release_id or (
        f"v120-{target[:12]}-20260730160000"
    )
    if parent_release_id is None:
        parent_release_id = (
            "none"
            if attempt_no == 1
            else "v120-cccccccccccc-20260730150000"
        )
    if parent_state_hash is None:
        parent_state_hash = ZERO_HASH if attempt_no == 1 else "9" * 64
    return {
        "STATE_FORMAT": "v120-1",
        "STATE_GENERATION": "0",
        "ATTEMPT_NO": str(attempt_no),
        "RELEASE_ID": release_id,
        "PARENT_RELEASE_ID": parent_release_id,
        "PARENT_STATE_HASH": parent_state_hash,
        "ROLLBACK_POLICY": rollback_policy,
        "TARGET_COMMIT": target,
        "OLD_COMMIT": "ab42005b5b94bf98b3db0e4bff87e5df9da2f7ca",
        "OLD_RUNNING_SOURCE_COMMIT": old_running_source_commit,
        "DB_HEAD": "f1c8e4a7b2d9",
        "OLD_APP_IMAGE_ID": old_app_image_id,
        "OLD_FRONTEND_IMAGE_ID": old_frontend_image_id,
        "APP_IMAGE_REF": "it-spareparts-app",
        "FRONTEND_IMAGE_REF": "it-spareparts-frontend",
        "OLD_APP_ROLLBACK_TAG": (
            f"it-spareparts-release/app:rollback-{release_id}"
        ),
        "OLD_FRONTEND_ROLLBACK_TAG": (
            f"it-spareparts-release/frontend:rollback-{release_id}"
        ),
        "NEW_APP_IMAGE_ID": "sha256:" + "d" * 64,
        "NEW_FRONTEND_IMAGE_ID": "sha256:" + "e" * 64,
        "NEW_APP_CANDIDATE_TAG": (
            f"it-spareparts-release/app:candidate-{release_id}"
        ),
        "NEW_FRONTEND_CANDIDATE_TAG": (
            f"it-spareparts-release/frontend:candidate-{release_id}"
        ),
        "SOURCE_TAR": (
            "/home/ubuntu/apps/it-spareparts/backups/"
            f"{release_id}-source.tar"
        ),
        "SOURCE_SUM": (
            "/home/ubuntu/apps/it-spareparts/backups/"
            f"{release_id}-source.tar.sha256"
        ),
        "SOURCE_HASH": "f" * 64,
        "CONTROL_MANIFEST_HASH": "2" * 64,
        "RELEASE_PHASE": "built",
        "APP_COMPOSE_HASH": "1" * 64,
    }


def _render_state(values: dict[str, str]) -> str:
    return "".join(f"{key}={value}\n" for key, value in values.items())


def _built_state(**kwargs) -> str:
    return _render_state(_built_values(**kwargs))


def _phase_values(
    phase: str,
    *,
    rollback_policy: str | None = None,
    attempt_no: int | None = None,
    target: str = DEFAULT_TARGET,
    release_id: str | None = None,
    parent_release_id: str | None = None,
    parent_state_hash: str | None = None,
) -> dict[str, str]:
    if rollback_policy is None:
        rollback_policy = (
            "forward_only"
            if phase in {"opening", "switched", "observed", "failed_closed"}
            else "old_allowed"
        )
    if attempt_no is None:
        attempt_no = 2 if rollback_policy == "forward_only" else 1
    values = _built_values(
        target=target,
        release_id=release_id,
        attempt_no=attempt_no,
        parent_release_id=parent_release_id,
        parent_state_hash=parent_state_hash,
        rollback_policy=rollback_policy,
    )
    generations = {
        "built": 0,
        "prepared": 1,
        "backup_verified": 2,
        "opening": 3,
        "switched": 4,
        "observed": 5,
        "failed_closed": 3,
        "rolled_back": 3,
    }
    values["STATE_GENERATION"] = str(generations[phase])
    values["RELEASE_PHASE"] = phase
    if phase == "built":
        return values

    evidence = (
        "/home/ubuntu/apps/it-spareparts/backups/"
        f"{values['RELEASE_ID']}-release"
    )
    values.update(
        {
            "BASE_DB_CID": "2" * 64,
            "BASE_DB_IMAGE_ID": "sha256:" + "3" * 64,
            "BASE_EDGE_CID": "4" * 64,
            "BASE_DB_RESTARTS": "0",
            "BASE_EDGE_RESTARTS": "0",
            "EDGE_CADDY_HASH": "5" * 64,
            "EDGE_COMPOSE_HASH": "6" * 64,
            "IMAGE_BUNDLE": f"{evidence}/images.tar",
            "IMAGE_BUNDLE_HASH": "7" * 64,
            "EVIDENCE_DIR": evidence,
        }
    )
    if phase in {
        "backup_verified",
        "opening",
        "switched",
        "observed",
    }:
        values.update(
            {
                "BACKUP": "/var/backups/spareparts/db-20260730-1600.dump",
                "BACKUP_HASH": "8" * 64,
            }
        )
    if phase in {"opening", "switched", "observed"}:
        values.update(
            {
                "NEW_APP_CID": "9" * 64,
                "PUBLIC_OPENED_AT": "2026-07-30T16:05:00+08:00",
            }
        )
    if phase in {"switched", "observed"}:
        values.update(
            {
                "NEW_FRONTEND_CID": "a" * 64,
                "MONITOR_SWITCH_MTIME": "1722330000",
                "SWITCHED_AT": "2026-07-30T16:10:00+08:00",
            }
        )
    if phase == "observed":
        values["OBSERVED_AT"] = "2026-07-30T16:40:00+08:00"
    elif phase == "failed_closed":
        values["FAILED_AT"] = "2026-07-30T16:15:00+08:00"
    elif phase == "rolled_back":
        values["ROLLED_BACK_AT"] = "2026-07-30T16:15:00+08:00"
    return values


def _phase_state(phase: str, **kwargs) -> str:
    return _render_state(_phase_values(phase, **kwargs))


def _prepared_update_args() -> list[str]:
    release_id = DEFAULT_RELEASE_ID
    evidence = (
        f"/home/ubuntu/apps/it-spareparts/backups/{release_id}-release"
    )
    return [
        "BASE_DB_CID",
        "2" * 64,
        "BASE_DB_IMAGE_ID",
        "sha256:" + "3" * 64,
        "BASE_EDGE_CID",
        "4" * 64,
        "BASE_DB_RESTARTS",
        "0",
        "BASE_EDGE_RESTARTS",
        "0",
        "EDGE_CADDY_HASH",
        "5" * 64,
        "EDGE_COMPOSE_HASH",
        "6" * 64,
        "IMAGE_BUNDLE",
        f"{evidence}/images.tar",
        "IMAGE_BUNDLE_HASH",
        "7" * 64,
        "EVIDENCE_DIR",
        evidence,
        "RELEASE_PHASE",
        "prepared",
    ]


def _run_state(
    state: Path, body: str, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    command = f'source "$1"; shift; {body}'
    return subprocess.run(
        ["bash", "-c", command, "bash", str(STATE_LIBRARY), str(state), *args],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def _run_release_library(
    body: str,
    *args: str,
    env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "V120_STATE_TEST_MODE": "1",
            "V120_RELEASE_LIBRARY_ONLY": "1",
        }
    )
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        ["bash", "-c", f'source "$1"; shift; {body}', "bash", str(RELEASE), *args],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def _run_observer_library(
    body: str,
    *args: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    command = f'source "$1"; shift; {body}'
    test_env = os.environ.copy()
    test_env.update(
        {
            "V120_STATE_TEST_MODE": "1",
            "V120_OBSERVER_LIBRARY_ONLY": "1",
        }
    )
    if env:
        test_env.update(env)
    return subprocess.run(
        ["bash", "-c", command, "bash", str(OBSERVE), *args],
        text=True,
        capture_output=True,
        env=test_env,
        check=False,
    )


def _run_installer_library(
    body: str,
    *args: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    command = f'source "$1"; shift; {body}'
    test_env = os.environ.copy()
    test_env.update(
        {
            "V120_STATE_TEST_MODE": "1",
            "V120_INSTALLER_LIBRARY_ONLY": "1",
        }
    )
    if env:
        test_env.update(env)
    return subprocess.run(
        ["bash", "-c", command, "bash", str(INSTALL_CONTROL), *args],
        text=True,
        capture_output=True,
        env=test_env,
        check=False,
    )


def _make_v3_control_version(versions: Path) -> tuple[Path, str, str]:
    target = "4" * 40
    names_and_keys = (
        ("v120_state.sh", "V120_STATE_SHA256", b"#!/bin/bash\ntrue\n"),
        (
            "sync-v120-root-state.sh",
            "ROOT_SYNC_SHA256",
            b"#!/bin/bash\ntrue\n",
        ),
        ("rollback-v120.sh", "ROLLBACK_SHA256", b"#!/bin/bash\ntrue\n"),
        (
            "install-v120-control.sh",
            "INSTALLER_SHA256",
            b"#!/bin/bash\ntrue\n",
        ),
        ("hsts-v120-root.sh", "HSTS_ROOT_SHA256", b"#!/bin/bash\ntrue\n"),
        (
            "hsts-v120-operator.sh",
            "HSTS_OPERATOR_SHA256",
            b"#!/bin/bash\ntrue\n",
        ),
        ("edge-v120-root.sh", "EDGE_ROOT_SHA256", b"#!/bin/bash\ntrue\n"),
        (
            "edge-v120-operator.sh",
            "EDGE_OPERATOR_SHA256",
            b"#!/bin/bash\ntrue\n",
        ),
        ("it-spareparts.cron", "CRON_SHA256", b"SHELL=/bin/sh\n"),
        ("source.tar", "SOURCE_TAR_SHA256", b"trusted-source\n"),
    )
    staging = versions / "staging"
    staging.mkdir(mode=0o700)
    lines = ["CONTROL_FORMAT=v120-control-3", f"TARGET_COMMIT={target}"]
    for name, key, content in names_and_keys:
        artifact = staging / name
        artifact.write_bytes(content)
        artifact.chmod(0o700 if name.endswith(".sh") else 0o600)
        lines.append(f"{key}={hashlib.sha256(content).hexdigest()}")
    for key in (
        "BACKEND_REQUIREMENTS_SHA256",
        "BACKEND_UV_LOCK_SHA256",
        "FRONTEND_PACKAGE_LOCK_SHA256",
        "BACKEND_SBOM_SHA256",
        "FRONTEND_SBOM_SHA256",
        "BACKEND_BASE_DIGEST",
        "FRONTEND_BUILD_BASE_DIGEST",
        "FRONTEND_RUNTIME_BASE_DIGEST",
    ):
        lines.append(f"{key}={hashlib.sha256(key.encode()).hexdigest()}")
    manifest = staging / "manifest.txt"
    manifest.write_text("\n".join(lines) + "\n", encoding="ascii")
    manifest.chmod(0o600)
    manifest_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
    final = versions / manifest_hash
    staging.rename(final)
    return final, manifest_hash, target


def _semantic_health_stub_body(function_name: str) -> str:
    return rf'''
curl() {{
  local target="${{@: -1}}"
  if [ "$target" = https://hbzgc.icu/ ]; then
    if [[ " $* " == *"%{{remote_ip}}"* ]]; then
      printf '200 118.25.94.90 0'
    else
      printf '200 0'
    fi
  elif [[ "$target" == http://hbzgc.icu/* ]]; then
    suffix=${{target#http://hbzgc.icu}}
    printf '308 https://hbzgc.icu%s' "$suffix"
  elif [ "$target" = https://118.25.94.90/health ]; then
    case "${{TEST_ASSISTANT_HEALTH:-ready}}" in
      ready) printf '{{"status":"ready"}}\n200\napplication/json' ;;
      html) printf '<!doctype html>assistant\n200\ntext/html' ;;
      mime) printf '{{"status":"ready"}}\n200\ntext/plain' ;;
      ok) printf '{{"status":"ok"}}\n200\napplication/json' ;;
      missing)
        printf '{{"service":"assistant"}}\n200\napplication/json' ;;
      malformed) printf '{{bad\n200\napplication/json' ;;
      http-error) return 22 ;;
      *) return 97 ;;
    esac
  elif [ "${{TEST_HEALTH_HTML:-0}}" = 1 ]; then
    printf '<!doctype html>SPA\n200\ntext/html'
  elif [[ "$target" == */health/db ]]; then
    printf '{{"status":"ok","db":"reachable"}}\n200\napplication/json'
  else
    printf '{{"status":"ok"}}\n200\napplication/json'
  fi
}}
{function_name}
'''


def test_release_external_readiness_rejects_spa_fake_200() -> None:
    accepted = _run_release_library(
        _semantic_health_stub_body("check_candidate_external_readiness")
    )
    rejected = _run_release_library(
        _semantic_health_stub_body("check_candidate_external_readiness"),
        env_overrides={"TEST_HEALTH_HTML": "1"},
    )

    assert accepted.returncode == 0, accepted.stderr
    assert rejected.returncode != 0


@pytest.mark.parametrize(
    ("mode", "accepted"),
    (
        ("ready", True),
        ("html", False),
        ("mime", False),
        ("ok", False),
        ("missing", False),
        ("malformed", False),
        ("http-error", False),
    ),
)
def test_release_candidate_requires_semantic_assistant_ready_health(
    mode: str,
    accepted: bool,
) -> None:
    result = _run_release_library(
        _semantic_health_stub_body(
            "check_candidate_external_readiness "
            "&& printf CANDIDATE_READY"
        ),
        env_overrides={"TEST_ASSISTANT_HEALTH": mode},
    )

    assert (result.returncode == 0) is accepted, result.stderr
    assert ("CANDIDATE_READY" in result.stdout) is accepted


def test_old_business_rollback_readiness_does_not_require_new_health_contract() -> None:
    body = r'''
curl() {
  local target="${@: -1}"
  if [[ "$target" == https://* ]]; then
    printf '200 0'
  else
    return 0
  fi
}
check_external_health_semantics() { return 99; }
check_rollback_frontend_readiness
'''

    result = _run_release_library(body)

    assert result.returncode == 0, result.stderr
    release = RELEASE.read_text(encoding="utf-8")
    restore = release[
        release.index("restore_old_business_inline()") :
        release.index("fail_closed_from_root()")
    ]
    assert "check_rollback_frontend_readiness" in restore
    assert "check_candidate_external_readiness" not in restore
    switch = release[release.index('advance_state \\\n  BACKUP "$BACKUP"') :]
    assert "check_candidate_external_readiness" in switch


def test_observer_external_health_rejects_spa_fake_200() -> None:
    accepted = _run_observer_library(
        _semantic_health_stub_body("check_external_health_semantics")
    )
    rejected = _run_observer_library(
        _semantic_health_stub_body("check_external_health_semantics"),
        env={"TEST_HEALTH_HTML": "1"},
    )

    assert accepted.returncode == 0, accepted.stderr
    assert rejected.returncode != 0


@pytest.mark.parametrize(
    ("mode", "accepted"),
    (
        ("ready", True),
        ("html", False),
        ("mime", False),
        ("ok", False),
        ("missing", False),
        ("malformed", False),
        ("http-error", False),
    ),
)
def test_observer_requires_semantic_assistant_ready_health(
    mode: str,
    accepted: bool,
) -> None:
    result = _run_observer_library(
        _semantic_health_stub_body(
            'probe_json_health "$ASSISTANT_HEALTH_URL" ready assistant'
        ),
        env={"TEST_ASSISTANT_HEALTH": mode},
    )

    assert (result.returncode == 0) is accepted, result.stderr


def test_observer_checks_assistant_health_inside_every_observation() -> None:
    source = OBSERVE.read_text(encoding="utf-8")
    observe_body = source[
        source.index("observe() {") : source.index(
            'if [ "${V120_OBSERVER_LIBRARY_ONLY:-0}" = 1 ]'
        )
    ]

    assert (
        'probe_json_health "$ASSISTANT_HEALTH_URL" ready assistant'
        in observe_body
    )


@pytest.mark.parametrize("minute", (0, 5, 15, 30))
@pytest.mark.parametrize(
    ("mode", "accepted"),
    (
        ("ready", True),
        ("html", False),
        ("mime", False),
        ("ok", False),
        ("missing", False),
        ("malformed", False),
    ),
)
def test_each_observation_window_gates_pass_on_assistant_health(
    tmp_path: Path,
    minute: int,
    mode: str,
    accepted: bool,
) -> None:
    evidence_dir = tmp_path / f"evidence-{minute}-{mode}"
    evidence_dir.mkdir()
    body = _semantic_health_stub_body("") + r'''
EVIDENCE_DIR=$1
minute=$2
NEW_APP_CID=app-cid
NEW_FRONTEND_CID=frontend-cid
NEW_APP_IMAGE_ID=app-image
NEW_FRONTEND_IMAGE_ID=frontend-image
BASE_DB_CID=db-cid
BASE_DB_IMAGE_ID=db-image
BASE_DB_RESTARTS=0
BASE_EDGE_CID=edge-cid
BASE_EDGE_RESTARTS=0
EDGE_CADDY_HASH=caddy-hash
EDGE_COMPOSE_HASH=compose-hash
MONITOR_SWITCH_MTIME=1
LAST_MONITOR_MTIME=1
SWITCHED_AT=2026-07-31T00:00:00+08:00
compose() {
  case "$*" in
    "ps -q app") printf '%s\n' "$NEW_APP_CID" ;;
    "ps -q frontend") printf '%s\n' "$NEW_FRONTEND_CID" ;;
    "ps -q db") printf '%s\n' "$BASE_DB_CID" ;;
    "port frontend 80") printf '127.0.0.1:8080\n' ;;
    "logs --since "*) return 0 ;;
    *) return 97 ;;
  esac
}
sudo() {
  local rendered=$*
  local target="${@: -1}"
  if [[ "$rendered" == *"docker inspect -f {{.Image}}"* ]]; then
    case "$target" in
      "$NEW_APP_CID") printf '%s\n' "$NEW_APP_IMAGE_ID" ;;
      "$NEW_FRONTEND_CID") printf '%s\n' "$NEW_FRONTEND_IMAGE_ID" ;;
      "$BASE_DB_CID") printf '%s\n' "$BASE_DB_IMAGE_ID" ;;
      *) return 97 ;;
    esac
  elif [[ "$rendered" == *"docker inspect -f {{.RestartCount}}"* ]]; then
    printf '0\n'
  elif [[ "$rendered" == *"docker ps -q --no-trunc"* ]]; then
    printf '%s\n' "$BASE_EDGE_CID"
  elif [[ "$rendered" == *"it-spareparts-ingress"* ]]; then
    printf 'yes\n'
  elif [[ "$rendered" == *"sha256sum $EDGE_CADDYFILE"* ]]; then
    printf '%s  %s\n' "$EDGE_CADDY_HASH" "$EDGE_CADDYFILE"
  elif [[ "$rendered" == *"sha256sum $EDGE_COMPOSE"* ]]; then
    printf '%s  %s\n' "$EDGE_COMPOSE_HASH" "$EDGE_COMPOSE"
  else
    return 97
  fi
}
check_compose_identity() { return 0; }
check_loopback_8080() { return 0; }
check_internal_health() { return 0; }
check_external_health_semantics() { return 0; }
capture_cron_journal() { : > "$1"; }
systemctl() { printf 'active\n'; }
stat() { printf '1\n'; }
grep() {
  [[ "$*" == *"ok=Y"* ]]
}
observe "$minute" 0
'''
    result = _run_observer_library(
        body,
        str(evidence_dir),
        str(minute),
        env={"TEST_ASSISTANT_HEALTH": mode},
    )
    evidence = evidence_dir / f"observe-{minute}m.txt"

    assert (result.returncode == 0) is accepted, result.stderr
    assert ("OBSERVE_OK" in result.stdout) is accepted
    assert evidence.exists() is accepted
    if accepted:
        assert "result=PASS" in evidence.read_text(encoding="utf-8")
    else:
        assert not list(evidence_dir.glob("observe-*m.txt"))


def test_observer_preflights_noninteractive_root_journal_access(
    tmp_path: Path,
) -> None:
    call_log = tmp_path / "sudo-call.txt"
    env = {"TEST_SUDO_CALL_LOG": str(call_log)}
    body = r'''
sudo() {
  printf '%s\n' "$*" > "$TEST_SUDO_CALL_LOG"
  [ "$1" = -n ] || return 97
  shift
  "$@"
}
journalctl() {
  [ "$1" = -u ] && [ "$2" = cron ]
}
SWITCHED_AT=2026-07-30T16:10:00+08:00
preflight_cron_journal
'''

    result = _run_observer_library(body, env=env)

    assert result.returncode == 0, result.stderr
    assert call_log.read_text(encoding="utf-8").startswith(
        "-n journalctl -u cron --since "
    )


def test_observer_rejects_unavailable_root_journal_access() -> None:
    body = r'''
sudo() {
  return 42
}
SWITCHED_AT=2026-07-30T16:10:00+08:00
preflight_cron_journal
'''

    result = _run_observer_library(body)

    assert result.returncode == 42


@pytest.mark.parametrize(
    "journal_line",
    [
        "cron: /home/ubuntu/apps/it-spareparts/backup.sh: Permission denied",
        "cron: monitor.sh: command not found",
        "cron: /bin/sh: 1: monitor.sh: not found",
        "cron: backup.sh failed to execute: No such file or directory",
        "cron: monitor.sh timed out",
        "sudo: a terminal is required to read the password",
    ],
)
def test_observer_rejects_cron_execution_errors(
    tmp_path: Path,
    journal_line: str,
) -> None:
    evidence = tmp_path / "cron-0m.txt"
    env = {
        "TEST_CRON_JOURNAL": journal_line,
    }
    body = r'''
sudo() {
  [ "$1" = -n ] || return 97
  shift
  "$@"
}
journalctl() {
  printf '%s\n' "$TEST_CRON_JOURNAL"
}
SWITCHED_AT=2026-07-30T16:10:00+08:00
capture_cron_journal "$1"
'''

    result = _run_observer_library(body, str(evidence), env=env)

    assert result.returncode != 0
    assert journal_line in evidence.read_text(encoding="utf-8")


def test_observer_rejects_cron_log_scan_failure(tmp_path: Path) -> None:
    evidence = tmp_path / "cron-0m.txt"
    body = r'''
sudo() {
  [ "$1" = -n ] || return 97
  shift
  "$@"
}
journalctl() {
  printf '%s\n' 'cron: normal execution'
}
grep() {
  return 2
}
SWITCHED_AT=2026-07-30T16:10:00+08:00
capture_cron_journal "$1"
'''

    result = _run_observer_library(body, str(evidence))

    assert result.returncode == 2
    assert "could not be scanned" in result.stderr


@pytest.mark.parametrize(
    "relative_path",
    [
        "etc/anacrontab",
        "etc/cron.hourly/it-spareparts",
        "etc/cron.daily/it-spareparts",
        "etc/cron.weekly/it-spareparts",
        "etc/cron.monthly/it-spareparts",
        "etc/cron.yearly/it-spareparts",
        "lib/systemd/system/it-spareparts-backup.service",
        "usr/lib/systemd/system/it-spareparts-monitor.service",
        "etc/systemd/user/it-spareparts-monitor.service",
        "usr/lib/systemd/user/it-spareparts-backup.service",
        "run/systemd/transient/it-spareparts-backup.service",
        "home/operator/.config/systemd/user/it-spareparts.timer",
        "home/operator/.local/share/systemd/user/it-spareparts.timer",
    ],
)
def test_installer_finds_project_jobs_in_all_static_scheduler_locations(
    tmp_path: Path,
    relative_path: str,
) -> None:
    scheduler_file = tmp_path / relative_path
    scheduler_file.parent.mkdir(parents=True, exist_ok=True)
    scheduler_file.write_text(
        "ExecStart=/home/ubuntu/apps/it-spareparts/.deploy/monitor.sh\n",
        encoding="utf-8",
    )

    result = _run_installer_library(
        'static_scheduler_duplicates_absent "$1"',
        str(tmp_path),
    )

    assert result.returncode == 75
    assert str(scheduler_file) in result.stderr


@pytest.mark.parametrize("scope", ["--system", "--user"])
def test_installer_finds_project_jobs_in_loaded_systemd_timers(
    scope: str,
) -> None:
    body = r'''
systemctl() {
  case "$*" in
    *"list-timers"*)
      printf '%s\n' '- - - - hidden.timer hidden.service'
      ;;
    *"show hidden.timer"*)
      printf '%s\n' 'FragmentPath=' 'Unit=hidden.service' 'ExecStart='
      ;;
    *"show hidden.service"*)
      printf '%s\n' \
        'FragmentPath=' \
        'ExecStart={ path=/home/ubuntu/apps/it-spareparts/backup.sh ; }'
      ;;
    *)
      return 98
      ;;
  esac
}
active_timer_scope_duplicates_absent test systemctl "$1"
'''

    result = _run_installer_library(body, scope)

    assert result.returncode == 75
    assert "hidden.service" in result.stderr


def test_installer_finds_relative_project_job_in_loaded_timer() -> None:
    body = r'''
systemctl() {
  case "$*" in
    *"list-timers"*)
      printf '%s\n' '- - - - relative.timer relative.service'
      ;;
    *"show relative.timer"*)
      printf '%s\n' 'Unit=relative.service'
      ;;
    *"show relative.service"*)
      printf '%s\n' \
        'WorkingDirectory=/home/ubuntu/apps/it-spareparts' \
        'ExecStart={ path=.deploy/monitor.sh ; }'
      ;;
    *)
      return 98
      ;;
  esac
}
active_timer_scope_duplicates_absent system systemctl --system
'''

    result = _run_installer_library(body)

    assert result.returncode == 75
    assert "relative.service" in result.stderr


def test_valid_built_state_is_parsed_as_inert_data(tmp_path: Path) -> None:
    state = tmp_path / "release.state"
    state.write_text(_built_state(), encoding="ascii")
    state.chmod(0o600)

    result = _run_state(
        state,
        'v120_state_load "$1"; '
        'printf "%s %s %s\\n" "$STATE_FORMAT" "$STATE_GENERATION" '
        '"$RELEASE_PHASE"',
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "v120-1 0 built\n"


@pytest.mark.parametrize(
    "payload",
    [
        "UNKNOWN_KEY=value\n",
        "APP_IMAGE_REF=$(touch /tmp/v120-pwned)\n",
        "APP_IMAGE_REF=`id`\n",
        "APP_IMAGE_REF=x;id\n",
        "APP_IMAGE_REF=x=y\n",
        "# comment-is-not-data\n",
        "\n",
        "APP_IMAGE_REF=it-spareparts-app\r\n",
        "APP_IMAGE_REF=it-spareparts-app\t\n",
        "APP_IMAGE_REF=应用\n",
    ],
)
def test_state_rejects_unknown_or_executable_syntax(
    tmp_path: Path, payload: str
) -> None:
    sentinel = Path("/tmp/v120-pwned")
    sentinel.unlink(missing_ok=True)
    state = tmp_path / "malicious.state"
    state.write_text(_built_state() + payload, encoding="utf-8")
    state.chmod(0o600)

    result = _run_state(state, 'v120_state_load "$1"')

    assert result.returncode == 64
    assert not sentinel.exists()


def test_state_rejects_duplicate_key_even_when_value_matches(
    tmp_path: Path,
) -> None:
    state = tmp_path / "duplicate.state"
    state.write_text(
        _built_state() + "STATE_GENERATION=0\n", encoding="ascii"
    )
    state.chmod(0o600)

    result = _run_state(state, 'v120_state_load "$1"')

    assert result.returncode == 64
    assert "duplicate" in result.stderr.lower()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda data: data[:-1],
        lambda data: data.replace(b"\n", b"\x00\n", 1),
        lambda data: data + b"A" * 17000,
    ],
)
def test_state_rejects_invalid_bytes_size_or_missing_final_lf(
    tmp_path: Path, mutation
) -> None:
    state = tmp_path / "invalid.state"
    state.write_bytes(mutation(_built_state().encode("ascii")))
    state.chmod(0o600)

    result = _run_state(state, 'v120_state_load "$1"')

    assert result.returncode == 64


def test_state_rejects_cross_release_derived_path(tmp_path: Path) -> None:
    state = tmp_path / "wrong-path.state"
    content = _built_state().replace(
        "v120-aaaaaaaaaaaa-20260730160000-source.tar",
        "v120-bbbbbbbbbbbb-20260730160000-source.tar",
        1,
    )
    state.write_text(content, encoding="ascii")
    state.chmod(0o600)

    result = _run_state(state, 'v120_state_load "$1"')

    assert result.returncode == 64


def test_state_rejects_hardlink_alias(tmp_path: Path) -> None:
    state = tmp_path / "release.state"
    alias = tmp_path / "release-alias.state"
    state.write_text(_built_state(), encoding="ascii")
    state.chmod(0o600)
    os.link(state, alias)

    result = _run_state(state, 'v120_state_load "$1"')

    assert result.returncode == 64
    assert "hard link" in result.stderr


def test_atomic_transition_advances_generation_and_phase(
    tmp_path: Path,
) -> None:
    state = tmp_path / "release.state"
    state.write_text(_built_state(), encoding="ascii")
    state.chmod(0o600)
    args = _prepared_update_args()

    result = _run_state(
        state,
        'state=$1; shift; v120_state_update_atomic "$state" "$@"; '
        'v120_state_load "$state"; '
        'printf "%s %s\\n" "$STATE_GENERATION" "$RELEASE_PHASE"',
        *args,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "1 prepared\n"
    lines = state.read_text(encoding="ascii").splitlines()
    assert len(lines) == len(set(line.split("=", 1)[0] for line in lines))


def test_atomic_failpoint_preserves_old_state_bytes(tmp_path: Path) -> None:
    state = tmp_path / "release.state"
    before = _built_state().encode("ascii")
    state.write_bytes(before)
    state.chmod(0o600)
    env = os.environ.copy()
    env.update(
        {
            "V120_STATE_TEST_MODE": "1",
            "V120_STATE_TEST_FAILPOINT": "before_rename",
        }
    )

    result = _run_state(
        state,
        'state=$1; shift; v120_state_update_atomic "$state" "$@"',
        *_prepared_update_args(),
        env=env,
    )

    assert result.returncode == 74
    assert state.read_bytes() == before
    assert not list(tmp_path.glob(".v120-state.next.*"))


@pytest.mark.parametrize("failed_io", ["mv", "sync_file", "sync_directory"])
def test_atomic_update_io_failure_propagates_from_or_list(
    tmp_path: Path,
    failed_io: str,
) -> None:
    state = tmp_path / "release.state"
    state.write_text(_built_state(), encoding="ascii")
    state.chmod(0o600)
    if failed_io == "mv":
        failure_stub = "mv() { return 41; }; sync() { return 0; };"
    elif failed_io == "sync_file":
        failure_stub = (
            'sync() { [ "${1:-}" != -f ] || return 42; return 0; };'
        )
    else:
        failure_stub = (
            'sync() { [ "${1:-}" != -d ] || return 43; return 0; };'
        )

    result = _run_state(
        state,
        (
            f"{failure_stub} "
            'state=$1; shift; '
            'v120_state_update_atomic "$state" "$@" || exit 93; exit 0'
        ),
        *_prepared_update_args(),
    )

    assert result.returncode == 93, result.stderr


def test_new_state_publish_is_atomic_and_has_one_link(tmp_path: Path) -> None:
    candidate = tmp_path / ".v120-state.new"
    destination = tmp_path / "release.state"
    candidate.write_text(_built_state(), encoding="ascii")
    candidate.chmod(0o600)

    result = _run_state(
        candidate,
        'v120_state_publish_new "$1" "$2"',
        str(destination),
    )

    assert result.returncode == 0, result.stderr
    assert not candidate.exists()
    assert destination.read_text(encoding="ascii") == _built_state()
    assert destination.stat().st_nlink == 1


def test_new_state_publish_never_overwrites_concurrent_name(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / ".v120-state.new"
    destination = tmp_path / "release.state"
    candidate.write_text(_built_state(), encoding="ascii")
    candidate.chmod(0o600)
    existing = b"concurrent-owner\n"
    destination.write_bytes(existing)
    destination.chmod(0o600)

    result = _run_state(
        candidate,
        'v120_state_publish_new "$1" "$2"',
        str(destination),
    )

    assert result.returncode == 74
    assert destination.read_bytes() == existing
    assert candidate.read_text(encoding="ascii") == _built_state()


def test_new_state_publish_detects_race_at_no_clobber_rename(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / ".v120-state.new"
    destination = tmp_path / "release.state"
    candidate.write_text(_built_state(), encoding="ascii")
    candidate.chmod(0o600)
    body = r'''
mv() {
  local raced_destination=${!#}
  printf 'racer-won\n' > "$raced_destination"
  command mv "$@"
}
v120_state_publish_new "$1" "$2"
'''

    result = _run_state(candidate, body, str(destination))

    assert result.returncode == 74
    assert destination.read_text(encoding="ascii") == "racer-won\n"
    assert candidate.read_text(encoding="ascii") == _built_state()


def test_new_state_publish_normalizes_nonzero_no_clobber_collision(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / ".v120-state.new"
    destination = tmp_path / "release.state"
    candidate.write_text(_built_state(), encoding="ascii")
    candidate.chmod(0o600)
    body = r'''
mv() {
  local raced_destination=${!#}
  printf 'racer-won\n' > "$raced_destination"
  return 1
}
v120_state_publish_new "$1" "$2"
'''

    result = _run_state(candidate, body, str(destination))

    assert result.returncode == 74
    assert destination.read_text(encoding="ascii") == "racer-won\n"
    assert candidate.read_text(encoding="ascii") == _built_state()


def test_new_state_publish_propagates_mv_failure_without_collision(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / ".v120-state.new"
    destination = tmp_path / "release.state"
    candidate.write_text(_built_state(), encoding="ascii")
    candidate.chmod(0o600)
    body = r'''
mv() {
  return 41
}
v120_state_publish_new "$1" "$2"
'''

    result = _run_state(candidate, body, str(destination))

    assert result.returncode == 41
    assert not destination.exists()
    assert candidate.read_text(encoding="ascii") == _built_state()


def test_build_uses_single_link_no_clobber_state_publish() -> None:
    build = _script(BUILD)

    assert 'v120_state_publish_new "$STATE_TEMP" "$STATE"' in build
    assert 'ln -- "$STATE_TEMP" "$STATE"' not in build


def test_control_only_successor_may_reuse_either_or_both_runtime_images() -> None:
    build = _script(BUILD)

    assert '[ "$NEW_APP_IMAGE_ID" != "$OLD_APP_IMAGE_ID" ]' not in build
    assert (
        '[ "$NEW_FRONTEND_IMAGE_ID" != "$OLD_FRONTEND_IMAGE_ID" ]'
        not in build
    )
    assert '[[ "$NEW_APP_IMAGE_ID" =~ ^sha256:[0-9a-f]{64}$ ]]' in build
    assert (
        '[[ "$NEW_FRONTEND_IMAGE_ID" =~ ^sha256:[0-9a-f]{64}$ ]]'
        in build
    )


def test_container_builds_use_immutable_amd64_bases_and_frozen_locks() -> None:
    backend = BACKEND_DOCKERFILE.read_text(encoding="utf-8")
    frontend = FRONTEND_DOCKERFILE.read_text(encoding="utf-8")

    backend_from = re.findall(r"^FROM .+$", backend, re.MULTILINE)
    frontend_from = re.findall(r"^FROM .+$", frontend, re.MULTILINE)
    assert len(backend_from) == 1
    assert len(frontend_from) == 2
    for instruction in (*backend_from, *frontend_from):
        assert "--platform=linux/amd64" in instruction
        assert re.search(r"@sha256:[0-9a-f]{64}(?:\s|$)", instruction)
    assert "COPY requirements.lock uv.lock pyproject.toml ./" in backend
    assert "pip install --no-cache-dir --require-hashes" in backend
    assert "-r requirements.lock" in backend
    assert (
        "pip install --no-cache-dir --no-deps --no-build-isolation ."
        in backend
    )
    assert "pip install --no-cache-dir --retries 5 ." not in backend
    assert "pip install --no-cache-dir --retries 5 --upgrade pip" not in backend
    assert "COPY package.json package-lock.json ./" in frontend
    assert "package-lock.json*" not in frontend
    assert "npm ci --no-audit --no-fund" in frontend
    assert "npm install " not in frontend


def test_dependency_locks_and_cyclonedx_sboms_are_fresh() -> None:
    assert BACKEND_REQUIREMENTS_LOCK.is_file()
    assert BACKEND_UV_LOCK.is_file()
    assert FRONTEND_PACKAGE_LOCK.is_file()
    assert BACKEND_SBOM.is_file()
    assert FRONTEND_SBOM.is_file()
    uv_hash_before = hashlib.sha256(BACKEND_UV_LOCK.read_bytes()).hexdigest()
    exported = subprocess.run(
        [
            "uv",
            "--no-config",
            "export",
            "--frozen",
            "--offline",
            "--default-index",
            "https://pypi.org/simple",
            "--no-dev",
            "--no-emit-project",
            "--format",
            "requirements-txt",
            "--no-header",
        ],
        cwd=ROOT / "backend",
        text=True,
        capture_output=True,
        env={
            "HOME": os.environ["HOME"],
            "PATH": os.environ["PATH"],
            "UV_NO_CONFIG": "1",
        },
        check=False,
    )
    assert exported.returncode == 0, exported.stderr
    assert hashlib.sha256(BACKEND_UV_LOCK.read_bytes()).hexdigest() == (
        uv_hash_before
    )
    assert BACKEND_REQUIREMENTS_LOCK.read_text(encoding="utf-8") == (
        exported.stdout
    )
    checked = subprocess.run(
        ["python3", str(SBOM_GENERATOR), "--check", str(ROOT)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert checked.returncode == 0, checked.stderr
    for path in (BACKEND_SBOM, FRONTEND_SBOM):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["bomFormat"] == "CycloneDX"
        assert payload["specVersion"] == "1.5"
        assert payload["metadata"]["component"]["type"] == "application"
        assert payload["components"]
        refs = [component["bom-ref"] for component in payload["components"]]
        assert len(refs) == len(set(refs))
    backend_components = json.loads(
        BACKEND_SBOM.read_text(encoding="utf-8")
    )["components"]
    backend_names = {component["name"] for component in backend_components}
    assert "fastapi" in backend_names
    assert "pytest" not in backend_names
    frontend_components = json.loads(
        FRONTEND_SBOM.read_text(encoding="utf-8")
    )["components"]
    frontend_names = {component["name"] for component in frontend_components}
    assert "react-router-dom" in frontend_names
    assert "vitest" not in frontend_names
    assert "@testing-library/react" not in frontend_names
    assert any(
        component["purl"].startswith("pkg:npm/%40ant-design/icons@")
        for component in frontend_components
    )


def test_sbom_check_rejects_pyproject_dependency_drift(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "repo"
    for relative in (
        ".deploy/generate_dependency_sbom.py",
        "backend/pyproject.toml",
        "backend/uv.lock",
        "backend/requirements.lock",
        "backend/dependency-sbom.cdx.json",
        "frontend/package-lock.json",
        "frontend/dependency-sbom.cdx.json",
    ):
        destination = fixture / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, destination)
    pyproject = fixture / "backend" / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8").replace(
            '"fastapi==0.136.1"',
            '"fastapi==0.136.2"',
        ),
        encoding="utf-8",
    )

    drifted = subprocess.run(
        [
            "python3",
            str(fixture / ".deploy" / "generate_dependency_sbom.py"),
            "--check",
            str(fixture),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert drifted.returncode != 0
    assert "pyproject.toml and uv.lock dependency metadata differ" in (
        drifted.stderr
    )


def test_supply_chain_inputs_and_image_provenance_are_fail_closed() -> None:
    package = _script(PACKAGE_CONTROL)
    installer = _script(INSTALL_CONTROL)
    build = _script(BUILD)
    release = _script(RELEASE)
    provenance_keys = (
        "BACKEND_REQUIREMENTS_SHA256",
        "BACKEND_UV_LOCK_SHA256",
        "FRONTEND_PACKAGE_LOCK_SHA256",
        "BACKEND_SBOM_SHA256",
        "FRONTEND_SBOM_SHA256",
        "BACKEND_BASE_DIGEST",
        "FRONTEND_BUILD_BASE_DIGEST",
        "FRONTEND_RUNTIME_BASE_DIGEST",
    )
    for key in provenance_keys:
        assert key in package
        assert key in installer
        assert key in build
        assert key in release
    assert "CONTROL_FORMAT=v120-control-3" in package
    assert (
        'sudo -n python3 "$RELEASE_SRC/.deploy/generate_dependency_sbom.py"'
        in build
    )
    assert '--check "$RELEASE_SRC"' in build
    for relative in (
        "backend/requirements.lock",
        "backend/uv.lock",
        "frontend/package-lock.json",
        "backend/dependency-sbom.cdx.json",
        "frontend/dependency-sbom.cdx.json",
    ):
        assert relative in build
    assert "supply-chain-provenance.txt" in release
    assert "NEW_APP_IMAGE_ID" in release
    assert "NEW_FRONTEND_IMAGE_ID" in release
    assert "v120_publish_exact_evidence" in release
    assert (
        '"$SUPPLY_CHAIN_TEMP" "$SUPPLY_CHAIN_EVIDENCE"' in release
    )
    assert 'mv -T -- "$SUPPLY_CHAIN_TEMP" "$SUPPLY_CHAIN_EVIDENCE"' not in (
        release
    )


_PROTECTED_BUILD_VARIABLE = re.compile(
    r"\$(?:\{)?(?:BUILD_ROOT|RELEASE_SRC_CANDIDATE|RELEASE_SRC|"
    r"BUILD_OVERRIDE|MIGRATION_DIR)(?:\})?"
)


def _normalize_shell_command(command: str) -> str:
    command = textwrap.dedent(command).strip()
    command = re.sub(r"\\\n[ \t]*", " ", command)
    return re.sub(r"\s+", " ", command)


def _logical_shell_commands(script: str) -> tuple[str, ...]:
    commands: list[str] = []
    current: list[str] = []
    single_quoted = False
    double_quoted = False
    command_substitution_depth = 0

    for line in script.splitlines():
        current.append(line)
        escaped = False
        index = 0
        while index < len(line):
            character = line[index]
            if escaped:
                escaped = False
            elif character == "\\" and not single_quoted:
                escaped = True
            elif single_quoted:
                if character == "'":
                    single_quoted = False
            elif character == "'" and not double_quoted:
                single_quoted = True
            elif character == '"':
                double_quoted = not double_quoted
            elif character == "$" and line[index : index + 2] == "$(":
                command_substitution_depth += 1
                index += 1
            elif character == ")" and command_substitution_depth:
                command_substitution_depth -= 1
            index += 1

        trailing_backslashes = len(line) - len(line.rstrip("\\"))
        continued = trailing_backslashes % 2 == 1
        if not any(
            (
                continued,
                single_quoted,
                double_quoted,
                command_substitution_depth,
            )
        ):
            command = "\n".join(current)
            commands.append(_normalize_shell_command(command))
            current = []

    assert not current, "unterminated logical shell command"
    return tuple(commands)


_SAFE_PROTECTED_NON_FILE_COMMANDS = frozenset(
    {
        'RELEASE_SRC_CANDIDATE="$BUILD_ROOT/$RELEASE_ID"',
        "RELEASE_SRC=$RELEASE_SRC_CANDIDATE",
        'BUILD_OVERRIDE="$RELEASE_SRC/docker-compose.build-override.yml"',
        'MIGRATION_DIR="$RELEASE_SRC/backend/alembic/versions"',
        "RELEASE_SRC=",
    }
)
_SAFE_PROTECTED_SUDO_COMMANDS = frozenset(
    _normalize_shell_command(command)
    for command in (
        r'''sudo mkdir -- "$RELEASE_SRC_CANDIDATE"''',
        r'''sudo chown root:root "$RELEASE_SRC"''',
        r'''sudo chmod 700 "$RELEASE_SRC"''',
        r'''sudo tar --no-same-owner --no-same-permissions \
          -xf "$CONTROL_CURRENT/source.tar" -C "$RELEASE_SRC"''',
        r'''sudo env \
          BUILD_OVERRIDE="$BUILD_OVERRIDE" \
          NEW_APP_CANDIDATE_TAG="$NEW_APP_CANDIDATE_TAG" \
          NEW_FRONTEND_CANDIDATE_TAG="$NEW_FRONTEND_CANDIDATE_TAG" \
          sh -c '
            set -eu
            umask 077
            {
              printf "services:\n"
              printf "  app:\n"
              printf "    image: %s\n" "$NEW_APP_CANDIDATE_TAG"
              printf "  frontend:\n"
              printf "    image: %s\n" "$NEW_FRONTEND_CANDIDATE_TAG"
            } > "$BUILD_OVERRIDE"
            chown root:root "$BUILD_OVERRIDE"
            chmod 400 "$BUILD_OVERRIDE"
          ' ''',
        r'''sudo find "$RELEASE_SRC" -xdev -type d -exec chmod 555 {} +''',
        r'''sudo find "$RELEASE_SRC" -xdev -type f \
          ! -path "$BUILD_OVERRIDE" -exec chmod 444 {} +''',
        r'''sudo grep -q 'APP_VERSION = "1.20.0"' \
          "$RELEASE_SRC/frontend/src/version.ts"''',
        r'''[ "$(sudo sha256sum "$RELEASE_SRC/$relative" | cut -d' ' -f1)" \
          = "$expected" ] || fatal "$relative differs from the control manifest"''',
        r'''sudo -n grep -Fx \
          "FROM --platform=linux/amd64 python:3.11-slim@sha256:$BACKEND_BASE_DIGEST" \
          "$RELEASE_SRC/backend/Dockerfile" >/dev/null \
          || fatal "backend base digest differs from the control manifest"''',
        r'''sudo -n grep -Fx \
          "FROM --platform=linux/amd64 node:20-alpine@sha256:$FRONTEND_BUILD_BASE_DIGEST AS build" \
          "$RELEASE_SRC/frontend/Dockerfile" >/dev/null \
          || fatal "frontend build base digest differs from the control manifest"''',
        r'''sudo -n grep -Fx \
          "FROM --platform=linux/amd64 nginx:1.27-alpine@sha256:$FRONTEND_RUNTIME_BASE_DIGEST" \
          "$RELEASE_SRC/frontend/Dockerfile" >/dev/null \
          || fatal "frontend runtime base digest differs from the control manifest"''',
        r'''sudo -n python3 "$RELEASE_SRC/.deploy/generate_dependency_sbom.py" \
          --check "$RELEASE_SRC" \
          || fatal "dependency SBOM does not match the committed locks"''',
        r'''[ "$(sudo find "$MIGRATION_DIR" -mindepth 1 -maxdepth 1 \
          -type f -printf x | wc -c)" = "$EXPECTED_MIGRATION_FILE_COUNT" ] \
          || fatal "v1.20 contains an unexpected DB migration count"''',
        r'''UNEXPECTED_MIGRATION_ENTRY=$(
          sudo find "$MIGRATION_DIR" -mindepth 1 -maxdepth 1 \
            ! -type f -print -quit
        )''',
        r'''MIGRATION_INVENTORY_SHA256=$(
          sudo find "$MIGRATION_DIR" -mindepth 1 -maxdepth 1 \
              -type f -printf '%f\n' |
            LC_ALL=C sort |
            while IFS= read -r migration_file; do
              migration_hash=$(
                sudo sha256sum "$MIGRATION_DIR/$migration_file" | cut -d' ' -f1
              )
              printf '%s  backend/alembic/versions/%s\n' \
                "$migration_hash" "$migration_file"
            done |
            sha256sum | cut -d' ' -f1
        )''',
        r'''sudo env \
          -u COMPOSE_FILE \
          -u COMPOSE_PROJECT_NAME \
          -u COMPOSE_PROFILES \
          docker compose \
            --project-name "$BUILD_PROJECT" \
            --env-file "$APP_DIR/.env" \
            --project-directory "$RELEASE_SRC" \
            -f "$RELEASE_SRC/docker-compose.yml" \
            -f "$BUILD_OVERRIDE" \
            build --pull app frontend''',
        r'''sudo find "$RELEASE_SRC" -xdev -depth -mindepth 1 -delete''',
        r'''sudo rmdir "$RELEASE_SRC"''',
    )
)


def _assert_protected_release_source_reads_use_sudo(build: str) -> None:
    assert 'sudo chmod 700 "$BUILD_ROOT"' in build
    assert 'sudo chmod 755 "$BUILD_ROOT"' not in build
    stage_start = 'RELEASE_SRC_CANDIDATE="$BUILD_ROOT/$RELEASE_ID"'
    stage_end = "\nv120_release_lock\n"
    assert build.count(stage_start) == 1
    assert build.count(stage_end) == 1
    protected_stage = build[build.index(stage_start) : build.index(stage_end)]
    protected_commands = tuple(
        command
        for command in _logical_shell_commands(protected_stage)
        if _PROTECTED_BUILD_VARIABLE.search(command)
    )
    unauthorized = tuple(
        command
        for command in protected_commands
        if command not in _SAFE_PROTECTED_NON_FILE_COMMANDS
        and command not in _SAFE_PROTECTED_SUDO_COMMANDS
    )

    assert not unauthorized, (
        "protected release source command is outside the exact sudo "
        f"allowlist: {unauthorized!r}"
    )


def test_protected_release_source_reads_use_noninteractive_sudo() -> None:
    _assert_protected_release_source_reads_use_sudo(_script(BUILD))


@pytest.mark.parametrize(
    ("marker", "unauthorized_read"),
    (
        (
            "verify_release_source_hash() {",
            '/usr/bin/grep -q root "$RELEASE_SRC/backend/Dockerfile"',
        ),
        (
            "verify_release_source_hash() {",
            '/usr/bin/env grep -q root "$RELEASE_SRC/backend/Dockerfile"',
        ),
        (
            "verify_release_source_hash() {",
            '/usr/bin/env \\\n+  grep -q root "$RELEASE_SRC/backend/Dockerfile"',
        ),
        (
            "verify_release_source_hash() {",
            'command grep -q root "$RELEASE_SRC/backend/Dockerfile"',
        ),
        (
            "verify_release_source_hash() {",
            'cat "$RELEASE_SRC/backend/Dockerfile" >/dev/null',
        ),
        (
            "verify_release_source_hash() {",
            'sed -n 1p "$RELEASE_SRC/backend/Dockerfile" >/dev/null',
        ),
        (
            "verify_release_source_hash() {",
            'awk "NR == 1" "$RELEASE_SRC/backend/Dockerfile"',
        ),
        (
            "verify_release_source_hash() {",
            'LEAK=$(cat "$RELEASE_SRC/backend/Dockerfile")',
        ),
        (
            "verify_release_source_hash() {",
            'sudo true; cat "$RELEASE_SRC/backend/Dockerfile" >/dev/null',
        ),
        (
            'MIGRATION_DIR="$RELEASE_SRC/backend/alembic/versions"',
            'cat "$MIGRATION_DIR/f1c8e4a7b2d9_v120.py" >/dev/null',
        ),
        (
            'BUILD_OVERRIDE="$RELEASE_SRC/docker-compose.build-override.yml"',
            'cat "$BUILD_OVERRIDE" >/dev/null',
        ),
    ),
    ids=(
        "absolute-grep",
        "env-grep",
        "continued-env-grep",
        "command-grep",
        "cat",
        "sed",
        "awk",
        "command-substitution",
        "sudo-token-is-not-enough",
        "after-old-slice",
        "before-old-slice",
    ),
)
def test_protected_release_source_audit_rejects_unauthorized_reads(
    marker: str,
    unauthorized_read: str,
) -> None:
    build = _script(BUILD)
    assert build.count(marker) == 1
    mutated = build.replace(
        marker,
        f"{marker}\n{unauthorized_read}",
        1,
    )

    with pytest.raises(AssertionError, match="protected release source"):
        _assert_protected_release_source_reads_use_sudo(mutated)


def test_supply_chain_evidence_publish_is_atomic_idempotent_and_fail_closed(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "supply-chain-provenance.txt"
    first = tmp_path / "first.tmp"
    first.write_text("exact evidence\n", encoding="ascii")
    first.chmod(0o600)
    body = 'v120_publish_exact_evidence "$1" "$2"'

    created = _run_release_library(body, str(first), str(destination))

    assert created.returncode == 0, created.stderr
    assert not first.exists()
    assert destination.read_text(encoding="ascii") == "exact evidence\n"
    assert destination.stat().st_nlink == 1
    assert destination.stat().st_mode & 0o777 == 0o600

    identical = tmp_path / "identical.tmp"
    identical.write_text("exact evidence\n", encoding="ascii")
    identical.chmod(0o600)
    repeated = _run_release_library(body, str(identical), str(destination))
    assert repeated.returncode == 0, repeated.stderr
    assert not identical.exists()
    assert destination.read_text(encoding="ascii") == "exact evidence\n"

    differing = tmp_path / "differing.tmp"
    differing.write_text("different evidence\n", encoding="ascii")
    differing.chmod(0o600)
    rejected = _run_release_library(body, str(differing), str(destination))
    assert rejected.returncode == 73
    assert not differing.exists()
    assert destination.read_text(encoding="ascii") == "exact evidence\n"

    for unsafe_kind in ("symlink", "directory"):
        unsafe_destination = tmp_path / f"unsafe-{unsafe_kind}"
        if unsafe_kind == "symlink":
            unsafe_destination.symlink_to(destination)
        else:
            unsafe_destination.mkdir()
        unsafe_mode = unsafe_destination.lstat().st_mode & 0o777
        candidate = tmp_path / f"{unsafe_kind}.tmp"
        candidate.write_text("exact evidence\n", encoding="ascii")
        candidate.chmod(0o600)
        unsafe = _run_release_library(
            body,
            str(candidate),
            str(unsafe_destination),
        )
        assert unsafe.returncode == 73
        assert not candidate.exists()
        if unsafe_kind == "symlink":
            assert unsafe_destination.is_symlink()
        else:
            assert unsafe_destination.is_dir()
            assert not list(unsafe_destination.iterdir())
            assert unsafe_destination.stat().st_mode & 0o777 == unsafe_mode


def test_v2_current_can_install_v3_successor_but_v3_gate_rejects_v2(
    tmp_path: Path,
) -> None:
    control = tmp_path / "control"
    versions = control / "versions"
    versions.mkdir(parents=True, mode=0o700)
    control.chmod(0o700)
    versions.chmod(0o700)
    v2_names_and_keys = (
        ("v120_state.sh", "V120_STATE_SHA256", b"#!/bin/bash\ntrue\n"),
        (
            "sync-v120-root-state.sh",
            "ROOT_SYNC_SHA256",
            b"#!/bin/bash\ntrue\n",
        ),
        ("rollback-v120.sh", "ROLLBACK_SHA256", b"#!/bin/bash\ntrue\n"),
        (
            "install-v120-control.sh",
            "INSTALLER_SHA256",
            b"#!/bin/bash\ntrue\n",
        ),
        ("it-spareparts.cron", "CRON_SHA256", b"SHELL=/bin/sh\n"),
        ("source.tar", "SOURCE_TAR_SHA256", b"trusted-source\n"),
    )
    v3_only_names_and_keys = (
        ("hsts-v120-root.sh", "HSTS_ROOT_SHA256", b"#!/bin/bash\ntrue\n"),
        (
            "hsts-v120-operator.sh",
            "HSTS_OPERATOR_SHA256",
            b"#!/bin/bash\ntrue\n",
        ),
        ("edge-v120-root.sh", "EDGE_ROOT_SHA256", b"#!/bin/bash\ntrue\n"),
        (
            "edge-v120-operator.sh",
            "EDGE_OPERATOR_SHA256",
            b"#!/bin/bash\ntrue\n",
        ),
    )

    def make_version(format_name: str) -> tuple[Path, str]:
        version = versions / f"{format_name}-staging"
        version.mkdir(mode=0o700)
        lines = [
            f"CONTROL_FORMAT={format_name}",
            "TARGET_COMMIT=" + "4" * 40,
        ]
        names_and_keys = v2_names_and_keys
        if format_name == "v120-control-3":
            names_and_keys = (
                *v2_names_and_keys[:4],
                *v3_only_names_and_keys,
                *v2_names_and_keys[4:],
            )
        for name, key, content in names_and_keys:
            artifact = version / name
            artifact.write_bytes(content)
            artifact.chmod(0o700 if name.endswith(".sh") else 0o600)
            lines.append(f"{key}={hashlib.sha256(content).hexdigest()}")
        if format_name == "v120-control-3":
            for key in (
                "BACKEND_REQUIREMENTS_SHA256",
                "BACKEND_UV_LOCK_SHA256",
                "FRONTEND_PACKAGE_LOCK_SHA256",
                "BACKEND_SBOM_SHA256",
                "FRONTEND_SBOM_SHA256",
                "BACKEND_BASE_DIGEST",
                "FRONTEND_BUILD_BASE_DIGEST",
                "FRONTEND_RUNTIME_BASE_DIGEST",
            ):
                lines.append(
                    f"{key}={hashlib.sha256(key.encode()).hexdigest()}"
                )
        manifest = version / "manifest.txt"
        manifest.write_text("\n".join(lines) + "\n", encoding="ascii")
        manifest.chmod(0o600)
        manifest_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
        final = versions / manifest_hash
        version.rename(final)
        return final, manifest_hash

    _, v2_hash = make_version("v120-control-2")
    v3, v3_hash = make_version("v120-control-3")
    current = control / "current"
    current.symlink_to(f"versions/{v2_hash}")
    env = os.environ.copy()
    env.update(
        {
            "V120_STATE_TEST_MODE": "1",
            "V120_INSTALLER_LIBRARY_ONLY": "1",
        }
    )
    command = r'''
source "$1"
validate_current_predecessor_for_successor "$2" "$3"
validate_package_directory "$4" "$5"
publish_current_pointer "$2" "$3" "$5"
'''
    upgraded = subprocess.run(
        [
            "bash",
            "-c",
            command,
            "bash",
            str(INSTALL_CONTROL),
            str(control),
            str(versions),
            str(v3),
            v3_hash,
        ],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert upgraded.returncode == 0, upgraded.stderr
    assert os.readlink(current) == f"versions/{v3_hash}"

    v2_manifest = versions / v2_hash / "manifest.txt"
    v3_only = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; declare -A parsed=(); '
            'parse_manifest "$2" parsed',
            "bash",
            str(INSTALL_CONTROL),
            str(v2_manifest),
        ],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert v3_only.returncode != 0


def _load_artifact_validator() -> object:
    spec = importlib.util.spec_from_file_location(
        "release_artifact_validator",
        ARTIFACT_VALIDATOR,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ci_pins_setup_uv_action_and_binary_version() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    setup = re.search(
        r"- uses: astral-sh/setup-uv@v8\.3\.2\n"
        r"\s+with:\n"
        r'\s+version: "0\.11\.31"\n',
        workflow,
    )

    assert setup is not None
    assert workflow.count("astral-sh/setup-uv@v8.3.2") == 1


def test_artifact_validator_preflights_member_count_before_inflation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validator = _load_artifact_validator()
    archive_path = tmp_path / "too-many.zip"
    archive_path.write_bytes(b"PK fixture")

    class Info:
        file_size = 1
        compress_size = 1

        def __init__(self, index: int) -> None:
            self.filename = f"合同-{index}.xlsx"

        @staticmethod
        def is_dir() -> bool:
            return False

    class Archive:
        def __enter__(self) -> object:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        @staticmethod
        def infolist() -> list[Info]:
            return [Info(index) for index in range(501)]

        @staticmethod
        def testzip() -> None:
            raise AssertionError("CRC/inflation ran before central-directory limits")

        @staticmethod
        def read(_member: object) -> bytes:
            raise AssertionError("member inflation ran before preflight")

    monkeypatch.setattr(validator.zipfile, "ZipFile", lambda *_args: Archive())

    with pytest.raises(SystemExit):
        validator.validate_zip(archive_path)


def test_artifact_validator_rejects_compression_bomb_before_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validator = _load_artifact_validator()
    archive_path = tmp_path / "bomb.zip"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("合同.xlsx", b"0" * (8 * 1024 * 1024))
    original_open = zipfile.ZipFile.open

    def forbidden_open(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("compressed bomb was inflated before ratio preflight")

    monkeypatch.setattr(zipfile.ZipFile, "open", forbidden_open)
    try:
        with pytest.raises(SystemExit):
            validator.validate_zip(archive_path)
    finally:
        monkeypatch.setattr(zipfile.ZipFile, "open", original_open)


def test_artifact_validator_streams_large_legitimate_worksheet() -> None:
    validator = _load_artifact_validator()
    original = validator.self_test_xlsx("worksheets/sheet1.xml")
    rewritten = io.BytesIO()
    worksheet = (
        b'<worksheet xmlns="'
        + validator.SHEET_NS.encode()
        + b'"><sheetData><row><c><v>'
        + base64.b64encode(os.urandom(13 * 1024 * 1024))
        + b"</v></c></row></sheetData></worksheet>"
    )
    assert len(worksheet) > 16 * 1024 * 1024
    with zipfile.ZipFile(io.BytesIO(original)) as source:
        with zipfile.ZipFile(rewritten, "w", zipfile.ZIP_DEFLATED) as target:
            for info in source.infolist():
                content = source.read(info)
                if info.filename == "xl/worksheets/sheet1.xml":
                    content = worksheet
                target.writestr(info.filename, content)
    assert len(rewritten.getvalue()) < 64 * 1024 * 1024

    assert validator.validate_xlsx_bytes(rewritten.getvalue()) == 1


def _xlsx_with_workbook_relationship(
    validator: object,
    *,
    relationship_id: str,
    relationship_type: str,
    target: str,
    member_name: str | None,
    member_content: bytes | None,
) -> bytes:
    original = validator.self_test_xlsx("worksheets/sheet1.xml")
    with zipfile.ZipFile(io.BytesIO(original)) as source:
        members = {
            info.filename: source.read(info)
            for info in source.infolist()
        }
    relationship = (
        f'<Relationship Id="{relationship_id}" '
        f'Type="{validator.DOCUMENT_REL_NS}/{relationship_type}" '
        f'Target="{target}"/>'
    ).encode()
    members["xl/_rels/workbook.xml.rels"] = members[
        "xl/_rels/workbook.xml.rels"
    ].replace(b"</Relationships>", relationship + b"</Relationships>")
    if member_name is not None and member_content is not None:
        members[member_name] = member_content
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_STORED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return output.getvalue()


@pytest.mark.parametrize(
    ("relationship_type", "target", "member_name", "member_content"),
    (
        ("styles", "styles.xml", "xl/styles.xml", b"<styleSheet"),
        (
            "sharedStrings",
            "sharedStrings.xml",
            "xl/sharedStrings.xml",
            b"<sst><si>",
        ),
    ),
)
def test_artifact_validator_rejects_malformed_referenced_ooxml_parts(
    relationship_type: str,
    target: str,
    member_name: str,
    member_content: bytes,
) -> None:
    validator = _load_artifact_validator()
    payload = _xlsx_with_workbook_relationship(
        validator,
        relationship_id="rId2",
        relationship_type=relationship_type,
        target=target,
        member_name=member_name,
        member_content=member_content,
    )

    with pytest.raises(SystemExit):
        validator.validate_xlsx_bytes(payload)


def test_artifact_validator_rejects_missing_relationship_target() -> None:
    validator = _load_artifact_validator()
    payload = _xlsx_with_workbook_relationship(
        validator,
        relationship_id="rId2",
        relationship_type="styles",
        target="styles.xml",
        member_name=None,
        member_content=None,
    )

    with pytest.raises(SystemExit):
        validator.validate_xlsx_bytes(payload)


def test_artifact_validator_reads_referenced_part_to_crc_eof() -> None:
    validator = _load_artifact_validator()
    payload = bytearray(
        _xlsx_with_workbook_relationship(
            validator,
            relationship_id="rId2",
            relationship_type="styles",
            target="styles.xml",
            member_name="xl/styles.xml",
            member_content=(
                f'<styleSheet xmlns="{validator.SHEET_NS}"/>'.encode()
            ),
        )
    )
    marker = b"<styleSheet"
    marker_offset = payload.index(marker)
    payload[marker_offset + 2] ^= 0x01

    with pytest.raises(SystemExit):
        validator.validate_xlsx_bytes(bytes(payload))


def _write_fake_pipe_chrome(path: Path) -> None:
    path.write_text(
        textwrap.dedent(
            r"""
        #!/usr/bin/env python3
        import base64
        import json
        import os
        import signal
        import sys
        import time
        from urllib.parse import urlparse

        args_log = os.environ.get("MOBILE_TEST_ARGS_LOG")
        cleanup_log = os.environ.get("MOBILE_PROBE_TEST_CLEANUP_LOG")
        failure_sentinel = os.environ.get("MOBILE_TEST_FAILURE_SENTINEL", "")
        cdp_error_method = os.environ.get("MOBILE_TEST_CDP_ERROR_METHOD")
        if args_log:
            with open(args_log, "w", encoding="utf-8") as handle:
                json.dump(sys.argv[1:], handle)
        if os.environ.get("MOBILE_TEST_STDERR_SENTINEL") == "1":
            print(
                f"fixture Chrome stderr: {failure_sentinel}",
                file=sys.stderr,
                flush=True,
            )
        if os.environ.get("MOBILE_TEST_IGNORE_TERM") == "1":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        elif cleanup_log:
            def terminate(_signal, _frame):
                with open(cleanup_log, "a", encoding="utf-8") as handle:
                    handle.write("chrome-terminal\n")
                raise SystemExit(0)

            signal.signal(signal.SIGTERM, terminate)
        route = "/"
        stall_method = os.environ.get("MOBILE_TEST_STALL_METHOD")
        delay_method = os.environ.get("MOBILE_TEST_DELAY_METHOD")
        delay_seconds = (
            int(os.environ.get("MOBILE_TEST_DELAY_MS", "0")) / 1000
        )
        buffer = b""
        while True:
            chunk = os.read(3, 65536)
            if not chunk:
                if os.environ.get("MOBILE_TEST_IGNORE_TERM") == "1":
                    time.sleep(30)
                break
            buffer += chunk
            while b"\0" in buffer:
                frame, buffer = buffer.split(b"\0", 1)
                if not frame:
                    continue
                message = json.loads(frame)
                method = message["method"]
                params = message.get("params", {})
                result = {}
                if method == stall_method:
                    time.sleep(30)
                if method == delay_method:
                    time.sleep(delay_seconds)
                    delay_method = None
                if method == cdp_error_method:
                    response = {
                        "id": message["id"],
                        "error": {
                            "message": f"fixture CDP error: {failure_sentinel}"
                        },
                    }
                    os.write(
                        4,
                        json.dumps(response).encode("utf-8") + b"\0",
                    )
                    continue
                if (
                    method not in ("Target.getTargets", "Target.attachToTarget")
                    and message.get("sessionId") != "session-1"
                ):
                    response = {
                        "id": message["id"],
                        "error": {"message": "missing page session"},
                    }
                    os.write(
                        4,
                        json.dumps(response).encode("utf-8") + b"\0",
                    )
                    continue
                if method == "Target.getTargets":
                    result = {
                        "targetInfos": [{
                            "targetId": "page-1",
                            "type": "page",
                            "url": "about:blank",
                        }]
                    }
                elif method == "Target.attachToTarget":
                    result = {"sessionId": "session-1"}
                elif method == "Page.navigate":
                    route = urlparse(params["url"]).path
                    if (
                        os.environ.get("MOBILE_TEST_REDIRECT_ROUTE")
                        and route == "/maintenance"
                    ):
                        route = os.environ["MOBILE_TEST_REDIRECT_ROUTE"]
                    result = {"frameId": "frame-1"}
                elif method == "Runtime.evaluate":
                    expression = params.get("expression", "")
                    if "location.pathname" in expression:
                        result = {"result": {"value": {
                            "route": route,
                            "width": 375,
                            "globalOverflow": False,
                            "failed": False,
                            "hasContent": True,
                            "hasAnchor": (
                                os.environ.get("MOBILE_TEST_MISSING_ANCHOR")
                                != route
                            ),
                        }}}
                    elif "/api/maintenance/board/export" in expression:
                        result = {"result": {"value": {
                            "status": 200,
                            "disposition": (
                                "attachment; filename=fixture.csv"
                            ),
                            "type": "text/csv; charset=utf-8",
                            "cache": "no-store",
                        }}}
                    else:
                        result = {"result": {"value": True}}
                elif method == "Page.captureScreenshot":
                    result = {
                        "data": base64.b64encode(b"png").decode("ascii")
                    }
                response = {
                    "id": message["id"],
                    "result": result,
                }
                if "sessionId" in message:
                    response["sessionId"] = message["sessionId"]
                os.write(
                    4,
                    json.dumps(response).encode("utf-8") + b"\0",
                )
        if cleanup_log:
            with open(cleanup_log, "a", encoding="utf-8") as handle:
                handle.write("chrome-terminal\n")
        """
        ).lstrip(),
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _run_mobile_pipe_case(
    tmp_path: Path,
    case: str,
    *,
    redirect: str | None = None,
    missing_anchor: str | None = None,
    ignore_term: bool = False,
    stall_method: str | None = None,
    delay_method: str | None = None,
    delay_ms: int | None = None,
    test_mode: bool | None = None,
    command_timeout_ms: int | None = None,
    navigation_timeout_ms: int | None = None,
    stderr_token_failure: bool = False,
    cdp_error_method: str | None = None,
    overall_timeout_ms: int | None = None,
    profile_rm_failures: str | None = None,
    cleanup_log: bool = False,
) -> tuple[subprocess.CompletedProcess[str], Path, Path, Path, Path]:
    case_dir = tmp_path / case
    case_dir.mkdir(mode=0o700)
    work = case_dir / "work"
    work.mkdir(mode=0o700)
    login = work / "login.json"
    login.write_text(
        json.dumps(
            {
                "token": MOBILE_TOKEN_SENTINEL,
                "role": "admin",
                "name": "fixture",
                "permissions": {},
            }
        ),
        encoding="utf-8",
    )
    login.chmod(0o600)
    fake_chrome = case_dir / "fake-chrome"
    _write_fake_pipe_chrome(fake_chrome)
    args_log = case_dir / "chrome-args.json"
    evidence = work / "evidence.txt"
    screenshot = work / "screen.png"
    env = os.environ.copy()
    env["MOBILE_TEST_ARGS_LOG"] = str(args_log)
    if cleanup_log:
        env["MOBILE_PROBE_TEST_CLEANUP_LOG"] = str(
            case_dir / "cleanup.log"
        )
    if redirect is not None:
        env["MOBILE_TEST_REDIRECT_ROUTE"] = redirect
    if missing_anchor is not None:
        env["MOBILE_TEST_MISSING_ANCHOR"] = missing_anchor
    if ignore_term:
        env["MOBILE_TEST_IGNORE_TERM"] = "1"
    if stall_method is not None:
        env["MOBILE_TEST_STALL_METHOD"] = stall_method
    if delay_method is not None:
        env["MOBILE_TEST_DELAY_METHOD"] = delay_method
    if delay_ms is not None:
        env["MOBILE_TEST_DELAY_MS"] = str(delay_ms)
    if test_mode is not None:
        env["MOBILE_PROBE_TEST_MODE"] = "1" if test_mode else "0"
    if command_timeout_ms is not None:
        env["MOBILE_PROBE_TEST_COMMAND_TIMEOUT_MS"] = str(
            command_timeout_ms
        )
    if navigation_timeout_ms is not None:
        env["MOBILE_PROBE_TEST_NAVIGATION_TIMEOUT_MS"] = str(
            navigation_timeout_ms
        )
    if stderr_token_failure or cdp_error_method is not None:
        env["MOBILE_TEST_FAILURE_SENTINEL"] = MOBILE_TOKEN_SENTINEL
    if stderr_token_failure:
        env["MOBILE_TEST_STDERR_SENTINEL"] = "1"
    if cdp_error_method is not None:
        env["MOBILE_TEST_CDP_ERROR_METHOD"] = cdp_error_method
    if overall_timeout_ms is not None:
        env["MOBILE_PROBE_TEST_MODE"] = "1"
        env["MOBILE_PROBE_TEST_OVERALL_TIMEOUT_MS"] = str(
            overall_timeout_ms
        )
    if profile_rm_failures is not None:
        env["MOBILE_PROBE_TEST_MODE"] = "1"
        env["MOBILE_PROBE_TEST_PROFILE_RM_FAILURES"] = profile_rm_failures
    result = subprocess.run(
        [
            _test_node(),
            str(MOBILE_RELEASE_PROBE),
            str(fake_chrome),
            str(login),
            str(evidence),
            str(screenshot),
            str(work),
        ],
        text=True,
        capture_output=True,
        env=env,
        check=False,
        timeout=20,
    )
    return result, login, evidence, screenshot, args_log


def _assert_mobile_token_absent(
    result: subprocess.CompletedProcess[str],
    *paths: Path,
) -> None:
    sentinel = MOBILE_TOKEN_SENTINEL.encode()
    outputs = [result.stdout.encode(), result.stderr.encode()]
    outputs.extend(path.read_bytes() for path in paths if path.exists())
    assert all(sentinel not in output for output in outputs)


def test_mobile_probe_uses_fake_cdp_pipe_without_tcp_or_token_leak(
    tmp_path: Path,
) -> None:
    result, login, evidence, screenshot, args_log = _run_mobile_pipe_case(
        tmp_path,
        "accepted",
        cleanup_log=True,
    )

    assert result.returncode == 0, result.stderr
    assert evidence.is_file() and screenshot.is_file()
    assert not login.exists()
    args = json.loads(args_log.read_text(encoding="utf-8"))
    assert "--remote-debugging-pipe" in args
    assert not any("remote-debugging-port" in arg for arg in args)
    assert not any("remote-debugging-address" in arg for arg in args)
    cleanup_log = args_log.parent / "cleanup.log"
    _assert_mobile_token_absent(
        result,
        args_log,
        evidence,
        screenshot,
        cleanup_log,
    )
    assert not list((tmp_path / "accepted" / "work").glob("chrome-profile-*"))


@pytest.mark.parametrize("failure_source", ("chrome-stderr", "cdp-error"))
def test_mobile_probe_redacts_known_token_from_failure_outputs(
    tmp_path: Path,
    failure_source: str,
) -> None:
    result, login, evidence, screenshot, args_log = _run_mobile_pipe_case(
        tmp_path,
        f"redacted-{failure_source}",
        redirect=(
            "/maintenance/projects"
            if failure_source == "chrome-stderr"
            else None
        ),
        stderr_token_failure=failure_source == "chrome-stderr",
        cdp_error_method=(
            "Target.getTargets" if failure_source == "cdp-error" else None
        ),
        test_mode=True,
        cleanup_log=True,
    )
    cleanup_log = args_log.parent / "cleanup.log"

    assert result.returncode != 0
    assert "[REDACTED]" in result.stderr
    assert not login.exists()
    assert not evidence.exists()
    assert not screenshot.exists()
    args = json.loads(args_log.read_text(encoding="utf-8"))
    profile_arg = next(arg for arg in args if arg.startswith("--user-data-dir="))
    assert not Path(profile_arg.partition("=")[2]).exists()
    assert cleanup_log.read_text(encoding="utf-8").splitlines() == [
        "chrome-terminal",
        "profile-removed",
    ]
    _assert_mobile_token_absent(
        result,
        args_log,
        evidence,
        screenshot,
        cleanup_log,
    )


@pytest.mark.parametrize(
    ("redirect", "missing_anchor"),
    (
        ("/maintenance/projects", None),
        (None, "/maintenance/downloads"),
    ),
)
def test_mobile_probe_fails_closed_on_route_or_anchor_mismatch(
    tmp_path: Path,
    redirect: str | None,
    missing_anchor: str | None,
) -> None:
    result, login, evidence, screenshot, _args = _run_mobile_pipe_case(
        tmp_path,
        "rejected-route" if redirect else "rejected-anchor",
        redirect=redirect,
        missing_anchor=missing_anchor,
    )

    assert result.returncode != 0
    assert not login.exists()
    assert not evidence.exists()
    assert not screenshot.exists()


def test_mobile_probe_double_failure_still_cleans_response_and_profile(
    tmp_path: Path,
) -> None:
    result, login, evidence, screenshot, args_log = _run_mobile_pipe_case(
        tmp_path,
        "double-failure",
        redirect="/maintenance/projects",
        ignore_term=True,
    )

    assert result.returncode != 0
    assert not login.exists()
    assert not evidence.exists()
    assert not screenshot.exists()
    args = json.loads(args_log.read_text(encoding="utf-8"))
    profile_arg = next(arg for arg in args if arg.startswith("--user-data-dir="))
    assert not Path(profile_arg.partition("=")[2]).exists()


def test_mobile_probe_does_not_delete_unvalidated_external_login(
    tmp_path: Path,
) -> None:
    case = tmp_path / "unsafe-external-login"
    case.mkdir(mode=0o700)
    work = case / "work"
    work.mkdir(mode=0o700)
    external_login = case / "external-login.json"
    payload = '{"token":"must-remain"}'
    external_login.write_text(payload, encoding="utf-8")
    external_login.chmod(0o600)
    fake_chrome = case / "fake-chrome"
    _write_fake_pipe_chrome(fake_chrome)

    result = subprocess.run(
        [
            _test_node(),
            str(MOBILE_RELEASE_PROBE),
            str(fake_chrome),
            str(external_login),
            str(work / "evidence.txt"),
            str(work / "screen.png"),
            str(work),
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )

    assert result.returncode != 0
    assert external_login.read_text(encoding="utf-8") == payload
    assert not (work / "evidence.txt").exists()
    assert not (work / "screen.png").exists()
    assert not list(work.glob("chrome-profile-*"))


@pytest.mark.parametrize("stall_method", ("Target.getTargets", "Page.navigate"))
def test_mobile_probe_overall_timeout_cancels_work_before_cleanup(
    tmp_path: Path,
    stall_method: str,
) -> None:
    started = time.monotonic()
    result, login, evidence, screenshot, args_log = _run_mobile_pipe_case(
        tmp_path,
        "overall-timeout",
        stall_method=stall_method,
        overall_timeout_ms=50,
    )
    elapsed = time.monotonic() - started

    assert result.returncode != 0
    assert "mobile release probe timed out" in result.stderr
    assert elapsed < 5
    assert not login.exists()
    assert not evidence.exists()
    assert not screenshot.exists()
    args = json.loads(args_log.read_text(encoding="utf-8"))
    profile_arg = next(arg for arg in args if arg.startswith("--user-data-dir="))
    profile = Path(profile_arg.partition("=")[2])
    time.sleep(0.2)
    assert not profile.exists()
    assert not evidence.exists()
    assert not screenshot.exists()


@pytest.mark.parametrize(
    ("stall_method", "expected_error"),
    (
        ("Target.getTargets", "Target.getTargets timed out"),
        ("Page.navigate", "Page.navigate timed out"),
    ),
)
def test_mobile_probe_method_timeout_precedes_overall_and_cleans_up(
    tmp_path: Path,
    stall_method: str,
    expected_error: str,
) -> None:
    started = time.monotonic()
    result, login, evidence, screenshot, args_log = _run_mobile_pipe_case(
        tmp_path,
        f"method-timeout-{stall_method.replace('.', '-')}",
        stall_method=stall_method,
        command_timeout_ms=500,
        navigation_timeout_ms=800,
        overall_timeout_ms=2_500,
        cleanup_log=True,
    )
    elapsed = time.monotonic() - started

    assert result.returncode != 0
    assert expected_error in result.stderr
    assert "mobile release probe timed out" not in result.stderr
    assert elapsed < 5
    assert not login.exists()
    assert not evidence.exists()
    assert not screenshot.exists()
    args = json.loads(args_log.read_text(encoding="utf-8"))
    profile_arg = next(arg for arg in args if arg.startswith("--user-data-dir="))
    assert not Path(profile_arg.partition("=")[2]).exists()
    assert (args_log.parent / "cleanup.log").read_text(
        encoding="utf-8"
    ).splitlines() == ["chrome-terminal", "profile-removed"]


def test_mobile_probe_navigation_delay_uses_navigation_timeout_layer(
    tmp_path: Path,
) -> None:
    started = time.monotonic()
    result, login, evidence, screenshot, args_log = _run_mobile_pipe_case(
        tmp_path,
        "navigation-timeout-discrimination",
        delay_method="Page.navigate",
        delay_ms=600,
        command_timeout_ms=500,
        navigation_timeout_ms=800,
        overall_timeout_ms=2_500,
        cleanup_log=True,
    )
    elapsed = time.monotonic() - started

    assert result.returncode == 0, result.stderr
    assert 0.5 <= elapsed < 5
    assert not login.exists()
    assert evidence.is_file() and screenshot.is_file()
    args = json.loads(args_log.read_text(encoding="utf-8"))
    profile_arg = next(arg for arg in args if arg.startswith("--user-data-dir="))
    assert not Path(profile_arg.partition("=")[2]).exists()
    assert (args_log.parent / "cleanup.log").read_text(
        encoding="utf-8"
    ).splitlines() == ["chrome-terminal", "profile-removed"]


def test_mobile_probe_command_delay_uses_command_timeout_layer(
    tmp_path: Path,
) -> None:
    started = time.monotonic()
    result, login, evidence, screenshot, args_log = _run_mobile_pipe_case(
        tmp_path,
        "command-timeout-discrimination",
        delay_method="Target.getTargets",
        delay_ms=600,
        command_timeout_ms=500,
        navigation_timeout_ms=800,
        overall_timeout_ms=2_500,
        cleanup_log=True,
    )
    elapsed = time.monotonic() - started

    assert result.returncode != 0
    assert "Target.getTargets timed out" in result.stderr
    assert "Page.navigate timed out" not in result.stderr
    assert "mobile release probe timed out" not in result.stderr
    assert 0.2 <= elapsed < 5
    assert not login.exists()
    assert not evidence.exists()
    assert not screenshot.exists()
    args = json.loads(args_log.read_text(encoding="utf-8"))
    profile_arg = next(arg for arg in args if arg.startswith("--user-data-dir="))
    assert not Path(profile_arg.partition("=")[2]).exists()
    assert (args_log.parent / "cleanup.log").read_text(
        encoding="utf-8"
    ).splitlines() == ["chrome-terminal", "profile-removed"]


@pytest.mark.parametrize("delay_method", ("Target.getTargets", "Page.navigate"))
def test_mobile_probe_production_ignores_test_timeout_injection(
    tmp_path: Path,
    delay_method: str,
) -> None:
    result, login, evidence, screenshot, _args = _run_mobile_pipe_case(
        tmp_path,
        f"production-timeout-override-{delay_method.replace('.', '-')}",
        delay_method=delay_method,
        delay_ms=60,
        test_mode=False,
        command_timeout_ms=20,
        navigation_timeout_ms=20,
    )

    assert result.returncode == 0, result.stderr
    assert not login.exists()
    assert evidence.is_file() and screenshot.is_file()


@pytest.mark.parametrize(
    ("delay_method", "requested_timeout", "delay_ms"),
    (
        ("Target.getTargets", 1, 50),
        ("Page.navigate", 1, 50),
        ("Target.getTargets", 1_001, 1_050),
        ("Page.navigate", 1_001, 1_050),
    ),
)
def test_mobile_probe_rejects_out_of_bounds_test_method_timeout(
    tmp_path: Path,
    delay_method: str,
    requested_timeout: int,
    delay_ms: int,
) -> None:
    is_navigation = delay_method == "Page.navigate"
    result, login, evidence, screenshot, _args = _run_mobile_pipe_case(
        tmp_path,
        (
            f"bounded-timeout-{delay_method.replace('.', '-')}"
            f"-{requested_timeout}"
        ),
        delay_method=delay_method,
        delay_ms=delay_ms,
        test_mode=True,
        command_timeout_ms=(None if is_navigation else requested_timeout),
        navigation_timeout_ms=(requested_timeout if is_navigation else None),
        overall_timeout_ms=5_000,
    )

    assert result.returncode == 0, result.stderr
    assert not login.exists()
    assert evidence.is_file() and screenshot.is_file()


@pytest.mark.parametrize(
    "failures",
    (
        "ENOTEMPTY",
        "EBUSY,EPERM,ENOTEMPTY",
    ),
)
def test_mobile_probe_retries_transient_profile_removal_after_chrome_exit(
    tmp_path: Path,
    failures: str,
) -> None:
    result, login, evidence, screenshot, args_log = _run_mobile_pipe_case(
        tmp_path,
        f"profile-retry-{failures.replace(',', '-')}",
        profile_rm_failures=failures,
        cleanup_log=True,
    )
    case = args_log.parent
    events = (case / "cleanup.log").read_text(encoding="utf-8").splitlines()
    args = json.loads(args_log.read_text(encoding="utf-8"))
    profile_arg = next(arg for arg in args if arg.startswith("--user-data-dir="))

    assert result.returncode == 0, result.stderr
    assert not login.exists()
    assert evidence.is_file() and screenshot.is_file()
    assert not Path(profile_arg.partition("=")[2]).exists()
    assert events[0] == "chrome-terminal"
    assert events[1:-1] == [
        f"profile-remove-error:{code}" for code in failures.split(",")
    ]
    assert events[-1] == "profile-removed"


def test_mobile_probe_profile_retry_exhaustion_fails_closed_for_outer_cleanup(
    tmp_path: Path,
) -> None:
    failures = ",".join(["ENOTEMPTY"] * 6)
    result, login, evidence, screenshot, args_log = _run_mobile_pipe_case(
        tmp_path,
        "profile-retry-exhausted",
        profile_rm_failures=failures,
        cleanup_log=True,
    )
    work = login.parent
    args = json.loads(args_log.read_text(encoding="utf-8"))
    profile_arg = next(arg for arg in args if arg.startswith("--user-data-dir="))
    profile = Path(profile_arg.partition("=")[2])

    assert result.returncode != 0
    assert "ENOTEMPTY" in result.stderr
    assert not login.exists()
    assert not evidence.exists()
    assert not screenshot.exists()
    assert profile.is_dir() and stat.S_IMODE(profile.stat().st_mode) == 0o700
    assert "profile-removed" not in (
        args_log.parent / "cleanup.log"
    ).read_text(encoding="utf-8")
    shutil.rmtree(work)
    assert not work.exists()


def test_mobile_probe_does_not_retry_or_swallow_non_transient_profile_error(
    tmp_path: Path,
) -> None:
    result, login, evidence, screenshot, args_log = _run_mobile_pipe_case(
        tmp_path,
        "profile-non-transient",
        profile_rm_failures="EIO",
        cleanup_log=True,
    )
    events = (
        args_log.parent / "cleanup.log"
    ).read_text(encoding="utf-8").splitlines()
    args = json.loads(args_log.read_text(encoding="utf-8"))
    profile_arg = next(arg for arg in args if arg.startswith("--user-data-dir="))
    profile = Path(profile_arg.partition("=")[2])

    assert result.returncode != 0
    assert "EIO" in result.stderr
    assert events == ["chrome-terminal", "profile-remove-error:EIO"]
    assert profile.is_dir()
    assert not login.exists()
    assert not evidence.exists()
    assert not screenshot.exists()
    shutil.rmtree(login.parent)


def test_mobile_probe_static_pipe_contract_is_portable() -> None:
    probe = MOBILE_RELEASE_PROBE.read_text(encoding="utf-8")
    runbook = (
        ROOT / "docs" / "releases" / "edge-v120-scoped-runbook.md"
    ).read_text(encoding="utf-8")

    assert 'from "node:child_process"' in probe
    assert "--remote-debugging-pipe" in probe
    assert "remote-debugging-port" not in probe
    assert "remote-debugging-address" not in probe
    assert "remote-allow-origins" not in probe
    assert "WebSocket" not in probe
    assert "DevToolsActivePort" not in probe
    assert 'stdio: ["ignore", "ignore", "pipe", "pipe", "pipe"]' in probe
    assert "chrome.stdio[3]" in probe and "chrome.stdio[4]" in probe
    assert "Emulation.setDeviceMetricsOverride" in probe
    assert "const TEST_TIMEOUT_MIN_MS = 20;" in probe
    assert "const TEST_TIMEOUT_MAX_MS = 1_000;" in probe
    assert "boundedTestTimeout(" in probe
    assert "MOBILE_PROBE_TEST_COMMAND_TIMEOUT_MS" in probe
    assert "MOBILE_PROBE_TEST_NAVIGATION_TIMEOUT_MS" in probe
    assert "const NAVIGATION_TIMEOUT_MS = 30_000;" in probe
    assert "NAVIGATION_COMMAND_TIMEOUT_MS" in probe
    assert "OVERALL_TIMEOUT_MS" in probe
    assert "rejectPending" in probe
    assert 'chrome.kill("SIGTERM")' in probe
    assert 'chrome.kill("SIGKILL")' in probe
    assert "fs.rmSync(profilePath" in probe
    assert "fs.rmSync(validatedLoginPath" in probe
    assert "for (const [output, owned]" in probe
    assert "MOBILE_PROBE_TEST_ORIGIN" in probe
    assert "MOBILE_PROBE_TEST_MODE" in probe
    assert "MOBILE_PROBE_TEST_OVERALL_TIMEOUT_MS" in probe
    assert "mobile probe work directory is unsafe" in probe
    assert "must be a direct child of the private work dir" in probe
    assert "env -u MOBILE_PROBE_TEST_ORIGIN -u MOBILE_PROBE_TEST_MODE" in runbook
    assert "mobile_listeners_before" in runbook
    assert "mobile_listeners_after" in runbook
    assert "origin=https://hbzgc.icu" in runbook
    assert (
        "CHROME_REAL_SHA256_EXPECTED="
        "4cf210c4a0aeee3e69a73639260918a7448626d6b99892ec61e20750bc7c7079"
        in runbook
    )
    assert (
        "CHROME_LAUNCHER_SHA256_EXPECTED="
        "aea09d69ce7f24d5901f6bfb15dd44d0c856e793e0a498f8d8393ec7d2c308ec"
        in runbook
    )
    assert (
        "NODE_SHA256_EXPECTED="
        "f3432a45b03b2da0d270095fdd8813dc34cbea73f5fc8b18c7a384b7cf9b333a"
        in runbook
    )
    assert (
        "CHROME_LAUNCHER=/opt/google/chrome/google-chrome" in runbook
    )
    assert "CHROME_REAL_BIN=/opt/google/chrome/chrome" in runbook
    assert (
        "CHROME_REAL_BIN=/opt/google/chrome/google-chrome" not in runbook
    )
    assert "assert_secure_release_parent() {" in runbook
    assert "assert_exact_release_file() {" in runbook
    assert "release_file_identity() {" in runbook
    assert "regular file|0|0|755|1" in runbook
    assert "8#$mode & 8#22" in runbook
    assert 'od -An -tx1 -N4 "$CHROME_REAL_BIN"' in runbook
    assert 'od -An -tx1 -N4 "$NODE_BIN"' in runbook
    assert "CHROME_LAUNCHER_IDENTITY_BEFORE=" in runbook
    assert "CHROME_REAL_IDENTITY_BEFORE=" in runbook
    assert "NODE_IDENTITY_BEFORE=" in runbook
    assert "NODE_BIN=/usr/bin/node" in runbook
    assert '"$mobile_script" "$CHROME_LAUNCHER"' in runbook


@pytest.mark.skipif(
    os.environ.get("IT_DATA_RELEASE_HOST_LIVE") != "1",
    reason="release-host Chrome pipe smoke is explicitly opt-in",
)
def test_mobile_probe_real_chrome_pipe_against_loopback_fixture(
    tmp_path: Path,
) -> None:
    class FixtureHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/api/maintenance/board/export?"):
                body = b"contract,profit\\nfixture,1.00\\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header(
                    "Content-Disposition",
                    "attachment; filename=fixture.csv",
                )
                self.send_header("Cache-Control", "no-store")
            else:
                body = (
                    "<!doctype html><meta charset=utf-8>"
                    "<meta name=viewport content='width=device-width'>"
                    "<main>详细盈亏 下载中心 项目提醒</main>"
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        case = tmp_path / "real-chrome-pipe"
        case.mkdir(mode=0o700)
        work = case / "work"
        work.mkdir(mode=0o700)
        login = work / "login.json"
        login.write_text(
            json.dumps(
                {
                    "token": "fixture-only-token",
                    "role": "admin",
                    "name": "fixture",
                    "permissions": {},
                }
            ),
            encoding="utf-8",
        )
        login.chmod(0o600)
        evidence = work / "evidence.txt"
        screenshot = work / "screen.png"
        listeners_before = subprocess.run(
            ["ss", "-H", "-ltnp"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        env = os.environ.copy()
        env["MOBILE_PROBE_TEST_MODE"] = "1"
        env["MOBILE_PROBE_TEST_ORIGIN"] = (
            f"http://127.0.0.1:{server.server_port}"
        )

        result = subprocess.run(
            [
                _test_node(),
                str(MOBILE_RELEASE_PROBE),
                "/opt/google/chrome/google-chrome",
                str(login),
                str(evidence),
                str(screenshot),
                str(work),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=45,
            env=env,
        )
        listeners_after = subprocess.run(
            ["ss", "-H", "-ltnp"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout

        assert result.returncode == 0, result.stderr
        assert listeners_after == listeners_before
        assert not login.exists()
        assert evidence.is_file() and evidence.stat().st_size > 0
        assert screenshot.is_file() and screenshot.stat().st_size > 0
        assert (
            f"origin=http://127.0.0.1:{server.server_port}"
            in evidence.read_text(encoding="utf-8")
        )
        assert not list(work.glob("chrome-profile-*"))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_built_retry_rebuilds_only_its_marked_evidence_directory(
    tmp_path: Path,
) -> None:
    release_id = DEFAULT_RELEASE_ID
    evidence = tmp_path / f"{release_id}-release"
    evidence_parent = evidence.parent
    evidence_parent.chmod(0o700)
    state_hash = "9" * 64
    body = (
        'v120_prepare_evidence_dir "$1" "$2" "$3" "$4"; '
        'printf partial > "$1/partial-artifact"; '
        'v120_prepare_evidence_dir "$1" "$2" "$3" "$4"'
    )

    result = _run_release_library(
        body,
        str(evidence),
        release_id,
        DEFAULT_TARGET,
        state_hash,
    )

    assert result.returncode == 0, result.stderr
    assert evidence.is_dir()
    assert not (evidence / "partial-artifact").exists()
    assert (evidence / ".v120-evidence.marker").read_text(
        encoding="ascii"
    ) == (
        "EVIDENCE_FORMAT=v120-evidence-1\n"
        f"RELEASE_ID={release_id}\n"
        f"TARGET_COMMIT={DEFAULT_TARGET}\n"
        f"STATE_HASH={state_hash}\n"
    )


def test_built_retry_recovers_after_sigkill_during_evidence_reset(
    tmp_path: Path,
) -> None:
    release_id = DEFAULT_RELEASE_ID
    evidence = tmp_path / f"{release_id}-release"
    evidence.parent.chmod(0o700)
    state_hash = "9" * 64
    args = (
        str(evidence),
        release_id,
        DEFAULT_TARGET,
        state_hash,
    )
    invocation = 'v120_prepare_evidence_dir "$1" "$2" "$3" "$4"'
    initial = _run_release_library(invocation, *args)
    assert initial.returncode == 0, initial.stderr
    (evidence / "partial-artifact").write_text("partial", encoding="ascii")

    interrupted = _run_release_library(
        invocation,
        *args,
        env_overrides={
            "V120_STATE_TEST_FAILPOINT": "after_evidence_quarantine",
        },
    )

    assert interrupted.returncode == -signal.SIGKILL
    assert not evidence.exists()
    quarantine = evidence.parent / f".{release_id}-evidence.reset"
    assert quarantine.is_dir()

    resumed = _run_release_library(invocation, *args)

    assert resumed.returncode == 0, resumed.stderr
    assert evidence.is_dir()
    assert not quarantine.exists()
    assert not (evidence / "partial-artifact").exists()
    assert (evidence / ".v120-evidence.marker").is_file()


def test_built_retry_refuses_unmarked_directory_without_deleting_data(
    tmp_path: Path,
) -> None:
    release_id = DEFAULT_RELEASE_ID
    evidence = tmp_path / f"{release_id}-release"
    evidence.mkdir(mode=0o700)
    evidence.chmod(0o700)
    protected = evidence / "business-data"
    protected.write_text("must-remain", encoding="ascii")

    result = _run_release_library(
        'v120_prepare_evidence_dir "$1" "$2" "$3" "$4"',
        str(evidence),
        release_id,
        DEFAULT_TARGET,
        "9" * 64,
    )

    assert result.returncode == 74
    assert protected.read_text(encoding="ascii") == "must-remain"


def test_mirror_failpoint_preserves_old_state_bytes(tmp_path: Path) -> None:
    state = tmp_path / "release.state"
    candidate = tmp_path / "authority.state"
    before = _built_state().encode("ascii")
    authority = _built_values()
    authority["SOURCE_HASH"] = "8" * 64
    state.write_bytes(before)
    candidate.write_text(_render_state(authority), encoding="ascii")
    state.chmod(0o600)
    candidate.chmod(0o600)
    env = os.environ.copy()
    env.update(
        {
            "V120_STATE_TEST_MODE": "1",
            "V120_STATE_TEST_FAILPOINT": "before_mirror_rename",
        }
    )

    result = _run_state(
        state,
        'v120_state_commit_mirror "$1" "$2"',
        str(candidate),
        env=env,
    )

    assert result.returncode == 74
    assert state.read_bytes() == before
    assert candidate.is_file()


@pytest.mark.parametrize("call_context", ["if_not", "or_list"])
@pytest.mark.parametrize("failed_io", ["mv", "sync_file", "sync_directory"])
def test_mirror_io_failure_propagates_from_conditional_call_context(
    tmp_path: Path,
    call_context: str,
    failed_io: str,
) -> None:
    state = tmp_path / "release.state"
    candidate = tmp_path / "authority.state"
    state.write_text(_built_state(), encoding="ascii")
    authority = _built_values()
    authority["SOURCE_HASH"] = "8" * 64
    candidate.write_text(_render_state(authority), encoding="ascii")
    state.chmod(0o600)
    candidate.chmod(0o600)

    if failed_io == "mv":
        failure_stub = "mv() { return 41; }; sync() { return 0; };"
    elif failed_io == "sync_file":
        failure_stub = (
            'sync() { [ "${1:-}" != -f ] || return 42; return 0; };'
        )
    else:
        failure_stub = (
            'sync() { [ "${1:-}" != -d ] || return 43; return 0; };'
        )
    if call_context == "if_not":
        invocation = (
            'if ! v120_state_commit_mirror "$1" "$2"; then exit 91; fi; '
            'exit 0'
        )
    else:
        invocation = (
            'v120_state_commit_mirror "$1" "$2" || exit 92; exit 0'
        )

    result = _run_state(
        state,
        f"{failure_stub} {invocation}",
        str(candidate),
    )

    assert result.returncode in {91, 92}, result.stderr


def test_illegal_phase_jump_is_rejected_without_modifying_state(
    tmp_path: Path,
) -> None:
    state = tmp_path / "release.state"
    before = _built_state().encode("ascii")
    state.write_bytes(before)
    state.chmod(0o600)
    args = _prepared_update_args()[:-2]
    args += [
        "BACKUP",
        "/var/backups/spareparts/db-20260730-1600.dump",
        "BACKUP_HASH",
        "8" * 64,
        "NEW_APP_CID",
        "9" * 64,
        "PUBLIC_OPENED_AT",
        "2026-07-30T16:00:00+08:00",
        "RELEASE_PHASE",
        "opening",
    ]

    result = _run_state(
        state,
        'state=$1; shift; v120_state_update_atomic "$state" "$@"',
        *args,
    )

    assert result.returncode == 73
    assert state.read_bytes() == before


@pytest.mark.parametrize(
    (
        "start_phase",
        "start_policy",
        "target_phase",
        "target_policy",
        "expected",
    ),
    [
        ("prepared", "old_allowed", "rolled_back", "old_allowed", True),
        (
            "backup_verified",
            "old_allowed",
            "rolled_back",
            "old_allowed",
            True,
        ),
        ("prepared", "old_allowed", "failed_closed", "old_allowed", False),
        (
            "backup_verified",
            "old_allowed",
            "failed_closed",
            "old_allowed",
            False,
        ),
        (
            "prepared",
            "forward_only",
            "failed_closed",
            "forward_only",
            True,
        ),
        (
            "backup_verified",
            "forward_only",
            "failed_closed",
            "forward_only",
            True,
        ),
        (
            "prepared",
            "forward_only",
            "rolled_back",
            "forward_only",
            False,
        ),
        (
            "backup_verified",
            "forward_only",
            "rolled_back",
            "forward_only",
            False,
        ),
        (
            "opening",
            "forward_only",
            "failed_closed",
            "forward_only",
            True,
        ),
        (
            "switched",
            "forward_only",
            "failed_closed",
            "forward_only",
            True,
        ),
        (
            "backup_verified",
            "old_allowed",
            "opening",
            "forward_only",
            True,
        ),
    ],
)
def test_rollback_policy_phase_table(
    tmp_path: Path,
    start_phase: str,
    start_policy: str,
    target_phase: str,
    target_policy: str,
    expected: bool,
) -> None:
    state = tmp_path / "release.state"
    attempt_no = 2 if start_policy == "forward_only" else 1
    before = _phase_state(
        start_phase,
        rollback_policy=start_policy,
        attempt_no=attempt_no,
    ).encode("ascii")
    state.write_bytes(before)
    state.chmod(0o600)
    if target_phase == "rolled_back":
        updates = [
            "ROLLED_BACK_AT",
            "2026-07-30T16:20:00+08:00",
            "ROLLBACK_POLICY",
            target_policy,
            "RELEASE_PHASE",
            target_phase,
        ]
    elif target_phase == "failed_closed":
        updates = [
            "FAILED_AT",
            "2026-07-30T16:20:00+08:00",
            "ROLLBACK_POLICY",
            target_policy,
            "RELEASE_PHASE",
            target_phase,
        ]
    else:
        updates = [
            "NEW_APP_CID",
            "b" * 64,
            "PUBLIC_OPENED_AT",
            "2026-07-30T16:20:00+08:00",
            "ROLLBACK_POLICY",
            target_policy,
            "RELEASE_PHASE",
            target_phase,
        ]

    result = _run_state(
        state,
        'state=$1; shift; v120_state_update_atomic "$state" "$@"',
        *updates,
    )

    if expected:
        assert result.returncode == 0, result.stderr
        loaded = _run_state(
            state,
            'v120_state_load "$1"; '
            'printf "%s %s\\n" "$ROLLBACK_POLICY" "$RELEASE_PHASE"',
        )
        assert loaded.returncode == 0, loaded.stderr
        assert loaded.stdout == f"{target_policy} {target_phase}\n"
    else:
        assert result.returncode in {64, 73}
        assert state.read_bytes() == before


@pytest.mark.parametrize(
    ("parent_phase", "parent_policy", "child_policy", "expected"),
    [
        ("rolled_back", "old_allowed", "old_allowed", True),
        ("rolled_back", "old_allowed", "forward_only", False),
        ("failed_closed", "forward_only", "forward_only", True),
        ("failed_closed", "forward_only", "old_allowed", False),
        ("prepared", "old_allowed", "old_allowed", False),
        ("observed", "forward_only", "forward_only", False),
    ],
)
def test_supersession_policy_phase_table(
    tmp_path: Path,
    parent_phase: str,
    parent_policy: str,
    child_policy: str,
    expected: bool,
) -> None:
    parent_attempt = 2 if parent_policy == "forward_only" else 1
    parent = tmp_path / "parent.state"
    parent.write_text(
        _phase_state(
            parent_phase,
            rollback_policy=parent_policy,
            attempt_no=parent_attempt,
        ),
        encoding="ascii",
    )
    parent.chmod(0o600)
    parent_hash = hashlib.sha256(parent.read_bytes()).hexdigest()
    parent_release_id = _phase_values(
        parent_phase,
        rollback_policy=parent_policy,
        attempt_no=parent_attempt,
    )["RELEASE_ID"]
    child = tmp_path / "child.state"
    child.write_text(
        _built_state(
            target="b" * 40,
            attempt_no=parent_attempt + 1,
            parent_release_id=parent_release_id,
            parent_state_hash=parent_hash,
            rollback_policy=child_policy,
        ),
        encoding="ascii",
    )
    child.chmod(0o600)

    result = _run_state(
        parent,
        'declare -A old_state=() new_state=(); '
        'v120_state_parse_to_array "$1" old_state; '
        'v120_state_parse_to_array "$2" new_state; '
        'v120_state_validate_supersession old_state new_state "$3"',
        str(child),
        parent_hash,
    )

    assert (result.returncode == 0) is expected, result.stderr


def test_observed_release_can_be_superseded_with_exact_runtime_parent(
    tmp_path: Path,
) -> None:
    parent_values = _phase_values(
        "observed",
        rollback_policy="forward_only",
        attempt_no=2,
    )
    parent = tmp_path / "parent.state"
    parent.write_text(_render_state(parent_values), encoding="ascii")
    parent.chmod(0o600)
    parent_hash = hashlib.sha256(parent.read_bytes()).hexdigest()
    child = tmp_path / "child.state"
    child.write_text(
        _built_state(
            target="b" * 40,
            attempt_no=3,
            parent_release_id=parent_values["RELEASE_ID"],
            parent_state_hash=parent_hash,
            rollback_policy="old_allowed",
            old_running_source_commit=parent_values["TARGET_COMMIT"],
            old_app_image_id=parent_values["NEW_APP_IMAGE_ID"],
            old_frontend_image_id=parent_values["NEW_FRONTEND_IMAGE_ID"],
        ),
        encoding="ascii",
    )
    child.chmod(0o600)

    result = _run_state(
        parent,
        'declare -A old_state=() new_state=(); '
        'v120_state_parse_to_array "$1" old_state; '
        'v120_state_parse_to_array "$2" new_state; '
        'v120_state_validate_supersession old_state new_state "$3"',
        str(child),
        parent_hash,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "wrong_field",
    (
        "OLD_RUNNING_SOURCE_COMMIT",
        "OLD_APP_IMAGE_ID",
        "OLD_FRONTEND_IMAGE_ID",
    ),
)
def test_observed_supersession_rejects_runtime_not_bound_to_parent(
    tmp_path: Path,
    wrong_field: str,
) -> None:
    parent_values = _phase_values(
        "observed",
        rollback_policy="forward_only",
        attempt_no=2,
    )
    parent = tmp_path / "parent.state"
    parent.write_text(_render_state(parent_values), encoding="ascii")
    parent.chmod(0o600)
    parent_hash = hashlib.sha256(parent.read_bytes()).hexdigest()
    child_values = _built_values(
        target="b" * 40,
        attempt_no=3,
        parent_release_id=parent_values["RELEASE_ID"],
        parent_state_hash=parent_hash,
        rollback_policy="old_allowed",
        old_running_source_commit=parent_values["TARGET_COMMIT"],
        old_app_image_id=parent_values["NEW_APP_IMAGE_ID"],
        old_frontend_image_id=parent_values["NEW_FRONTEND_IMAGE_ID"],
    )
    if wrong_field.endswith("_IMAGE_ID"):
        child_values[wrong_field] = "sha256:" + "0" * 64
    else:
        child_values[wrong_field] = "e" * 40
    child = tmp_path / "child.state"
    child.write_text(_render_state(child_values), encoding="ascii")
    child.chmod(0o600)

    result = _run_state(
        parent,
        'declare -A old_state=() new_state=(); '
        'v120_state_parse_to_array "$1" old_state; '
        'v120_state_parse_to_array "$2" new_state; '
        'v120_state_validate_supersession old_state new_state "$3"',
        str(child),
        parent_hash,
    )

    assert result.returncode == 73


@pytest.mark.parametrize(
    "phase",
    ("prepared", "backup_verified", "opening", "switched"),
)
def test_supersession_base_rejects_inflight_parent_phase(
    tmp_path: Path,
    phase: str,
) -> None:
    parent = tmp_path / "parent.state"
    parent.write_text(_phase_state(phase), encoding="ascii")
    parent.chmod(0o600)

    result = _run_state(
        parent,
        'declare -A parent=() base=(); '
        'v120_state_parse_to_array "$1" parent; '
        "v120_state_select_supersession_base parent base",
    )

    assert result.returncode == 73


def test_observed_parent_selects_promoted_images_as_rollback_base(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "parent.state"
    parent_values = _phase_values(
        "observed",
        rollback_policy="forward_only",
        attempt_no=2,
    )
    parent.write_text(_render_state(parent_values), encoding="ascii")
    parent.chmod(0o600)

    result = _run_state(
        parent,
        'declare -A parent=() base=(); '
        'v120_state_parse_to_array "$1" parent; '
        'v120_state_select_supersession_base parent base; '
        'printf "%s\\n%s\\n%s\\n%s\\n%s\\n" '
        '"${base[RUNNING_SOURCE_COMMIT]}" "${base[APP_IMAGE_ID]}" '
        '"${base[FRONTEND_IMAGE_ID]}" "${base[ROLLBACK_POLICY]}" '
        '"${base[REQUIRE_RUNNING]}"',
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        parent_values["TARGET_COMMIT"],
        parent_values["NEW_APP_IMAGE_ID"],
        parent_values["NEW_FRONTEND_IMAGE_ID"],
        "old_allowed",
        "1",
    ]


def test_release_and_rollback_accept_parent_bound_dynamic_running_source() -> None:
    release = _script(RELEASE)
    rollback = _script(ROLLBACK)

    assert "EXPECTED_OLD_RUNNING_SOURCE_COMMIT" not in release
    assert "EXPECTED_OLD_RUNNING_SOURCE_COMMIT" not in rollback
    release_main = release.split("trap release_abort EXIT", 1)[1]
    authority_check = 'v120_evidence_reset_authorized "$STATE"'
    container_read = "BASE_DB_CID=$(compose ps -q db)"
    assert release_main.index(authority_check) < release_main.index(container_read)


def test_supersession_rejects_wrong_parent_state_hash(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "parent.state"
    parent.write_text(_phase_state("rolled_back"), encoding="ascii")
    parent.chmod(0o600)
    actual_hash = hashlib.sha256(parent.read_bytes()).hexdigest()
    child = tmp_path / "child.state"
    child.write_text(
        _built_state(
            target="b" * 40,
            attempt_no=2,
            parent_release_id=DEFAULT_RELEASE_ID,
            parent_state_hash="1" * 64,
        ),
        encoding="ascii",
    )
    child.chmod(0o600)

    result = _run_state(
        parent,
        'declare -A old_state=() new_state=(); '
        'v120_state_parse_to_array "$1" old_state; '
        'v120_state_parse_to_array "$2" new_state; '
        'v120_state_validate_supersession old_state new_state "$3"',
        str(child),
        actual_hash,
    )

    assert result.returncode == 73


def test_second_operation_fails_fast_on_shared_directory_lock(
    tmp_path: Path,
) -> None:
    lock = tmp_path / "release.lock"
    lock.mkdir(mode=0o750)
    lock.chmod(0o750)
    account = pwd.getpwuid(os.getuid())
    group = grp.getgrgid(os.getgid())
    expected = f"750 {account.pw_name}:{group.gr_name}"
    holder = subprocess.Popen(
        [
            "bash",
            "-c",
            'source "$1"; v120_acquire_lock "$2" "$3"; '
            'printf "held\\n"; sleep 30',
            "bash",
            str(STATE_LIBRARY),
            str(lock),
            expected,
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline() == "held\n"
        inode = lock.stat().st_ino
        subprocess.run(
            ["install", "-d", "-m", "750", str(lock)],
            check=True,
        )
        assert lock.stat().st_ino == inode
        started = time.monotonic()
        contender = subprocess.run(
            [
                "bash",
                "-c",
                'source "$1"; v120_acquire_lock "$2" "$3"',
                "bash",
                str(STATE_LIBRARY),
                str(lock),
                expected,
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        elapsed = time.monotonic() - started
        assert contender.returncode == 75
        assert elapsed < 1
    finally:
        try:
            os.killpg(holder.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        holder.wait(timeout=5)


def test_directory_lock_rejects_symlink_without_repair(
    tmp_path: Path,
) -> None:
    real_lock = tmp_path / "real-lock"
    real_lock.mkdir(mode=0o750)
    real_lock.chmod(0o750)
    lock = tmp_path / "release.lock"
    lock.symlink_to(real_lock, target_is_directory=True)
    account = pwd.getpwuid(os.getuid())
    group = grp.getgrgid(os.getgid())
    expected = f"750 {account.pw_name}:{group.gr_name}"

    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; v120_acquire_lock "$2" "$3"',
            "bash",
            str(STATE_LIBRARY),
            str(lock),
            expected,
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 75
    assert lock.is_symlink()
    assert lock.resolve() == real_lock.resolve()


@pytest.mark.skipif(os.geteuid() == 0, reason="builder forbids root")
def test_control_package_is_built_from_exact_git_objects(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    output = tmp_path / "packages"
    repo.mkdir()
    output.mkdir()
    source_paths = (
        ".deploy/v120_state.sh",
        ".deploy/sync_v120_root_state.sh",
        ".deploy/rollback_v120.sh",
        ".deploy/install_v120_control.sh",
        ".deploy/hsts_v120_root.sh",
        ".deploy/hsts_v120_operator.sh",
        ".deploy/edge_v120_root.sh",
        ".deploy/edge_v120_operator.sh",
        ".deploy/it-spareparts.cron",
    )
    supply_paths = (
        "backend/Dockerfile",
        "backend/requirements.lock",
        "backend/uv.lock",
        "backend/dependency-sbom.cdx.json",
        "frontend/Dockerfile",
        "frontend/package-lock.json",
        "frontend/dependency-sbom.cdx.json",
    )
    for relative in (*source_paths, *supply_paths):
        destination = repo / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, destination)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.name", "v120-test"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "v120-test@example.invalid"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-qm", "fixture"],
        cwd=repo,
        check=True,
    )
    target = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()

    result = subprocess.run(
        [str(PACKAGE_CONTROL), target, str(output)],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    match = re.fullmatch(
        r"PACKAGE_OK path=(.+) manifest_sha256=([0-9a-f]{64}) "
        rf"target={target}\n",
        result.stdout,
    )
    assert match is not None
    package = Path(match.group(1))
    manifest_hash = match.group(2)
    assert package == output / f"it-spareparts-control-{manifest_hash}"
    assert package.stat().st_mode & 0o777 == 0o700
    manifest = package / "manifest.txt"
    assert hashlib.sha256(manifest.read_bytes()).hexdigest() == manifest_hash
    assert manifest.stat().st_mode & 0o777 == 0o600
    assert manifest.read_text(encoding="ascii").splitlines()[:2] == [
        "CONTROL_FORMAT=v120-control-3",
        f"TARGET_COMMIT={target}",
    ]
    source_tar = package / "source.tar"
    assert source_tar.is_file()
    assert not source_tar.is_symlink()
    assert source_tar.stat().st_mode & 0o777 == 0o600
    source_hash = hashlib.sha256(source_tar.read_bytes()).hexdigest()
    assert (
        f"SOURCE_TAR_SHA256={source_hash}"
        in manifest.read_text(encoding="ascii").splitlines()
    )
    archived_target = subprocess.run(
        ["git", "get-tar-commit-id"],
        input=source_tar.read_bytes(),
        capture_output=True,
        check=True,
    ).stdout.decode("ascii").strip()
    assert archived_target == target
    package_names = (
        "v120_state.sh",
        "sync-v120-root-state.sh",
        "rollback-v120.sh",
        "install-v120-control.sh",
        "hsts-v120-root.sh",
        "hsts-v120-operator.sh",
        "edge-v120-root.sh",
        "edge-v120-operator.sh",
        "it-spareparts.cron",
    )
    for relative, name in zip(source_paths, package_names, strict=True):
        artifact = package / name
        assert artifact.read_bytes() == (repo / relative).read_bytes()
        expected_mode = 0o700 if name.endswith(".sh") else 0o600
        assert artifact.stat().st_mode & 0o777 == expected_mode
        assert not artifact.is_symlink()


@pytest.mark.skipif(os.geteuid() == 0, reason="builder forbids root")
def test_control_packager_rejects_forged_loose_git_object(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    output = tmp_path / "packages"
    repo.mkdir()
    output.mkdir()
    source_paths = (
        ".deploy/v120_state.sh",
        ".deploy/sync_v120_root_state.sh",
        ".deploy/rollback_v120.sh",
        ".deploy/install_v120_control.sh",
        ".deploy/hsts_v120_root.sh",
        ".deploy/hsts_v120_operator.sh",
        ".deploy/edge_v120_root.sh",
        ".deploy/edge_v120_operator.sh",
        ".deploy/it-spareparts.cron",
    )
    supply_paths = (
        "backend/Dockerfile",
        "backend/requirements.lock",
        "backend/uv.lock",
        "backend/dependency-sbom.cdx.json",
        "frontend/Dockerfile",
        "frontend/package-lock.json",
        "frontend/dependency-sbom.cdx.json",
    )
    for relative in (*source_paths, *supply_paths):
        destination = repo / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, destination)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.name", "v120-test"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "v120-test@example.invalid"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-qm", "fixture"],
        cwd=repo,
        check=True,
    )
    target = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    safe_oid = subprocess.run(
        ["git", "rev-parse", f"{target}:{source_paths[0]}"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    evil = repo / "evil"
    evil.write_text("#!/usr/bin/env bash\nprintf 'forged\\n'\n", encoding="utf-8")
    evil_oid = subprocess.run(
        ["git", "hash-object", "-w", str(evil)],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    object_dir = repo / ".git" / "objects"
    safe_object = object_dir / safe_oid[:2] / safe_oid[2:]
    evil_object = object_dir / evil_oid[:2] / evil_oid[2:]
    safe_object.chmod(0o600)
    shutil.copyfile(evil_object, safe_object)

    result = subprocess.run(
        [str(PACKAGE_CONTROL), target, str(output)],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "PACKAGE_OK" not in result.stdout
    assert list(output.iterdir()) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="requires non-root")
def test_root_installer_refuses_non_root_execution() -> None:
    result = subprocess.run(
        [str(INSTALL_CONTROL), "verify", "0" * 64],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "installer must run as root" in result.stderr


def test_installer_rejects_symlink_directory_without_changing_target_mode(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o711)
    target.chmod(0o711)
    link = tmp_path / "control"
    link.symlink_to(target, target_is_directory=True)
    account = pwd.getpwuid(os.getuid())
    group = grp.getgrgid(os.getgid())
    env = os.environ.copy()
    env.update(
        {
            "V120_STATE_TEST_MODE": "1",
            "V120_INSTALLER_LIBRARY_ONLY": "1",
        }
    )

    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; '
            'ensure_new_or_exact_directory "$2" 700 "$3" "$4"',
            "bash",
            str(INSTALL_CONTROL),
            str(link),
            account.pw_name,
            group.gr_name,
        ],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode != 0
    assert "unsafe directory" in result.stderr
    assert link.is_symlink()
    assert target.stat().st_mode & 0o777 == 0o711


def test_installer_creates_exact_persistent_shared_caddy_lock(
    tmp_path: Path,
) -> None:
    lock = tmp_path / "shared-caddy.lock"
    account = pwd.getpwuid(os.getuid())
    group = grp.getgrgid(os.getgid())
    env = os.environ.copy()
    env.update(
        {
            "V120_STATE_TEST_MODE": "1",
            "V120_INSTALLER_LIBRARY_ONLY": "1",
        }
    )
    command = (
        'source "$1"; '
        'ensure_new_or_exact_lock_file "$2" "$3" "$4"'
    )

    created = subprocess.run(
        [
            "bash",
            "-c",
            command,
            "bash",
            str(INSTALL_CONTROL),
            str(lock),
            account.pw_name,
            group.gr_name,
        ],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert created.returncode == 0, created.stderr
    metadata = lock.stat()
    assert stat.S_ISREG(metadata.st_mode)
    assert stat.S_IMODE(metadata.st_mode) == 0o600
    assert metadata.st_nlink == 1
    hardlink = tmp_path / "shared-caddy-hardlink"
    os.link(lock, hardlink)
    rejected = subprocess.run(
        [
            "bash",
            "-c",
            command,
            "bash",
            str(INSTALL_CONTROL),
            str(lock),
            account.pw_name,
            group.gr_name,
        ],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert rejected.returncode != 0
    assert "lock file" in rejected.stderr


def test_bootstrap_authorization_is_explicit_and_never_self_minted(
    tmp_path: Path,
) -> None:
    authorization = tmp_path / "bootstrap.authorization"
    manifest = tmp_path / "manifest.txt"
    target = "4" * 40
    manifest.write_text(
        "\n".join(
            (
                "CONTROL_FORMAT=v120-control-3",
                f"TARGET_COMMIT={target}",
                "V120_STATE_SHA256=" + "5" * 64,
                "ROOT_SYNC_SHA256=" + "6" * 64,
                "ROLLBACK_SHA256=" + "7" * 64,
                "INSTALLER_SHA256=" + "8" * 64,
                "HSTS_ROOT_SHA256=" + "b" * 64,
                "HSTS_OPERATOR_SHA256=" + "c" * 64,
                "EDGE_ROOT_SHA256=" + "d" * 64,
                "EDGE_OPERATOR_SHA256=" + "e" * 64,
                "CRON_SHA256=" + "9" * 64,
                "SOURCE_TAR_SHA256=" + "a" * 64,
                "BACKEND_REQUIREMENTS_SHA256=" + "1" * 64,
                "BACKEND_UV_LOCK_SHA256=" + "2" * 64,
                "FRONTEND_PACKAGE_LOCK_SHA256=" + "3" * 64,
                "BACKEND_SBOM_SHA256=" + "4" * 64,
                "FRONTEND_SBOM_SHA256=" + "5" * 64,
                "BACKEND_BASE_DIGEST=" + "6" * 64,
                "FRONTEND_BUILD_BASE_DIGEST=" + "7" * 64,
                "FRONTEND_RUNTIME_BASE_DIGEST=" + "8" * 64,
            )
        )
        + "\n",
        encoding="ascii",
    )
    manifest.chmod(0o600)
    manifest_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
    env = os.environ.copy()
    env.update(
        {
            "V120_STATE_TEST_MODE": "1",
            "V120_INSTALLER_LIBRARY_ONLY": "1",
        }
    )
    command = (
        'source "$1"; '
        'validate_bootstrap_authorization "$2" "$3" "$4"'
    )

    missing = subprocess.run(
        [
            "bash",
            "-c",
            command,
            "bash",
            str(INSTALL_CONTROL),
            str(authorization),
            str(manifest),
            manifest_hash,
        ],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert missing.returncode != 0
    assert not authorization.exists()

    authorization.write_text(
        "\n".join(
            (
                "AUTHORIZATION_FORMAT=v120-bootstrap-1",
                f"CONTROL_MANIFEST_HASH={manifest_hash}",
                f"TARGET_COMMIT={target}",
            )
        )
        + "\n",
        encoding="ascii",
    )
    authorization.chmod(0o600)
    accepted = subprocess.run(
        [
            "bash",
            "-c",
            command,
            "bash",
            str(INSTALL_CONTROL),
            str(authorization),
            str(manifest),
            manifest_hash,
        ],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert accepted.returncode == 0, accepted.stderr


@pytest.mark.parametrize(
    ("marker", "state", "control", "authorization", "expected"),
    [
        (1, 1, 1, 0, "existing"),
        (0, 0, 0, 1, "initializing"),
        (0, 0, 1, 1, None),
        (0, 1, 1, 0, None),
        (1, 0, 1, 0, None),
        (0, 0, 0, 0, None),
    ],
)
def test_authority_evidence_loss_never_becomes_initialization(
    marker: int,
    state: int,
    control: int,
    authorization: int,
    expected: str | None,
) -> None:
    env = os.environ.copy()
    env.update(
        {
            "V120_STATE_TEST_MODE": "1",
            "V120_INSTALLER_LIBRARY_ONLY": "1",
        }
    )
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; authority_evidence_mode "$2" "$3" "$4" "$5"',
            "bash",
            str(INSTALL_CONTROL),
            str(marker),
            str(state),
            str(control),
            str(authorization),
        ],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    if expected is None:
        assert result.returncode != 0
    else:
        assert result.returncode == 0, result.stderr
        assert result.stdout == f"{expected}\n"


def test_current_control_pointer_switch_is_atomic_at_failpoint(
    tmp_path: Path,
) -> None:
    control = tmp_path / "control"
    versions = control / "versions"
    versions.mkdir(parents=True)
    old_hash = "1" * 64
    new_hash = "2" * 64
    (versions / old_hash).mkdir()
    (versions / new_hash).mkdir()
    (versions / old_hash / "generation").write_text(
        "old\n", encoding="ascii"
    )
    (versions / new_hash / "generation").write_text(
        "new\n", encoding="ascii"
    )
    env = os.environ.copy()
    env.update(
        {
            "V120_STATE_TEST_MODE": "1",
            "V120_INSTALLER_LIBRARY_ONLY": "1",
        }
    )
    command = (
        'source "$1"; '
        'publish_current_pointer "$2" "$3" "$4"'
    )
    first = subprocess.run(
        [
            "bash",
            "-c",
            command,
            "bash",
            str(INSTALL_CONTROL),
            str(control),
            str(versions),
            old_hash,
        ],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert first.returncode == 0, first.stderr
    current = control / "current"
    assert current.is_symlink()
    assert os.readlink(current) == f"versions/{old_hash}"
    assert (current / "generation").read_text(encoding="ascii") == "old\n"

    failed_env = env | {
        "V120_INSTALLER_TEST_FAILPOINT": "before_current_rename"
    }
    failed = subprocess.run(
        [
            "bash",
            "-c",
            command,
            "bash",
            str(INSTALL_CONTROL),
            str(control),
            str(versions),
            new_hash,
        ],
        text=True,
        capture_output=True,
        env=failed_env,
        check=False,
    )
    assert failed.returncode != 0
    assert os.readlink(current) == f"versions/{old_hash}"
    assert (current / "generation").read_text(encoding="ascii") == "old\n"

    killed_env = env | {
        "V120_INSTALLER_TEST_FAILPOINT": "kill_before_current_rename"
    }
    killed = subprocess.run(
        [
            "bash",
            "-c",
            command,
            "bash",
            str(INSTALL_CONTROL),
            str(control),
            str(versions),
            new_hash,
        ],
        text=True,
        capture_output=True,
        env=killed_env,
        check=False,
    )
    assert killed.returncode != 0
    assert os.readlink(current) == f"versions/{old_hash}"
    assert (current / "generation").read_text(encoding="ascii") == "old\n"

    switched = subprocess.run(
        [
            "bash",
            "-c",
            command,
            "bash",
            str(INSTALL_CONTROL),
            str(control),
            str(versions),
            new_hash,
        ],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert switched.returncode == 0, switched.stderr
    assert os.readlink(current) == f"versions/{new_hash}"
    assert (current / "generation").read_text(encoding="ascii") == "new\n"


def test_interrupted_initial_bootstrap_is_exactly_reentrant(
    tmp_path: Path,
) -> None:
    control = tmp_path / "control"
    versions = control / "versions"
    archive = control / "archive"
    versions.mkdir(parents=True, mode=0o700)
    archive.mkdir(mode=0o700)
    control.chmod(0o700)
    versions.chmod(0o700)
    archive.chmod(0o700)
    _version, manifest_hash, target = _make_v3_control_version(versions)
    authorization = tmp_path / "bootstrap.authorization"
    authorization.write_text(
        "AUTHORIZATION_FORMAT=v120-bootstrap-1\n"
        f"CONTROL_MANIFEST_HASH={manifest_hash}\n"
        f"TARGET_COMMIT={target}\n",
        encoding="ascii",
    )
    authorization.chmod(0o600)
    marker = tmp_path / "authority.marker"
    state = control / "v120-state.state"
    invocation = (
        'validate_interrupted_bootstrap_state "$1" "$2" "$3" '
        '"$4" "$5" "$6" "$7"'
    )

    accepted = _run_installer_library(
        invocation,
        str(control),
        str(versions),
        str(archive),
        str(authorization),
        str(marker),
        str(state),
        manifest_hash,
    )

    assert accepted.returncode == 0, accepted.stderr
    current = control / "current"
    failed_pointer = _run_installer_library(
        'publish_current_pointer "$1" "$2" "$3"',
        str(control),
        str(versions),
        manifest_hash,
        env={"V120_INSTALLER_TEST_FAILPOINT": "kill_before_current_rename"},
    )
    assert failed_pointer.returncode != 0
    assert not current.exists() and not current.is_symlink()
    retried = _run_installer_library(
        invocation + '; publish_current_pointer "$1" "$2" "$7"',
        str(control),
        str(versions),
        str(archive),
        str(authorization),
        str(marker),
        str(state),
        manifest_hash,
    )
    assert retried.returncode == 0, retried.stderr
    assert current.is_symlink()
    assert os.readlink(current) == f"versions/{manifest_hash}"

    current.unlink()
    rogue = versions / ("f" * 64)
    rogue.mkdir(mode=0o700)
    rejected = _run_installer_library(
        invocation,
        str(control),
        str(versions),
        str(archive),
        str(authorization),
        str(marker),
        str(state),
        manifest_hash,
    )
    assert rejected.returncode != 0


def test_whole_control_manifest_is_revalidated_before_use(
    tmp_path: Path,
) -> None:
    package = tmp_path / "version"
    package.mkdir(mode=0o700)
    names_and_keys = (
        ("v120_state.sh", "V120_STATE_SHA256", b"#!/bin/bash\ntrue\n"),
        (
            "sync-v120-root-state.sh",
            "ROOT_SYNC_SHA256",
            b"#!/bin/bash\ntrue\n",
        ),
        ("rollback-v120.sh", "ROLLBACK_SHA256", b"#!/bin/bash\ntrue\n"),
        (
            "install-v120-control.sh",
            "INSTALLER_SHA256",
            b"#!/bin/bash\ntrue\n",
        ),
        (
            "hsts-v120-root.sh",
            "HSTS_ROOT_SHA256",
            b"#!/bin/bash\ntrue\n",
        ),
        (
            "hsts-v120-operator.sh",
            "HSTS_OPERATOR_SHA256",
            b"#!/bin/bash\ntrue\n",
        ),
        (
            "edge-v120-root.sh",
            "EDGE_ROOT_SHA256",
            b"#!/bin/bash\ntrue\n",
        ),
        (
            "edge-v120-operator.sh",
            "EDGE_OPERATOR_SHA256",
            b"#!/bin/bash\ntrue\n",
        ),
        ("it-spareparts.cron", "CRON_SHA256", b"SHELL=/bin/sh\n"),
        ("source.tar", "SOURCE_TAR_SHA256", b"trusted-source\n"),
    )
    manifest_lines = [
        "CONTROL_FORMAT=v120-control-3",
        "TARGET_COMMIT=" + "4" * 40,
    ]
    for name, key, content in names_and_keys:
        artifact = package / name
        artifact.write_bytes(content)
        artifact.chmod(0o700 if name.endswith(".sh") else 0o600)
        manifest_lines.append(f"{key}={hashlib.sha256(content).hexdigest()}")
    for key in (
        "BACKEND_REQUIREMENTS_SHA256",
        "BACKEND_UV_LOCK_SHA256",
        "FRONTEND_PACKAGE_LOCK_SHA256",
        "BACKEND_SBOM_SHA256",
        "FRONTEND_SBOM_SHA256",
        "BACKEND_BASE_DIGEST",
        "FRONTEND_BUILD_BASE_DIGEST",
        "FRONTEND_RUNTIME_BASE_DIGEST",
    ):
        manifest_lines.append(f"{key}={hashlib.sha256(key.encode()).hexdigest()}")
    manifest = package / "manifest.txt"
    manifest.write_text("\n".join(manifest_lines) + "\n", encoding="ascii")
    manifest.chmod(0o600)
    manifest_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
    env = os.environ.copy()
    env.update(
        {
            "V120_STATE_TEST_MODE": "1",
            "V120_INSTALLER_LIBRARY_ONLY": "1",
        }
    )
    command = (
        'source "$1"; validate_package_directory "$2" "$3"'
    )

    accepted = subprocess.run(
        [
            "bash",
            "-c",
            command,
            "bash",
            str(INSTALL_CONTROL),
            str(package),
            manifest_hash,
        ],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert accepted.returncode == 0, accepted.stderr

    (package / "source.tar").write_bytes(b"forged-source\n")
    rejected = subprocess.run(
        [
            "bash",
            "-c",
            command,
            "bash",
            str(INSTALL_CONTROL),
            str(package),
            manifest_hash,
        ],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert rejected.returncode != 0
    assert "packaged control file hash mismatch" in rejected.stderr


def test_build_migration_inventory_gate_matches_reviewed_tree() -> None:
    migration_dir = ROOT / "backend" / "alembic" / "versions"
    migration_files = sorted(
        candidate
        for candidate in migration_dir.iterdir()
        if candidate.is_file()
    )
    inventory = "".join(
        f"{hashlib.sha256(candidate.read_bytes()).hexdigest()}  "
        f"backend/alembic/versions/{candidate.name}\n"
        for candidate in migration_files
    ).encode("ascii")
    inventory_hash = hashlib.sha256(inventory).hexdigest()
    build = _script(BUILD)

    assert len(migration_files) == 32
    assert "readonly EXPECTED_MIGRATION_FILE_COUNT=32" in build
    assert inventory_hash in build


@pytest.mark.parametrize(
    ("scenario", "expected_status"),
    [("stop_fails", 42), ("still_running", 1)],
)
def test_fail_closed_from_root_never_commits_until_stop_is_proven(
    tmp_path: Path,
    scenario: str,
    expected_status: int,
) -> None:
    state = tmp_path / "authority.state"
    state.write_text(_phase_state("opening"), encoding="ascii")
    state.chmod(0o600)
    commit_log = tmp_path / "commit.log"
    env = os.environ.copy()
    env.update(
        {
            "V120_STATE_TEST_MODE": "1",
            "V120_RELEASE_LIBRARY_ONLY": "1",
            "TEST_SCENARIO": scenario,
            "TEST_COMMIT_LOG": str(commit_log),
        }
    )
    command = r'''
source "$1"
compose() {
  case "$1" in
    stop)
      [ "$TEST_SCENARIO" != stop_fails ] || return 42
      return 0
      ;;
    ps)
      [ "$TEST_SCENARIO" != still_running ] || printf 'app\n'
      return 0
      ;;
    *) return 99 ;;
  esac
}
commit_root_transition() {
  printf '%s\n' "$*" > "$TEST_COMMIT_LOG"
}
fail_closed_from_root "$2"
'''

    result = subprocess.run(
        ["bash", "-c", command, "bash", str(RELEASE), str(state)],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode == expected_status, result.stderr
    assert not commit_log.exists()


def test_fail_closed_from_root_commits_after_stop_is_proven(
    tmp_path: Path,
) -> None:
    state = tmp_path / "authority.state"
    state.write_text(_phase_state("opening"), encoding="ascii")
    state.chmod(0o600)
    commit_log = tmp_path / "commit.log"
    env = os.environ.copy()
    env.update(
        {
            "V120_STATE_TEST_MODE": "1",
            "V120_RELEASE_LIBRARY_ONLY": "1",
            "TEST_COMMIT_LOG": str(commit_log),
        }
    )
    command = r'''
source "$1"
compose() {
  case "$1" in
    stop) return 0 ;;
    ps) return 0 ;;
    *) return 99 ;;
  esac
}
commit_root_transition() {
  printf '%s\n' "$*" > "$TEST_COMMIT_LOG"
}
fail_closed_from_root "$2"
'''

    result = subprocess.run(
        ["bash", "-c", command, "bash", str(RELEASE), str(state)],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    commit = commit_log.read_text(encoding="ascii")
    assert str(state) in commit
    assert "FAILED_AT" in commit
    assert "RELEASE_PHASE failed_closed" in commit


def test_release_control_never_executes_state_and_has_one_public_boundary() -> None:
    build = _script(BUILD)
    release = _script(RELEASE)
    observe = _script(OBSERVE)
    rollback = _script(ROLLBACK)
    state_library = _script(STATE_LIBRARY)
    root_sync = _script(ROOT_SYNC)
    installer = _script(INSTALL_CONTROL)
    _script(PACKAGE_CONTROL)

    for text in (build, release, observe, rollback, root_sync, installer):
        assert 'source "$STATE"' not in text
        assert "eval " not in text
    assert "v120_state_parse_to_array" in state_library
    assert "duplicate state key" in state_library
    assert "unknown state key" in state_library
    assert "v120_state_validate_transition" in state_library
    assert "v120_state_validate_supersession" in state_library
    assert "v120_state_commit_mirror" in state_library
    for key in (
        "ATTEMPT_NO",
        "PARENT_RELEASE_ID",
        "PARENT_STATE_HASH",
        "ROLLBACK_POLICY",
        "CONTROL_MANIFEST_HASH",
    ):
        assert key in _built_values()
        assert key in state_library
    switch = release.rsplit(
        "# From this persisted boundary onward", 1
    )[1]
    assert "RELEASE_PHASE opening" in switch
    assert switch.index("RELEASE_PHASE opening") < switch.index(
        "compose up -d --no-deps --no-build --force-recreate frontend"
    )
    assert "ROLLBACK_POLICY forward_only" in release
    assert "prepared|backup_verified" in rollback
    assert "old-image rollback is forbidden after public opening" in rollback
    assert "v120-state.state" in rollback
    assert 'fatal "usage: rollback_v120.sh"' in rollback
    assert "--lock-held" not in rollback
    assert 'v120_acquire_lock "$LOCK_PATH" "750 root:ubuntu"' in rollback


def test_release_guards_and_rebuilds_evidence_before_root_built_retry() -> None:
    release = _script(RELEASE)
    authorization = 'v120_evidence_reset_authorized "$STATE"'
    preparation = "v120_prepare_evidence_dir"
    root_commit = "sync_root_state"

    main = release.split('trap release_abort EXIT', 1)[1]
    assert authorization in main
    assert preparation in main
    assert main.index(authorization) < main.index(preparation)
    assert main.index(preparation) < main.index(root_commit)
    assert 'mkdir "$EVIDENCE_DIR"' not in main


def test_root_rollback_mirror_checks_rename_and_both_syncs() -> None:
    rollback = _script(ROLLBACK)
    mirror = rollback.split("mirror_root_state() {", 1)[1].split(
        "\n}", 1
    )[0]

    assert 'mv -fT -- "$temporary" "$destination" || return $?' in mirror
    assert 'sync -f "$destination" || return $?' in mirror
    assert 'sync -d "$APP_DIR/backups" || return $?' in mirror


def test_root_installer_uses_package_without_git_and_shares_build_lock() -> None:
    build = _script(BUILD)
    release = _script(RELEASE)
    observe = _script(OBSERVE)
    installer = _script(INSTALL_CONTROL)
    root_sync = _script(ROOT_SYNC)
    rollback = _script(ROLLBACK)
    package = _script(PACKAGE_CONTROL)
    lock_definition = (
        "readonly LOCK_PATH=/run/lock/it-spareparts-v120"
    )

    assert lock_definition in build
    assert lock_definition in installer
    assert 'v120_acquire_lock "$LOCK_PATH"' in build
    assert "git fetch origin main" not in build
    assert "git archive" not in build
    assert 'readonly CONTROL_CURRENT="$CONTROL_DIR/current"' in build
    assert 'SOURCE_TAR_SHA256' in build
    assert '"$CONTROL_CURRENT/source.tar" "$SOURCE_TEMP"' in build
    assert 'readonly BUILD_ROOT=/var/lib/it-spareparts-v120-build' in build
    context_mkdir = 'sudo mkdir -- "$RELEASE_SRC_CANDIDATE"'
    context_cleanup_arm = "RELEASE_SRC=$RELEASE_SRC_CANDIDATE"
    assert build.index(context_mkdir) < build.index(context_cleanup_arm)
    assert 'RELEASE_SRC="$BUILD_ROOT/$RELEASE_ID"' not in build
    assert 'sudo tar --no-same-owner --no-same-permissions' in build
    assert "EXPECTED_MIGRATION_INVENTORY_SHA256" in build
    for runtime_script in (build, release, observe):
        assert re.search(
            r"(?m)^[ \t]*(?:sudo[ \t]+)?git[ \t]+"
            r"(?:archive|diff|fetch|merge-base|rev-parse|show|status)"
            r"(?:[ \t]|$)",
            runtime_script,
        ) is None
        assert 'readonly CONTROL_CURRENT="$CONTROL_DIR/current"' in (
            runtime_script
        )
    install_case = installer.split("  install)", 1)[1].split("    ;;", 1)[0]
    cron_case = installer.split("  install-cron)", 1)[1].split(
        "    ;;", 1
    )[0]
    assert install_case.index("acquire_release_lock") < install_case.index(
        "stage_inbox_package"
    )
    assert cron_case.index("acquire_release_lock") < cron_case.index(
        "install_cron"
    )
    for root_script in (installer, root_sync, rollback):
        assert re.search(
            r"(?m)^[ \t]*(?:sudo[ \t]+)?git(?:[ \t]|$)",
            root_script,
        ) is None
        assert "$APP_DIR/.git" not in root_script
        assert "objects/" not in root_script
    for runtime_root_script in (root_sync, rollback):
        assert (
            'RUNTIME_CONTROL_MANIFEST_HASH=$(basename -- "$SCRIPT_DIR")'
            in runtime_root_script
        )
        assert (
            '"$SCRIPT_DIR/install-v120-control.sh" '
            'verify "$RUNTIME_CONTROL_MANIFEST_HASH"'
        ) in runtime_root_script
    assert "/var/tmp/it-spareparts-control-$expected_manifest_hash" in installer
    assert 'readonly VERSIONS_DIR="$CONTROL_DIR/versions"' in installer
    assert 'ln -s -- "versions/$expected_manifest_hash"' in installer
    assert 'mv -fT -- "$temporary" "$current"' in installer
    assert "validate_package_directory" in installer
    stage_case = installer.split(
        "stage_inbox_package() {", 1
    )[1].split("\n}", 1)[0]
    assert stage_case.index("STAGED_PACKAGE=$staging") < stage_case.index(
        "copy_bounded_nofollow"
    )
    persist_case = installer.split(
        "persist_version() {", 1
    )[1].split("\n}", 1)[0]
    assert persist_case.index('sync -f "$STAGED_PACKAGE/manifest.txt"') < (
        persist_case.index('mv -T -- "$STAGED_PACKAGE" "$destination"')
    )
    assert "git --no-replace-objects" in package
    assert 'archive --format=tar "$TARGET_COMMIT"' in package
    assert '"${SOURCE_PATHS[$index]}" > "$destination"' in package
    assert "[ ! -L \"$path\" ]" in installer
    assert 'fatal "unsafe directory: $path"' in installer


def test_dedicated_cron_has_system_user_and_no_crontab_candidate_write() -> None:
    cron = CRON_SPEC.read_text(encoding="ascii")
    installer = _script(INSTALL_CONTROL)
    release = _script(RELEASE)
    lines = cron.splitlines()

    assert lines[:2] == [
        "SHELL=/bin/sh",
        "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    ]
    jobs = lines[2:]
    assert len(jobs) == 2
    parsed = [job.split(maxsplit=6) for job in jobs]
    assert all(len(parts) == 7 for parts in parsed)
    assert all(parts[5] == "ubuntu" for parts in parsed)
    assert "/home/ubuntu/apps/it-spareparts/backup.sh" in jobs[0]
    assert "/home/ubuntu/apps/it-spareparts/.deploy/monitor.sh" in jobs[1]
    assert "readonly CRON_DEST=/etc/cron.d/it-spareparts" in installer
    assert 'mv -fT -- "$temporary" "$CRON_DEST"' in installer
    crontab_calls = [
        line
        for text in (installer, release)
        for line in text.splitlines()
        if re.match(
            r"^\s*(?:if\s+)?(?:LC_ALL=C\s+)?crontab\b",
            line,
        )
    ]
    assert len(crontab_calls) == 2
    assert all(re.search(r"\s-l(?:\s|$)", line) for line in crontab_calls)
    assert "/var/spool/cron/crontabs" in installer
    for scheduler_location in (
        "/etc/anacrontab",
        "/etc/cron.hourly",
        "/etc/cron.daily",
        "/etc/cron.weekly",
        "/etc/cron.monthly",
        "/lib/systemd/system",
        "/usr/lib/systemd/system",
        "/etc/systemd/user",
        "/usr/lib/systemd/user",
        "/run/systemd/transient",
        "/.local/share/systemd/user",
        "/home",
    ):
        assert scheduler_location in installer
    assert "systemctl --system" in installer
    assert "systemctl --user" in installer
    assert "/run/user/[0-9]*/bus" in installer


def test_release_uses_fixed_compose_identity_and_preserves_db_edge() -> None:
    release = _script(RELEASE)
    rollback = _script(ROLLBACK)
    observe = _script(OBSERVE)
    command = "up -d --no-deps --no-build --force-recreate"

    for text in (release, rollback, observe):
        assert "-u COMPOSE_FILE" in text
        assert "--project-name it-spareparts" in text
        assert '-f "$APP_DIR/docker-compose.yml"' in text
        assert "com.docker.compose.project.config_files" in text
    for text in (release, rollback):
        assert "docker compose down" not in text
        assert f"{command} db" not in text
        assert f"{command} caddy" not in text
    assert 'compose up -d --no-deps --no-build --force-recreate app' in release
    assert "BASE_DB_IMAGE_ID" in release
    assert '"$BASE_DB_IMAGE_ID" >/dev/null' in release
    assert "EDGE_CADDY_HASH" in rollback
    assert "EDGE_COMPOSE_HASH" in rollback
    assert "127.0.0.1:8080" in rollback


def test_release_requires_backup_restore_dedicated_cron_and_durable_images() -> None:
    release = _script(RELEASE)

    assert "verify_legacy_cron_absent" in release
    cron_verification = (
        'sudo "$CONTROL_CURRENT/install-v120-control.sh" \\\n'
        '  verify-cron "$CONTROL_MANIFEST_HASH"'
    )
    assert release.count(cron_verification) == 2
    assert '"$CONTROL_DIR/install-v120-control.sh"' not in release
    first_cron_check = release.index(cron_verification)
    second_cron_check = release.index(cron_verification, first_cron_check + 1)
    assert first_cron_check < release.index("\nsync_root_state\n")
    assert second_cron_check > release.index("run_monitor_with_retry || fatal")
    assert "systemctl is-active cron" in release
    assert "CONTROL_MANIFEST_HASH" in release
    assert "docker save" in release
    assert "insufficient space for durable image bundle" in release
    assert "IMAGE_BUNDLE_HASH" in release
    assert "pg_restore --list" in release
    assert "--exit-on-error" in release
    assert "--network none" in release
    assert '"$BASE_DB_IMAGE_ID" >/dev/null' in release
    assert "source-counts.txt" in release
    assert "restored-counts.txt" in release
    assert "diff -u" in release


def test_observer_uses_real_cron_heartbeat_and_fails_closed() -> None:
    observe = _script(OBSERVE)

    assert "observe 0 0" in observe
    assert "observe 5 1" in observe
    assert "observe 15 1" in observe
    assert "observe 30 1" in observe
    assert "wait_for_monitor_advance" in observe
    assert "sudo -n journalctl -u cron" in observe
    assert "capture_cron_journal" in observe
    main_start = observe.index('cd "$APP_DIR"')
    assert observe.index("OBSERVATION_ARMED=1", main_start) < observe.index(
        "preflight_cron_journal", main_start
    )
    assert '"$APP_DIR/.deploy/monitor.sh"' not in observe
    assert "compose stop frontend app" in observe
    assert "RELEASE_PHASE failed_closed" in observe
    assert "RELEASE_PHASE observed" in observe
    assert "roundtrip-import" not in observe
    assert "recompute" not in observe
    assert "POST " not in observe


def test_deploy_guide_routes_v120_to_the_versioned_runbook() -> None:
    guide = DEPLOY_GUIDE.read_text(encoding="utf-8")

    assert "v1.20-release-runbook.md" in guide
    assert "release_v120.sh" in guide
    assert "rollback_v120.sh" in guide
    assert "observe_v120.sh" in guide


def test_release_runbook_extracts_all_dependencies_before_shellcheck() -> None:
    runbook = RELEASE_RUNBOOK.read_text(encoding="utf-8")

    names = "build_v120.sh v120_state.sh"
    first_loop = f"for name in {names}; do"
    start = runbook.index(first_loop)
    end = runbook.index('chmod 755 "$tools"', start)
    section = runbook[start:end]
    extraction_done = section.index("\ndone\n")
    source_root = section.index('cd "$tools"')
    shellcheck_loop = section.index(first_loop, extraction_done + 1)
    shellcheck_call = section.index(
        'shellcheck -x "$tools/.deploy/$name"',
        shellcheck_loop,
    )

    assert extraction_done < source_root < shellcheck_loop < shellcheck_call
    assert section.count(first_loop) == 2


def test_control_installer_passes_bare_shellcheck_x() -> None:
    result = subprocess.run(
        ["shellcheck", "-x", str(INSTALL_CONTROL)],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_release_runbook_stages_the_exact_required_release_artifacts() -> None:
    runbook = RELEASE_RUNBOOK.read_text(encoding="utf-8")
    release_script = _script(RELEASE)
    expected_scripts = (
        "backup.sh",
        "monitor.sh",
        "build_v120.sh",
        "release_v120.sh",
        "observe_v120.sh",
        "rollback_v120.sh",
        "install_v120_control.sh",
        "hsts_v120_root.sh",
        "hsts_v120_operator.sh",
        "package_v120_control.sh",
        "v120_state.sh",
        "sync_v120_root_state.sh",
    )

    required_start = release_script.index("for script_name in \\\n")
    required_end = release_script.index("\ndo\n", required_start)
    required_scripts = tuple(
        line.strip().removesuffix("\\").strip()
        for line in release_script[required_start:required_end]
        .splitlines()[1:]
    )
    assert required_scripts == expected_scripts

    array_start = runbook.index("release_scripts=(\n")
    array_end = runbook.index("\n)", array_start)
    runbook_scripts = tuple(
        line.strip()
        for line in runbook[array_start:array_end].splitlines()[1:]
    )
    assert runbook_scripts == required_scripts

    release_command = (
        'sudo -u ubuntu /usr/bin/bash "$tools/.deploy/release_v120.sh" '
        '"$state" </dev/null'
    )
    observer_command = (
        'sudo -u ubuntu /usr/bin/bash "$tools/.deploy/observe_v120.sh" '
        '"$state" </dev/null'
    )
    section_end = runbook.index(observer_command, array_start) + len(
        observer_command
    )
    section = runbook[array_start:section_end]
    scripts_loop = 'for name in "${release_scripts[@]}"; do'
    first_loop = section.index(scripts_loop)
    script_owner = section.index(
        'chown root:root "$tools/.deploy/$name"',
        first_loop,
    )
    script_mode = section.index(
        'chmod 555 "$tools/.deploy/$name"',
        script_owner,
    )
    script_syntax = section.index(
        'bash -n "$tools/.deploy/$name"',
        script_mode,
    )
    extraction_done = section.index("\ndone\n", first_loop)
    cron_extract = section.index(
        'tar -xOf "$control/source.tar" ".deploy/$release_data"',
        extraction_done,
    )
    cron_owner = section.index(
        'chown root:root "$tools/.deploy/$release_data"',
        cron_extract,
    )
    cron_mode = section.index(
        'chmod 444 "$tools/.deploy/$release_data"',
        cron_owner,
    )
    source_root = section.index('cd "$tools"', cron_mode)
    shellcheck_loop = section.index(
        scripts_loop,
        extraction_done + 1,
    )
    shellcheck_call = section.index(
        'shellcheck -x "$tools/.deploy/$name"',
        shellcheck_loop,
    )
    release_exec = section.index(release_command, shellcheck_call)
    observer_exec = section.index(observer_command, release_exec)

    assert "release_data=it-spareparts.cron" in section
    assert section.count(scripts_loop) == 2
    assert (
        first_loop
        < script_owner
        < script_mode
        < script_syntax
        < extraction_done
        < cron_extract
        < cron_owner
        < cron_mode
        < source_root
        < shellcheck_loop
        < shellcheck_call
        < release_exec
        < observer_exec
    )


def test_v3_runbook_names_all_nine_control_files_and_source_tar() -> None:
    runbook = RELEASE_RUNBOOK.read_text(encoding="utf-8")

    assert "九个固定控制件和完整 `source.tar`" in runbook
    assert "五个固定控制件" not in runbook


def test_release_runbook_runs_extracted_tools_through_trusted_bash() -> None:
    runbook = RELEASE_RUNBOOK.read_text(encoding="utf-8")

    build_command = (
        'sudo -u ubuntu /usr/bin/bash "$tools/.deploy/build_v120.sh"'
    )
    build = (
        f'{build_command} "$target_commit"'
    )
    superseding_build = (
        f'{build_command} \\\n'
        '  "$target_commit" --supersedes \'<父 RELEASE_ID>\''
    )
    release = (
        'sudo -u ubuntu /usr/bin/bash "$tools/.deploy/release_v120.sh" '
        '"$state" </dev/null'
    )
    observer = (
        'sudo -u ubuntu /usr/bin/bash "$tools/.deploy/observe_v120.sh" '
        '"$state" </dev/null'
    )

    assert runbook.count(build_command) == 2
    assert build in runbook
    assert superseding_build in runbook
    assert runbook.count(release) == 1
    assert runbook.count(observer) == 1
    assert 'sudo -u ubuntu "$tools/.deploy/' not in runbook


def test_release_runbook_subscripts_cannot_drain_outer_stdin(
    tmp_path: Path,
) -> None:
    runbook = RELEASE_RUNBOOK.read_text(encoding="utf-8")
    commands = [
        line
        for line in runbook.splitlines()
        if line.startswith(
            (
                "sudo -u ubuntu /usr/bin/bash "
                '"$tools/.deploy/release_v120.sh"',
                "sudo -u ubuntu /usr/bin/bash "
                '"$tools/.deploy/observe_v120.sh"',
            )
        )
    ]
    assert len(commands) == 2

    deploy = tmp_path / ".deploy"
    deploy.mkdir()
    log = tmp_path / "calls.log"
    (deploy / "release_v120.sh").write_text(
        'cat >/dev/null\nprintf "release\\n" >> "$TEST_CALL_LOG"\n',
        encoding="utf-8",
    )
    (deploy / "observe_v120.sh").write_text(
        'printf "observe\\n" >> "$TEST_CALL_LOG"\n',
        encoding="utf-8",
    )
    outer_script = "\n".join(
        [
            "set -euo pipefail",
            "sudo() { shift 2; \"$@\"; }",
            'tools=$1',
            'state=$2',
            'export TEST_CALL_LOG=$3',
            *commands,
            "",
        ]
    )

    result = subprocess.run(
        ["/usr/bin/bash", "-s", str(tmp_path), "unused", str(log)],
        input=outer_script,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert log.read_text(encoding="utf-8") == "release\nobserve\n"


def test_release_runbook_archives_only_the_exact_legacy_https_control() -> None:
    runbook = RELEASE_RUNBOOK.read_text(encoding="utf-8")
    legacy_hash = (
        "1d377dea50581047e9a22ad1144925d6e"
        "68965b2df2df8a4be5c3cd834a6a893"
    )
    archive = (
        "/var/lib/it-spareparts-release-control."
        "https-legacy-1d377dea5058"
    )

    assert legacy_hash in runbook
    assert archive in runbook
    inventory_check = (
        'find "$control" -mindepth 1 -maxdepth 1 -printf x | wc -c'
    )
    exact_file_check = (
        'test "$(sha256sum "$legacy" | cut -d\' \' -f1)" = "$legacy_hash"'
    )
    archive_move = 'mv -T -- "$control" "$legacy_archive"'
    assert inventory_check in runbook
    assert exact_file_check in runbook
    assert runbook.index("trap cleanup EXIT") < runbook.index(archive_move)
    assert runbook.index(inventory_check) < runbook.index(archive_move)
    assert runbook.index(exact_file_check) < runbook.index(archive_move)
    assert (
        'mv -T -- "$legacy_archive" "$control" || result=97'
        in runbook
    )
    assert f"sudo {archive}/rollback-now.sh" in runbook

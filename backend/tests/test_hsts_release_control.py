from __future__ import annotations

import fcntl
import hashlib
import os
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
HSTS_ROOT = ROOT / ".deploy" / "hsts_v120_root.sh"
HSTS_OPERATOR = ROOT / ".deploy" / "hsts_v120_operator.sh"
HSTS_RUNBOOK = ROOT / "docs" / "releases" / "hsts-v120-scoped-runbook.md"
TARGET = "1" * 40
GENERATION = "hsts-111111111111-20260730T230000"
EDGE_GENERATION = "edge-111111111111-20260730T220000"
CONTROL_HASH = "2" * 64
RELEASE_ID = "v120-111111111111-20260730230000"


def _tree_snapshot(*roots: Path) -> dict[str, tuple[int, int, str]]:
    snapshot: dict[str, tuple[int, int, str]] = {}
    for root in roots:
        for path in (root, *sorted(root.rglob("*"))):
            relative = f"{root.name}/{path.relative_to(root)}"
            metadata = path.lstat()
            if path.is_symlink():
                content = f"link:{os.readlink(path)}"
            elif path.is_file():
                content = hashlib.sha256(path.read_bytes()).hexdigest()
            else:
                content = "directory"
            snapshot[relative] = (
                stat.S_IMODE(metadata.st_mode),
                metadata.st_mtime_ns,
                content,
            )
    return snapshot


def _observed_state(app_compose_hash: str) -> str:
    values = (
        ("STATE_FORMAT", "v120-1"),
        ("STATE_GENERATION", "5"),
        ("ATTEMPT_NO", "2"),
        ("RELEASE_ID", RELEASE_ID),
        ("PARENT_RELEASE_ID", "v120-000000000000-20260730150000"),
        ("PARENT_STATE_HASH", "9" * 64),
        ("ROLLBACK_POLICY", "forward_only"),
        ("TARGET_COMMIT", TARGET),
        ("OLD_COMMIT", "ab42005b5b94bf98b3db0e4bff87e5df9da2f7ca"),
        ("OLD_RUNNING_SOURCE_COMMIT", "4" * 40),
        ("DB_HEAD", "f1c8e4a7b2d9"),
        ("OLD_APP_IMAGE_ID", "sha256:" + "6" * 64),
        ("OLD_FRONTEND_IMAGE_ID", "sha256:" + "7" * 64),
        ("APP_IMAGE_REF", "it-spareparts-app"),
        ("FRONTEND_IMAGE_REF", "it-spareparts-frontend"),
        ("OLD_APP_ROLLBACK_TAG", f"it-spareparts-release/app:rollback-{RELEASE_ID}"),
        (
            "OLD_FRONTEND_ROLLBACK_TAG",
            f"it-spareparts-release/frontend:rollback-{RELEASE_ID}",
        ),
        ("NEW_APP_IMAGE_ID", "sha256:" + "d" * 64),
        ("NEW_FRONTEND_IMAGE_ID", "sha256:" + "e" * 64),
        ("NEW_APP_CANDIDATE_TAG", f"it-spareparts-release/app:candidate-{RELEASE_ID}"),
        (
            "NEW_FRONTEND_CANDIDATE_TAG",
            f"it-spareparts-release/frontend:candidate-{RELEASE_ID}",
        ),
        (
            "SOURCE_TAR",
            f"/home/ubuntu/apps/it-spareparts/backups/{RELEASE_ID}-source.tar",
        ),
        (
            "SOURCE_SUM",
            f"/home/ubuntu/apps/it-spareparts/backups/{RELEASE_ID}-source.tar.sha256",
        ),
        ("SOURCE_HASH", "8" * 64),
        ("CONTROL_MANIFEST_HASH", CONTROL_HASH),
        ("RELEASE_PHASE", "observed"),
        ("APP_COMPOSE_HASH", app_compose_hash),
        ("BASE_DB_CID", "b" * 64),
        ("BASE_DB_IMAGE_ID", "sha256:" + "3" * 64),
        ("BASE_EDGE_CID", "c" * 64),
        ("BASE_DB_RESTARTS", "0"),
        ("BASE_EDGE_RESTARTS", "0"),
        ("EDGE_CADDY_HASH", "5" * 64),
        ("EDGE_COMPOSE_HASH", "6" * 64),
        (
            "IMAGE_BUNDLE",
            f"/home/ubuntu/apps/it-spareparts/backups/{RELEASE_ID}-release/images.tar",
        ),
        ("IMAGE_BUNDLE_HASH", "7" * 64),
        (
            "EVIDENCE_DIR",
            f"/home/ubuntu/apps/it-spareparts/backups/{RELEASE_ID}-release",
        ),
        ("BACKUP", "/var/backups/spareparts/db-20260730-2300.dump"),
        ("BACKUP_HASH", "a" * 64),
        ("NEW_APP_CID", "a" * 64),
        ("NEW_FRONTEND_CID", "f" * 64),
        ("MONITOR_SWITCH_MTIME", "1"),
        ("PUBLIC_OPENED_AT", "2026-07-30T23:00:00+08:00"),
        ("SWITCHED_AT", "2026-07-30T23:01:00+08:00"),
        ("OBSERVED_AT", "2026-07-30T23:31:00+08:00"),
    )
    return "".join(f"{key}={value}\n" for key, value in values)


def _write_executable(path: Path, body: str) -> None:
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _fixture(tmp_path: Path) -> tuple[dict[str, str], Path, Path, Path]:
    app = tmp_path / "app"
    assistant = tmp_path / "assistant"
    control = tmp_path / "control"
    lock = tmp_path / "release-lock"
    shared_caddy_lock = tmp_path / "shared-caddy.lock"
    command_dir = tmp_path / "bin"
    for directory in (app, assistant, control, lock, command_dir):
        directory.mkdir(mode=0o700)
    shared_caddy_lock.write_bytes(b"")
    shared_caddy_lock.chmod(0o600)

    (app / "docker-compose.yml").write_text(
        "services:\n  frontend:\n    ports:\n      - 127.0.0.1:8080:80\n",
        encoding="utf-8",
    )
    (app / "docker-compose.yml").chmod(0o600)
    app_compose_hash = hashlib.sha256(
        (app / "docker-compose.yml").read_bytes()
    ).hexdigest()
    state_text = _observed_state(app_compose_hash)
    root_state = control / "v120-state.state"
    root_state.write_text(state_text, encoding="ascii")
    root_state.chmod(0o600)
    backups = app / "backups"
    backups.mkdir(mode=0o700)
    app_state = backups / f"{RELEASE_ID}.state"
    app_state.write_text(state_text, encoding="ascii")
    app_state.chmod(0o600)
    authority_marker = control / "v120-authority.marker"
    authority_marker.write_text(
        "AUTHORITY_FORMAT=v120-authority-1\n"
        f"INITIAL_CONTROL_MANIFEST_HASH={CONTROL_HASH}\n"
        f"INITIAL_TARGET_COMMIT={TARGET}\n",
        encoding="ascii",
    )
    authority_marker.chmod(0o600)
    (app / ".env").write_text("POSTGRES_PASSWORD=not-recorded\n", encoding="utf-8")
    (app / ".env").chmod(0o600)
    (assistant / "Caddyfile").write_text(
        "assistant.example.test { respond /health 200 }\n"
        "https://it.example.test { reverse_proxy it-spareparts-frontend:80 }\n"
        ":8080 {\n"
        "  @safe method GET HEAD\n"
        "  redir @safe https://hbzgc.icu{uri} 308\n"
        "  respond 405\n"
        "}\n",
        encoding="utf-8",
    )
    (assistant / "Caddyfile").chmod(0o600)
    (assistant / "compose.production.yml").write_text(
        "services:\n"
        "  caddy:\n"
        "    ports:\n"
        '      - "10.0.0.11:8080:8080"\n'
        "    environment:\n"
        '      IT_DATA_HSTS_MAX_AGE: "300"\n'
        "      UNRELATED_SECRET_REF: ${UNRELATED_SECRET_REF}\n",
        encoding="utf-8",
    )
    (assistant / "compose.production.yml").chmod(0o600)
    (assistant / ".env").write_text(
        "UNRELATED_SECRET_REF=fixture-secret-must-not-be-copied\n",
        encoding="utf-8",
    )
    (assistant / ".env").chmod(0o600)
    assistant_health = assistant / ".health_url"
    assistant_health.write_text(
        "https://assistant.example.test/health\n", encoding="ascii"
    )
    assistant_health.chmod(0o600)
    edge_generation = control / "edge" / "generations" / EDGE_GENERATION
    edge_generation.mkdir(parents=True, mode=0o700)
    for source, name in (
        (assistant / "compose.production.yml", "compose.pre"),
        (assistant / "compose.production.yml", "compose.post"),
        (assistant / "Caddyfile", "Caddyfile.pre"),
        (assistant / "Caddyfile", "Caddyfile.post"),
    ):
        destination = edge_generation / name
        destination.write_bytes(source.read_bytes())
        destination.chmod(0o600)
    state_hash = hashlib.sha256(state_text.encode("ascii")).hexdigest()
    edge_manifest = edge_generation / "manifest.txt"
    edge_manifest.write_text(
        "EDGE_FORMAT=edge-v120-1\n"
        f"TARGET_COMMIT={TARGET}\n"
        f"GENERATION={EDGE_GENERATION}\n"
        f"CONTROL_MANIFEST_HASH={CONTROL_HASH}\n"
        f"RELEASE_ID={RELEASE_ID}\n"
        "RELEASE_STATE_GENERATION=5\n"
        f"RELEASE_STATE_SHA256={state_hash}\n"
        f"APP_COMPOSE_SHA256={app_compose_hash}\n"
        "ASSISTANT_COMPOSE_PRE_SHA256="
        f"{hashlib.sha256((assistant / 'compose.production.yml').read_bytes()).hexdigest()}\n"
        "ASSISTANT_COMPOSE_POST_SHA256="
        f"{hashlib.sha256((assistant / 'compose.production.yml').read_bytes()).hexdigest()}\n"
        "ASSISTANT_RENDER_PRE_SHA256=test-render\n"
        "ASSISTANT_RENDER_POST_SHA256=test-render\n"
        "CADDYFILE_PRE_SHA256="
        f"{hashlib.sha256((assistant / 'Caddyfile').read_bytes()).hexdigest()}\n"
        "CADDYFILE_POST_SHA256="
        f"{hashlib.sha256((assistant / 'Caddyfile').read_bytes()).hexdigest()}\n"
        f"AUTH_APP_CID={'a' * 64}\n"
        "AUTH_APP_IMAGE=sha256:" + "d" * 64 + "\n"
        "AUTH_APP_RESTARTS=0\n"
        f"AUTH_FRONTEND_CID={'f' * 64}\n"
        "AUTH_FRONTEND_IMAGE=sha256:" + "e" * 64 + "\n"
        "AUTH_FRONTEND_RESTARTS=0\n"
        f"AUTH_DB_CID={'b' * 64}\n"
        "AUTH_DB_IMAGE=sha256:" + "3" * 64 + "\n"
        "AUTH_DB_RESTARTS=0\n"
        f"AUTH_CADDY_CID={'c' * 64}\n"
        "AUTH_CADDY_IMAGE=sha256:" + "5" * 64 + "\n"
        "AUTH_CADDY_RESTARTS=0\n",
        encoding="ascii",
    )
    edge_manifest.chmod(0o600)
    edge_state = edge_generation / "state.txt"
    edge_state.write_text("EDGE_STATE=promoted\n", encoding="ascii")
    edge_state.chmod(0o600)
    edge_sums = edge_generation / "SHA256SUMS"
    immutable = (
        "manifest.txt",
        "compose.pre",
        "compose.post",
        "Caddyfile.pre",
        "Caddyfile.post",
    )
    edge_sums.write_text(
        "".join(
            f"{hashlib.sha256((edge_generation / name).read_bytes()).hexdigest()}  {name}\n"
            for name in immutable
        ),
        encoding="ascii",
    )
    edge_sums.chmod(0o600)

    calls = tmp_path / "calls.log"
    caddy_cid_file = tmp_path / "caddy.cid"
    caddy_cid_file.write_text(f"{'c' * 64}\n", encoding="ascii")
    _write_executable(
        command_dir / "docker",
        r"""
        #!/usr/bin/env bash
        set -euo pipefail
        current_caddy_cid() {
          if [ -n "${HSTS_STUB_CADDY_CID:-}" ]; then
            printf '%s\n' "$HSTS_STUB_CADDY_CID"
          else
            cat "$HSTS_STUB_CADDY_CID_FILE"
          fi
        }
        printf 'docker %s\n' "$*" >> "$HSTS_TEST_CALL_LOG"
        if [[ "$*" == *"config --format json"* ]]; then
          compose=
          while [ "$#" -gt 0 ]; do
            if [ "$1" = -f ]; then
              compose=$2
              shift 2
              continue
            fi
            shift
          done
          hsts=$(sed -n 's/.*IT_DATA_HSTS_MAX_AGE: "\([0-9]*\)".*/\1/p' "$compose")
          printf '{"services":{"caddy":{"environment":{"IT_DATA_HSTS_MAX_AGE":"%s"}}}}\n' "$hsts"
          exit 0
        fi
        if [[ "$*" == *"inspect -f {{.State.Running}}"* ]]; then
          printf 'true\n'
          exit 0
        fi
        if [[ "$*" == *"inspect -f {{.Image}}"* ]]; then
          case "${@: -1}" in
            aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa)
              printf '%s\n' "${HSTS_STUB_APP_IMAGE:-sha256:$(printf '%064d' 0 | tr 0 d)}" ;;
            ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff)
              printf '%s\n' "${HSTS_STUB_FRONTEND_IMAGE:-sha256:$(printf '%064d' 0 | tr 0 e)}" ;;
            bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb)
              printf '%s\n' "${HSTS_STUB_DB_IMAGE:-sha256:$(printf '%064d' 0 | tr 0 3)}" ;;
            cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc)
              printf 'sha256:%064d\n' 0 | tr 0 5 ;;
            *)
              if [ "${@: -1}" = "$(current_caddy_cid)" ]; then
                printf 'sha256:%064d\n' 0 | tr 0 5
              else
                exit 98
              fi ;;
          esac
          exit 0
        fi
        if [[ "$*" == *"inspect -f {{.RestartCount}}"* ]]; then
          printf '%s\n' "${HSTS_STUB_RESTART_COUNT:-0}"
          exit 0
        fi
        if [[ "$*" == *"inspect -f {{json .NetworkSettings.Networks}}"* ]]; then
          case "${@: -1}" in
            personal-ai-assistant-caddy)
              printf '%s\n' "${HSTS_STUB_CADDY_NETWORKS:-}" \
                | grep . || printf '%s\n' '{"personal-ai-assistant-network":{},"it-spareparts-ingress":{}}'
              ;;
            ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff)
              printf '%s\n' "${HSTS_STUB_FRONTEND_NETWORKS:-}" \
                | grep . || printf '%s\n' '{"it-spareparts_default":{},"it-spareparts-ingress":{}}'
              ;;
            aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa)
              printf '%s\n' "${HSTS_STUB_APP_NETWORKS:-}" \
                | grep . || printf '%s\n' '{"it-spareparts_default":{}}'
              ;;
            bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb)
              printf '%s\n' "${HSTS_STUB_DB_NETWORKS:-}" \
                | grep . || printf '%s\n' '{"it-spareparts_default":{}}'
              ;;
            *) exit 98 ;;
          esac
          exit 0
        fi
        if [[ "$*" == *"inspect -f {{.Id}}"* ]]; then
          current_caddy_cid
          exit 0
        fi
        if [[ "$*" == *"network inspect -f {{json .Containers}}"* ]]; then
          if [ -n "${HSTS_STUB_INGRESS_MEMBERS:-}" ]; then
            printf '%s\n' "$HSTS_STUB_INGRESS_MEMBERS"
          else
            current_cid=$(current_caddy_cid)
            printf '{"%s":{},"%s":{}}\n' \
              "$current_cid" \
              "$(printf '%064d' 0 | tr 0 f)"
          fi
          exit 0
        fi
        if [[ "$*" == *"network inspect -f {{.Internal}}"* ]]; then
          printf 'true\n'
          exit 0
        fi
        if [[ "$*" == *"up -d --no-deps --force-recreate caddy"* ]] \
            && [ -n "${HSTS_TEST_RUNTIME_FILE:-}" ]; then
          hsts=$(sed -n 's/.*IT_DATA_HSTS_MAX_AGE: "\([0-9]*\)".*/\1/p' \
            "$HSTS_ASSISTANT_DIR/compose.production.yml")
          printf '%s\n' "$hsts" > "$HSTS_TEST_RUNTIME_FILE"
          if [ -n "${HSTS_STUB_NEXT_CADDY_CID:-}" ]; then
            printf '%s\n' "$HSTS_STUB_NEXT_CADDY_CID" \
              > "$HSTS_STUB_CADDY_CID_FILE"
          fi
          exit 0
        fi
        if [[ "$*" == *"up -d --no-deps --force-recreate caddy"* ]]; then
          if [ -n "${HSTS_STUB_NEXT_CADDY_CID:-}" ]; then
            printf '%s\n' "$HSTS_STUB_NEXT_CADDY_CID" \
              > "$HSTS_STUB_CADDY_CID_FILE"
          fi
          exit 0
        fi
        if [[ "$*" == *"port caddy 8080"* ]]; then
          printf '10.0.0.11:8080\n'
          exit 0
        fi
        if [[ "$*" == *"ps -q frontend"* ]]; then
          printf '%s\n' "${HSTS_STUB_FRONTEND_CID:-$(printf '%064d' 0 | tr 0 f)}"
          exit 0
        fi
        if [[ "$*" == *"ps -q app"* ]]; then
          printf '%s\n' "${HSTS_STUB_APP_CID:-$(printf '%064d' 0 | tr 0 a)}"
          exit 0
        fi
        if [[ "$*" == *"ps -q db"* ]]; then
          printf '%s\n' "${HSTS_STUB_DB_CID:-$(printf '%064d' 0 | tr 0 b)}"
          exit 0
        fi
        exit 0
        """,
    )
    _write_executable(
        command_dir / "sync",
        r"""
        #!/usr/bin/env bash
        set -euo pipefail
        rendered=$*
        armed() {
          [ -z "${HSTS_STUB_FAIL_IF_FILE:-}" ] \
            || grep -Fq -- "$HSTS_STUB_FAIL_IF_CONTAINS" \
              "$HSTS_STUB_FAIL_IF_FILE"
        }
        if [ -n "${HSTS_STUB_FAIL_SYNC_EXACT:-}" ] \
            && [ "$rendered" = "$HSTS_STUB_FAIL_SYNC_EXACT" ] \
            && armed; then
          exit "${HSTS_STUB_PERSIST_RC:-73}"
        fi
        if [ -n "${HSTS_STUB_FAIL_SYNC_CONTAINS:-}" ] \
            && [[ "$rendered" == *"$HSTS_STUB_FAIL_SYNC_CONTAINS"* ]] \
            && armed; then
          exit "${HSTS_STUB_PERSIST_RC:-73}"
        fi
        exec /usr/bin/sync "$@"
        """,
    )
    _write_executable(
        command_dir / "mv",
        r"""
        #!/usr/bin/env bash
        set -euo pipefail
        rendered=$*
        armed() {
          [ -z "${HSTS_STUB_FAIL_IF_FILE:-}" ] \
            || grep -Fq -- "$HSTS_STUB_FAIL_IF_CONTAINS" \
              "$HSTS_STUB_FAIL_IF_FILE"
        }
        if [ -n "${HSTS_STUB_FAIL_MV_CONTAINS:-}" ] \
            && [[ "$rendered" == *"$HSTS_STUB_FAIL_MV_CONTAINS"* ]] \
            && armed; then
          exit "${HSTS_STUB_PERSIST_RC:-73}"
        fi
        exec /usr/bin/mv "$@"
        """,
    )
    _write_executable(
        command_dir / "curl",
        r"""
        #!/usr/bin/env bash
        set -euo pipefail
        target="${@: -1}"
        printf 'curl %s\n' "$*" >> "$HSTS_TEST_CALL_LOG"
        case "$target" in
          https://it.example.test/)
            if [ -n "${HSTS_TEST_RUNTIME_FILE:-}" ]; then
              hsts=$(cat "$HSTS_TEST_RUNTIME_FILE")
            else
              hsts=$(sed -n 's/.*IT_DATA_HSTS_MAX_AGE: "\([0-9]*\)".*/\1/p' \
                "$HSTS_ASSISTANT_DIR/compose.production.yml")
            fi
            printf 'HTTP/2 200\r\nstrict-transport-security: max-age=%s%s\r\n\r\n' \
              "$hsts" "${HSTS_STUB_HEADER_SUFFIX:-}"
            ;;
          https://it.example.test/health|https://it.example.test/health/db)
            if [ "${HSTS_STUB_HEALTH_HTML:-0}" = 1 ]; then
              printf '<!doctype html>SPA\n200\ntext/html'
            elif [[ "$target" == */health/db ]]; then
              printf '{"status":"ok","db":"reachable"}\n200\napplication/json'
            else
              printf '{"status":"ok"}\n200\napplication/json'
            fi
            ;;
          https://assistant.example.test/health)
            case "${HSTS_STUB_ASSISTANT_HEALTH:-ready}" in
              ready) printf '{"status":"ready"}\n200\napplication/json' ;;
              html) printf '<!doctype html>SPA\n200\ntext/html' ;;
              mime) printf '{"status":"ready"}\n200\ntext/plain' ;;
              ok) printf '{"status":"ok"}\n200\napplication/json' ;;
              missing)
                printf '{"service":"assistant"}\n200\napplication/json' ;;
              malformed) printf '{bad\n200\napplication/json' ;;
              http-error) exit 22 ;;
              *) exit 97 ;;
            esac
            ;;
          http://127.0.0.1:8080/)
            printf 'ok\n'
            ;;
          http://10.0.0.11:8080/edge-check/path?scope=1)
            if [[ " $* " == *" -X POST "* ]] \
                || [[ " $* " == *" -X PUT "* ]] \
                || [[ " $* " == *" -X PATCH "* ]] \
                || [[ " $* " == *" -X DELETE "* ]]; then
              printf 'HTTP/1.1 405 Method Not Allowed\r\n\r\n'
            else
              printf 'HTTP/1.1 308 Permanent Redirect\r\n'
              printf 'Location: https://hbzgc.icu/edge-check/path?scope=1\r\n\r\n'
            fi
            ;;
          http://it.example.test/edge-check/path?scope=1)
            printf 'HTTP/1.1 308 Permanent Redirect\r\n'
            printf 'Location: https://it.example.test/edge-check/path?scope=1\r\n\r\n'
            ;;
          *) exit 97 ;;
        esac
        """,
    )
    _write_executable(
        command_dir / "ss",
        r"""
        #!/usr/bin/env bash
        set -euo pipefail
        printf 'LISTEN 0 4096 127.0.0.1:8080 0.0.0.0:*\n'
        printf 'LISTEN 0 4096 10.0.0.11:8080 0.0.0.0:* users:(("docker-proxy",pid=1,fd=4))\n'
        """,
    )
    _write_executable(
        command_dir / "ip",
        r"""
        #!/usr/bin/env bash
        set -euo pipefail
        printf '%s\n' '[{"addr_info":[{"family":"inet","local":"10.0.0.11","prefixlen":22,"scope":"global"}]}]'
        """,
    )

    env = {
        **os.environ,
        "HSTS_V120_TEST_MODE": "1",
        "HSTS_APP_DIR": str(app),
        "HSTS_ASSISTANT_DIR": str(assistant),
        "HSTS_CONTROL_DIR": str(control),
        "HSTS_LOCK_PATH": str(lock),
        "HSTS_CADDY_LOCK_PATH": str(shared_caddy_lock),
        "HSTS_COMMAND_DIR": str(command_dir),
        "HSTS_TEST_CALL_LOG": str(calls),
        "HSTS_STUB_CADDY_CID_FILE": str(caddy_cid_file),
        "HSTS_IT_URL": "https://it.example.test/",
        "HSTS_ASSISTANT_HEALTH_FILE": str(assistant_health),
        "HSTS_ASSISTANT_HEALTH_URL": ("https://assistant.example.test/health"),
        "HSTS_AUTHORITY_MARKER": str(authority_marker),
        "HSTS_CONTROL_MANIFEST_HASH": CONTROL_HASH,
        "HSTS_V120_STATE_LIBRARY": str(ROOT / ".deploy" / "v120_state.sh"),
    }
    return env, control, assistant, calls


def _run_root(env: dict[str, str], action: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            str(HSTS_ROOT),
            action,
            TARGET,
            GENERATION,
            EDGE_GENERATION,
        ],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def _run_root_library(
    env: dict[str, str],
    body: str,
) -> subprocess.CompletedProcess[str]:
    library_env = {
        **env,
        "HSTS_ROOT_LIBRARY_ONLY": "1",
    }
    return subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; shift; eval "$1"',
            "bash",
            str(HSTS_ROOT),
            body,
        ],
        text=True,
        capture_output=True,
        check=False,
        env=library_env,
    )


def _operator_fixture(
    tmp_path: Path, responses: list[tuple[int, str]]
) -> tuple[dict[str, str], Path, Path]:
    command_dir = tmp_path / "operator-bin"
    command_dir.mkdir()
    response_file = tmp_path / "ssh-responses"
    response_file.write_text(
        "".join(f"{status}|{output}\n" for status, output in responses),
        encoding="ascii",
    )
    count = tmp_path / "ssh-count"
    count.write_text("0\n", encoding="ascii")
    call_log = tmp_path / "ssh-calls"
    stdin_log = tmp_path / "ssh-stdin"
    _write_executable(
        command_dir / "ssh",
        r"""
        #!/usr/bin/env bash
        set -euo pipefail
        printf '%s\n' "$*" >> "$HSTS_SSH_CALL_LOG"
        wc -c >> "$HSTS_SSH_STDIN_LOG"
        index=$(cat "$HSTS_SSH_COUNT")
        line=$(sed -n "$((index + 1))p" "$HSTS_SSH_RESPONSES")
        printf '%s\n' "$((index + 1))" > "$HSTS_SSH_COUNT"
        status=${line%%|*}
        output=${line#*|}
        [ -z "$output" ] || printf '%s\n' "$output"
        exit "$status"
        """,
    )
    env = {
        **os.environ,
        "HSTS_OPERATOR_TEST_MODE": "1",
        "HSTS_OPERATOR_COMMAND_DIR": str(command_dir),
        "HSTS_SSH_RESPONSES": str(response_file),
        "HSTS_SSH_COUNT": str(count),
        "HSTS_SSH_CALL_LOG": str(call_log),
        "HSTS_SSH_STDIN_LOG": str(stdin_log),
    }
    return env, call_log, stdin_log


def _run_operator(env: dict[str, str], action: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            str(HSTS_OPERATOR),
            action,
            "it-spareparts-prod",
            TARGET,
            GENERATION,
            EDGE_GENERATION,
        ],
        input="outer-input-must-survive\n",
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def _install_final_edge_successor_lineage(
    env: dict[str, str],
    control: Path,
    assistant: Path,
    *,
    successor: str = "9" * 64,
) -> None:
    edge_dir = control / "edge" / "generations" / EDGE_GENERATION
    edge_manifest = edge_dir / "manifest.txt"
    edge_manifest.write_text(
        edge_manifest.read_text(encoding="ascii").replace(
            f"AUTH_CADDY_CID={'c' * 64}",
            f"AUTH_CADDY_CID={'8' * 64}",
        ),
        encoding="ascii",
    )
    immutable = (
        "manifest.txt",
        "compose.pre",
        "compose.post",
        "Caddyfile.pre",
        "Caddyfile.post",
    )
    (edge_dir / "SHA256SUMS").write_text(
        "".join(
            f"{hashlib.sha256((edge_dir / name).read_bytes()).hexdigest()}  {name}\n"
            for name in immutable
        ),
        encoding="ascii",
    )
    env["HSTS_STUB_CADDY_CID"] = successor
    lineage = control / "edge" / "successor-lineage.txt"
    lineage.write_text(
        "LINEAGE_FORMAT=edge-successor-v1\n"
        f"TARGET_COMMIT={TARGET}\n"
        f"CONTROL_MANIFEST_HASH={CONTROL_HASH}\n"
        f"ROOT_BASE_CADDY_CID={'c' * 64}\n"
        "ROOT_BASE_CADDY_IMAGE=sha256:" + "5" * 64 + "\n"
        "ROOT_BASE_CADDY_RESTARTS=0\n"
        f"GENERATION={EDGE_GENERATION}\n"
        "MUTATION_DOMAIN=edge\n"
        f"MUTATION_GENERATION={EDGE_GENERATION}\n"
        "ACTION=promote\n"
        f"GENERATION_BASE_CADDY_CID={'8' * 64}\n"
        "GENERATION_BASE_CADDY_IMAGE=sha256:" + "5" * 64 + "\n"
        "GENERATION_BASE_CADDY_RESTARTS=0\n"
        f"ACTION_BASE_CADDY_CID={'8' * 64}\n"
        "ACTION_BASE_CADDY_IMAGE=sha256:" + "5" * 64 + "\n"
        "ACTION_BASE_CADDY_RESTARTS=0\n"
        f"CURRENT_CADDY_CID={successor}\n"
        "CURRENT_CADDY_IMAGE=sha256:" + "5" * 64 + "\n"
        "CURRENT_CADDY_RESTARTS=0\n"
        "ASSISTANT_COMPOSE_SHA256="
        f"{hashlib.sha256((assistant / 'compose.production.yml').read_bytes()).hexdigest()}\n"
        "CADDYFILE_SHA256="
        f"{hashlib.sha256((assistant / 'Caddyfile').read_bytes()).hexdigest()}\n",
        encoding="ascii",
    )
    lineage.chmod(0o600)


def test_prepare_creates_root_scoped_generation_without_secret_copy(
    tmp_path: Path,
) -> None:
    env, control, _assistant, _calls = _fixture(tmp_path)

    result = _run_root(env, "prepare")

    assert result.returncode == 0, result.stderr
    generation = control / "hsts" / "generations" / GENERATION
    assert generation.is_dir()
    assert generation.stat().st_mode & 0o777 == 0o700
    assert {path.name for path in generation.iterdir()} == {
        "manifest.txt",
        "snapshot.txt",
        "SHA256SUMS",
        "state.txt",
    }
    for artifact in generation.iterdir():
        assert artifact.is_file()
        assert not artifact.is_symlink()
        assert artifact.stat().st_nlink == 1
        assert artifact.stat().st_mode & 0o777 == 0o600
        assert "fixture-secret-must-not-be-copied" not in artifact.read_text(
            encoding="ascii"
        )
    assert (generation / "snapshot.txt").read_text(encoding="ascii") == (
        "HSTS_PRE=300\nHSTS_POST=31536000\n"
    )
    manifest = (generation / "manifest.txt").read_text(encoding="ascii")
    assert f"TARGET_COMMIT={TARGET}\n" in manifest
    assert f"GENERATION={GENERATION}\n" in manifest
    assert "RELEASE_ID=v120-111111111111-20260730230000\n" in manifest
    assert "RELEASE_STATE_GENERATION=5\n" in manifest
    assert "RELEASE_STATE_SHA256=" in manifest
    assert "ASSISTANT_COMPOSE_PRE_SHA256=" in manifest
    assert "ASSISTANT_COMPOSE_POST_SHA256=" in manifest
    assert "ASSISTANT_RENDER_PRE_SHA256=" in manifest
    assert "ASSISTANT_RENDER_POST_SHA256=" in manifest
    assert "CADDYFILE_SHA256=" in manifest
    assert "APP_COMPOSE_SHA256=" in manifest
    assert f"AUTH_APP_CID={'a' * 64}\n" in manifest
    assert "AUTH_APP_IMAGE=sha256:" + "d" * 64 + "\n" in manifest
    assert "AUTH_APP_RESTARTS=0\n" in manifest
    assert f"AUTH_FRONTEND_CID={'f' * 64}\n" in manifest
    assert "AUTH_FRONTEND_IMAGE=sha256:" + "e" * 64 + "\n" in manifest
    assert "AUTH_FRONTEND_RESTARTS=0\n" in manifest
    assert f"AUTH_DB_CID={'b' * 64}\n" in manifest
    assert "AUTH_DB_IMAGE=sha256:" + "3" * 64 + "\n" in manifest
    assert "AUTH_DB_RESTARTS=0\n" in manifest
    assert f"EDGE_GENERATION={EDGE_GENERATION}\n" in manifest
    assert "EDGE_MANIFEST_SHA256=" in manifest
    assert "EDGE_STATE_SHA256=" in manifest
    assert "EDGE_COMPOSE_POST_SHA256=" in manifest
    assert "EDGE_CADDYFILE_POST_SHA256=" in manifest


def test_hsts_prepare_requires_exact_promoted_edge_generation(
    tmp_path: Path,
) -> None:
    env, control, _assistant, calls = _fixture(tmp_path)
    edge_state = control / "edge" / "generations" / EDGE_GENERATION / "state.txt"
    edge_state.write_text("EDGE_STATE=prepared\n", encoding="ascii")

    result = _run_root(env, "prepare")

    assert result.returncode != 0
    assert "edge generation" in result.stderr.lower()
    assert not (control / "hsts").exists()
    assert not calls.exists() or "up -d" not in calls.read_text(encoding="utf-8")


def test_hsts_prepare_accepts_exact_final_edge_successor_lineage(
    tmp_path: Path,
) -> None:
    env, control, assistant, _calls = _fixture(tmp_path)
    _install_final_edge_successor_lineage(env, control, assistant)

    result = _run_root(env, "prepare")

    assert result.returncode == 0, result.stderr


def test_prepare_rejects_extra_root_authority_field_before_mutation(
    tmp_path: Path,
) -> None:
    env, control, _assistant, calls = _fixture(tmp_path)
    root_state = control / "v120-state.state"
    malformed = root_state.read_text(encoding="ascii") + "UNAPPROVED=x\n"
    root_state.write_text(malformed, encoding="ascii")
    Path(env["HSTS_APP_DIR"], "backups", f"{RELEASE_ID}.state").write_text(
        malformed, encoding="ascii"
    )

    result = _run_root(env, "prepare")

    assert result.returncode != 0
    assert "root release authority" in result.stderr
    assert not (control / "hsts").exists()
    assert not calls.exists() or calls.read_text(encoding="utf-8") == ""


def test_prepare_rejects_incomplete_duplicate_or_wrong_control_authority(
    tmp_path: Path,
) -> None:
    mutations = (
        lambda text: text.replace("RELEASE_PHASE=observed\n", ""),
        lambda text: text + "TARGET_COMMIT=" + TARGET + "\n",
        lambda text: text.replace(
            f"CONTROL_MANIFEST_HASH={CONTROL_HASH}",
            "CONTROL_MANIFEST_HASH=" + "3" * 64,
        ),
    )
    for index, mutate in enumerate(mutations):
        case = tmp_path / str(index)
        case.mkdir()
        env, control, _assistant, calls = _fixture(case)
        root_state = control / "v120-state.state"
        malformed = mutate(root_state.read_text(encoding="ascii"))
        root_state.write_text(malformed, encoding="ascii")
        Path(env["HSTS_APP_DIR"], "backups", f"{RELEASE_ID}.state").write_text(
            malformed, encoding="ascii"
        )

        result = _run_root(env, "prepare")

        assert result.returncode != 0
        assert not (control / "hsts").exists()
        assert not calls.exists() or calls.read_text(encoding="utf-8") == ""


def test_prepare_rejects_missing_unsafe_or_malformed_authority_marker(
    tmp_path: Path,
) -> None:
    for index, mutation in enumerate(("missing", "mode", "content")):
        case = tmp_path / str(index)
        case.mkdir()
        env, control, _assistant, calls = _fixture(case)
        marker = Path(env["HSTS_AUTHORITY_MARKER"])
        if mutation == "missing":
            marker.unlink()
        elif mutation == "mode":
            marker.chmod(0o644)
        else:
            marker.write_text("AUTHORITY_FORMAT=wrong\n", encoding="ascii")

        result = _run_root(env, "prepare")

        assert result.returncode != 0
        assert "authority marker" in result.stderr
        assert not (control / "hsts").exists()
        assert not calls.exists() or calls.read_text(encoding="utf-8") == ""


def test_prepare_rejects_root_app_mirror_or_app_compose_drift(
    tmp_path: Path,
) -> None:
    for index, mutation in enumerate(("mirror", "compose")):
        case = tmp_path / str(index)
        case.mkdir()
        env, control, _assistant, calls = _fixture(case)
        if mutation == "mirror":
            Path(env["HSTS_APP_DIR"], "backups", f"{RELEASE_ID}.state").write_text(
                (control / "v120-state.state")
                .read_text(encoding="ascii")
                .replace("STATE_GENERATION=5", "STATE_GENERATION=4"),
                encoding="ascii",
            )
        else:
            Path(env["HSTS_APP_DIR"], "docker-compose.yml").write_text(
                "services:\n  drift: {}\n", encoding="utf-8"
            )

        result = _run_root(env, "prepare")

        assert result.returncode != 0
        assert not (control / "hsts").exists()
        assert not calls.exists() or calls.read_text(encoding="utf-8") == ""


def test_prepare_rejects_live_container_authority_drift_before_mutation(
    tmp_path: Path,
) -> None:
    overrides = (
        ("HSTS_STUB_APP_CID", "0" * 64),
        ("HSTS_STUB_APP_IMAGE", "sha256:" + "0" * 64),
        ("HSTS_STUB_RESTART_COUNT", "1"),
    )
    for index, (key, value) in enumerate(overrides):
        case = tmp_path / str(index)
        case.mkdir()
        env, control, _assistant, _calls = _fixture(case)
        env[key] = value

        result = _run_root(env, "prepare")

        assert result.returncode != 0
        assert "live release containers differ" in result.stderr
        assert not (control / "hsts").exists()


def test_prepare_atomically_creates_and_binds_fixed_assistant_health_locator(
    tmp_path: Path,
) -> None:
    env, control, assistant, _calls = _fixture(tmp_path)
    locator = Path(env["HSTS_ASSISTANT_HEALTH_FILE"])
    locator.unlink()

    result = _run_root(env, "prepare")

    assert result.returncode == 0, result.stderr
    assert locator.read_text(encoding="ascii") == (
        "https://assistant.example.test/health\n"
    )
    assert locator.stat().st_mode & 0o777 == 0o600
    assert locator.stat().st_nlink == 1
    assert not list(assistant.glob(".assistant-health.*"))
    manifest = (
        control / "hsts" / "generations" / GENERATION / "manifest.txt"
    ).read_text(encoding="ascii")
    assert "ASSISTANT_HEALTH_URL_SHA256=" in manifest


def test_prepare_rejects_unsafe_or_mismatched_assistant_health_locator(
    tmp_path: Path,
) -> None:
    for index, mutate in enumerate(("unsafe-mode", "mismatch")):
        case = tmp_path / str(index)
        case.mkdir()
        env, control, assistant, _calls = _fixture(case)
        locator = Path(env["HSTS_ASSISTANT_HEALTH_FILE"])
        if mutate == "unsafe-mode":
            locator.chmod(0o644)
        else:
            locator.write_text("https://other.example.test/health\n", encoding="ascii")
        compose_before = (assistant / "compose.production.yml").read_bytes()

        result = _run_root(env, "prepare")

        assert result.returncode != 0
        assert "assistant health locator" in result.stderr
        assert (assistant / "compose.production.yml").read_bytes() == (compose_before)
        assert not (control / "hsts" / "generations" / GENERATION).exists()


def test_promote_and_rollback_change_only_hsts_with_runtime_invariants(
    tmp_path: Path,
) -> None:
    env, control, assistant, calls = _fixture(tmp_path)
    caddy_before = (assistant / "Caddyfile").read_bytes()
    app_before = Path(env["HSTS_APP_DIR"], "docker-compose.yml").read_bytes()

    prepared = _run_root(env, "prepare")
    promoted = _run_root(env, "promote")

    assert prepared.returncode == 0, prepared.stderr
    assert promoted.returncode == 0, promoted.stderr
    compose = (assistant / "compose.production.yml").read_text(encoding="utf-8")
    assert 'IT_DATA_HSTS_MAX_AGE: "31536000"' in compose
    assert "UNRELATED_SECRET_REF: ${UNRELATED_SECRET_REF}" in compose
    generation = control / "hsts" / "generations" / GENERATION
    assert (generation / "state.txt").read_text(encoding="ascii") == (
        "HSTS_STATE=promoted\n"
    )

    rolled_back = _run_root(env, "rollback")

    assert rolled_back.returncode == 0, rolled_back.stderr
    compose = (assistant / "compose.production.yml").read_text(encoding="utf-8")
    assert 'IT_DATA_HSTS_MAX_AGE: "300"' in compose
    assert "UNRELATED_SECRET_REF: ${UNRELATED_SECRET_REF}" in compose
    assert (assistant / "Caddyfile").read_bytes() == caddy_before
    assert Path(env["HSTS_APP_DIR"], "docker-compose.yml").read_bytes() == (app_before)
    assert (generation / "state.txt").read_text(encoding="ascii") == (
        "HSTS_STATE=rolled_back\n"
    )
    log = calls.read_text(encoding="utf-8")
    assert "force-recreate caddy" in log
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert f" -X {method} " in f" {log} "
    assert "network disconnect" not in log
    assert "0.0.0.0:8080" not in log


def test_rollback_rejects_unrelated_compose_change_without_overwrite(
    tmp_path: Path,
) -> None:
    env, _control, assistant, calls = _fixture(tmp_path)
    assert _run_root(env, "prepare").returncode == 0
    assert _run_root(env, "promote").returncode == 0
    compose = assistant / "compose.production.yml"
    compose.write_text(
        compose.read_text(encoding="utf-8")
        + "  unrelated_service:\n    image: changed-concurrently\n",
        encoding="utf-8",
    )
    concurrent = compose.read_bytes()
    calls_before = calls.read_bytes()

    result = _run_root(env, "rollback")

    assert result.returncode != 0
    assert "CAS precondition mismatch" in result.stderr
    assert compose.read_bytes() == concurrent
    new_calls = calls.read_bytes()[len(calls_before) :]
    assert b" up -d " not in new_calls


def test_prepare_promote_and_rollback_are_idempotent(tmp_path: Path) -> None:
    env, control, _assistant, _calls = _fixture(tmp_path)

    results = [
        _run_root(env, "prepare"),
        _run_root(env, "prepare"),
        _run_root(env, "promote"),
        _run_root(env, "promote"),
        _run_root(env, "rollback"),
        _run_root(env, "rollback"),
    ]

    assert all(result.returncode == 0 for result in results), [
        result.stderr for result in results
    ]
    assert "idempotent=1" in results[1].stdout
    assert "idempotent=1" in results[3].stdout
    assert "idempotent=1" in results[5].stdout
    generation = control / "hsts" / "generations" / GENERATION
    assert not list(generation.parent.glob(".incoming-*"))


def test_inspect_classifies_pre_promoted_rolled_back_and_divergent(
    tmp_path: Path,
) -> None:
    env, control, assistant, _calls = _fixture(tmp_path)
    assert _run_root(env, "prepare").returncode == 0
    generation = control / "hsts" / "generations" / GENERATION

    exact_pre = _run_root(env, "inspect")
    assert exact_pre.returncode == 0
    assert exact_pre.stdout == "exact-pre\n"

    assert _run_root(env, "promote").returncode == 0
    (generation / "state.txt").write_text("HSTS_STATE=prepared\n", encoding="ascii")
    exact_promoted = _run_root(env, "inspect")
    assert exact_promoted.returncode == 0
    assert exact_promoted.stdout == "exact-promoted\n"

    (generation / "state.txt").write_text("HSTS_STATE=promoted\n", encoding="ascii")
    assert _run_root(env, "rollback").returncode == 0
    exact_rolled_back = _run_root(env, "inspect")
    assert exact_rolled_back.returncode == 0
    assert exact_rolled_back.stdout == "exact-rolled-back\n"

    compose = assistant / "compose.production.yml"
    compose.write_text(
        compose.read_text(encoding="utf-8") + "  mixed:\n    image: concurrent\n",
        encoding="utf-8",
    )
    divergent = _run_root(env, "inspect")
    assert divergent.returncode == 78
    assert divergent.stdout == "divergent-or-unknown\n"


def test_unknown_ssh_after_remote_change_reconciles_to_exact_promoted(
    tmp_path: Path,
) -> None:
    env, calls, stdin_log = _operator_fixture(
        tmp_path,
        [(255, ""), (0, "exact-promoted")],
    )

    result = _run_operator(env, "promote")

    assert result.returncode == 0, result.stderr
    assert "RECONCILED exact-promoted" in result.stdout
    assert stdin_log.read_text(encoding="ascii").splitlines() == ["0", "0"]
    call_text = calls.read_text(encoding="utf-8")
    assert " promote " in f" {call_text} "
    assert " inspect " in f" {call_text} "
    assert "rollback_https_ingress" not in call_text
    assert "rollback-now.sh" not in call_text


def test_unknown_ssh_states_fail_closed_without_wider_rollback(
    tmp_path: Path,
) -> None:
    cases = [
        ([(255, ""), (0, "exact-pre")], 75, "retry-safe"),
        ([(255, ""), (0, "exact-rolled-back")], 76, "already-rolled-back"),
        ([(255, ""), (0, "divergent-or-unknown")], 78, "manual-stop"),
        ([(255, ""), (78, "divergent-or-unknown")], 69, "unreachable"),
        ([(255, ""), (255, "")], 69, "unreachable"),
    ]
    for index, (responses, status, marker) in enumerate(cases):
        case = tmp_path / str(index)
        case.mkdir()
        env, calls, stdin_log = _operator_fixture(case, responses)

        result = _run_operator(env, "promote")

        assert result.returncode == status
        assert marker in result.stdout
        assert stdin_log.read_text(encoding="ascii").splitlines() == ["0", "0"]
        call_text = calls.read_text(encoding="utf-8")
        assert "rollback_https_ingress" not in call_text
        assert "rollback-now.sh" not in call_text


def test_operator_only_trusts_inspect_classification_with_zero_exit(
    tmp_path: Path,
) -> None:
    env, _calls, _stdin_log = _operator_fixture(
        tmp_path,
        [(255, ""), (78, "exact-promoted")],
    )

    result = _run_operator(env, "promote")

    assert result.returncode == 69
    assert "RECONCILED unreachable status=78" in result.stdout


def test_operator_action_reconciliation_accepts_only_expected_exact_state(
    tmp_path: Path,
) -> None:
    cases = (
        ("prepare", "exact-promoted", 76),
        ("rollback", "exact-promoted", 76),
        ("promote", "exact-pre", 75),
        ("promote", "exact-rolled-back", 76),
    )
    for index, (action, state, expected_status) in enumerate(cases):
        case = tmp_path / str(index)
        case.mkdir()
        env, _calls, _stdin_log = _operator_fixture(
            case,
            [(255, ""), (0, state)],
        )

        result = _run_operator(env, action)

        assert result.returncode == expected_status


def test_operator_ssh_is_batch_bounded_and_has_outer_timeout(
    tmp_path: Path,
) -> None:
    env, calls, _stdin_log = _operator_fixture(
        tmp_path,
        [(255, ""), (0, "exact-promoted")],
    )

    result = _run_operator(env, "promote")

    assert result.returncode == 0
    call_text = calls.read_text(encoding="utf-8")
    assert "-o BatchMode=yes" in call_text
    assert "-o ConnectTimeout=10" in call_text
    assert "-o ServerAliveInterval=5" in call_text
    assert "-o ServerAliveCountMax=2" in call_text
    operator = HSTS_OPERATOR.read_text(encoding="utf-8")
    assert "timeout --kill-after=5s 30s" in operator


def test_unknown_prepare_reconciles_exact_pre_as_completed(
    tmp_path: Path,
) -> None:
    env, _calls, stdin_log = _operator_fixture(
        tmp_path,
        [(255, ""), (0, "exact-pre")],
    )

    result = _run_operator(env, "prepare")

    assert result.returncode == 0, result.stderr
    assert "prepare-complete" in result.stdout
    assert stdin_log.read_text(encoding="ascii").splitlines() == ["0", "0"]


def test_explicit_reconcile_is_repeatable_and_read_only(tmp_path: Path) -> None:
    env, calls, stdin_log = _operator_fixture(
        tmp_path,
        [(0, "exact-promoted"), (0, "exact-promoted")],
    )

    first = _run_operator(env, "reconcile")
    second = _run_operator(env, "reconcile")

    assert first.returncode == second.returncode == 0
    assert (
        first.stdout
        == second.stdout
        == ("RECONCILED exact-promoted continue-verification\n")
    )
    call_lines = calls.read_text(encoding="utf-8").splitlines()
    assert len(call_lines) == 2
    assert all(" inspect " in f" {line} " for line in call_lines)
    assert all(" promote " not in f" {line} " for line in call_lines)
    assert stdin_log.read_text(encoding="ascii").splitlines() == ["0", "0"]


def test_explicit_reconcile_reports_each_known_exact_state_successfully(
    tmp_path: Path,
) -> None:
    cases = (
        ("exact-pre", "RECONCILED exact-pre observed"),
        ("exact-rolled-back", "RECONCILED exact-rolled-back observed"),
    )
    for index, (state, expected) in enumerate(cases):
        case = tmp_path / str(index)
        case.mkdir()
        env, calls, stdin_log = _operator_fixture(case, [(0, state)])

        result = _run_operator(env, "reconcile")

        assert result.returncode == 0, result.stdout
        assert result.stdout.strip() == expected
        assert len(calls.read_text(encoding="utf-8").splitlines()) == 1
        assert stdin_log.read_text(encoding="ascii").splitlines() == ["0"]


def test_prepare_failure_cleans_owned_staging_and_candidate(
    tmp_path: Path,
) -> None:
    env, control, assistant, _calls = _fixture(tmp_path)
    env["HSTS_TEST_FAILPOINT"] = "prepare-before-rename"

    result = _run_root(env, "prepare")

    assert result.returncode != 0
    generations = control / "hsts" / "generations"
    assert not (generations / GENERATION).exists()
    assert not list(generations.glob(".incoming-*"))
    assert not list(assistant.glob(".hsts-candidate.*"))


def test_generation_symlink_is_rejected_before_mutation(tmp_path: Path) -> None:
    env, control, assistant, _calls = _fixture(tmp_path)
    assert _run_root(env, "prepare").returncode == 0
    generation = control / "hsts" / "generations" / GENERATION
    manifest = generation / "manifest.txt"
    original = generation / "manifest.original"
    manifest.rename(original)
    manifest.symlink_to(original)
    before = (assistant / "compose.production.yml").read_bytes()

    result = _run_root(env, "promote")

    assert result.returncode != 0
    assert (assistant / "compose.production.yml").read_bytes() == before


def test_hsts_generation_rejects_extra_manifest_field_even_if_rehashed(
    tmp_path: Path,
) -> None:
    env, control, assistant, _calls = _fixture(tmp_path)
    assert _run_root(env, "prepare").returncode == 0
    generation = control / "hsts" / "generations" / GENERATION
    manifest = generation / "manifest.txt"
    manifest.write_text(
        manifest.read_text(encoding="ascii") + "EXTRA=x\n",
        encoding="ascii",
    )
    sums = generation / "SHA256SUMS"
    sums.write_text(
        f"{hashlib.sha256(manifest.read_bytes()).hexdigest()}  manifest.txt\n"
        f"{hashlib.sha256((generation / 'snapshot.txt').read_bytes()).hexdigest()}  snapshot.txt\n",
        encoding="ascii",
    )
    before = (assistant / "compose.production.yml").read_bytes()

    result = _run_root(env, "promote")

    assert result.returncode != 0
    assert (assistant / "compose.production.yml").read_bytes() == before


def test_release_lock_rejects_concurrent_hsts_operation_without_mutation(
    tmp_path: Path,
) -> None:
    env, control, assistant, _calls = _fixture(tmp_path)
    lock_fd = os.open(env["HSTS_LOCK_PATH"], os.O_RDONLY)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    before = (assistant / "compose.production.yml").read_bytes()
    try:
        result = _run_root(env, "prepare")
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)

    assert result.returncode == 75
    assert "HSTS_BUSY" in result.stderr
    assert (assistant / "compose.production.yml").read_bytes() == before
    assert not (control / "hsts").exists()


def test_promotion_cas_rechecks_live_compose_immediately_before_rename(
    tmp_path: Path,
) -> None:
    env, control, assistant, _calls = _fixture(tmp_path)
    assert _run_root(env, "prepare").returncode == 0
    env["HSTS_TEST_FAILPOINT"] = "cas-before-rename"

    result = _run_root(env, "promote")

    compose = assistant / "compose.production.yml"
    assert result.returncode != 0
    assert "# concurrent-cas-writer" in compose.read_text(encoding="utf-8")
    assert 'IT_DATA_HSTS_MAX_AGE: "300"' in compose.read_text(encoding="utf-8")
    generation = control / "hsts" / "generations" / GENERATION
    assert (generation / "state.txt").read_text(encoding="ascii") == (
        "HSTS_STATE=prepared\n"
    )


def test_hsts_exchange_restores_noncooperative_writer_without_recreate(
    tmp_path: Path,
) -> None:
    env, _control, assistant, calls = _fixture(tmp_path)
    assert _run_root(env, "prepare").returncode == 0
    calls_before = calls.read_bytes()
    env["HSTS_TEST_FAILPOINT"] = "cas-after-live-check-before-exchange"

    result = _run_root(env, "promote")

    assert result.returncode != 0
    compose = (assistant / "compose.production.yml").read_text(encoding="utf-8")
    assert "# concurrent-cas-writer" in compose
    assert 'IT_DATA_HSTS_MAX_AGE: "300"' in compose
    assert not list(assistant.glob(".hsts-cas-*"))
    new_calls = calls.read_bytes()[len(calls_before) :]
    assert b"up -d --no-deps --force-recreate caddy" not in new_calls


def test_hsts_shared_caddy_lock_blocks_inspect_without_mutation(
    tmp_path: Path,
) -> None:
    env, control, assistant, _calls = _fixture(tmp_path)
    assert _run_root(env, "prepare").returncode == 0
    lock_fd = os.open(env["HSTS_CADDY_LOCK_PATH"], os.O_RDONLY)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    before = _tree_snapshot(control, assistant)
    try:
        result = _run_root(env, "inspect")
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)

    assert result.returncode == 75
    assert "CADDY_BUSY" in result.stderr
    assert _tree_snapshot(control, assistant) == before


def test_live_hsts_directives_fail_exact_promoted_reconciliation(
    tmp_path: Path,
) -> None:
    env, _control, _assistant, _calls = _fixture(tmp_path)
    assert _run_root(env, "prepare").returncode == 0
    assert _run_root(env, "promote").returncode == 0
    env["HSTS_STUB_HEADER_SUFFIX"] = "; includeSubDomains; preload"

    result = _run_root(env, "inspect")

    assert result.returncode == 78
    assert result.stdout == "divergent-or-unknown\n"


def test_generation_rejects_same_target_with_changed_root_authority(
    tmp_path: Path,
) -> None:
    env, control, _assistant, _calls = _fixture(tmp_path)
    assert _run_root(env, "prepare").returncode == 0
    state = control / "v120-state.state"
    state.write_text(
        state.read_text(encoding="ascii").replace(
            "STATE_GENERATION=5", "STATE_GENERATION=6"
        ),
        encoding="ascii",
    )

    result = _run_root(env, "inspect")

    assert result.returncode != 0
    assert "root and app release authority mirrors differ" in result.stderr


def test_reconcile_completes_interrupted_scoped_rollback_state(
    tmp_path: Path,
) -> None:
    env, control, _assistant, _calls = _fixture(tmp_path)
    runtime = tmp_path / "runtime-hsts"
    runtime.write_text("300\n", encoding="ascii")
    env["HSTS_TEST_RUNTIME_FILE"] = str(runtime)
    assert _run_root(env, "prepare").returncode == 0
    assert _run_root(env, "promote").returncode == 0
    generation = control / "hsts" / "generations" / GENERATION
    env["HSTS_TEST_FAILPOINT"] = "after-caddy-recreate-before-state"

    result = _run_root(env, "rollback")

    assert result.returncode != 0
    assert "HSTS_ROLLBACK_OK" not in result.stdout
    env.pop("HSTS_TEST_FAILPOINT")
    assert (generation / "state.txt").read_text(encoding="ascii") == (
        "HSTS_STATE=promoted\n"
    )

    inspected = _run_root(env, "inspect")
    completed = _run_root(env, "rollback")

    assert inspected.returncode == 0
    assert inspected.stdout == "exact-rolled-back\n"
    assert completed.returncode == 0, completed.stderr
    assert (generation / "state.txt").read_text(encoding="ascii") == (
        "HSTS_STATE=rolled_back\n"
    )


def test_hsts_inspect_classifies_exact_config_pending_runtime_recovery(
    tmp_path: Path,
) -> None:
    env, control, _assistant, _calls = _fixture(tmp_path)
    runtime = tmp_path / "runtime-hsts"
    runtime.write_text("300\n", encoding="ascii")
    env["HSTS_TEST_RUNTIME_FILE"] = str(runtime)
    assert _run_root(env, "prepare").returncode == 0
    env["HSTS_TEST_FAILPOINT"] = "after-config-cas-before-lineage"
    interrupted = _run_root(env, "promote")
    assert interrupted.returncode != 0
    env.pop("HSTS_TEST_FAILPOINT")

    result = _run_root(env, "inspect")

    assert result.returncode == 0
    assert result.stdout == "exact-promote-pending\n"


def test_hsts_successor_lineage_recovers_after_compose_cas_before_recreate(
    tmp_path: Path,
) -> None:
    env, control, assistant, _calls = _fixture(tmp_path)
    _install_final_edge_successor_lineage(env, control, assistant)
    runtime = tmp_path / "runtime-hsts"
    runtime.write_text("300\n", encoding="ascii")
    env["HSTS_TEST_RUNTIME_FILE"] = str(runtime)
    assert _run_root(env, "prepare").returncode == 0
    env["HSTS_TEST_FAILPOINT"] = "after-config-cas-before-lineage"
    interrupted = _run_root(env, "promote")
    assert interrupted.returncode != 0
    env.pop("HSTS_TEST_FAILPOINT")
    assert runtime.read_text(encoding="ascii") == "300\n"
    assert (control / "edge" / "recreate-pending.txt").is_file()
    before_inspect = _tree_snapshot(control, assistant)

    inspected = _run_root(env, "inspect")

    assert inspected.returncode == 0, inspected.stderr
    assert inspected.stdout == "exact-promote-pending\n"
    assert _tree_snapshot(control, assistant) == before_inspect

    resumed = _run_root(env, "promote")

    assert resumed.returncode == 0, resumed.stderr
    assert runtime.read_text(encoding="ascii") == "31536000\n"
    assert _run_root(env, "inspect").stdout == "exact-promoted\n"


def test_hsts_recovers_exact_recreate_pending_after_container_replacement(
    tmp_path: Path,
) -> None:
    env, control, assistant, _calls = _fixture(tmp_path)
    runtime = tmp_path / "runtime-hsts"
    runtime.write_text("300\n", encoding="ascii")
    env["HSTS_TEST_RUNTIME_FILE"] = str(runtime)
    assert _run_root(env, "prepare").returncode == 0
    env["HSTS_STUB_NEXT_CADDY_CID"] = "9" * 64
    env["HSTS_TEST_FAILPOINT"] = "after-caddy-recreate-before-lineage"

    interrupted = _run_root(env, "promote")

    assert interrupted.returncode != 0
    assert "HSTS_PROMOTE_OK" not in interrupted.stdout
    pending = control / "edge" / "recreate-pending.txt"
    assert pending.is_file()
    pending_text = pending.read_text(encoding="ascii")
    assert "RECREATE_PENDING_FORMAT=caddy-recreate-v1" in pending_text
    assert f"TARGET_COMMIT={TARGET}" in pending_text
    assert f"CONTROL_MANIFEST_HASH={CONTROL_HASH}" in pending_text
    assert f"EDGE_GENERATION={EDGE_GENERATION}" in pending_text
    assert "MUTATION_DOMAIN=hsts" in pending_text
    assert f"MUTATION_GENERATION={GENERATION}" in pending_text
    assert "ACTION=promote" in pending_text
    assert f"OLD_CADDY_CID={'c' * 64}" in pending_text
    assert runtime.read_text(encoding="ascii") == "31536000\n"
    env.pop("HSTS_TEST_FAILPOINT")
    before_inspect = _tree_snapshot(control, assistant)

    inspected = _run_root(env, "inspect")

    assert inspected.returncode == 0, inspected.stderr
    assert inspected.stdout == "exact-promote-pending\n"
    assert _tree_snapshot(control, assistant) == before_inspect
    assert pending.exists()

    resumed = _run_root(env, "promote")

    assert resumed.returncode == 0, resumed.stderr
    assert not pending.exists()
    lineage = (control / "edge" / "successor-lineage.txt").read_text(
        encoding="ascii"
    )
    assert f"CURRENT_CADDY_CID={'9' * 64}" in lineage
    assert _run_root(env, "inspect").stdout == "exact-promoted\n"


def test_hsts_recovers_when_lineage_published_before_intent_clear(
    tmp_path: Path,
) -> None:
    env, control, assistant, _calls = _fixture(tmp_path)
    runtime = tmp_path / "runtime-hsts"
    runtime.write_text("300\n", encoding="ascii")
    env["HSTS_TEST_RUNTIME_FILE"] = str(runtime)
    assert _run_root(env, "prepare").returncode == 0
    env["HSTS_STUB_NEXT_CADDY_CID"] = "9" * 64
    env["HSTS_TEST_FAILPOINT"] = "after-successor-lineage-before-intent-clear"

    interrupted = _run_root(env, "promote")

    assert interrupted.returncode != 0
    assert "HSTS_PROMOTE_OK" not in interrupted.stdout
    pending = control / "edge" / "recreate-pending.txt"
    assert pending.is_file()
    lineage = (control / "edge" / "successor-lineage.txt").read_text(encoding="ascii")
    assert f"CURRENT_CADDY_CID={'9' * 64}" in lineage
    env.pop("HSTS_TEST_FAILPOINT")
    before_inspect = _tree_snapshot(control, assistant)

    inspected = _run_root(env, "inspect")

    assert inspected.returncode == 0, inspected.stderr
    assert inspected.stdout == "exact-promote-pending\n"
    assert _tree_snapshot(control, assistant) == before_inspect
    assert pending.exists()

    resumed = _run_root(env, "promote")

    assert resumed.returncode == 0, resumed.stderr
    assert not pending.exists()


def test_hsts_reconciles_non_durable_intent_delete_after_restart(
    tmp_path: Path,
) -> None:
    env, control, _assistant, _calls = _fixture(tmp_path)
    runtime = tmp_path / "runtime-hsts"
    runtime.write_text("300\n", encoding="ascii")
    env["HSTS_TEST_RUNTIME_FILE"] = str(runtime)
    assert _run_root(env, "prepare").returncode == 0
    env["HSTS_STUB_NEXT_CADDY_CID"] = "9" * 64
    env["HSTS_TEST_FAILPOINT"] = "after-successor-lineage-before-intent-clear"
    assert _run_root(env, "promote").returncode != 0
    env.pop("HSTS_TEST_FAILPOINT")
    pending = control / "edge" / "recreate-pending.txt"
    pending_bytes = pending.read_bytes()
    env["HSTS_STUB_FAIL_SYNC_EXACT"] = f"-d {control / 'edge'}"

    inspected = _run_root(env, "inspect")

    assert inspected.returncode == 0
    assert inspected.stdout == "exact-promote-pending\n"
    assert pending.exists()

    failed_clear = _run_root(env, "promote")

    assert failed_clear.returncode != 0
    assert not pending.exists()
    env.pop("HSTS_STUB_FAIL_SYNC_EXACT")
    pending.write_bytes(pending_bytes)
    pending.chmod(0o600)

    inspected_after_restart = _run_root(env, "inspect")

    assert inspected_after_restart.returncode == 0
    assert inspected_after_restart.stdout == "exact-promote-pending\n"
    assert pending.exists()

    reconciled = _run_root(env, "promote")

    assert reconciled.returncode == 0, reconciled.stderr
    assert not pending.exists()


def test_hsts_recreate_intent_propagates_every_durable_publish_failure(
    tmp_path: Path,
) -> None:
    cases = ("temp-sync", "rename", "file-sync", "directory-sync")
    for index, case_name in enumerate(cases):
        case = tmp_path / str(index)
        case.mkdir()
        env, control, _assistant, calls = _fixture(case)
        runtime = case / "runtime-hsts"
        runtime.write_text("300\n", encoding="ascii")
        env["HSTS_TEST_RUNTIME_FILE"] = str(runtime)
        assert _run_root(env, "prepare").returncode == 0
        pending = control / "edge" / "recreate-pending.txt"
        if case_name == "temp-sync":
            env["HSTS_STUB_FAIL_SYNC_CONTAINS"] = ".recreate-pending."
        elif case_name == "rename":
            env["HSTS_STUB_FAIL_MV_CONTAINS"] = ".recreate-pending."
        elif case_name == "file-sync":
            env["HSTS_STUB_FAIL_SYNC_EXACT"] = f"-f {pending}"
        else:
            env["HSTS_STUB_FAIL_SYNC_EXACT"] = f"-d {control / 'edge'}"
        up_before = calls.read_text(encoding="utf-8").count(
            "up -d --no-deps --force-recreate caddy"
        )

        failed = _run_root(env, "promote")

        assert failed.returncode != 0, case_name
        assert failed.returncode != 97, case_name
        assert "HSTS_PROMOTE_OK" not in failed.stdout
        state = control / "hsts" / "generations" / GENERATION / "state.txt"
        assert state.read_text(encoding="ascii") == "HSTS_STATE=prepared\n"
        assert (
            calls.read_text(encoding="utf-8").count(
                "up -d --no-deps --force-recreate caddy"
            )
            == up_before
        )
        env.pop("HSTS_STUB_FAIL_SYNC_CONTAINS", None)
        env.pop("HSTS_STUB_FAIL_MV_CONTAINS", None)
        env.pop("HSTS_STUB_FAIL_SYNC_EXACT", None)

        resumed = _run_root(env, "promote")

        assert resumed.returncode == 0, (case_name, resumed.stderr)
        assert not pending.exists()


def test_hsts_all_durable_publish_failures_stop_without_success(
    tmp_path: Path,
) -> None:
    cases = (
        ("health-locator-sync", "prepare", "sync", ".assistant-health."),
        ("generation-sync", "prepare", "sync", ".incoming-"),
        ("generation-rename", "prepare", "mv", ".incoming-"),
        ("candidate-sync", "promote", "sync", ".hsts-cas-"),
        ("lineage-sync", "promote", "sync", ".successor-lineage."),
        ("state-sync", "promote", "sync", "/.state."),
    )
    for index, (name, action, command, needle) in enumerate(cases):
        case = tmp_path / str(index)
        case.mkdir()
        env, _control, _assistant, _calls = _fixture(case)
        runtime = case / "runtime-hsts"
        runtime.write_text("300\n", encoding="ascii")
        env["HSTS_TEST_RUNTIME_FILE"] = str(runtime)
        if name == "health-locator-sync":
            Path(env["HSTS_ASSISTANT_HEALTH_FILE"]).unlink()
        if action == "promote":
            assert _run_root(env, "prepare").returncode == 0
            env["HSTS_STUB_NEXT_CADDY_CID"] = str(index + 3) * 64
        env[f"HSTS_STUB_FAIL_{command.upper()}_CONTAINS"] = needle

        failed = _run_root(env, action)

        assert failed.returncode != 0, name
        assert "HSTS_PREPARE_OK" not in failed.stdout, name
        assert "HSTS_PROMOTE_OK" not in failed.stdout, name
        env.pop(f"HSTS_STUB_FAIL_{command.upper()}_CONTAINS")
        resumed = _run_root(env, action)
        assert resumed.returncode == 0, (name, resumed.stderr)


def test_hsts_persistence_commands_are_explicitly_checked() -> None:
    for number, line in enumerate(
        HSTS_ROOT.read_text(encoding="utf-8").splitlines(), 1
    ):
        stripped = line.strip()
        if stripped.startswith(("sync ", "mv ", "ln --")):
            assert "||" in stripped, (number, stripped)


def test_hsts_recovers_every_lineage_publish_failure_after_config_cas(
    tmp_path: Path,
) -> None:
    cases = ("temp-sync", "rename", "file-sync", "directory-sync")
    for index, case_name in enumerate(cases):
        case = tmp_path / str(index)
        case.mkdir()
        env, control, assistant, calls = _fixture(case)
        runtime = case / "runtime-hsts"
        runtime.write_text("300\n", encoding="ascii")
        env["HSTS_TEST_RUNTIME_FILE"] = str(runtime)
        assert _run_root(env, "prepare").returncode == 0
        lineage = control / "edge" / "successor-lineage.txt"
        env["HSTS_STUB_FAIL_IF_FILE"] = str(assistant / "compose.production.yml")
        env["HSTS_STUB_FAIL_IF_CONTAINS"] = "31536000"
        if case_name == "temp-sync":
            env["HSTS_STUB_FAIL_SYNC_CONTAINS"] = ".successor-lineage."
        elif case_name == "rename":
            env["HSTS_STUB_FAIL_MV_CONTAINS"] = ".successor-lineage."
        elif case_name == "file-sync":
            env["HSTS_STUB_FAIL_SYNC_EXACT"] = f"-f {lineage}"
        else:
            env["HSTS_STUB_FAIL_SYNC_EXACT"] = f"-d {control / 'edge'}"

        interrupted = _run_root(env, "promote")

        assert interrupted.returncode != 0, case_name
        assert "HSTS_PROMOTE_OK" not in interrupted.stdout
        assert (control / "edge" / "recreate-pending.txt").is_file()
        assert "up -d --no-deps --force-recreate caddy" not in (
            calls.read_text(encoding="utf-8")
        )
        for key in (
            "HSTS_STUB_FAIL_SYNC_CONTAINS",
            "HSTS_STUB_FAIL_MV_CONTAINS",
            "HSTS_STUB_FAIL_SYNC_EXACT",
            "HSTS_STUB_FAIL_IF_FILE",
            "HSTS_STUB_FAIL_IF_CONTAINS",
        ):
            env.pop(key, None)

        inspected = _run_root(env, "inspect")
        resumed = _run_root(env, "promote")

        assert inspected.returncode == 0, (case_name, inspected.stderr)
        assert inspected.stdout == "exact-promote-pending\n"
        lineage_text = lineage.read_text(encoding="ascii")
        assert "MUTATION_DOMAIN=hsts" in lineage_text
        assert f"MUTATION_GENERATION={GENERATION}" in lineage_text
        assert "ACTION=promote" in lineage_text
        assert resumed.returncode == 0, (case_name, resumed.stderr)
        assert _run_root(env, "inspect").stdout == "exact-promoted\n"


def test_hsts_operator_resumes_only_exact_expected_transition(
    tmp_path: Path,
) -> None:
    env, calls, _stdin_log = _operator_fixture(
        tmp_path,
        [
            (255, ""),
            (0, "exact-promote-pending"),
            (0, ""),
            (0, "exact-promoted"),
        ],
    )

    result = _run_operator(env, "promote")

    assert result.returncode == 0, result.stderr
    assert "resumed-promote" in result.stdout
    lines = calls.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4
    assert " inspect " in f" {lines[1]} "
    assert " promote " in f" {lines[2]} "
    assert " inspect " in f" {lines[3]} "


def test_rollback_recovers_when_compose_changed_before_caddy_recreate(
    tmp_path: Path,
) -> None:
    env, control, assistant, _calls = _fixture(tmp_path)
    runtime = tmp_path / "runtime-hsts"
    runtime.write_text("300\n", encoding="ascii")
    env["HSTS_TEST_RUNTIME_FILE"] = str(runtime)
    assert _run_root(env, "prepare").returncode == 0
    assert _run_root(env, "promote").returncode == 0
    assert runtime.read_text(encoding="ascii") == "31536000\n"
    generation = control / "hsts" / "generations" / GENERATION
    env["HSTS_TEST_FAILPOINT"] = "after-config-cas-before-lineage"
    interrupted = _run_root(env, "rollback")
    assert interrupted.returncode != 0
    env.pop("HSTS_TEST_FAILPOINT")
    assert runtime.read_text(encoding="ascii") == "31536000\n"
    pending = control / "edge" / "recreate-pending.txt"
    assert pending.is_file()
    before_inspect = _tree_snapshot(control, assistant)

    inspected = _run_root(env, "inspect")

    assert inspected.returncode == 0, inspected.stderr
    assert inspected.stdout == "exact-rollback-pending\n"
    assert _tree_snapshot(control, assistant) == before_inspect
    assert pending.exists()

    recovered = _run_root(env, "rollback")

    assert recovered.returncode == 0, recovered.stderr
    assert runtime.read_text(encoding="ascii") == "300\n"
    assert (generation / "state.txt").read_text(encoding="ascii") == (
        "HSTS_STATE=rolled_back\n"
    )
    assert not pending.exists()


def test_root_control_production_mode_is_fixed_and_hash_addressed() -> None:
    script = HSTS_ROOT.read_text(encoding="utf-8")
    operator = HSTS_OPERATOR.read_text(encoding="utf-8")

    assert '[ "$EUID" -ne 0 ]' in script
    assert "root may not enable HSTS test mode" in script
    assert "APP_DIR=/home/ubuntu/apps/it-spareparts" in script
    assert "ASSISTANT_DIR=/opt/personal-ai-assistant" in script
    assert "COMMAND_DIR=" in script
    assert "ASSISTANT_HEALTH_URL=https://118.25.94.90/health" in script
    assert '"600 ubuntu:ubuntu 1"' in script
    assert "verify_packaged_control" in script
    assert 'install-v120-control.sh" verify "$manifest_hash"' in script
    assert "control package target differs from HSTS target" in script
    entrypoint = script.split(
        '[ "$#" -eq 4 ]',
        maxsplit=1,
    )[1]
    assert entrypoint.index("acquire_lock") < entrypoint.index(
        "verify_packaged_control"
    )
    assert "current control pointer changed under release lock" in script
    assert 'readlink -- "$CONTROL_DIR/current"' in script
    assert "--noproxy '*'" in script
    assert "--proto '=https'" in script
    assert "--tlsv1.2" in script
    assert "--max-redirs 0" in script
    assert (
        "/var/lib/it-spareparts-release-control/current/hsts-v120-root.sh"
    ) in operator
    assert "HSTS_OPERATOR_TEST_MODE" in operator
    assert (
        "REMOTE_ROOT=/var/lib/it-spareparts-release-control/current/hsts-v120-root.sh"
    ) in operator
    assert "${HSTS_REMOTE_ROOT_SCRIPT:-" not in operator


def test_runtime_verification_checks_tls_network_and_service_boundaries(
    tmp_path: Path,
) -> None:
    env, _control, _assistant, calls = _fixture(tmp_path)
    assert _run_root(env, "prepare").returncode == 0

    result = _run_root(env, "promote")

    assert result.returncode == 0, result.stderr
    log = calls.read_text(encoding="utf-8")
    assert "network inspect -f {{.Internal}} it-spareparts-ingress" in log
    assert "ps -q frontend" in log
    assert "ps -q app" in log
    assert "ps -q db" in log
    assert "https://assistant.example.test/health" in log
    assert "https://it.example.test/" in log
    script = HSTS_ROOT.read_text(encoding="utf-8")
    assert "probe_json_health" in script
    assert 'mime != "application/json"' in script
    assert 'payload.get("status") != sys.argv[1]' in script
    assert "not 200 <= status_code < 300" in script
    assert 'payload.get("db") != "reachable"' in script


def test_hsts_runtime_rejects_spa_html_health_response(
    tmp_path: Path,
) -> None:
    env, control, _assistant, _calls = _fixture(tmp_path)
    env["HSTS_STUB_HEALTH_HTML"] = "1"

    rejected = _run_root(env, "prepare")

    assert rejected.returncode != 0
    assert not (
        control / "hsts" / "generations" / GENERATION
    ).exists()


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
def test_hsts_assistant_health_requires_semantic_ready_json(
    tmp_path: Path,
    mode: str,
    accepted: bool,
) -> None:
    env, _control, _assistant, _calls = _fixture(tmp_path)
    env["HSTS_STUB_ASSISTANT_HEALTH"] = mode

    result = _run_root_library(
        env,
        'probe_json_health "$ASSISTANT_HEALTH_URL" ready assistant',
    )

    assert (result.returncode == 0) is accepted, result.stderr


@pytest.mark.parametrize("action", ("prepare", "promote", "rollback", "inspect"))
def test_hsts_all_actions_fail_closed_on_invalid_assistant_health(
    tmp_path: Path,
    action: str,
) -> None:
    env, _control, _assistant, _calls = _fixture(tmp_path)
    if action != "prepare":
        assert _run_root(env, "prepare").returncode == 0
    if action == "rollback":
        assert _run_root(env, "promote").returncode == 0
    env["HSTS_STUB_ASSISTANT_HEALTH"] = "html"

    result = _run_root(env, action)

    assert result.returncode != 0
    assert "HSTS_PREPARE_OK" not in result.stdout
    assert "HSTS_PROMOTE_OK" not in result.stdout
    assert "HSTS_ROLLBACK_OK" not in result.stdout
    script = HSTS_ROOT.read_text(encoding="utf-8")
    assert script.count(
        'probe_json_health "$assistant_health" ready assistant'
    ) == 1


def test_runtime_rejects_extra_network_or_ingress_member(
    tmp_path: Path,
) -> None:
    cases = (
        (
            "HSTS_STUB_CADDY_NETWORKS",
            '{"personal-ai-assistant-network":{},'
            '"it-spareparts-ingress":{},"rogue":{}}',
        ),
        (
            "HSTS_STUB_INGRESS_MEMBERS",
            '{"' + "c" * 64 + '":{},"' + "f" * 64 + '":{},"' + "9" * 64 + '":{}}',
        ),
    )
    for index, (key, value) in enumerate(cases):
        case = tmp_path / str(index)
        case.mkdir()
        env, _control, _assistant, _calls = _fixture(case)
        assert _run_root(env, "prepare").returncode == 0
        env[key] = value

        result = _run_root(env, "promote")

        assert result.returncode != 0


def test_hsts_runbook_uses_exact_control_and_reconciles_every_checkpoint() -> None:
    runbook = HSTS_RUNBOOK.read_text(encoding="utf-8")

    assert "HSTS_ROOT_SHA256" in runbook
    assert "HSTS_OPERATOR_SHA256" in runbook
    assert "CONTROL_MANIFEST_HASH" in runbook
    assert "RELEASE_PHASE=observed" in runbook
    assert "--supersedes" in runbook
    assert "assistant-health.url" in runbook
    assert "root:root:600" in runbook
    assert "https://118.25.94.90/health" in runbook
    assert "不得由应用账号、环境变量或操作员输入" in runbook
    assert "HSTS scoped rollback 保留" in runbook
    assert '"$OPERATOR" prepare' in runbook
    assert '"$OPERATOR" promote' in runbook
    assert '"$OPERATOR" rollback' in runbook
    assert '"$OPERATOR" reconcile' in runbook
    assert "exact-promoted" in runbook
    assert "max-age=31536000" in runbook
    assert "max-age=300" in runbook
    assert "includeSubDomains" in runbook
    assert "preload" in runbook
    assert all(f"{minute} 分钟" in runbook for minute in (0, 5, 15, 30))
    assert "rollback_https_ingress.sh" not in runbook
    assert "rollback-now.sh" not in runbook

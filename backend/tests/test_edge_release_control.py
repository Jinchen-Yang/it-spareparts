from __future__ import annotations

import fcntl
import hashlib
import http.client
import io
import os
import re
import runpy
import stat
import struct
import subprocess
import sys
import textwrap
import time
import zipfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
EDGE_ROOT = ROOT / ".deploy" / "edge_v120_root.sh"
EDGE_OPERATOR = ROOT / ".deploy" / "edge_v120_operator.sh"
EDGE_RUNBOOK = ROOT / "docs" / "releases" / "edge-v120-scoped-runbook.md"
HSTS_RUNBOOK = ROOT / "docs" / "releases" / "hsts-v120-scoped-runbook.md"
V120_RUNBOOK = ROOT / "docs" / "releases" / "v1.20-release-runbook.md"
ARTIFACT_VALIDATOR = ROOT / ".deploy" / "validate_release_artifacts.py"
MOBILE_PROBE = ROOT / ".deploy" / "mobile_release_probe.mjs"
TARGET = "1" * 40
CONTROL_HASH = "2" * 64
RELEASE_ID = "v120-111111111111-20260730230000"
GENERATION = "edge-111111111111-20260731T010000"
FINAL_GENERATION = "edge-111111111111-20260731T020000-final"
APP_CID = "a" * 64
DB_CID = "b" * 64
CADDY_CID = "c" * 64
FRONTEND_CID = "f" * 64


def _write_executable(path: Path, body: str) -> None:
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


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


def _minimal_xlsx() -> bytes:
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>""",
        )
        archive.writestr(
            "_rels/.rels",
            """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""",
        )
        archive.writestr(
            "xl/workbook.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>
</workbook>""",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>""",
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData/></worksheet>""",
        )
    return content.getvalue()


def _zip64_eocd(raw: bytes, *, disk_count: int = 1) -> bytes:
    eocd_offset = raw.rfind(b"PK\x05\x06")
    assert eocd_offset >= 0
    (
        _signature,
        disk_number,
        directory_disk,
        entries_on_disk,
        entries_total,
        directory_size,
        directory_offset,
        comment_size,
    ) = struct.unpack("<4s4H2IH", raw[eocd_offset : eocd_offset + 22])
    assert disk_number == directory_disk == comment_size == 0
    assert directory_offset + directory_size == eocd_offset
    zip64_record = struct.pack(
        "<4sQ2H2I4Q",
        b"PK\x06\x06",
        44,
        45,
        45,
        0,
        0,
        entries_on_disk,
        entries_total,
        directory_size,
        directory_offset,
    )
    locator = struct.pack(
        "<4sIQI",
        b"PK\x06\x07",
        0,
        eocd_offset,
        disk_count,
    )
    legacy_eocd = struct.pack(
        "<4s4H2IH",
        b"PK\x05\x06",
        0,
        0,
        0xFFFF,
        0xFFFF,
        0xFFFFFFFF,
        0xFFFFFFFF,
        0,
    )
    return raw[:eocd_offset] + zip64_record + locator + legacy_eocd


def _state(app_compose_hash: str) -> str:
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
        ("BASE_DB_CID", DB_CID),
        ("BASE_DB_IMAGE_ID", "sha256:" + "3" * 64),
        ("BASE_EDGE_CID", CADDY_CID),
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
        ("BACKUP_HASH", "8" * 64),
        ("NEW_APP_CID", APP_CID),
        ("NEW_FRONTEND_CID", FRONTEND_CID),
        ("MONITOR_SWITCH_MTIME", "1"),
        ("PUBLIC_OPENED_AT", "2026-07-30T23:00:00+08:00"),
        ("SWITCHED_AT", "2026-07-30T23:01:00+08:00"),
        ("OBSERVED_AT", "2026-07-30T23:31:00+08:00"),
    )
    return "".join(f"{key}={value}\n" for key, value in values)


def _fixture(tmp_path: Path) -> tuple[dict[str, str], Path, Path, Path]:
    app = tmp_path / "app"
    assistant = tmp_path / "assistant"
    control = tmp_path / "control"
    lock = tmp_path / "release-lock"
    shared_caddy_lock = tmp_path / "shared-caddy.lock"
    command = tmp_path / "bin"
    for directory in (app, assistant, control, lock, command):
        directory.mkdir(mode=0o700)
    shared_caddy_lock.write_bytes(b"")
    shared_caddy_lock.chmod(0o600)
    app_compose = app / "docker-compose.yml"
    app_compose.write_text(
        'services:\n  frontend:\n    ports:\n      - "127.0.0.1:8080:80"\n',
        encoding="utf-8",
    )
    app_compose.chmod(0o600)
    app_hash = hashlib.sha256(app_compose.read_bytes()).hexdigest()
    state = _state(app_hash)
    root_state = control / "v120-state.state"
    root_state.write_text(state, encoding="ascii")
    root_state.chmod(0o600)
    backups = app / "backups"
    backups.mkdir(mode=0o700)
    mirror = backups / f"{RELEASE_ID}.state"
    mirror.write_text(state, encoding="ascii")
    mirror.chmod(0o600)
    marker = control / "authority.marker"
    marker.write_text(
        "AUTHORITY_FORMAT=v120-authority-1\n"
        f"INITIAL_CONTROL_MANIFEST_HASH={CONTROL_HASH}\n"
        f"INITIAL_TARGET_COMMIT={TARGET}\n",
        encoding="ascii",
    )
    marker.chmod(0o600)
    for env_file in (app / ".env", assistant / ".env"):
        env_file.write_text("SAFE_FIXTURE=1\n", encoding="ascii")
        env_file.chmod(0o600)
    compose = assistant / "compose.production.yml"
    compose.write_text(
        "services:\n"
        "  caddy:\n"
        "    image: caddy:2\n"
        "    ports:\n"
        '      - "80:80"\n'
        '      - "443:443"\n'
        "    environment:\n"
        '      IT_DATA_HSTS_MAX_AGE: "300"\n',
        encoding="utf-8",
    )
    compose.chmod(0o600)
    caddyfile = assistant / "Caddyfile"
    caddyfile.write_text(
        "https://118.25.94.90 {\n"
        "  respond /health 200\n"
        "}\n"
        "http://hbzgc.icu {\n"
        "  redir https://hbzgc.icu{uri} permanent\n"
        "}\n"
        "https://hbzgc.icu {\n"
        "  reverse_proxy it-spareparts-frontend:80\n"
        "}\n",
        encoding="utf-8",
    )
    caddyfile.chmod(0o600)
    assistant_health = control / "assistant-health.url"
    assistant_health.write_text("https://118.25.94.90/health\n", encoding="ascii")
    assistant_health.chmod(0o600)
    calls = tmp_path / "calls.log"
    caddy_cid_file = tmp_path / "caddy.cid"
    caddy_cid_file.write_text(f"{CADDY_CID}\n", encoding="ascii")
    _write_executable(
        command / "docker",
        f"""
        #!/usr/bin/env bash
        set -euo pipefail
        printf 'docker %s\\n' "$*" >> "$EDGE_TEST_CALL_LOG"
        if [[ "$*" == *"config --format json"* ]]; then
          compose=
          while [ "$#" -gt 0 ]; do
            if [ "$1" = -f ]; then compose=$2; shift 2; continue; fi
            shift
          done
          python3 - "$compose" <<'PY'
import hashlib, json, sys
print(json.dumps({{"source": hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest()}}))
PY
          exit 0
        fi
        case "$*" in
          *"ps -q app"*) printf '%s\\n' '{APP_CID}'; exit 0 ;;
          *"ps -q frontend"*) printf '%s\\n' '{FRONTEND_CID}'; exit 0 ;;
          *"ps -q db"*) printf '%s\\n' '{DB_CID}'; exit 0 ;;
          *"inspect -f "*"Id"*personal-ai-assistant-caddy*)
            cat "$EDGE_STUB_CADDY_CID_FILE"; exit 0 ;;
          *"inspect -f "*"State.Running"*personal-ai-assistant-caddy*) printf 'true\\n'; exit 0 ;;
          *"inspect -f "*"Image"*)
            case "${{@: -1}}" in
              {APP_CID}) printf 'sha256:%064d\\n' 0 | tr 0 d ;;
              {FRONTEND_CID}) printf 'sha256:%064d\\n' 0 | tr 0 e ;;
              {DB_CID}) printf 'sha256:%064d\\n' 0 | tr 0 3 ;;
              {CADDY_CID}) printf 'sha256:%064d\\n' 0 | tr 0 5 ;;
              *) printf 'sha256:%064d\\n' 0 | tr 0 5 ;;
            esac
            exit 0 ;;
          *"inspect -f "*"RestartCount"*) printf '0\\n'; exit 0 ;;
          *"inspect -f "*"NetworkSettings.Networks"*)
            case "${{@: -1}}" in
              personal-ai-assistant-caddy)
                printf '%s\\n' '{{"personal-ai-assistant-network":{{}},"it-spareparts-ingress":{{}}}}' ;;
              {FRONTEND_CID})
                printf '%s\\n' '{{"it-spareparts_default":{{}},"it-spareparts-ingress":{{}}}}' ;;
              *) printf '%s\\n' '{{"it-spareparts_default":{{}}}}' ;;
            esac
            exit 0 ;;
          *"network inspect -f "*"Internal"*) printf 'true\\n'; exit 0 ;;
          *"network inspect -f "*"Containers"*)
            if [ -n "${{EDGE_STUB_INGRESS_MEMBERS:-}}" ]; then
              printf '%s\\n' "$EDGE_STUB_INGRESS_MEMBERS"
            else
              current_cid=$(cat "$EDGE_STUB_CADDY_CID_FILE")
              printf '{{"%s":{{}},"{FRONTEND_CID}":{{}}}}\\n' "$current_cid"
            fi
            exit 0 ;;
          *"port caddy 8080"*)
            grep -q '10.0.0.11:8080:8080' "$EDGE_ASSISTANT_DIR/compose.production.yml" \
              && printf '10.0.0.11:8080\\n'
            exit 0 ;;
          *"up -d --no-deps --force-recreate caddy"*)
            if [ -n "${{EDGE_STUB_NEXT_CADDY_CID:-}}" ]; then
              printf '%s\\n' "$EDGE_STUB_NEXT_CADDY_CID" \
                > "$EDGE_STUB_CADDY_CID_FILE"
            fi
            exit 0 ;;
          *"caddy validate"*) exit 0 ;;
        esac
        exit 0
        """,
    )
    _write_executable(
        command / "sync",
        r"""
        #!/usr/bin/env bash
        set -euo pipefail
        rendered=$*
        armed() {
          [ -z "${EDGE_STUB_FAIL_IF_FILE:-}" ] \
            || grep -Fq -- "$EDGE_STUB_FAIL_IF_CONTAINS" \
              "$EDGE_STUB_FAIL_IF_FILE"
        }
        if [ -n "${EDGE_STUB_FAIL_SYNC_EXACT:-}" ] \
            && [ "$rendered" = "$EDGE_STUB_FAIL_SYNC_EXACT" ] \
            && armed; then
          exit "${EDGE_STUB_PERSIST_RC:-73}"
        fi
        if [ -n "${EDGE_STUB_FAIL_SYNC_CONTAINS:-}" ] \
            && [[ "$rendered" == *"$EDGE_STUB_FAIL_SYNC_CONTAINS"* ]] \
            && armed; then
          exit "${EDGE_STUB_PERSIST_RC:-73}"
        fi
        exec /usr/bin/sync "$@"
        """,
    )
    _write_executable(
        command / "mv",
        r"""
        #!/usr/bin/env bash
        set -euo pipefail
        rendered=$*
        armed() {
          [ -z "${EDGE_STUB_FAIL_IF_FILE:-}" ] \
            || grep -Fq -- "$EDGE_STUB_FAIL_IF_CONTAINS" \
              "$EDGE_STUB_FAIL_IF_FILE"
        }
        if [ -n "${EDGE_STUB_FAIL_MV_CONTAINS:-}" ] \
            && [[ "$rendered" == *"$EDGE_STUB_FAIL_MV_CONTAINS"* ]] \
            && armed; then
          exit "${EDGE_STUB_PERSIST_RC:-73}"
        fi
        exec /usr/bin/mv "$@"
        """,
    )
    _write_executable(
        command / "curl",
        r"""
        #!/usr/bin/env bash
        set -euo pipefail
        printf 'curl %s\n' "$*" >> "$EDGE_TEST_CALL_LOG"
        target="${@: -1}"
        if [[ "$target" == http://10.0.0.11:8080/* ]]; then
          grep -q '10.0.0.11:8080:8080' \
            "$EDGE_ASSISTANT_DIR/compose.production.yml" || exit 7
          if [[ " $* " == *" -X POST "* ]] \
              || [[ " $* " == *" -X PUT "* ]] \
              || [[ " $* " == *" -X PATCH "* ]] \
              || [[ " $* " == *" -X DELETE "* ]]; then
            printf 'HTTP/1.1 405 Method Not Allowed\r\n\r\n'
          else
            suffix=${target#http://10.0.0.11:8080}
            printf 'HTTP/1.1 308 Permanent Redirect\r\nLocation: https://hbzgc.icu%s\r\n\r\n' "$suffix"
          fi
          exit 0
        fi
        case "$target" in
          https://hbzgc.icu/)
            printf 'HTTP/2 200\r\nstrict-transport-security: %s\r\n\r\n' \
              "${EDGE_STUB_HSTS:-max-age=300}"; exit 0 ;;
          https://118.25.94.90/health)
            [ "${EDGE_STUB_FAIL_HEALTH:-0}" != 1 ] || exit 22
            if [[ "$*" == *"%{content_type}"* ]]; then
              case "${EDGE_STUB_ASSISTANT_HEALTH:-ready}" in
                ready)
                  printf '{"status":"ready"}\n200\napplication/json' ;;
                html) printf '<!doctype html>SPA\n200\ntext/html' ;;
                mime) printf '{"status":"ready"}\n200\ntext/plain' ;;
                ok) printf '{"status":"ok"}\n200\napplication/json' ;;
                missing)
                  printf '{"service":"assistant"}\n200\napplication/json' ;;
                malformed) printf '{bad\n200\napplication/json' ;;
                http-error) exit 22 ;;
                *) exit 97 ;;
              esac
            else
              printf 'HTTP/2 200\r\n\r\n'
            fi
            exit 0 ;;
          https://hbzgc.icu/health|https://hbzgc.icu/health/db)
            [ "${EDGE_STUB_FAIL_HEALTH:-0}" != 1 ] || exit 22
            if [[ "$*" == *"%{content_type}"* ]]; then
              if [ "${EDGE_STUB_HEALTH_HTML:-0}" = 1 ]; then
                printf '<!doctype html>SPA\n200\ntext/html'
              elif [[ "$target" == */health/db ]]; then
                printf '{"status":"ok","db":"reachable"}\n200\napplication/json'
              else
                printf '{"status":"ok"}\n200\napplication/json'
              fi
            else
              printf 'HTTP/2 200\r\n\r\n'
            fi
            exit 0 ;;
          http://127.0.0.1:8080/health|http://127.0.0.1:8080/health/db)
            [ "${EDGE_STUB_FAIL_LOOPBACK:-0}" != 1 ] || exit 22
            if [ "${EDGE_STUB_HEALTH_HTML:-0}" = 1 ]; then
              printf '<!doctype html>SPA\n200\ntext/html'
            elif [[ "$target" == */health/db ]]; then
              printf '{"status":"ok","db":"reachable"}\n200\napplication/json'
            else
              printf '{"status":"ok"}\n200\napplication/json'
            fi
            exit 0 ;;
          http://hbzgc.icu/*)
            suffix=${target#http://hbzgc.icu}
            printf 'HTTP/1.1 308 Permanent Redirect\r\nLocation: https://hbzgc.icu%s\r\n\r\n' "$suffix"
            exit 0 ;;
        esac
        exit 97
        """,
    )
    _write_executable(
        command / "ss",
        r"""
        #!/usr/bin/env bash
        set -euo pipefail
        printf 'LISTEN 0 4096 127.0.0.1:8080 0.0.0.0:*\n'
        if grep -q '10.0.0.11:8080:8080' \
            "$EDGE_ASSISTANT_DIR/compose.production.yml"; then
          printf 'LISTEN 0 4096 10.0.0.11:8080 0.0.0.0:* users:(("docker-proxy",pid=1,fd=4))\n'
        fi
        """,
    )
    _write_executable(
        command / "ip",
        r"""
        #!/usr/bin/env bash
        set -euo pipefail
        if [ -n "${EDGE_STUB_IP_JSON:-}" ]; then
          printf '%s\n' "$EDGE_STUB_IP_JSON"
        else
          printf '%s\n' '[{"addr_info":[{"family":"inet","local":"10.0.0.11","prefixlen":22,"scope":"global"}]}]'
        fi
        """,
    )
    env = {
        **os.environ,
        "EDGE_V120_TEST_MODE": "1",
        "EDGE_APP_DIR": str(app),
        "EDGE_ASSISTANT_DIR": str(assistant),
        "EDGE_CONTROL_DIR": str(control),
        "EDGE_LOCK_PATH": str(lock),
        "EDGE_CADDY_LOCK_PATH": str(shared_caddy_lock),
        "EDGE_COMMAND_DIR": str(command),
        "EDGE_TEST_CALL_LOG": str(calls),
        "EDGE_STUB_CADDY_CID_FILE": str(caddy_cid_file),
        "EDGE_AUTHORITY_MARKER": str(marker),
        "EDGE_CONTROL_MANIFEST_HASH": CONTROL_HASH,
        "EDGE_V120_STATE_LIBRARY": str(ROOT / ".deploy" / "v120_state.sh"),
        "EDGE_ASSISTANT_HEALTH_FILE": str(assistant_health),
    }
    return env, control, assistant, calls


def _run_root(
    env: dict[str, str], action: str, generation: str = GENERATION
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(EDGE_ROOT), action, TARGET, generation],
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
        "EDGE_ROOT_LIBRARY_ONLY": "1",
    }
    return subprocess.run(
        ["bash", "-c", 'source "$1"; shift; eval "$1"', "bash", str(EDGE_ROOT), body],
        text=True,
        capture_output=True,
        check=False,
        env=library_env,
    )


def _operator_fixture(
    tmp_path: Path, responses: list[tuple[int, str]]
) -> tuple[dict[str, str], Path]:
    command = tmp_path / "operator-bin"
    command.mkdir()
    responses_file = tmp_path / "responses"
    responses_file.write_text(
        "".join(f"{status}|{output}\n" for status, output in responses),
        encoding="ascii",
    )
    count = tmp_path / "count"
    count.write_text("0\n", encoding="ascii")
    calls = tmp_path / "ssh-calls"
    _write_executable(
        command / "ssh",
        r"""
        #!/usr/bin/env bash
        set -euo pipefail
        printf '%s\n' "$*" >> "$EDGE_SSH_CALL_LOG"
        wc -c >/dev/null
        index=$(cat "$EDGE_SSH_COUNT")
        line=$(sed -n "$((index + 1))p" "$EDGE_SSH_RESPONSES")
        printf '%s\n' "$((index + 1))" > "$EDGE_SSH_COUNT"
        status=${line%%|*}
        output=${line#*|}
        [ -z "$output" ] || printf '%s\n' "$output"
        exit "$status"
        """,
    )
    return {
        **os.environ,
        "EDGE_OPERATOR_TEST_MODE": "1",
        "EDGE_OPERATOR_COMMAND_DIR": str(command),
        "EDGE_SSH_RESPONSES": str(responses_file),
        "EDGE_SSH_COUNT": str(count),
        "EDGE_SSH_CALL_LOG": str(calls),
    }, calls


def _run_operator(env: dict[str, str], action: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            str(EDGE_OPERATOR),
            action,
            "it-spareparts-prod",
            TARGET,
            GENERATION,
        ],
        input="must-not-reach-ssh\n",
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def test_edge_prepare_promote_and_rollback_are_exact_and_scoped(
    tmp_path: Path,
) -> None:
    env, control, assistant, calls = _fixture(tmp_path)
    before_compose = (assistant / "compose.production.yml").read_bytes()
    before_caddy = (assistant / "Caddyfile").read_bytes()

    prepared = _run_root(env, "prepare")
    promoted = _run_root(env, "promote")

    assert prepared.returncode == 0, prepared.stderr
    assert promoted.returncode == 0, promoted.stderr
    compose = (assistant / "compose.production.yml").read_text(encoding="utf-8")
    caddy = (assistant / "Caddyfile").read_text(encoding="utf-8")
    assert compose.count("10.0.0.11:8080:8080") == 1
    assert "118.25.94.90:8080" not in compose
    assert "0.0.0.0:8080" not in compose
    assert "[::]:8080" not in compose
    assert caddy.count(":8080 {") == 1
    assert "https://hbzgc.icu{uri}" in caddy
    assert "redir @safe https://hbzgc.icu{uri} 308" in caddy
    assert "permanent" not in caddy.split(":8080 {", 1)[1]
    assert "@safe method GET HEAD" in caddy
    assert "respond 405" in caddy
    edge_block = caddy.split(":8080 {", 1)[1].split("}", 1)[0]
    assert "reverse_proxy" not in edge_block
    assert "header" not in edge_block
    assert _run_root(env, "inspect").stdout == "exact-promoted\n"
    log = calls.read_text(encoding="utf-8")
    assert "http://10.0.0.11:8080/edge-check/path?scope=1" in log
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert f" -X {method} " in f" {log} "

    rolled_back = _run_root(env, "rollback")

    assert rolled_back.returncode == 0, rolled_back.stderr
    assert (assistant / "compose.production.yml").read_bytes() == before_compose
    assert (assistant / "Caddyfile").read_bytes() == before_caddy
    assert _run_root(env, "inspect").stdout == "exact-rolled-back\n"
    generation = control / "edge" / "generations" / GENERATION
    assert (generation / "state.txt").read_text(encoding="ascii") == (
        "EDGE_STATE=rolled_back\n"
    )


def test_edge_cas_rechecks_live_file_before_each_rename(tmp_path: Path) -> None:
    env, _control, assistant, _calls = _fixture(tmp_path)
    assert _run_root(env, "prepare").returncode == 0
    env["EDGE_TEST_FAILPOINT"] = "caddy-before-rename"

    result = _run_root(env, "promote")

    assert result.returncode != 0
    caddy = (assistant / "Caddyfile").read_text(encoding="utf-8")
    assert "# concurrent-edge-writer" in caddy
    assert ":8080 {" not in caddy
    assert "10.0.0.11:8080:8080" not in (
        assistant / "compose.production.yml"
    ).read_text(encoding="utf-8")


def test_edge_exchange_restores_noncooperative_writer_without_recreate(
    tmp_path: Path,
) -> None:
    env, _control, assistant, calls = _fixture(tmp_path)
    assert _run_root(env, "prepare").returncode == 0
    calls_before = calls.read_bytes()
    env["EDGE_TEST_FAILPOINT"] = "caddy-after-live-check-before-exchange"

    result = _run_root(env, "promote")

    assert result.returncode != 0
    caddy = (assistant / "Caddyfile").read_text(encoding="utf-8")
    assert "# concurrent-edge-writer" in caddy
    assert ":8080 {" not in caddy
    assert not list(assistant.glob(".edge-cas-*"))
    new_calls = calls.read_bytes()[len(calls_before) :]
    assert b"up -d --no-deps --force-recreate caddy" not in new_calls


def test_edge_shared_caddy_lock_blocks_inspect_without_mutation(
    tmp_path: Path,
) -> None:
    env, control, assistant, _calls = _fixture(tmp_path)
    assert _run_root(env, "prepare").returncode == 0
    lock_fd = os.open(env["EDGE_CADDY_LOCK_PATH"], os.O_RDONLY)
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


def test_edge_and_hsts_share_persistent_lock_and_document_writer_order() -> None:
    edge = EDGE_ROOT.read_text(encoding="utf-8")
    hsts = (ROOT / ".deploy" / "hsts_v120_root.sh").read_text(
        encoding="utf-8"
    )
    runbook = EDGE_RUNBOOK.read_text(encoding="utf-8")
    lock_path = "/etc/it-spareparts/shared-caddy.lock"

    assert lock_path in edge and lock_path in hsts and lock_path in runbook
    for script in (edge, hsts):
        assert script.index("\nacquire_lock\n") < script.index(
            "\nacquire_shared_caddy_lock\n"
        )
    assert "任何人工 writer" in runbook
    assert "v120 锁，再获取 shared-Caddy 锁" in runbook


def test_edge_promotion_requires_exact_pre_hsts_header(tmp_path: Path) -> None:
    env, _control, _assistant, _calls = _fixture(tmp_path)
    assert _run_root(env, "prepare").returncode == 0
    env["EDGE_STUB_HSTS"] = "max-age=300; includeSubDomains"

    result = _run_root(env, "promote")

    assert result.returncode != 0


def test_edge_requires_exact_private_host_address_before_mutation(
    tmp_path: Path,
) -> None:
    env, control, assistant, _calls = _fixture(tmp_path)
    env["EDGE_STUB_IP_JSON"] = (
        '[{"addr_info":[{"family":"inet","local":"10.0.0.11",'
        '"prefixlen":23,"scope":"global"}]}]'
    )
    before = (assistant / "compose.production.yml").read_bytes()

    result = _run_root(env, "prepare")

    assert result.returncode != 0
    assert (assistant / "compose.production.yml").read_bytes() == before
    assert not (control / "edge").exists()


def test_edge_exact_mixed_transition_is_classified_and_resumable(
    tmp_path: Path,
) -> None:
    env, _control, _assistant, _calls = _fixture(tmp_path)
    assert _run_root(env, "prepare").returncode == 0
    env["EDGE_TEST_FAILPOINT"] = "after-caddy-rename"
    interrupted = _run_root(env, "promote")
    assert interrupted.returncode != 0
    env.pop("EDGE_TEST_FAILPOINT")

    inspected = _run_root(env, "inspect")
    resumed = _run_root(env, "promote")

    assert inspected.returncode == 0
    assert inspected.stdout == "exact-promote-pending\n"
    assert resumed.returncode == 0, resumed.stderr
    assert _run_root(env, "inspect").stdout == "exact-promoted\n"


def test_edge_exact_mixed_rollback_is_classified_and_resumable(
    tmp_path: Path,
) -> None:
    env, _control, _assistant, _calls = _fixture(tmp_path)
    assert _run_root(env, "prepare").returncode == 0
    assert _run_root(env, "promote").returncode == 0
    env["EDGE_TEST_FAILPOINT"] = "after-compose-rollback"
    interrupted = _run_root(env, "rollback")
    assert interrupted.returncode != 0
    env.pop("EDGE_TEST_FAILPOINT")

    inspected = _run_root(env, "inspect")
    resumed = _run_root(env, "rollback")

    assert inspected.returncode == 0
    assert inspected.stdout == "exact-rollback-pending\n"
    assert resumed.returncode == 0, resumed.stderr
    assert _run_root(env, "inspect").stdout == "exact-rolled-back\n"


def test_edge_global_release_lock_blocks_before_generation_mutation(
    tmp_path: Path,
) -> None:
    env, control, _assistant, _calls = _fixture(tmp_path)
    lock_fd = os.open(env["EDGE_LOCK_PATH"], os.O_RDONLY)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = _run_root(env, "prepare")
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)

    assert result.returncode == 75
    assert not (control / "edge").exists()


def test_edge_operator_reconciles_unknown_ssh_and_rejects_nonzero_inspect(
    tmp_path: Path,
) -> None:
    env, calls = _operator_fixture(
        tmp_path,
        [(255, ""), (0, "exact-promoted")],
    )
    recovered = _run_operator(env, "promote")
    assert recovered.returncode == 0
    assert "exact-promoted" in recovered.stdout
    call_text = calls.read_text(encoding="utf-8")
    assert "-o BatchMode=yes" in call_text
    assert "-o ConnectTimeout=10" in call_text
    assert "-o ServerAliveInterval=5" in call_text
    assert "-o ServerAliveCountMax=2" in call_text

    failed_case = tmp_path / "nonzero"
    failed_case.mkdir()
    env, _calls = _operator_fixture(
        failed_case,
        [(255, ""), (78, "exact-promoted")],
    )
    failed = _run_operator(env, "promote")
    assert failed.returncode == 69
    assert "unreachable status=78" in failed.stdout


def test_edge_operator_resumes_only_exact_expected_transition(
    tmp_path: Path,
) -> None:
    env, calls = _operator_fixture(
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


def test_edge_reconciliation_accepts_only_root_recorded_caddy_successor(
    tmp_path: Path,
) -> None:
    env, _control, _assistant, _calls = _fixture(tmp_path)
    assert _run_root(env, "prepare").returncode == 0
    successor = "9" * 64
    env["EDGE_STUB_NEXT_CADDY_CID"] = successor
    assert _run_root(env, "promote").returncode == 0

    result = _run_root(env, "inspect")

    assert result.returncode == 0, result.stderr
    assert result.stdout == "exact-promoted\n"


def test_edge_real_caddy_2114_contract_is_explicit_308_and_bodyless(
    tmp_path: Path,
) -> None:
    env, control, _assistant, _calls = _fixture(tmp_path)
    prepared = _run_root(env, "prepare")
    assert prepared.returncode == 0, prepared.stderr
    candidate = control / "edge" / "generations" / GENERATION / "Caddyfile.post"
    image = "caddy:2.11.4"
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "-v",
            f"{candidate}:/etc/caddy/Caddyfile:ro",
            image,
            "caddy",
            "adapt",
            "--validate",
            "--config",
            "/etc/caddy/Caddyfile",
            "--adapter",
            "caddyfile",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    container = subprocess.run(
        [
            "docker",
            "run",
            "--detach",
            "--rm",
            "-p",
            "127.0.0.1::8080",
            "-v",
            f"{candidate}:/etc/caddy/Caddyfile:ro",
            image,
            "caddy",
            "run",
            "--config",
            "/etc/caddy/Caddyfile",
            "--adapter",
            "caddyfile",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    try:
        port_text = subprocess.run(
            ["docker", "port", container, "8080/tcp"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        port = int(port_text.rsplit(":", 1)[1])
        for _ in range(50):
            try:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                conn.request("GET", "/ready?probe=1")
                response = conn.getresponse()
                response.read()
                conn.close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise AssertionError("real Caddy edge did not become ready")

        for method in ("GET", "HEAD"):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            conn.request(method, "/a/b?x=1")
            response = conn.getresponse()
            body = response.read()
            headers = {key.lower(): value for key, value in response.getheaders()}
            conn.close()
            assert response.status == 308
            assert headers["location"] == "https://hbzgc.icu/a/b?x=1"
            assert "set-cookie" not in headers
            assert body == b""
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            conn.request(method, "/a/b?x=1", body=b"must-not-reach-app")
            response = conn.getresponse()
            body = response.read()
            headers = {key.lower(): value for key, value in response.getheaders()}
            conn.close()
            assert response.status == 405
            assert "set-cookie" not in headers
            assert body == b""
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container],
            check=False,
            capture_output=True,
            text=True,
        )


def test_edge_drill_rollback_authorizes_distinct_final_generation(
    tmp_path: Path,
) -> None:
    env, control, _assistant, _calls = _fixture(tmp_path)
    cid_file = Path(env["EDGE_STUB_CADDY_CID_FILE"])
    assert _run_root(env, "prepare").returncode == 0
    env["EDGE_STUB_NEXT_CADDY_CID"] = "9" * 64
    assert _run_root(env, "promote").returncode == 0
    assert cid_file.read_text(encoding="ascii").strip() == "9" * 64
    env["EDGE_STUB_NEXT_CADDY_CID"] = "8" * 64
    assert _run_root(env, "rollback").returncode == 0
    assert cid_file.read_text(encoding="ascii").strip() == "8" * 64

    final_prepare = _run_root(env, "prepare", FINAL_GENERATION)

    assert final_prepare.returncode == 0, final_prepare.stderr
    lineage = control / "edge" / "successor-lineage.txt"
    assert lineage.stat().st_mode & 0o777 == 0o600
    text = lineage.read_text(encoding="ascii")
    assert "LINEAGE_FORMAT=edge-successor-v1" in text
    assert f"ROOT_BASE_CADDY_CID={CADDY_CID}" in text
    assert f"CURRENT_CADDY_CID={'8' * 64}" in text
    assert f"GENERATION={GENERATION}" in text
    assert "ACTION=rollback" in text


def test_edge_successor_final_generation_recovers_after_first_cas(
    tmp_path: Path,
) -> None:
    env, control, _assistant, _calls = _fixture(tmp_path)
    cid_file = Path(env["EDGE_STUB_CADDY_CID_FILE"])
    assert _run_root(env, "prepare").returncode == 0
    env["EDGE_STUB_NEXT_CADDY_CID"] = "9" * 64
    assert _run_root(env, "promote").returncode == 0
    env["EDGE_STUB_NEXT_CADDY_CID"] = "8" * 64
    assert _run_root(env, "rollback").returncode == 0
    assert cid_file.read_text(encoding="ascii").strip() == "8" * 64
    assert _run_root(env, "prepare", FINAL_GENERATION).returncode == 0

    env["EDGE_TEST_FAILPOINT"] = "after-caddy-rename"
    interrupted = _run_root(env, "promote", FINAL_GENERATION)
    assert interrupted.returncode != 0
    env.pop("EDGE_TEST_FAILPOINT")

    lineage = (control / "edge" / "successor-lineage.txt").read_text(encoding="ascii")
    assert f"GENERATION={FINAL_GENERATION}" in lineage
    assert "MUTATION_DOMAIN=edge" in lineage
    assert f"MUTATION_GENERATION={FINAL_GENERATION}" in lineage
    assert "ACTION=promote" in lineage
    assert f"CURRENT_CADDY_CID={'8' * 64}" in lineage
    assert (
        _run_root(env, "inspect", FINAL_GENERATION).stdout == "exact-promote-pending\n"
    )

    env["EDGE_STUB_NEXT_CADDY_CID"] = "7" * 64
    resumed = _run_root(env, "promote", FINAL_GENERATION)

    assert resumed.returncode == 0, resumed.stderr
    assert cid_file.read_text(encoding="ascii").strip() == "7" * 64
    assert _run_root(env, "inspect", FINAL_GENERATION).stdout == "exact-promoted\n"


def test_edge_recovers_exact_recreate_pending_after_container_replacement(
    tmp_path: Path,
) -> None:
    env, control, assistant, _calls = _fixture(tmp_path)
    assert _run_root(env, "prepare").returncode == 0
    env["EDGE_STUB_NEXT_CADDY_CID"] = "9" * 64
    env["EDGE_TEST_FAILPOINT"] = "after-caddy-recreate-before-lineage"

    interrupted = _run_root(env, "promote")

    assert interrupted.returncode != 0
    assert "EDGE_PROMOTE_OK" not in interrupted.stdout
    pending = control / "edge" / "recreate-pending.txt"
    assert pending.is_file()
    pending_text = pending.read_text(encoding="ascii")
    assert "RECREATE_PENDING_FORMAT=caddy-recreate-v1" in pending_text
    assert f"TARGET_COMMIT={TARGET}" in pending_text
    assert f"CONTROL_MANIFEST_HASH={CONTROL_HASH}" in pending_text
    assert f"EDGE_GENERATION={GENERATION}" in pending_text
    assert "MUTATION_DOMAIN=edge" in pending_text
    assert f"MUTATION_GENERATION={GENERATION}" in pending_text
    assert "ACTION=promote" in pending_text
    assert f"OLD_CADDY_CID={CADDY_CID}" in pending_text
    assert f"TARGET_CADDY_CID={'9' * 64}" not in pending_text
    env.pop("EDGE_TEST_FAILPOINT")
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


def test_edge_prepare_uses_hash_addressed_builtin_assistant_locator(
    tmp_path: Path,
) -> None:
    env, _control, _assistant, _calls = _fixture(tmp_path)
    locator = Path(env["EDGE_ASSISTANT_HEALTH_FILE"])
    locator.unlink()

    prepared = _run_root(env, "prepare")

    assert prepared.returncode == 0, prepared.stderr
    assert not locator.exists()


def test_edge_control_has_no_hsts_locator_file_dependency() -> None:
    source = EDGE_ROOT.read_text(encoding="utf-8")

    assert "ASSISTANT_HEALTH_FILE" not in source
    assert "ASSISTANT_HEALTH_URL=https://118.25.94.90/health" in source


def test_edge_rollback_resumes_after_final_cas_without_read_only_mutation(
    tmp_path: Path,
) -> None:
    env, control, assistant, _calls = _fixture(tmp_path)
    cid_file = Path(env["EDGE_STUB_CADDY_CID_FILE"])
    assert _run_root(env, "prepare").returncode == 0
    env["EDGE_STUB_NEXT_CADDY_CID"] = "9" * 64
    assert _run_root(env, "promote").returncode == 0
    assert cid_file.read_text(encoding="ascii").strip() == "9" * 64

    env["EDGE_TEST_FAILPOINT"] = "after-final-cas-before-recreate"
    interrupted = _run_root(env, "rollback")

    assert interrupted.returncode != 0
    pending = control / "edge" / "recreate-pending.txt"
    assert pending.is_file()
    assert "ACTION=rollback" in pending.read_text(encoding="ascii")
    env.pop("EDGE_TEST_FAILPOINT")
    before_inspect = _tree_snapshot(control, assistant)

    inspected = _run_root(env, "inspect")

    assert inspected.returncode == 0, inspected.stderr
    assert inspected.stdout == "exact-rollback-pending\n"
    assert _tree_snapshot(control, assistant) == before_inspect
    assert cid_file.read_text(encoding="ascii").strip() == "9" * 64

    env["EDGE_STUB_NEXT_CADDY_CID"] = "8" * 64
    recovered = _run_root(env, "rollback")

    assert recovered.returncode == 0, recovered.stderr
    assert cid_file.read_text(encoding="ascii").strip() == "8" * 64
    assert not pending.exists()
    assert _run_root(env, "inspect").stdout == "exact-rolled-back\n"


def test_edge_promote_resumes_after_final_cas_without_read_only_mutation(
    tmp_path: Path,
) -> None:
    env, control, assistant, _calls = _fixture(tmp_path)
    cid_file = Path(env["EDGE_STUB_CADDY_CID_FILE"])
    assert _run_root(env, "prepare").returncode == 0
    env["EDGE_TEST_FAILPOINT"] = "after-final-cas-before-recreate"

    interrupted = _run_root(env, "promote")

    assert interrupted.returncode != 0
    pending = control / "edge" / "recreate-pending.txt"
    assert pending.is_file()
    assert "ACTION=promote" in pending.read_text(encoding="ascii")
    env.pop("EDGE_TEST_FAILPOINT")
    before_inspect = _tree_snapshot(control, assistant)

    inspected = _run_root(env, "inspect")

    assert inspected.returncode == 0, inspected.stderr
    assert inspected.stdout == "exact-promote-pending\n"
    assert _tree_snapshot(control, assistant) == before_inspect
    assert cid_file.read_text(encoding="ascii").strip() == CADDY_CID

    env["EDGE_STUB_NEXT_CADDY_CID"] = "9" * 64
    recovered = _run_root(env, "promote")

    assert recovered.returncode == 0, recovered.stderr
    assert cid_file.read_text(encoding="ascii").strip() == "9" * 64
    assert not pending.exists()
    assert _run_root(env, "inspect").stdout == "exact-promoted\n"


def test_edge_recovers_when_lineage_published_before_intent_clear(
    tmp_path: Path,
) -> None:
    env, control, assistant, _calls = _fixture(tmp_path)
    assert _run_root(env, "prepare").returncode == 0
    env["EDGE_STUB_NEXT_CADDY_CID"] = "9" * 64
    env["EDGE_TEST_FAILPOINT"] = "after-successor-lineage-before-intent-clear"

    interrupted = _run_root(env, "promote")

    assert interrupted.returncode != 0
    assert "EDGE_PROMOTE_OK" not in interrupted.stdout
    pending = control / "edge" / "recreate-pending.txt"
    assert pending.is_file()
    lineage = (control / "edge" / "successor-lineage.txt").read_text(encoding="ascii")
    assert f"CURRENT_CADDY_CID={'9' * 64}" in lineage
    env.pop("EDGE_TEST_FAILPOINT")
    before_inspect = _tree_snapshot(control, assistant)

    inspected = _run_root(env, "inspect")

    assert inspected.returncode == 0, inspected.stderr
    assert inspected.stdout == "exact-promote-pending\n"
    assert _tree_snapshot(control, assistant) == before_inspect
    assert pending.exists()

    resumed = _run_root(env, "promote")

    assert resumed.returncode == 0, resumed.stderr
    assert not pending.exists()


def test_edge_reconciles_non_durable_intent_delete_after_restart(
    tmp_path: Path,
) -> None:
    env, control, _assistant, _calls = _fixture(tmp_path)
    assert _run_root(env, "prepare").returncode == 0
    env["EDGE_STUB_NEXT_CADDY_CID"] = "9" * 64
    env["EDGE_TEST_FAILPOINT"] = "after-successor-lineage-before-intent-clear"
    assert _run_root(env, "promote").returncode != 0
    env.pop("EDGE_TEST_FAILPOINT")
    pending = control / "edge" / "recreate-pending.txt"
    pending_bytes = pending.read_bytes()
    env["EDGE_STUB_FAIL_SYNC_EXACT"] = f"-d {control / 'edge'}"

    inspected = _run_root(env, "inspect")

    assert inspected.returncode == 0
    assert inspected.stdout == "exact-promote-pending\n"
    assert pending.exists()

    failed_clear = _run_root(env, "promote")

    assert failed_clear.returncode != 0
    assert not pending.exists()
    env.pop("EDGE_STUB_FAIL_SYNC_EXACT")
    # Model a crash after unlink but before the directory fsync: the old,
    # already-durable intent may reappear after remount/restart.
    pending.write_bytes(pending_bytes)
    pending.chmod(0o600)

    inspected_after_restart = _run_root(env, "inspect")

    assert inspected_after_restart.returncode == 0
    assert inspected_after_restart.stdout == "exact-promote-pending\n"
    assert pending.exists()

    reconciled = _run_root(env, "promote")

    assert reconciled.returncode == 0, reconciled.stderr
    assert not pending.exists()


def test_edge_recreate_intent_propagates_every_durable_publish_failure(
    tmp_path: Path,
) -> None:
    cases = ("temp-sync", "rename", "file-sync", "directory-sync")
    for index, case_name in enumerate(cases):
        case = tmp_path / str(index)
        case.mkdir()
        env, control, _assistant, calls = _fixture(case)
        assert _run_root(env, "prepare").returncode == 0
        pending = control / "edge" / "recreate-pending.txt"
        if case_name == "temp-sync":
            env["EDGE_STUB_FAIL_SYNC_CONTAINS"] = ".recreate-pending."
        elif case_name == "rename":
            env["EDGE_STUB_FAIL_MV_CONTAINS"] = ".recreate-pending."
        elif case_name == "file-sync":
            env["EDGE_STUB_FAIL_SYNC_EXACT"] = f"-f {pending}"
        else:
            env["EDGE_STUB_FAIL_SYNC_EXACT"] = f"-d {control / 'edge'}"
        up_before = calls.read_text(encoding="utf-8").count(
            "up -d --no-deps --force-recreate caddy"
        )

        failed = _run_root(env, "promote")

        assert failed.returncode != 0, case_name
        assert failed.returncode != 97, case_name
        assert "EDGE_PROMOTE_OK" not in failed.stdout
        state = control / "edge" / "generations" / GENERATION / "state.txt"
        assert state.read_text(encoding="ascii") == "EDGE_STATE=prepared\n"
        assert (
            calls.read_text(encoding="utf-8").count(
                "up -d --no-deps --force-recreate caddy"
            )
            == up_before
        )
        env.pop("EDGE_STUB_FAIL_SYNC_CONTAINS", None)
        env.pop("EDGE_STUB_FAIL_MV_CONTAINS", None)
        env.pop("EDGE_STUB_FAIL_SYNC_EXACT", None)

        resumed = _run_root(env, "promote")

        assert resumed.returncode == 0, (case_name, resumed.stderr)
        assert not pending.exists()


def test_edge_all_durable_publish_failures_stop_without_success(
    tmp_path: Path,
) -> None:
    cases = (
        ("generation-sync", "prepare", "sync", ".incoming-"),
        ("generation-rename", "prepare", "mv", ".incoming-"),
        ("snapshot-sync", "promote", "sync", ".edge-cas-"),
        ("lineage-sync", "promote", "sync", ".successor-lineage."),
        ("state-sync", "promote", "sync", "/.state."),
    )
    for index, (name, action, command, needle) in enumerate(cases):
        case = tmp_path / str(index)
        case.mkdir()
        env, _control, _assistant, _calls = _fixture(case)
        if action == "promote":
            assert _run_root(env, "prepare").returncode == 0
            env["EDGE_STUB_NEXT_CADDY_CID"] = str(index + 3) * 64
        env[f"EDGE_STUB_FAIL_{command.upper()}_CONTAINS"] = needle

        failed = _run_root(env, action)

        assert failed.returncode != 0, name
        assert "EDGE_PREPARE_OK" not in failed.stdout, name
        assert "EDGE_PROMOTE_OK" not in failed.stdout, name
        env.pop(f"EDGE_STUB_FAIL_{command.upper()}_CONTAINS")
        resumed = _run_root(env, action)
        assert resumed.returncode == 0, (name, resumed.stderr)


def test_edge_persistence_commands_are_explicitly_checked() -> None:
    for number, line in enumerate(
        EDGE_ROOT.read_text(encoding="utf-8").splitlines(), 1
    ):
        stripped = line.strip()
        if stripped.startswith(("sync ", "mv ", "ln --")):
            assert "||" in stripped, (number, stripped)


def test_edge_recovers_every_lineage_publish_failure_after_config_cas(
    tmp_path: Path,
) -> None:
    cases = ("temp-sync", "rename", "file-sync", "directory-sync")
    for index, case_name in enumerate(cases):
        case = tmp_path / str(index)
        case.mkdir()
        env, control, assistant, calls = _fixture(case)
        assert _run_root(env, "prepare").returncode == 0
        lineage = control / "edge" / "successor-lineage.txt"
        env["EDGE_STUB_FAIL_IF_FILE"] = str(assistant / "Caddyfile")
        env["EDGE_STUB_FAIL_IF_CONTAINS"] = ":8080 {"
        if case_name == "temp-sync":
            env["EDGE_STUB_FAIL_SYNC_CONTAINS"] = ".successor-lineage."
        elif case_name == "rename":
            env["EDGE_STUB_FAIL_MV_CONTAINS"] = ".successor-lineage."
        elif case_name == "file-sync":
            env["EDGE_STUB_FAIL_SYNC_EXACT"] = f"-f {lineage}"
        else:
            env["EDGE_STUB_FAIL_SYNC_EXACT"] = f"-d {control / 'edge'}"

        interrupted = _run_root(env, "promote")

        assert interrupted.returncode != 0, case_name
        assert "EDGE_PROMOTE_OK" not in interrupted.stdout
        assert (control / "edge" / "recreate-pending.txt").is_file()
        assert "up -d --no-deps --force-recreate caddy" not in (
            calls.read_text(encoding="utf-8")
        )
        for key in (
            "EDGE_STUB_FAIL_SYNC_CONTAINS",
            "EDGE_STUB_FAIL_MV_CONTAINS",
            "EDGE_STUB_FAIL_SYNC_EXACT",
            "EDGE_STUB_FAIL_IF_FILE",
            "EDGE_STUB_FAIL_IF_CONTAINS",
        ):
            env.pop(key, None)

        inspected = _run_root(env, "inspect")
        resumed = _run_root(env, "promote")

        assert inspected.returncode == 0, (case_name, inspected.stderr)
        assert inspected.stdout == "exact-promote-pending\n"
        lineage_text = lineage.read_text(encoding="ascii")
        assert "MUTATION_DOMAIN=edge" in lineage_text
        assert f"MUTATION_GENERATION={GENERATION}" in lineage_text
        assert "ACTION=promote" in lineage_text
        assert resumed.returncode == 0, (case_name, resumed.stderr)
        assert _run_root(env, "inspect").stdout == "exact-promoted\n"


def test_edge_rejects_unrecorded_same_image_caddy_replacement(
    tmp_path: Path,
) -> None:
    env, control, _assistant, _calls = _fixture(tmp_path)
    Path(env["EDGE_STUB_CADDY_CID_FILE"]).write_text(f"{'9' * 64}\n", encoding="ascii")

    result = _run_root(env, "prepare", FINAL_GENERATION)

    assert result.returncode != 0
    assert not (control / "edge").exists()


def test_edge_rollback_and_pre_inspect_fail_closed_on_runtime_probe(
    tmp_path: Path,
) -> None:
    env, control, _assistant, _calls = _fixture(tmp_path)
    assert _run_root(env, "prepare").returncode == 0
    assert _run_root(env, "promote").returncode == 0
    env["EDGE_STUB_FAIL_LOOPBACK"] = "1"

    failed = _run_root(env, "rollback")

    assert failed.returncode != 0
    state = control / "edge" / "generations" / GENERATION / "state.txt"
    assert state.read_text(encoding="ascii") == "EDGE_STATE=promoted\n"
    inspected = _run_root(env, "inspect")
    assert inspected.returncode != 0
    assert inspected.stdout == "divergent-or-unknown\n", (
        inspected.returncode,
        inspected.stderr,
    )


def test_edge_idempotent_rollback_rechecks_pre_runtime(
    tmp_path: Path,
) -> None:
    env, _control, _assistant, _calls = _fixture(tmp_path)
    assert _run_root(env, "prepare").returncode == 0
    assert _run_root(env, "promote").returncode == 0
    assert _run_root(env, "rollback").returncode == 0
    env["EDGE_STUB_FAIL_HEALTH"] = "1"

    repeated = _run_root(env, "rollback")

    assert repeated.returncode != 0


def test_edge_repeated_promote_does_not_recreate_or_change_cid(
    tmp_path: Path,
) -> None:
    env, _control, _assistant, calls = _fixture(tmp_path)
    assert _run_root(env, "prepare").returncode == 0
    env["EDGE_STUB_NEXT_CADDY_CID"] = "9" * 64
    assert _run_root(env, "promote").returncode == 0
    first_cid = Path(env["EDGE_STUB_CADDY_CID_FILE"]).read_text(encoding="ascii")
    first_up_count = calls.read_text(encoding="utf-8").count(
        "up -d --no-deps --force-recreate caddy"
    )
    env["EDGE_STUB_NEXT_CADDY_CID"] = "8" * 64

    repeated = _run_root(env, "promote")

    assert repeated.returncode == 0, repeated.stderr
    assert (
        Path(env["EDGE_STUB_CADDY_CID_FILE"]).read_text(encoding="ascii") == first_cid
    )
    assert (
        calls.read_text(encoding="utf-8").count(
            "up -d --no-deps --force-recreate caddy"
        )
        == first_up_count
    )


def test_edge_rejects_extra_authority_or_generation_fields(
    tmp_path: Path,
) -> None:
    env, control, _assistant, _calls = _fixture(tmp_path)
    marker = Path(env["EDGE_AUTHORITY_MARKER"])
    marker.write_text(
        marker.read_text(encoding="ascii") + "EXTRA=x\n",
        encoding="ascii",
    )
    rejected_authority = _run_root(env, "prepare")
    assert rejected_authority.returncode != 0
    assert not (control / "edge").exists()

    generation_case = tmp_path / "generation"
    generation_case.mkdir()
    env, control, _assistant, _calls = _fixture(generation_case)
    assert _run_root(env, "prepare").returncode == 0
    manifest = control / "edge" / "generations" / GENERATION / "manifest.txt"
    manifest.write_text(
        manifest.read_text(encoding="ascii") + "EXTRA=x\n",
        encoding="ascii",
    )
    sums = manifest.parent / "SHA256SUMS"
    sums.write_text(
        "".join(
            f"{hashlib.sha256((manifest.parent / name).read_bytes()).hexdigest()}  {name}\n"
            for name in (
                "manifest.txt",
                "compose.pre",
                "compose.post",
                "Caddyfile.pre",
                "Caddyfile.post",
            )
        ),
        encoding="ascii",
    )
    rejected_generation = _run_root(env, "promote")
    assert rejected_generation.returncode != 0


def test_edge_control_is_fixed_root_only_and_packaged() -> None:
    root = EDGE_ROOT.read_text(encoding="utf-8")
    operator = EDGE_OPERATOR.read_text(encoding="utf-8")
    package = (ROOT / ".deploy" / "package_v120_control.sh").read_text(encoding="utf-8")
    installer = (ROOT / ".deploy" / "install_v120_control.sh").read_text(
        encoding="utf-8"
    )

    assert "10.0.0.11:8080:8080" in root
    assert "https://hbzgc.icu{uri}" in root
    assert "acquire_lock" in root
    assert root.rindex("acquire_lock") < root.rindex("verify_packaged_control")
    assert "edge-v120-root.sh" in operator
    assert "timeout --kill-after=5s 30s" in operator
    assert ".deploy/edge_v120_root.sh" in package
    assert "EDGE_ROOT_SHA256" in package
    assert "edge-v120-root.sh" in installer
    assert "EDGE_ROOT_SHA256" in installer


def test_public_https_health_paths_proxy_to_backend_without_spa_fallback() -> None:
    nginx = (ROOT / "frontend" / "nginx.conf").read_text(encoding="utf-8")
    edge = EDGE_ROOT.read_text(encoding="utf-8")

    assert "location = /health {" in nginx
    assert "proxy_pass http://app:8000/health;" in nginx
    assert "location = /health/db {" in nginx
    assert "proxy_pass http://app:8000/health/db;" in nginx
    spa = nginx.split("location / {", 1)[1].split("}", 1)[0]
    assert "/health" not in spa
    assert "probe_json_health" in edge
    assert 'mime != "application/json"' in edge
    assert 'payload.get("status") != sys.argv[1]' in edge
    assert "not 200 <= status_code < 300" in edge
    assert 'payload.get("db") != "reachable"' in edge


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
def test_edge_assistant_health_requires_semantic_ready_json(
    tmp_path: Path,
    mode: str,
    accepted: bool,
) -> None:
    env, _control, _assistant, _calls = _fixture(tmp_path)
    env["EDGE_STUB_ASSISTANT_HEALTH"] = mode

    result = _run_root_library(
        env,
        'probe_json_health "$ASSISTANT_HEALTH_URL" ready assistant',
    )

    assert (result.returncode == 0) is accepted, result.stderr


@pytest.mark.parametrize("action", ("prepare", "promote", "rollback", "inspect"))
def test_edge_all_actions_fail_closed_on_invalid_assistant_health(
    tmp_path: Path,
    action: str,
) -> None:
    env, _control, _assistant, _calls = _fixture(tmp_path)
    if action != "prepare":
        assert _run_root(env, "prepare").returncode == 0
    if action == "rollback":
        assert _run_root(env, "promote").returncode == 0
    env["EDGE_STUB_ASSISTANT_HEALTH"] = "html"

    result = _run_root(env, action)

    assert result.returncode != 0
    assert "EDGE_PREPARE_OK" not in result.stdout
    assert "EDGE_PROMOTE_OK" not in result.stdout
    assert "EDGE_ROLLBACK_OK" not in result.stdout
    edge = EDGE_ROOT.read_text(encoding="utf-8")
    assert edge.count(
        'probe_json_health "$ASSISTANT_HEALTH_URL" ready assistant'
    ) == 2


@pytest.mark.parametrize(
    "mode",
    ("html", "mime", "malformed", "missing", "ok"),
)
def test_edge_repeated_prepare_rechecks_assistant_semantic_health(
    tmp_path: Path,
    mode: str,
) -> None:
    env, control, _assistant, _calls = _fixture(tmp_path)
    first = _run_root(env, "prepare")
    generation = control / "edge" / "generations" / GENERATION
    before = _tree_snapshot(generation)
    env["EDGE_STUB_ASSISTANT_HEALTH"] = mode

    repeated = _run_root(env, "prepare")

    assert first.returncode == 0, first.stderr
    assert repeated.returncode != 0
    assert "EDGE_PREPARE_OK" not in repeated.stdout
    assert "idempotent=1" not in repeated.stdout
    assert _tree_snapshot(generation) == before


def test_edge_runtime_rejects_spa_html_health_response(
    tmp_path: Path,
) -> None:
    env, control, _assistant, _calls = _fixture(tmp_path)
    env["EDGE_STUB_HEALTH_HTML"] = "1"

    rejected = _run_root(env, "prepare")

    assert rejected.returncode != 0
    assert not (
        control / "edge" / "generations" / GENERATION
    ).exists()


def test_final_runbook_orders_app_edge_hsts_and_observation_with_rollback() -> None:
    runbook = EDGE_RUNBOOK.read_text(encoding="utf-8")
    mobile_probe = MOBILE_PROBE.read_text(encoding="utf-8")

    assert "TARGET_COMMIT='填写 40 位 merge commit'" in runbook
    assert "CONTROL_MANIFEST_HASH='填写 64 位 control manifest SHA-256'" in runbook
    assert "trap cleanup EXIT" in runbook
    assert 'find "$LOCAL_EVIDENCE" -depth' not in runbook
    assert 'find "$WORK_DIR"' in runbook
    assert "EDGE_DRILL=" in runbook
    assert "EDGE_FINAL=" in runbook
    assert "HSTS_DRILL=" in runbook
    assert "HSTS_FINAL=" in runbook
    assert '"$HSTS_FINAL" "$EDGE_FINAL"' in runbook
    assert "10.0.0.11:8080" in runbook
    assert "127.0.0.1:8080" in runbook
    assert "0.0.0.0" in runbook
    assert "[::]" in runbook
    assert "minute=$((elapsed / 60))" in runbook
    assert all(value in runbook for value in ("0 300 600 900", "ZIP CRC"))
    hsts_rollback = runbook.rindex('"$HSTS_OPERATOR" rollback')
    edge_rollback = runbook.rindex('"$EDGE_OPERATOR" rollback')
    assert hsts_rollback < edge_rollback
    assert "不得伪造 Token 或账号" in runbook
    assert "/final-observation/$TARGET_COMMIT/$EDGE_FINAL/$HSTS_FINAL" in runbook
    assert 'test ! -e "$evidence"' in runbook
    assert "authorized-rbac.txt" in runbook
    assert "Page.captureScreenshot" in mobile_probe
    assert "--window-size=375,812" in mobile_probe
    assert "--remote-debugging-pipe" in mobile_probe
    assert "--remote-debugging-port" not in mobile_probe
    assert "--remote-debugging-address" not in mobile_probe
    assert "--remote-allow-origins" not in mobile_probe
    assert "download_probe=200" in mobile_probe
    assert "issue153-remote-v1" in runbook
    assert "issue153-final-v1" in runbook
    assert "probe_json_health() {" in runbook
    assert 'mime != "application/json"' in runbook
    assert 'payload.get("status") != sys.argv[1]' in runbook
    assert 'payload.get("db") != "reachable"' in runbook
    assert (
        "probe_json_health https://118.25.94.90/health ready assistant"
        in runbook
    )
    assert "CHROME_BIN=/opt/google/chrome/google-chrome" in runbook
    assert "NODE_BIN=/usr/bin/node" in runbook
    assert "CHROME_SHA256_EXPECTED=" in runbook
    assert "NODE_SHA256_EXPECTED=" in runbook
    assert "timeout --kill-after=5s 180s" in runbook
    assert "AbortSignal.timeout" in mobile_probe
    assert "expectedRoute" in mobile_probe
    assert all(
        anchor in mobile_probe for anchor in ("详细盈亏", "下载中心", "项目提醒")
    )
    assert 'cat > "$mobile_script"' not in runbook
    assert 'import fs from "node:fs";' not in runbook

    release = V120_RUNBOOK.read_text(encoding="utf-8")
    app_observer = release.index("应用 0/5/15/30 分钟 observer")
    edge_final = release.index("Edge scoped drill/final", app_observer)
    hsts_final = release.index("HSTS scoped drill/final", edge_final)
    assert app_observer < edge_final < hsts_final
    gate = release.split("## 3.", 1)[0]
    assert "公网 `118.25.94.90:8080` 关闭" in gate
    assert "不得出现 `10.0.0.11:8080`" in gate


@pytest.mark.skipif(
    os.environ.get("IT_DATA_RELEASE_HOST_LIVE") != "1",
    reason="release-host binary pin verification is explicitly opt-in",
)
def test_mobile_runtime_contract_matches_this_control_host() -> None:
    runbook = EDGE_RUNBOOK.read_text(encoding="utf-8")
    chrome = Path("/opt/google/chrome/google-chrome")
    node = Path("/usr/bin/node")

    assert chrome.is_file() and not chrome.is_symlink()
    assert node.is_file() and not node.is_symlink()
    assert hashlib.sha256(chrome.read_bytes()).hexdigest() == (
        "aea09d69ce7f24d5901f6bfb15dd44d0c856e793e0a498f8d8393ec7d2c308ec"
    )
    assert hashlib.sha256(node.read_bytes()).hexdigest() == (
        "41a74efb34cbde5c7632cdac0cf8bd1a14d0b8d73dc1e82755014d9a9ce70f5c"
    )
    chrome_version = subprocess.run(
        [str(chrome), "--version"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.rstrip(" \t\r\n")
    node_version = subprocess.run(
        [str(node), "--version"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.rstrip(" \t\r\n")
    assert chrome_version == "Google Chrome 150.0.7871.186"
    assert node_version == "v24.18.0"
    assert "tr -d '\\r\\n' | sed 's/[[:space:]]*$//'" in runbook


def test_authoritative_docs_use_issue178_final_edge_contract() -> None:
    paths = (
        ROOT / "docs" / "DEPLOY.md",
        ROOT / "docs" / "releases" / "v1.20-release-runbook.md",
        ROOT / "docs" / "releases" / "https-ingress-runbook.md",
        HSTS_RUNBOOK,
        EDGE_RUNBOOK,
    )
    combined = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert "Issue #178" in text, path
        assert "10.0.0.11:8080" in text, path
    assert "经单独批准" in combined
    assert "安全组" in combined and "防火墙" in combined
    assert "TCP 8080" in combined
    assert "外部" in combined and "失败关闭" in combined
    assert "安全组永不" not in combined
    release = V120_RUNBOOK.read_text(encoding="utf-8")
    assert "当前应用发布阶段仍须保持旧公网" in release
    assert "Edge scoped control 建立" in release


def test_issue153_runbooks_are_clean_shell_copyable_and_define_inputs(
    tmp_path: Path,
) -> None:
    edge = EDGE_RUNBOOK.read_text(encoding="utf-8")
    hsts = HSTS_RUNBOOK.read_text(encoding="utf-8")

    assert 'CONTROL_PACKAGE="$PACKAGE_DIR"' in edge
    assert 'CONTROL_PACKAGE="/var/tmp/' not in edge
    for assignment in (
        "TARGET_COMMIT=",
        "PACKAGE_DIR=",
        "HSTS_OPERATOR_SHA256=",
        "EDGE_FINAL=",
        "OPERATOR=",
    ):
        assert assignment in hsts
    for stale_name in (
        "$tools",
        "$target_commit",
        "$parent_release_id",
    ):
        assert stale_name not in hsts
    assert "env -i" in edge
    assert "runbook-contract" in edge
    for text in (edge, hsts):
        blocks = "\n".join(
            match.group(1)
            for match in re.finditer(r"```bash\n(.*?)```", text, re.DOTALL)
        )
        parsed = subprocess.run(
            ["bash", "-n"],
            input=blocks,
            text=True,
            capture_output=True,
            check=False,
            env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        )
        assert parsed.returncode == 0, parsed.stderr
        checked = subprocess.run(
            ["shellcheck", "-s", "bash", "-e", "SC2016", "-"],
            input=blocks,
            text=True,
            capture_output=True,
            check=False,
            env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        )
        assert checked.returncode == 0, checked.stdout + checked.stderr

    package = tmp_path / "package"
    package.mkdir()
    manifest = package / "manifest.txt"
    manifest.write_text(f"TARGET_COMMIT={TARGET}\n", encoding="ascii")
    manifest_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
    contract = re.search(
        r"```bash\n(env -i .*?^runbook-contract\n)```",
        edge,
        re.DOTALL | re.MULTILINE,
    )
    assert contract is not None
    executed = subprocess.run(
        ["bash"],
        input=(
            f"TARGET_COMMIT={TARGET!r}\n"
            f"CONTROL_MANIFEST_HASH={manifest_hash!r}\n"
            f"PACKAGE_DIR={str(package)!r}\n" + contract.group(1)
        ),
        text=True,
        capture_output=True,
        check=False,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    assert executed.returncode == 0, executed.stderr

    evidence_contract = re.search(
        r"```bash\n(EVIDENCE_CONTRACT_ROOT=.*?^"
        r"rmdir \"\$EVIDENCE_CONTRACT_ROOT\"\n)```",
        edge,
        re.DOTALL | re.MULTILINE,
    )
    assert evidence_contract is not None
    evidence_executed = subprocess.run(
        ["bash"],
        input=(f"TARGET_COMMIT={TARGET!r}\n" + evidence_contract.group(1)),
        text=True,
        capture_output=True,
        check=False,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    assert evidence_executed.returncode == 0, evidence_executed.stderr


def test_final_observation_rechecks_public_nat_and_business_artifacts() -> None:
    runbook = EDGE_RUNBOOK.read_text(encoding="utf-8")

    assert "minute-$minute-public-8080.txt" in runbook
    for method in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"):
        assert f"method={method}" in runbook
    assert "Set-Cookie" in runbook
    assert "Location" in runbook
    assert "anonymous-api-401" in runbook
    assert "technical-token-403" in runbook
    assert "authorized-csv" in runbook
    assert "authorized-xlsx" in runbook
    assert "authorized-zip-crc" in runbook
    assert "mobile-browser" in runbook
    assert "external-limitation-restricted-account.txt" in runbook
    assert "external-limitation-admin-account.txt" not in runbook
    assert "external-limitation-mobile-browser.txt" not in runbook
    assert "from openpyxl" not in runbook
    assert "validate-release-artifacts.py" in runbook
    assert "/api/maintenance/board/export?lifecycle=all" in runbook
    assert "/api/maintenance/orders/export" in runbook
    assert "/api/maintenance/export-workbooks" in runbook
    assert '--max-time "$max_time"' in runbook
    assert '"$zip_file" "$zip_headers" 120' in runbook
    assert "ServerAliveInterval=5" in runbook
    assert "ServerAliveCountMax=2" in runbook


def test_release_artifact_validator_rejects_mime_and_fake_files(
    tmp_path: Path,
) -> None:
    csv_header = tmp_path / "csv.headers"
    xlsx_header = tmp_path / "xlsx.headers"
    zip_header = tmp_path / "zip.headers"
    csv_header.write_text(
        "Content-Type: text/csv; charset=utf-8\r\n",
        encoding="ascii",
    )
    xlsx_header.write_text(
        "Content-Type: "
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n",
        encoding="ascii",
    )
    zip_header.write_text("Content-Type: application/zip\r\n", encoding="ascii")
    csv_file = tmp_path / "profit.csv"
    csv_file.write_text(
        "\ufeff合同,关联项目,order_count,missing_detail_orders,"
        "revenue_inc,revenue_ex,expense_inc,expense_ex,"
        "parts_cost_inc_tax,parts_cost_ex_tax,"
        "parts_gross_profit_inc,parts_gross_profit_ex,"
        "parts_gross_margin_inc,parts_gross_margin_ex,"
        "contribution_profit_inc,contribution_profit_ex,"
        "contribution_margin_inc,contribution_margin_ex,"
        "parts_profit_status_inc,parts_profit_status_ex,"
        "contribution_status_inc,contribution_status_ex,"
        "成本证据状态,成本证据状态-含税,成本证据状态-未税,"
        "收入证据状态-含税,收入证据状态-未税,费用证据状态\n"
        "C-1,P-1,1,0,100,90,10,9,20,18,80,72,0.8,0.8,"
        "70,63,0.7,0.7,complete,complete,complete,complete,"
        "complete,complete,complete,available,available,available\n",
        encoding="utf-8",
    )
    xlsx_file = tmp_path / "orders.xlsx"
    xlsx_file.write_bytes(_minimal_xlsx())
    zip_file = tmp_path / "workbooks.zip"
    with zipfile.ZipFile(zip_file, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("合同一.xlsx", _minimal_xlsx())

    for kind, artifact, headers in (
        ("csv", csv_file, csv_header),
        ("xlsx", xlsx_file, xlsx_header),
        ("zip", zip_file, zip_header),
    ):
        valid = subprocess.run(
            ["python3", str(ARTIFACT_VALIDATOR), kind, str(artifact), str(headers)],
            text=True,
            capture_output=True,
            check=False,
        )
        assert valid.returncode == 0, (kind, valid.stderr)

    wrong_mime = tmp_path / "wrong.headers"
    wrong_mime.write_text("Content-Type: text/plain\r\n", encoding="ascii")
    garbage_csv = tmp_path / "garbage.csv"
    garbage_csv.write_text("not,a,real,export\n", encoding="utf-8")
    fake_xlsx = tmp_path / "fake.xlsx"
    fake_xlsx.write_bytes(b"PK fake spreadsheet")
    invalid_cases = (
        ("csv", csv_file, wrong_mime),
        ("csv", garbage_csv, csv_header),
        ("xlsx", fake_xlsx, xlsx_header),
    )
    for kind, artifact, headers in invalid_cases:
        invalid = subprocess.run(
            ["python3", str(ARTIFACT_VALIDATOR), kind, str(artifact), str(headers)],
            text=True,
            capture_output=True,
            check=False,
        )
        assert invalid.returncode != 0, kind


def test_release_artifact_validator_rejects_oversized_xlsx_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = tmp_path / "xlsx.headers"
    headers.write_text(
        "Content-Type: "
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n",
        encoding="ascii",
    )
    oversized = tmp_path / "oversized.xlsx"
    with oversized.open("wb") as target:
        target.seek(64 * 1024 * 1024)
        target.write(b"\0")

    validator = runpy.run_path(str(ARTIFACT_VALIDATOR))
    real_open = Path.open
    real_os_open = os.open

    def reject_artifact_open(path: Path, *args: object, **kwargs: object):
        if path == oversized:
            pytest.fail("oversized XLSX was opened before its size was rejected")
        return real_open(path, *args, **kwargs)

    def reject_artifact_os_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *args: object,
        **kwargs: object,
    ):
        if Path(path) == oversized:
            pytest.fail("oversized XLSX was opened before its size was rejected")
        return real_os_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", reject_artifact_open)
    monkeypatch.setattr(os, "open", reject_artifact_os_open)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(ARTIFACT_VALIDATOR),
            "xlsx",
            str(oversized),
            str(headers),
        ],
    )

    with pytest.raises(SystemExit, match="XLSX is empty or exceeds 64 MiB"):
        validator["main"]()


def test_release_artifact_validator_rejects_declared_zip_fanout_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = tmp_path / "zip.headers"
    headers.write_text("Content-Type: application/zip\r\n", encoding="ascii")
    malicious = tmp_path / "declared-fanout.zip"
    declared_entries = 501
    central_directory_size = declared_entries * 46
    with malicious.open("wb") as target:
        target.seek(central_directory_size)
        target.write(
            struct.pack(
                "<4s4H2IH",
                b"PK\x05\x06",
                0,
                0,
                declared_entries,
                declared_entries,
                central_directory_size,
                0,
                0,
            )
        )

    validator = runpy.run_path(str(ARTIFACT_VALIDATOR))

    def reject_zipfile_open(*args: object, **kwargs: object) -> None:
        pytest.fail("ZipFile was called before the central directory was bounded")

    monkeypatch.setattr(zipfile, "ZipFile", reject_zipfile_open)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(ARTIFACT_VALIDATOR),
            "zip",
            str(malicious),
            str(headers),
        ],
    )

    with pytest.raises(SystemExit, match="ZIP entry count is outside"):
        validator["main"]()


def test_release_artifact_validator_rejects_actual_xlsx_fanout_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = tmp_path / "xlsx.headers"
    headers.write_text(
        "Content-Type: "
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n",
        encoding="ascii",
    )
    malicious = tmp_path / "actual-fanout.xlsx"
    central_header = b"PK\x01\x02" + b"\0" * 42
    central_directory = central_header * 257
    malicious.write_bytes(
        central_directory
        + struct.pack(
            "<4s4H2IH",
            b"PK\x05\x06",
            0,
            0,
            1,
            1,
            len(central_directory),
            0,
            0,
        )
    )

    validator = runpy.run_path(str(ARTIFACT_VALIDATOR))

    def reject_zipfile_open(*args: object, **kwargs: object) -> None:
        pytest.fail("ZipFile was called before XLSX directory fanout was bounded")

    monkeypatch.setattr(zipfile, "ZipFile", reject_zipfile_open)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(ARTIFACT_VALIDATOR),
            "xlsx",
            str(malicious),
            str(headers),
        ],
    )

    with pytest.raises(SystemExit, match="ZIP entry count is outside"):
        validator["main"]()


def test_release_artifact_validator_rejects_prefixed_zip_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = tmp_path / "zip.headers"
    headers.write_text("Content-Type: application/zip\r\n", encoding="ascii")
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("合同一.xlsx", _minimal_xlsx())
    prefixed = tmp_path / "prefixed.zip"
    prefixed.write_bytes(b"self-extracting-prefix" + content.getvalue())

    validator = runpy.run_path(str(ARTIFACT_VALIDATOR))

    def reject_zipfile_open(*args: object, **kwargs: object) -> None:
        pytest.fail("ZipFile was called before prefixed ZIP metadata was rejected")

    monkeypatch.setattr(zipfile, "ZipFile", reject_zipfile_open)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(ARTIFACT_VALIDATOR),
            "zip",
            str(prefixed),
            str(headers),
        ],
    )

    with pytest.raises(SystemExit, match="directory bounds are inconsistent"):
        validator["main"]()


def test_release_artifact_validator_rejects_encrypted_metadata_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = tmp_path / "zip.headers"
    headers.write_text("Content-Type: application/zip\r\n", encoding="ascii")
    central_header = bytearray(b"PK\x01\x02" + b"\0" * 42)
    struct.pack_into("<H", central_header, 8, 0x1)
    malicious = tmp_path / "encrypted-directory.zip"
    malicious.write_bytes(
        central_header
        + struct.pack(
            "<4s4H2IH",
            b"PK\x05\x06",
            0,
            0,
            1,
            1,
            len(central_header),
            0,
            0,
        )
    )
    validator = runpy.run_path(str(ARTIFACT_VALIDATOR))

    def reject_zipfile_open(*args: object, **kwargs: object) -> None:
        pytest.fail("ZipFile was called before encrypted metadata was rejected")

    monkeypatch.setattr(zipfile, "ZipFile", reject_zipfile_open)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(ARTIFACT_VALIDATOR),
            "zip",
            str(malicious),
            str(headers),
        ],
    )

    with pytest.raises(SystemExit, match="encrypted or masked"):
        validator["main"]()


def test_release_artifact_validator_accepts_zip64_and_rejects_multidisk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = tmp_path / "zip.headers"
    headers.write_text("Content-Type: application/zip\r\n", encoding="ascii")
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("合同一.xlsx", _minimal_xlsx())
    valid_zip64 = tmp_path / "valid-zip64.zip"
    valid_zip64.write_bytes(_zip64_eocd(content.getvalue()))

    valid = subprocess.run(
        [
            "python3",
            str(ARTIFACT_VALIDATOR),
            "zip",
            str(valid_zip64),
            str(headers),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert valid.returncode == 0, valid.stderr

    multidisk = tmp_path / "multidisk-zip64.zip"
    multidisk.write_bytes(_zip64_eocd(content.getvalue(), disk_count=2))
    validator = runpy.run_path(str(ARTIFACT_VALIDATOR))

    def reject_zipfile_open(*args: object, **kwargs: object) -> None:
        pytest.fail("ZipFile was called before multi-disk ZIP64 was rejected")

    monkeypatch.setattr(zipfile, "ZipFile", reject_zipfile_open)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(ARTIFACT_VALIDATOR),
            "zip",
            str(multidisk),
            str(headers),
        ],
    )

    with pytest.raises(SystemExit, match="multi-disk ZIP64"):
        validator["main"]()


def test_release_artifact_validator_accepts_opc_targets_and_rejects_traversal(
    tmp_path: Path,
) -> None:
    from openpyxl import Workbook

    headers = tmp_path / "xlsx.headers"
    headers.write_text(
        "Content-Type: "
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n",
        encoding="ascii",
    )
    rendered = tmp_path / "renderer.xlsx"
    workbook = Workbook()
    workbook.active.title = "维保订单"
    workbook.active.append(["订单号", "合同号"])
    workbook.save(rendered)
    workbook.close()

    production_shape = subprocess.run(
        [
            "python3",
            str(ARTIFACT_VALIDATOR),
            "xlsx",
            str(rendered),
            str(headers),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert production_shape.returncode == 0, production_shape.stderr

    unsafe = tmp_path / "traversal.xlsx"
    with zipfile.ZipFile(io.BytesIO(_minimal_xlsx())) as source:
        with zipfile.ZipFile(unsafe, "w", zipfile.ZIP_DEFLATED) as target:
            for info in source.infolist():
                content = source.read(info)
                if info.filename == "xl/_rels/workbook.xml.rels":
                    content = content.replace(
                        b'Target="worksheets/sheet1.xml"',
                        b'Target="../../outside.xml"',
                    )
                target.writestr(info, content)
    traversal = subprocess.run(
        [
            "python3",
            str(ARTIFACT_VALIDATOR),
            "xlsx",
            str(unsafe),
            str(headers),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert traversal.returncode != 0


def test_final_observation_uses_real_api_and_authenticated_restricted_probe() -> None:
    edge = EDGE_RUNBOOK.read_text(encoding="utf-8")
    release = V120_RUNBOOK.read_text(encoding="utf-8")

    for runbook in (edge, release):
        assert "/api/v1/maintenance/board" not in runbook
    assert "https://hbzgc.icu/api/maintenance/board" in edge
    assert 'test "$anonymous_code" = 401' in edge

    # HEAD semantics must come from curl itself.  -X HEAD still follows the
    # generic request-body path and has caused misleading redirect evidence.
    assert "-X HEAD" not in edge
    assert '-I -o "$headers"' in edge

    # An invalid bearer is unauthenticated (401), never evidence of an
    # authenticated permission denial.  The technical identity is a signed,
    # DB-less fallback identity with an explicit readonly permission graph,
    # generated inside the production app container and kept in a root-only
    # temporary header file.
    assert "intentionally-invalid-technical-token" not in edge
    assert 'case "$technical_code" in 401|403)' not in edge
    assert 'test "$technical_code" = 403' in edge
    assert "technical-release-probe" in edge
    assert "fallback=True" in edge
    assert "runtime_safe" in edge
    assert "cd /home/ubuntu/apps/it-spareparts" not in edge
    assert "docker compose exec" not in edge
    assert "/var/lib/it-spareparts-release-control/v120-state.state" in edge
    assert "NEW_APP_CID" in edge
    assert 'docker exec "$app_cid"' in edge
    assert "mktemp -d /run/it-spareparts-release." in edge
    assert 'chmod 600 "$technical_header"' in edge
    assert "Authorization: Bearer" in edge
    assert "technical-token-403 status=%s" in edge
    assert "technical_header" not in re.search(
        r"technical-token-403 status=.*", edge
    ).group(0)

"""生产巡检部署工件契约与可执行行为。"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import stat
import subprocess
import textwrap
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
MONITOR_SCRIPT = REPO_ROOT / ".deploy" / "monitor.sh"
DEPLOY_DOC = REPO_ROOT / "docs" / "DEPLOY.md"
GITIGNORE = REPO_ROOT / ".gitignore"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _monitor_fixture(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    app_root = tmp_path / "app"
    deploy_dir = app_root / ".deploy"
    deploy_dir.mkdir(parents=True)
    script = deploy_dir / "monitor.sh"
    shutil.copy2(MONITOR_SCRIPT, script)

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    (backup_dir / "db-current.dump").write_bytes(b"fresh")

    calls = tmp_path / "calls.log"
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    _write_executable(
        stub_dir / "sudo",
        r"""
        #!/usr/bin/env bash
        set -u
        printf '%s\n' "$*" >> "$STUB_CALL_LOG"
        if [ -n "${STUB_SUDO_SLEEP_SECONDS:-}" ]; then
          sleep "$STUB_SUDO_SLEEP_SECONDS"
        fi
        if [[ "$*" == *"docker compose exec -T app python -"* ]] \
            && [ "${STUB_EXECUTE_APP_PROBE:-0}" = "1" ]; then
          endpoint="${@: -2:1}"
          base_url="${@: -1}"
          exec /usr/bin/python3 - "$endpoint" "$base_url"
        fi
        case "$*" in
          *"docker compose ps --format"*)
            printf '%s\n' "db Up 1 hour (healthy)" "app Up 1 hour" "frontend Up 1 hour"
            ;;
          *"docker compose exec -T db pg_isready"*)
            exit "${STUB_DB_READY_EXIT:-0}"
            ;;
          *"docker compose exec -T app python - /health/db"*)
            exit "${STUB_APP_DB_HEALTH_EXIT:-0}"
            ;;
          *"docker compose exec -T app python - /health"*)
            exit "${STUB_APP_HEALTH_EXIT:-0}"
            ;;
          *)
            exit 97
            ;;
        esac
        """,
    )
    _write_executable(
        stub_dir / "curl",
        r"""
        #!/usr/bin/env bash
        set -u
        if [[ "$*" == *"http://localhost:8080/"* ]]; then
          printf '%s\n' "curl frontend" >> "$STUB_CALL_LOG"
          printf '%s' "${STUB_FRONTEND_HTTP_CODE:-200}"
          exit "${STUB_FRONTEND_EXIT:-0}"
        fi
        printf '%s\n' "curl webhook" >> "$STUB_CALL_LOG"
        """,
    )
    _write_executable(
        stub_dir / "df",
        r"""
        #!/usr/bin/env bash
        printf '%s\n' \
          "Filesystem 1K-blocks Used Available Use% Mounted on" \
          "/dev/test 100000 20000 80000 ${STUB_DISK_USE:-20}% /"
        """,
    )

    env = os.environ.copy()
    env.update(
        {
            "MONITOR_COMMAND_DIR": str(stub_dir),
            "MONITOR_BACKUP_DIR": str(backup_dir),
            "STUB_CALL_LOG": str(calls),
            "TZ": "Asia/Shanghai",
        }
    )
    return script, env, calls


@contextmanager
def _health_server(payloads: dict[str, dict[str, str]]):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            payload = payloads.get(self.path, {"status": "missing"})
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_monitor_deployment_artifact_contract() -> None:
    """仓库中的脚本必须能由 cron 直接执行，且文档只引用真实路径。"""
    mode = MONITOR_SCRIPT.stat().st_mode
    assert mode & stat.S_IXUSR, ".deploy/monitor.sh 缺少 owner executable 位"

    subprocess.run(["bash", "-n", str(MONITOR_SCRIPT)], check=True)

    deploy_text = DEPLOY_DOC.read_text(encoding="utf-8")
    assert "cd ~/apps/it-spareparts || exit 1" in deploy_text
    assert "APP_DIR=$(pwd -P)" in deploy_text
    assert 'LEGACY_MONITOR="$APP_DIR/monitor.sh"' in deploy_text
    assert 'MONITOR_SCRIPT="$APP_DIR/.deploy/monitor.sh"' in deploy_text
    assert 'grep -Fv -e "$LEGACY_MONITOR" -e "$MONITOR_SCRIPT"' in deploy_text
    assert 'printf \'*/5 * * * * %s\\n\' "$MONITOR_SCRIPT"' in deploy_text
    assert "/home/ubuntu/apps/it-spareparts/monitor.sh" not in deploy_text
    assert "umask 077" in deploy_text
    assert 'chmod 600 "$APP_DIR/.alert_webhook"' in deploy_text
    assert "连续两个 cron 周期" in deploy_text
    assert "ok=Y" in deploy_text
    assert "monitor.log" in deploy_text
    assert "journalctl" in deploy_text

    assert ".alert_webhook" in GITIGNORE.read_text(encoding="utf-8").splitlines()

    script_text = MONITOR_SCRIPT.read_text(encoding="utf-8")
    assert script_text.count("sudo ") == script_text.count("sudo -n ")
    assert "timeout --kill-after=" in script_text
    assert "flock -n" in script_text


def test_monitor_crontab_rewrite_is_scoped_to_this_application(tmp_path: Path) -> None:
    app_dir = tmp_path / "apps" / "it-spareparts"
    legacy = app_dir / "monitor.sh"
    current = app_dir / ".deploy" / "monitor.sh"
    existing = "\n".join(
        [
            "0 3 * * * /srv/backup.sh",
            f"*/5 * * * * {legacy}",
            f"*/10 * * * * {current}",
            "*/5 * * * * /srv/another-product/monitor.sh",
        ]
    )
    command = r"""
    LEGACY_MONITOR="$APP_DIR/monitor.sh"
    MONITOR_SCRIPT="$APP_DIR/.deploy/monitor.sh"
    {
      grep -Fv -e "$LEGACY_MONITOR" -e "$MONITOR_SCRIPT"
      printf '*/5 * * * * %s\n' "$MONITOR_SCRIPT"
    }
    """

    result = subprocess.run(
        ["bash", "-c", textwrap.dedent(command)],
        check=True,
        capture_output=True,
        env={**os.environ, "APP_DIR": str(app_dir)},
        input=existing,
        text=True,
    )

    lines = result.stdout.splitlines()
    assert "0 3 * * * /srv/backup.sh" in lines
    assert "*/5 * * * * /srv/another-product/monitor.sh" in lines
    assert not any(str(legacy) in line for line in lines)
    assert lines.count(f"*/5 * * * * {current}") == 1


def test_monitor_healthy_path_checks_internal_app_and_writes_iso_heartbeat(
    tmp_path: Path,
) -> None:
    script, env, calls = _monitor_fixture(tmp_path)
    status_path = script.parents[1] / "monitor.status"
    status_path.write_text("stale partial-looking status", encoding="utf-8")
    stale_inode = status_path.stat().st_ino

    result = subprocess.run(
        [str(script)],
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""
    status = status_path.read_text(encoding="utf-8")
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2} ok=Y\n",
        status,
    )
    assert status_path.stat().st_ino != stale_inode, "monitor.status 必须原子替换"
    command_log = calls.read_text(encoding="utf-8")
    assert (
        "-n docker compose exec -T app python - /health http://127.0.0.1:8000\n"
        in command_log
    )
    assert (
        "-n docker compose exec -T app python - /health/db http://127.0.0.1:8000\n"
        in command_log
    )
    assert not (script.parents[1] / "monitor.log").exists()
    assert not list(script.parents[1].glob(".monitor.status.tmp.*"))


def test_monitor_unhealthy_db_probe_logs_safely_and_sets_failure_heartbeat(
    tmp_path: Path,
) -> None:
    script, env, calls = _monitor_fixture(tmp_path)
    secret = "TOP_SECRET_MONITOR_WEBHOOK"
    (script.parents[1] / ".alert_webhook").write_text(
        f"https://example.invalid/robot?access_token={secret}\n",
        encoding="utf-8",
    )
    (script.parents[1] / ".alert_webhook").chmod(0o600)
    env["STUB_APP_DB_HEALTH_EXIT"] = "1"

    result = subprocess.run(
        [str(script)],
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )

    assert result.returncode == 1
    status = (script.parents[1] / "monitor.status").read_text(encoding="utf-8")
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2} ok=N\(1\)\n",
        status,
    )
    monitor_log = (script.parents[1] / "monitor.log").read_text(encoding="utf-8")
    assert "应用数据库探针异常（容器内 /health/db）" in monitor_log
    assert "curl webhook\n" in calls.read_text(encoding="utf-8")
    combined_output = result.stdout + result.stderr + status + monitor_log
    assert secret not in combined_output
    assert not list(script.parents[1].glob(".monitor.status.tmp.*"))


def test_monitor_refuses_insecure_webhook_without_reading_secret(
    tmp_path: Path,
) -> None:
    script, env, calls = _monitor_fixture(tmp_path)
    secret = "INSECURE_TOP_SECRET_WEBHOOK"
    webhook = script.parents[1] / ".alert_webhook"
    webhook.write_text(
        f"https://example.invalid/robot?access_token={secret}\n",
        encoding="utf-8",
    )
    webhook.chmod(0o644)

    result = subprocess.run(
        [str(script)],
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )

    assert result.returncode == 1
    status = (script.parents[1] / "monitor.status").read_text(encoding="utf-8")
    assert "ok=N(1)" in status
    monitor_log = (script.parents[1] / "monitor.log").read_text(encoding="utf-8")
    assert "webhook 权限不安全" in monitor_log
    assert "curl webhook" not in calls.read_text(encoding="utf-8")
    assert secret not in result.stdout + result.stderr + status + monitor_log


def test_monitor_lock_conflict_writes_failure_without_running_probes(
    tmp_path: Path,
) -> None:
    script, env, calls = _monitor_fixture(tmp_path)
    lock_path = script.parents[1] / ".monitor.lock"
    with lock_path.open("w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run(
            [str(script)],
            check=False,
            capture_output=True,
            env=env,
            text=True,
        )

    assert result.returncode == 1
    status = (script.parents[1] / "monitor.status").read_text(encoding="utf-8")
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2} ok=N\(1\)\n",
        status,
    )
    monitor_log = (script.parents[1] / "monitor.log").read_text(encoding="utf-8")
    assert "上一次巡检仍在运行，本轮已拒绝重叠执行" in monitor_log
    assert not calls.exists(), "锁冲突后不得继续运行 Docker/HTTP 探针"


def test_monitor_bounds_hung_docker_commands(tmp_path: Path) -> None:
    script, env, _calls = _monitor_fixture(tmp_path)
    env.update(
        {
            "MONITOR_DOCKER_TIMEOUT": "0.05s",
            "MONITOR_DOCKER_KILL_AFTER": "0.05s",
            "STUB_SUDO_SLEEP_SECONDS": "5",
        }
    )

    started = time.monotonic()
    result = subprocess.run(
        [str(script)],
        check=False,
        capture_output=True,
        env=env,
        text=True,
        timeout=3,
    )
    elapsed = time.monotonic() - started

    assert result.returncode == 1
    assert elapsed < 2
    status = (script.parents[1] / "monitor.status").read_text(encoding="utf-8")
    assert "ok=N(6)" in status


@pytest.mark.parametrize(
    ("payloads", "expected_problem"),
    [
        (
            {
                "/health": {"status": "degraded"},
                "/health/db": {"status": "ok", "db": "reachable"},
            },
            "应用存活探针异常（容器内 /health）",
        ),
        (
            {
                "/health": {"status": "ok"},
                "/health/db": {"status": "ok", "db": "unreachable"},
            },
            "应用数据库探针异常（容器内 /health/db）",
        ),
    ],
)
def test_monitor_rejects_unhealthy_json_payloads(
    tmp_path: Path,
    payloads: dict[str, dict[str, str]],
    expected_problem: str,
) -> None:
    script, env, _calls = _monitor_fixture(tmp_path)
    env["STUB_EXECUTE_APP_PROBE"] = "1"
    with _health_server(payloads) as base_url:
        env["MONITOR_APP_HEALTH_BASE_URL"] = base_url
        result = subprocess.run(
            [str(script)],
            check=False,
            capture_output=True,
            env=env,
            text=True,
        )

    assert result.returncode == 1
    status = (script.parents[1] / "monitor.status").read_text(encoding="utf-8")
    assert "ok=N(1)" in status
    monitor_log = (script.parents[1] / "monitor.log").read_text(encoding="utf-8")
    assert expected_problem in monitor_log

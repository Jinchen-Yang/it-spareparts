"""生产巡检部署工件契约与可执行行为。"""

from __future__ import annotations

import fcntl
import hashlib
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
    edge_url = app_root / ".https_monitor_url"
    edge_url.write_text("https://itdata.example.test/\n", encoding="utf-8")
    edge_url.chmod(0o600)

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    backup = backup_dir / "db-current.dump"
    backup.write_bytes(b"fresh")
    digest = hashlib.sha256(backup.read_bytes()).hexdigest()
    (backup_dir / "db-current.dump.sha256").write_text(
        f"{digest}  {backup}\n",
        encoding="utf-8",
    )

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
        if [[ "$*" == *"http://127.0.0.1:8080/"* ]]; then
          printf '%s\n' "curl frontend" >> "$STUB_CALL_LOG"
          printf '%s' "${STUB_FRONTEND_HTTP_CODE:-200}"
          exit "${STUB_FRONTEND_EXIT:-0}"
        fi
        if [[ "$*" == *" -d "* ]]; then
          printf '%s\n' "curl webhook" >> "$STUB_CALL_LOG"
          exit 0
        fi
        target="${@: -1}"
        if [[ "$target" == https://* ]]; then
          printf '%s\n' "curl https edge $target" >> "$STUB_CALL_LOG"
          printf '%s' "${STUB_HTTPS_HTTP_CODE:-200}"
          exit "${STUB_HTTPS_EXIT:-0}"
        fi
        if [[ "$target" == http://* ]]; then
          printf '%s\n' "curl http redirect $target" >> "$STUB_CALL_LOG"
          printf '%s %s' \
            "${STUB_REDIRECT_HTTP_CODE:-308}" \
            "${STUB_REDIRECT_URL:-https://itdata.example.test/}"
          exit "${STUB_REDIRECT_EXIT:-0}"
        fi
        printf '%s\n' "curl webhook" >> "$STUB_CALL_LOG"
        """,
    )
    _write_executable(
        stub_dir / "openssl",
        r"""
        #!/usr/bin/env bash
        set -u
        case "${1:-}" in
          s_client)
            host=""
            while [ "$#" -gt 0 ]; do
              if [ "$1" = "-servername" ]; then
                host=$2
                break
              fi
              shift
            done
            printf '%s\n' "openssl s_client $host" >> "$STUB_CALL_LOG"
            printf '%s\n' "stub certificate"
            exit "${STUB_TLS_CONNECT_EXIT:-0}"
            ;;
          x509)
            cat >/dev/null
            printf '%s\n' "openssl x509 checkend ${3:-missing}" >> "$STUB_CALL_LOG"
            exit "${STUB_CERT_CHECK_EXIT:-0}"
            ;;
          *)
            exit 97
            ;;
        esac
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
    assert 'chmod 600 "$APP_DIR/.https_monitor_url"' in deploy_text
    assert "证书 7 天续期余量" in deploy_text
    assert "连续两个 cron 周期" in deploy_text
    assert "ok=Y" in deploy_text
    assert "monitor.log" in deploy_text
    assert "journalctl" in deploy_text

    assert ".alert_webhook" in GITIGNORE.read_text(encoding="utf-8").splitlines()
    assert ".https_monitor_url" in GITIGNORE.read_text(
        encoding="utf-8"
    ).splitlines()

    script_text = MONITOR_SCRIPT.read_text(encoding="utf-8")
    assert script_text.count("sudo ") == script_text.count("sudo -n ")
    assert "timeout --kill-after=" in script_text
    assert "flock -n" in script_text


def test_monitor_crontab_rewrite_is_scoped_to_this_application(tmp_path: Path) -> None:
    app_dir = tmp_path / "apps" / "it-spareparts"
    (app_dir / ".deploy").mkdir(parents=True)
    legacy = app_dir / "monitor.sh"
    current = app_dir / ".deploy" / "monitor.sh"
    _write_executable(current, "#!/bin/sh\n")
    cron_state = tmp_path / "crontab.txt"
    cron_state.write_text(
        "\n".join(
            [
                "0 3 * * * /srv/backup.sh",
                f"*/5 * * * * {legacy}",
                f"*/10 * * * * {current}",
                "*/5 * * * * /srv/another-product/monitor.sh",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    _write_executable(
        stub_dir / "crontab",
        r"""
        #!/usr/bin/env bash
        case "${1:-}" in
          -l)
            if [ "${STUB_CRONTAB_LIST_STATUS:-0}" -ne 0 ]; then
              printf 'simulated monitor crontab read failure\n' >&2
              exit "$STUB_CRONTAB_LIST_STATUS"
            fi
            if [ ! -f "$STUB_CRONTAB_STATE" ]; then
              printf 'no crontab for %s\n' "$(/usr/bin/id -un)" >&2
              exit 1
            fi
            cat "$STUB_CRONTAB_STATE"
            ;;
          -*)
            exit 64
            ;;
          *)
            [ "$#" -eq 1 ] && [ -f "$1" ] || exit 64
            tmp=$(/usr/bin/mktemp)
            cat "$1" > "$tmp"
            /usr/bin/mv "$tmp" "$STUB_CRONTAB_STATE"
            ;;
        esac
        """,
    )
    deploy_text = DEPLOY_DOC.read_text(encoding="utf-8")
    begin = "# MONITOR_CRON_INSTALL_BEGIN"
    end = "# MONITOR_CRON_INSTALL_END"
    assert begin in deploy_text and end in deploy_text
    snippet = deploy_text.split(begin, 1)[1].split(end, 1)[0]
    assert snippet.index("trap cleanup_monitor_cron_install EXIT") < snippet.index(
        "MONITOR_CRON_CURRENT=$(mktemp)"
    )
    env = {
        **os.environ,
        "APP_DIR": str(app_dir),
        "PATH": f"{stub_dir}:{os.environ['PATH']}",
        "STUB_CRONTAB_STATE": str(cron_state),
    }

    for _ in range(2):
        result = subprocess.run(
            ["bash", "-eu", "-o", "pipefail", "-c", snippet],
            check=False,
            capture_output=True,
            env=env,
            text=True,
        )
        assert result.returncode == 0, result.stderr or result.stdout

    lines = cron_state.read_text(encoding="utf-8").splitlines()
    assert "0 3 * * * /srv/backup.sh" in lines
    assert "*/5 * * * * /srv/another-product/monitor.sh" in lines
    assert not any(str(legacy) in line for line in lines)
    assert lines.count(f"*/5 * * * * {current}") == 1

    before_failure = cron_state.read_bytes()
    failed = subprocess.run(
        ["bash", "-eu", "-o", "pipefail", "-c", snippet],
        check=False,
        capture_output=True,
        env={**env, "STUB_CRONTAB_LIST_STATUS": "74"},
        text=True,
    )
    assert failed.returncode != 0
    assert cron_state.read_bytes() == before_failure

    _write_executable(
        stub_dir / "grep",
        "#!/usr/bin/env bash\nexit 2\n",
    )
    filter_failed = subprocess.run(
        ["bash", "-eu", "-o", "pipefail", "-c", snippet],
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )
    assert filter_failed.returncode != 0
    assert cron_state.read_bytes() == before_failure


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


def test_monitor_rejects_latest_backup_without_checksum(tmp_path: Path) -> None:
    """新鲜 dump 没有匹配 checksum 时不能被巡检当成健康恢复点。"""
    script, env, _calls = _monitor_fixture(tmp_path)
    backup_dir = Path(env["MONITOR_BACKUP_DIR"])
    (backup_dir / "db-current.dump.sha256").unlink()

    result = subprocess.run(
        [str(script)],
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )

    assert result.returncode == 1
    monitor_log = (script.parents[1] / "monitor.log").read_text(encoding="utf-8")
    assert "最新备份缺少有效 checksum" in monitor_log


def test_monitor_rejects_latest_backup_with_invalid_checksum(tmp_path: Path) -> None:
    """dump 在 checksum 生成后被损坏时，巡检必须告警。"""
    script, env, _calls = _monitor_fixture(tmp_path)
    backup_dir = Path(env["MONITOR_BACKUP_DIR"])
    (backup_dir / "db-current.dump").write_bytes(b"corrupted-after-publish")

    result = subprocess.run(
        [str(script)],
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )

    assert result.returncode == 1
    monitor_log = (script.parents[1] / "monitor.log").read_text(encoding="utf-8")
    assert "最新备份 checksum 校验失败" in monitor_log


def test_monitor_verifies_checksum_against_the_selected_latest_dump(
    tmp_path: Path,
) -> None:
    """checksum 内部即使指向另一个有效文件，也不能替代对 latest dump 的校验。"""
    script, env, _calls = _monitor_fixture(tmp_path)
    backup_dir = Path(env["MONITOR_BACKUP_DIR"])
    wrong_target = backup_dir / "different-file.bin"
    wrong_target.write_bytes(b"valid-but-not-the-selected-backup")
    wrong_digest = hashlib.sha256(wrong_target.read_bytes()).hexdigest()
    (backup_dir / "db-current.dump.sha256").write_text(
        f"{wrong_digest}  {wrong_target}\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [str(script)],
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )

    assert result.returncode == 1
    monitor_log = (script.parents[1] / "monitor.log").read_text(encoding="utf-8")
    assert "最新备份 checksum 校验失败" in monitor_log


def test_monitor_probes_https_edge_redirect_and_certificate_expiry(
    tmp_path: Path,
) -> None:
    script, env, calls = _monitor_fixture(tmp_path)

    result = subprocess.run(
        [str(script)],
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    command_log = calls.read_text(encoding="utf-8")
    assert "curl https edge https://itdata.example.test/\n" in command_log
    assert "curl http redirect http://itdata.example.test/\n" in command_log
    assert "openssl s_client itdata.example.test\n" in command_log
    assert "openssl x509 checkend 604800\n" in command_log


@pytest.mark.parametrize(
    ("env_override", "expected_problem"),
    [
        ({"STUB_HTTPS_HTTP_CODE": "502"}, "HTTPS 正式入口异常：HTTP 502"),
        (
            {
                "STUB_REDIRECT_HTTP_CODE": "200",
                "STUB_REDIRECT_URL": "-",
            },
            "HTTP 到 HTTPS 跳转异常：HTTP 200",
        ),
        (
            {
                "STUB_REDIRECT_HTTP_CODE": "308",
                "STUB_REDIRECT_URL": "https://attacker.example/",
            },
            "HTTP 跳转目标异常（必须回到同域 HTTPS 根路径）",
        ),
        (
            {"STUB_CERT_CHECK_EXIT": "1"},
            "HTTPS 证书将在 7 天内到期或证书链读取失败",
        ),
    ],
)
def test_monitor_https_edge_failures_raise_alert(
    tmp_path: Path,
    env_override: dict[str, str],
    expected_problem: str,
) -> None:
    script, env, _calls = _monitor_fixture(tmp_path)
    env.update(env_override)

    result = subprocess.run(
        [str(script)],
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )

    assert result.returncode == 1
    status = (script.parents[1] / "monitor.status").read_text(encoding="utf-8")
    assert "ok=N(" in status
    monitor_log = (script.parents[1] / "monitor.log").read_text(encoding="utf-8")
    assert expected_problem in monitor_log


@pytest.mark.parametrize(
    ("url", "mode", "expected_problem"),
    [
        (
            "https://itdata.example.test/",
            0o644,
            "HTTPS 监控配置权限不安全",
        ),
        (
            "https://itdata.example.test/private",
            0o600,
            "HTTPS 监控地址非法",
        ),
        (
            "https://118.25.94.90/",
            0o600,
            "HTTPS 监控地址非法",
        ),
    ],
)
def test_monitor_rejects_unsafe_https_probe_configuration(
    tmp_path: Path,
    url: str,
    mode: int,
    expected_problem: str,
) -> None:
    script, env, calls = _monitor_fixture(tmp_path)
    edge_url = script.parents[1] / ".https_monitor_url"
    edge_url.write_text(f"{url}\n", encoding="utf-8")
    edge_url.chmod(mode)

    result = subprocess.run(
        [str(script)],
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )

    assert result.returncode == 1
    monitor_log = (script.parents[1] / "monitor.log").read_text(encoding="utf-8")
    assert expected_problem in monitor_log
    command_log = calls.read_text(encoding="utf-8")
    assert "curl https edge" not in command_log
    assert "openssl s_client" not in command_log


def test_monitor_fails_closed_when_https_probe_configuration_is_missing(
    tmp_path: Path,
) -> None:
    script, env, calls = _monitor_fixture(tmp_path)
    (script.parents[1] / ".https_monitor_url").unlink()

    result = subprocess.run(
        [str(script)],
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )

    assert result.returncode == 1
    monitor_log = (script.parents[1] / "monitor.log").read_text(encoding="utf-8")
    assert "HTTPS 监控配置缺失" in monitor_log
    command_log = calls.read_text(encoding="utf-8")
    assert "curl https edge" not in command_log
    assert "openssl s_client" not in command_log


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

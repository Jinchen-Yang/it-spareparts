"""HTTPS 边缘配置的一键安全回滚。"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
ROLLBACK_SCRIPT = REPO_ROOT / ".deploy" / "rollback_https_ingress.sh"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rollback_fixture(
    tmp_path: Path,
) -> tuple[Path, Path, dict[str, str], Path, dict[str, str]]:
    assistant_dir = tmp_path / "assistant"
    assistant_dir.mkdir()
    assistant_dir.chmod(0o750)
    (assistant_dir / "Caddyfile").write_text("new caddy config\n", encoding="utf-8")
    (assistant_dir / "compose.production.yml").write_text(
        "new compose config\n", encoding="utf-8"
    )

    app_dir = tmp_path / "it-spareparts"
    app_dir.mkdir()
    shutil.copy2(REPO_ROOT / "docker-compose.yml", app_dir / "docker-compose.yml")
    (assistant_dir / ".env").write_text(
        "ASSISTANT_HOST=assistant.example.test\n", encoding="utf-8"
    )
    (assistant_dir / ".env").chmod(0o600)

    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir(mode=0o700)
    backups = {
        "Caddyfile.before": "old caddy config\n",
        "assistant-compose.before.yml": "old compose config\n",
        "it-compose.before.yml": "old public compose retained as evidence only\n",
    }
    for name, content in backups.items():
        path = evidence_dir / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o600)
    checksum_lines = [
        f"{_sha256(evidence_dir / name)}  {evidence_dir / name}"
        for name in backups
    ]
    (evidence_dir / "SHA256SUMS").write_text(
        "\n".join(checksum_lines) + "\n", encoding="utf-8"
    )
    (evidence_dir / "SHA256SUMS").chmod(0o600)

    calls = tmp_path / "calls.log"
    lock_dir = tmp_path / "rollback-lock"
    lock_dir.mkdir(mode=0o700)
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    _write_executable(
        stub_dir / "docker",
        r"""
        #!/usr/bin/env bash
        set -u
        printf '%s\n' "$*" >> "$STUB_CALL_LOG"
        if [[ "$*" == *"--project-directory $STUB_APP_DIR"* ]] \
            && [[ "$*" == *"config --format json"* ]]; then
          printf \
            '{"services":{"frontend":{"ports":[{"mode":"ingress","target":80,"published":"8080","protocol":"tcp","host_ip":"%s"}],"networks":{"default":{},"ingress":{"aliases":["it-spareparts-frontend"]}}},"app":{"networks":{"default":{}}},"db":{"networks":{"default":{}}}},"networks":{"ingress":{"name":"it-spareparts-ingress","internal":true}}}\n' \
            "${STUB_APP_HOST_IP:-127.0.0.1}"
          exit 0
        fi
        if [[ "$*" == *"up -d --no-deps --force-recreate caddy"* ]]; then
          exit "${STUB_CADDY_UP_EXIT:-0}"
        fi
        case "$*" in
          *"inspect -f {{.State.Running}}"*)
            printf '%s\n' true
            ;;
          *"inspect --format {{json .NetworkSettings.Networks}}"*)
            printf '%s\n' '{"personal-ai-assistant-network":{},"it-spareparts-ingress":{}}'
            ;;
          *)
            ;;
        esac
        """,
    )
    _write_executable(
        stub_dir / "ss",
        r"""
        #!/usr/bin/env bash
        exit_code="${STUB_SS_EXIT:-0}"
        [ "$exit_code" -eq 0 ] || exit "$exit_code"
        [ "${STUB_SS_EMPTY:-0}" = "1" ] && exit 0
        printf '%s\n' \
          "${STUB_SS_OUTPUT:-LISTEN 0 4096 127.0.0.1:8080 0.0.0.0:*}"
        """,
    )
    _write_executable(
        stub_dir / "curl",
        r"""
        #!/usr/bin/env bash
        target="${@: -1}"
        printf 'curl %s\n' "$target" >> "$STUB_CALL_LOG"
        case "$target" in
          http://127.0.0.1:8080/)
            exit "${STUB_LOOPBACK_CURL_EXIT:-0}"
            ;;
          https://assistant.example.test/health)
            exit "${STUB_ASSISTANT_CURL_EXIT:-0}"
            ;;
          *)
            exit 97
            ;;
        esac
        """,
    )

    env = {
        **os.environ,
        "HTTPS_ROLLBACK_TEST_MODE": "1",
        "HTTPS_ROLLBACK_ASSISTANT_DIR": str(assistant_dir),
        "HTTPS_ROLLBACK_APP_DIR": str(app_dir),
        "HTTPS_ROLLBACK_COMMAND_DIR": str(stub_dir),
        "HTTPS_ROLLBACK_LOCK_DIR": str(lock_dir),
        "STUB_CALL_LOG": str(calls),
        "STUB_APP_DIR": str(app_dir),
    }
    return assistant_dir, evidence_dir, backups, calls, env


def _run_rollback(evidence_dir: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            str(ROLLBACK_SCRIPT),
            str(evidence_dir),
            "https://assistant.example.test/health",
        ],
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )


def test_https_rollback_restores_edge_without_reopening_public_8080(
    tmp_path: Path,
) -> None:
    assistant_dir, evidence_dir, backups, calls, env = _rollback_fixture(tmp_path)
    result = _run_rollback(evidence_dir, env)

    assert result.returncode == 0, result.stderr
    assert (assistant_dir / "Caddyfile").read_text(encoding="utf-8") == backups[
        "Caddyfile.before"
    ]
    assert (
        assistant_dir / "compose.production.yml"
    ).read_text(encoding="utf-8") == backups["assistant-compose.before.yml"]
    assert list(evidence_dir.glob("Caddyfile.failed-*"))
    assert list(evidence_dir.glob("assistant-compose.failed-*.yml"))

    command_log = calls.read_text(encoding="utf-8")
    assert "compose --env-file .env -f compose.production.yml config --quiet" in command_log
    assert (
        "compose --env-file .env -f compose.production.yml "
        "up -d --no-deps --force-recreate caddy"
    ) in command_log
    assert "exec personal-ai-assistant-caddy caddy validate" in command_log
    assert "network disconnect it-spareparts-ingress personal-ai-assistant-caddy" in command_log
    assert "rollback complete; public 8080 remains closed" in result.stdout


def test_https_rollback_rejects_checksum_mismatch_before_mutation(
    tmp_path: Path,
) -> None:
    assistant_dir, evidence_dir, _backups, calls, env = _rollback_fixture(tmp_path)
    (evidence_dir / "Caddyfile.before").write_text(
        "tampered backup\n", encoding="utf-8"
    )

    result = _run_rollback(evidence_dir, env)

    assert result.returncode != 0
    assert "checksum mismatch: Caddyfile.before" in result.stderr
    assert (assistant_dir / "Caddyfile").read_text(encoding="utf-8") == (
        "new caddy config\n"
    )
    assert not calls.exists()


def test_https_rollback_rejects_world_writable_lock_directory_before_mutation(
    tmp_path: Path,
) -> None:
    assistant_dir, evidence_dir, _backups, calls, env = _rollback_fixture(tmp_path)
    Path(env["HTTPS_ROLLBACK_LOCK_DIR"]).chmod(0o777)

    result = _run_rollback(evidence_dir, env)

    assert result.returncode != 0
    assert "lock directory ownership or mode is unsafe" in result.stderr
    assert (assistant_dir / "Caddyfile").read_text(encoding="utf-8") == (
        "new caddy config\n"
    )
    assert not calls.exists()


def test_https_rollback_rejects_symlink_lock_directory_before_mutation(
    tmp_path: Path,
) -> None:
    assistant_dir, evidence_dir, _backups, calls, env = _rollback_fixture(tmp_path)
    lock_dir = Path(env["HTTPS_ROLLBACK_LOCK_DIR"])
    lock_target = tmp_path / "attacker-controlled-lock-target"
    lock_target.mkdir(mode=0o700)
    lock_dir.rmdir()
    lock_dir.symlink_to(lock_target, target_is_directory=True)

    result = _run_rollback(evidence_dir, env)

    assert result.returncode != 0
    assert "rollback lock directory is missing or unsafe" in result.stderr
    assert (assistant_dir / "Caddyfile").read_text(encoding="utf-8") == (
        "new caddy config\n"
    )
    assert not calls.exists()


def test_https_rollback_recreates_missing_runtime_lock_directory(
    tmp_path: Path,
) -> None:
    _assistant_dir, evidence_dir, _backups, _calls, env = _rollback_fixture(tmp_path)
    lock_dir = Path(env["HTTPS_ROLLBACK_LOCK_DIR"])
    lock_dir.rmdir()

    result = _run_rollback(evidence_dir, env)

    assert result.returncode == 0, result.stderr
    assert lock_dir.is_dir()
    assert stat.S_IMODE(lock_dir.stat().st_mode) == 0o700


def test_https_rollback_requires_the_known_assistant_health_path(
    tmp_path: Path,
) -> None:
    assistant_dir, evidence_dir, _backups, calls, env = _rollback_fixture(tmp_path)

    result = subprocess.run(
        [str(ROLLBACK_SCRIPT), str(evidence_dir), "https://assistant.example.test/"],
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )

    assert result.returncode != 0
    assert "must be an HTTPS /health URL" in result.stderr
    assert (assistant_dir / "Caddyfile").read_text(encoding="utf-8") == (
        "new caddy config\n"
    )
    assert not calls.exists()


def test_https_rollback_rejects_persistent_public_port_before_mutation(
    tmp_path: Path,
) -> None:
    assistant_dir, evidence_dir, _backups, _calls, env = _rollback_fixture(tmp_path)
    env["STUB_APP_HOST_IP"] = "0.0.0.0"

    result = _run_rollback(evidence_dir, env)

    assert result.returncode != 0
    assert "persistent IT compose could reopen plaintext access" in result.stderr
    assert (assistant_dir / "Caddyfile").read_text(encoding="utf-8") == (
        "new caddy config\n"
    )


def test_https_rollback_transaction_restores_pre_rollback_pair_on_failure(
    tmp_path: Path,
) -> None:
    assistant_dir, evidence_dir, _backups, _calls, env = _rollback_fixture(tmp_path)
    env["STUB_CADDY_UP_EXIT"] = "1"

    result = _run_rollback(evidence_dir, env)

    assert result.returncode != 0
    assert "failed to recreate the restored Caddy service" in result.stderr
    assert "restoring the pre-rollback edge snapshot" in result.stderr
    assert (assistant_dir / "Caddyfile").read_text(encoding="utf-8") == (
        "new caddy config\n"
    )
    assert (assistant_dir / "compose.production.yml").read_text(
        encoding="utf-8"
    ) == "new compose config\n"


def test_https_rollback_fails_closed_when_listener_check_fails(
    tmp_path: Path,
) -> None:
    assistant_dir, evidence_dir, _backups, _calls, env = _rollback_fixture(tmp_path)
    env["STUB_SS_EXIT"] = "2"

    result = _run_rollback(evidence_dir, env)

    assert result.returncode != 0
    assert "cannot verify port 8080 listeners" in result.stderr
    assert (assistant_dir / "Caddyfile").read_text(encoding="utf-8") == (
        "new caddy config\n"
    )


def test_https_rollback_rejects_missing_loopback_listener(
    tmp_path: Path,
) -> None:
    assistant_dir, evidence_dir, _backups, _calls, env = _rollback_fixture(tmp_path)
    env["STUB_SS_EMPTY"] = "1"

    result = _run_rollback(evidence_dir, env)

    assert result.returncode != 0
    assert "expected loopback port 8080 listener is missing" in result.stderr
    assert (assistant_dir / "Caddyfile").read_text(encoding="utf-8") == (
        "new caddy config\n"
    )


def test_https_rollback_rejects_unusable_loopback_frontend(
    tmp_path: Path,
) -> None:
    assistant_dir, evidence_dir, _backups, _calls, env = _rollback_fixture(tmp_path)
    env["STUB_LOOPBACK_CURL_EXIT"] = "22"

    result = _run_rollback(evidence_dir, env)

    assert result.returncode != 0
    assert "loopback IT frontend is not usable" in result.stderr
    assert (assistant_dir / "Caddyfile").read_text(encoding="utf-8") == (
        "new caddy config\n"
    )


def test_https_rollback_restores_pair_when_assistant_https_smoke_fails(
    tmp_path: Path,
) -> None:
    assistant_dir, evidence_dir, _backups, _calls, env = _rollback_fixture(tmp_path)
    env["STUB_ASSISTANT_CURL_EXIT"] = "22"

    result = _run_rollback(evidence_dir, env)

    assert result.returncode != 0
    assert "original personal assistant HTTPS route is not usable" in result.stderr
    assert "restoring the pre-rollback edge snapshot" in result.stderr
    assert (assistant_dir / "Caddyfile").read_text(encoding="utf-8") == (
        "new caddy config\n"
    )
    assert (assistant_dir / "compose.production.yml").read_text(
        encoding="utf-8"
    ) == "new compose config\n"


def test_https_rollback_rejects_public_ipv4_and_ipv6_listener(
    tmp_path: Path,
) -> None:
    assistant_dir, evidence_dir, _backups, _calls, env = _rollback_fixture(tmp_path)
    env["STUB_SS_OUTPUT"] = (
        "LISTEN 0 4096 0.0.0.0:8080 0.0.0.0:*\n"
        "LISTEN 0 4096 [::]:8080 [::]:*"
    )

    result = _run_rollback(evidence_dir, env)

    assert result.returncode != 0
    assert "unsafe port 8080 listener remains: 0.0.0.0:8080" in result.stderr
    assert (assistant_dir / "Caddyfile").read_text(encoding="utf-8") == (
        "new caddy config\n"
    )


def test_https_rollback_production_entrypoint_cannot_inherit_test_bash() -> None:
    script = ROLLBACK_SCRIPT.read_text(encoding="utf-8")
    assert script.startswith("#!/bin/bash\n")
    assert "/usr/local/sbin/it-spareparts-https-rollback" in script
    root_rejection = '[ "${EUID:-$(id -u)}" -ne 0 ]'
    command_injection = 'export PATH="$COMMAND_DIR:$BASE_PATH"'
    assert script.index(root_rejection) < script.index(command_injection)
    assert 'exec 9<"$LOCK_DIR"' in script
    assert 'exec 9>"$LOCK_FILE"' not in script

from __future__ import annotations

import grp
import hashlib
import os
import pwd
import re
import shutil
import signal
import stat
import subprocess
import time
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
DEFAULT_TARGET = "a" * 40
DEFAULT_RELEASE_ID = "v120-aaaaaaaaaaaa-20260730160000"
ZERO_HASH = "0" * 64


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
        "OLD_RUNNING_SOURCE_COMMIT": (
            "a1cf00910f08da7f27a9e6e0faaacc3a3cce9bab"
        ),
        "DB_HEAD": "f1c8e4a7b2d9",
        "OLD_APP_IMAGE_ID": "sha256:" + "b" * 64,
        "OLD_FRONTEND_IMAGE_ID": "sha256:" + "c" * 64,
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


def test_build_uses_single_link_no_clobber_state_publish() -> None:
    build = _script(BUILD)

    assert 'v120_state_publish_new "$STATE_TEMP" "$STATE"' in build
    assert 'ln -- "$STATE_TEMP" "$STATE"' not in build


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
        ".deploy/it-spareparts.cron",
    )
    for relative in source_paths:
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
        "CONTROL_FORMAT=v120-control-2",
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
        ".deploy/it-spareparts.cron",
    )
    for relative in source_paths:
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


def test_bootstrap_authorization_is_explicit_and_never_self_minted(
    tmp_path: Path,
) -> None:
    authorization = tmp_path / "bootstrap.authorization"
    manifest = tmp_path / "manifest.txt"
    target = "4" * 40
    manifest.write_text(
        "\n".join(
            (
                "CONTROL_FORMAT=v120-control-2",
                f"TARGET_COMMIT={target}",
                "V120_STATE_SHA256=" + "5" * 64,
                "ROOT_SYNC_SHA256=" + "6" * 64,
                "ROLLBACK_SHA256=" + "7" * 64,
                "INSTALLER_SHA256=" + "8" * 64,
                "CRON_SHA256=" + "9" * 64,
                "SOURCE_TAR_SHA256=" + "a" * 64,
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
        ("it-spareparts.cron", "CRON_SHA256", b"SHELL=/bin/sh\n"),
        ("source.tar", "SOURCE_TAR_SHA256", b"trusted-source\n"),
    )
    manifest_lines = [
        "CONTROL_FORMAT=v120-control-2",
        "TARGET_COMMIT=" + "4" * 40,
    ]
    for name, key, content in names_and_keys:
        artifact = package / name
        artifact.write_bytes(content)
        artifact.chmod(0o700 if name.endswith(".sh") else 0o600)
        manifest_lines.append(f"{key}={hashlib.sha256(content).hexdigest()}")
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

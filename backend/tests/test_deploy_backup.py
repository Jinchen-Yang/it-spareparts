"""数据库备份部署脚本的权限安全契约。"""

from __future__ import annotations

import os
import stat
import subprocess
import textwrap
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
BACKUP_SCRIPT = REPO_ROOT / ".deploy" / "backup.sh"
RESTORE_DRILL_SCRIPT = REPO_ROOT / ".deploy" / "restore_drill.sh"
DEPLOY_DOC = REPO_ROOT / "docs" / "DEPLOY.md"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")
    path.chmod(0o700)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_backup_forces_private_directory_and_artifact_modes(tmp_path: Path) -> None:
    """即使旧目录和同名文件过宽，备份完成后也必须收紧为 700/600。"""
    script_text = BACKUP_SCRIPT.read_text(encoding="utf-8")
    assert "BACKUP_TEST_MODE" in script_text, "脚本缺少隔离执行测试入口"

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    backup_dir.chmod(0o755)
    fixed_dump = backup_dir / "db-20260730-0300.dump"
    fixed_checksum = backup_dir / "db-20260730-0300.dump.sha256"
    historical_dump = backup_dir / "db-20260729-0300.dump"
    historical_checksum = backup_dir / "db-20260729-0300.dump.sha256"
    for path in (historical_dump, historical_checksum):
        path.write_bytes(b"pre-existing")
        path.chmod(0o664)
    assert _mode(backup_dir) == 0o755
    assert _mode(historical_dump) == 0o664
    assert _mode(historical_checksum) == 0o664

    command_dir = tmp_path / "bin"
    command_dir.mkdir()
    _write_executable(
        command_dir / "date",
        r"""
        #!/usr/bin/env bash
        if [ "${1:-}" = "+%Y%m%d-%H%M" ]; then
          printf '%s\n' "20260730-0300"
        else
          printf '%s\n' "2026-07-30 03:00:00"
        fi
        """,
    )
    _write_executable(
        command_dir / "sudo",
        r"""
        #!/usr/bin/env bash
        umask > "$STUB_UMASK_LOG"
        case "$*" in
          *"docker compose exec -T db pg_dump"*)
            /usr/bin/head -c 12000 /dev/zero
            ;;
          *"docker compose exec -T db pg_restore --list"*)
            for item in {1..25}; do
              printf 'object-%s\n' "$item"
            done
            ;;
          *)
            exit 97
            ;;
        esac
        """,
    )

    umask_log = tmp_path / "umask.log"
    result = subprocess.run(
        ["bash", str(BACKUP_SCRIPT)],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "BACKUP_TEST_MODE": "1",
            "BACKUP_TEST_DEST": str(backup_dir),
            "BACKUP_TEST_COMMAND_DIR": str(command_dir),
            "STUB_UMASK_LOG": str(umask_log),
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert umask_log.read_text(encoding="utf-8").strip() == "0077"
    assert _mode(backup_dir) == 0o700
    assert _mode(fixed_dump) == 0o600
    assert _mode(fixed_checksum) == 0o600
    assert _mode(historical_dump) == 0o600
    assert _mode(historical_checksum) == 0o600
    checksum_result = subprocess.run(
        ["sha256sum", "-c", str(fixed_checksum)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert checksum_result.returncode == 0, checksum_result.stderr

    # 模拟同一分钟重跑：既有恢复点保留，新恢复点使用独立名称。
    backup_dir.chmod(0o755)
    for path in (
        fixed_dump,
        fixed_checksum,
        historical_dump,
        historical_checksum,
    ):
        path.chmod(0o664)
    assert _mode(backup_dir) == 0o755
    assert all(
        _mode(path) == 0o664
        for path in (
            fixed_dump,
            fixed_checksum,
            historical_dump,
            historical_checksum,
        )
    )

    rerun = subprocess.run(
        ["bash", str(BACKUP_SCRIPT)],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "BACKUP_TEST_MODE": "1",
            "BACKUP_TEST_DEST": str(backup_dir),
            "BACKUP_TEST_COMMAND_DIR": str(command_dir),
            "STUB_UMASK_LOG": str(umask_log),
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert rerun.returncode == 0, rerun.stderr or rerun.stdout
    assert _mode(backup_dir) == 0o700
    assert all(
        _mode(path) == 0o600
        for path in (
            fixed_dump,
            fixed_checksum,
            historical_dump,
            historical_checksum,
        )
    )
    same_minute_dumps = sorted(backup_dir.glob("db-20260730-0300*.dump"))
    assert len(same_minute_dumps) == 2
    for dump in same_minute_dumps:
        checksum = Path(f"{dump}.sha256")
        assert _mode(dump) == _mode(checksum) == 0o600
        checksum_result = subprocess.run(
            ["sha256sum", "-c", str(checksum)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert checksum_result.returncode == 0, checksum_result.stderr


def test_failed_same_minute_rerun_preserves_last_good_backup(
    tmp_path: Path,
) -> None:
    """pg_dump 失败只能清理本次临时文件，不能截断同分钟的有效恢复点。"""
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    fixed_dump = backup_dir / "db-20260730-0300.dump"
    fixed_checksum = backup_dir / "db-20260730-0300.dump.sha256"
    original_dump = b"known-good-backup" * 1000
    original_checksum = b"known-good-checksum\n"
    fixed_dump.write_bytes(original_dump)
    fixed_checksum.write_bytes(original_checksum)

    command_dir = tmp_path / "bin"
    command_dir.mkdir()
    _write_executable(
        command_dir / "date",
        r"""
        #!/usr/bin/env bash
        if [ "${1:-}" = "+%Y%m%d-%H%M" ]; then
          printf '%s\n' "20260730-0300"
        else
          printf '%s\n' "2026-07-30 03:00:00"
        fi
        """,
    )
    _write_executable(
        command_dir / "sudo",
        r"""
        #!/usr/bin/env bash
        case "$*" in
          *"docker compose exec -T db pg_dump"*)
            printf 'partial-output'
            exit 42
            ;;
          *)
            exit 97
            ;;
        esac
        """,
    )

    result = subprocess.run(
        ["bash", str(BACKUP_SCRIPT)],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "BACKUP_TEST_MODE": "1",
            "BACKUP_TEST_DEST": str(backup_dir),
            "BACKUP_TEST_COMMAND_DIR": str(command_dir),
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 42
    assert fixed_dump.read_bytes() == original_dump
    assert fixed_checksum.read_bytes() == original_checksum
    assert not list(backup_dir.glob(".db-*.tmp.*"))


def test_failed_pg_restore_listing_never_publishes_a_backup(
    tmp_path: Path,
) -> None:
    """TOC 命令即使先输出足量行，只要非零退出就不能发布 dump。"""
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    command_dir = tmp_path / "bin"
    command_dir.mkdir()
    _write_executable(
        command_dir / "date",
        r"""
        #!/usr/bin/env bash
        if [ "${1:-}" = "+%Y%m%d-%H%M" ]; then
          printf '%s\n' "20260730-0300"
        else
          printf '%s\n' "2026-07-30 03:00:00"
        fi
        """,
    )
    _write_executable(
        command_dir / "sudo",
        r"""
        #!/usr/bin/env bash
        case "$*" in
          *"docker compose exec -T db pg_dump"*)
            /usr/bin/head -c 12000 /dev/zero
            ;;
          *"docker compose exec -T db pg_restore --list"*)
            for item in {1..25}; do
              printf 'object-%s\n' "$item"
            done
            exit 42
            ;;
          *)
            exit 97
            ;;
        esac
        """,
    )

    result = subprocess.run(
        ["bash", str(BACKUP_SCRIPT)],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "BACKUP_TEST_MODE": "1",
            "BACKUP_TEST_DEST": str(backup_dir),
            "BACKUP_TEST_COMMAND_DIR": str(command_dir),
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 42
    assert "TOC 校验命令失败" in result.stdout + result.stderr
    assert not list(backup_dir.glob("db-*.dump"))
    assert not list(backup_dir.glob("db-*.dump.sha256"))
    assert not list(backup_dir.glob(".db-*.tmp.*"))


def test_publish_failure_never_leaves_an_incomplete_recovery_point(
    tmp_path: Path,
) -> None:
    """任一步 rename 失败都不能留下只有 dump 或只有 checksum 的正式恢复点。"""
    for failure_kind in ("checksum", "dump"):
        case_dir = tmp_path / failure_kind
        case_dir.mkdir()
        backup_dir = case_dir / "backups"
        backup_dir.mkdir()
        command_dir = case_dir / "bin"
        command_dir.mkdir()
        _write_executable(
            command_dir / "date",
            r"""
            #!/usr/bin/env bash
            if [ "${1:-}" = "+%Y%m%d-%H%M" ]; then
              printf '%s\n' "20260730-0300"
            else
              printf '%s\n' "2026-07-30 03:00:00"
            fi
            """,
        )
        _write_executable(
            command_dir / "sudo",
            r"""
            #!/usr/bin/env bash
            case "$*" in
              *"docker compose exec -T db pg_dump"*)
                /usr/bin/head -c 12000 /dev/zero
                ;;
              *"docker compose exec -T db pg_restore --list"*)
                for item in {1..25}; do
                  printf 'object-%s\n' "$item"
                done
                ;;
              *)
                exit 97
                ;;
            esac
            """,
        )
        _write_executable(
            command_dir / "mv",
            r"""
            #!/usr/bin/env bash
            if [ "${1:-}" = "--" ]; then
              shift
            fi
            source_path=${1:?}
            case "$STUB_FAIL_MOVE:$source_path" in
              checksum:*.sha256.tmp.*|dump:*.dump.tmp.*)
                exit 44
                ;;
            esac
            exec /usr/bin/mv "$@"
            """,
        )

        result = subprocess.run(
            ["bash", str(BACKUP_SCRIPT)],
            cwd=REPO_ROOT,
            env={
                **os.environ,
                "BACKUP_TEST_MODE": "1",
                "BACKUP_TEST_DEST": str(backup_dir),
                "BACKUP_TEST_COMMAND_DIR": str(command_dir),
                "STUB_FAIL_MOVE": failure_kind,
            },
            check=False,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 44
        assert not list(backup_dir.glob("db-*.dump"))
        assert not list(backup_dir.glob("db-*.dump.sha256"))
        assert not list(backup_dir.glob(".db-*.tmp.*"))


def test_backup_rejects_directory_and_lock_symlinks(tmp_path: Path) -> None:
    """目录或稳定锁被替换为 symlink 时必须失败且不触碰链接目标。"""
    command_dir = tmp_path / "bin"
    command_dir.mkdir()
    env_base = {
        **os.environ,
        "BACKUP_TEST_MODE": "1",
        "BACKUP_TEST_COMMAND_DIR": str(command_dir),
    }

    for name, target_exists in (("live", True), ("dangling", False)):
        target = tmp_path / f"{name}-target"
        if target_exists:
            target.mkdir()
            (target / "sentinel").write_text("unchanged", encoding="utf-8")
        destination = tmp_path / f"{name}-link"
        destination.symlink_to(target, target_is_directory=True)
        result = subprocess.run(
            ["bash", str(BACKUP_SCRIPT)],
            cwd=REPO_ROOT,
            env={**env_base, "BACKUP_TEST_DEST": str(destination)},
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1
        assert "备份目录不能是符号链接" in result.stdout + result.stderr
        if target_exists:
            assert (target / "sentinel").read_text(encoding="utf-8") == "unchanged"
            assert not (target / ".backup.lock").exists()

    backup_dir = tmp_path / "backups-with-symlink-lock"
    backup_dir.mkdir()
    lock_target = tmp_path / "lock-target"
    lock_target.write_text("unchanged", encoding="utf-8")
    (backup_dir / ".backup.lock").symlink_to(lock_target)
    lock_result = subprocess.run(
        ["bash", str(BACKUP_SCRIPT)],
        cwd=REPO_ROOT,
        env={**env_base, "BACKUP_TEST_DEST": str(backup_dir)},
        check=False,
        capture_output=True,
        text=True,
    )
    assert lock_result.returncode == 1
    assert "备份锁文件不能是符号链接" in (
        lock_result.stdout + lock_result.stderr
    )
    assert lock_target.read_text(encoding="utf-8") == "unchanged"


def test_overlapping_backup_run_is_rejected_without_touching_artifacts(
    tmp_path: Path,
) -> None:
    """已有备份仍在运行时，本轮必须快速拒绝，不能进入 pg_dump。"""
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    command_dir = tmp_path / "bin"
    command_dir.mkdir()
    _write_executable(
        command_dir / "date",
        r"""
        #!/usr/bin/env bash
        if [ "${1:-}" = "+%Y%m%d-%H%M" ]; then
          printf '%s\n' "20260730-0300"
        else
          printf '%s\n' "2026-07-30 03:00:00"
        fi
        """,
    )
    _write_executable(
        command_dir / "sudo",
        r"""
        #!/usr/bin/env bash
        case "$*" in
          *"docker compose exec -T db pg_dump"*)
            if mkdir "$STUB_GATE_OWNER" 2>/dev/null; then
              : > "$STUB_READY"
              while [ ! -e "$STUB_RELEASE" ]; do
                /usr/bin/sleep 0.02
              done
            fi
            /usr/bin/head -c 12000 /dev/zero
            ;;
          *"docker compose exec -T db pg_restore --list"*)
            for item in {1..25}; do
              printf 'object-%s\n' "$item"
            done
            ;;
          *)
            exit 97
            ;;
        esac
        """,
    )
    ready = tmp_path / "first-ready"
    release = tmp_path / "release-first"
    env = {
        **os.environ,
        "BACKUP_TEST_MODE": "1",
        "BACKUP_TEST_DEST": str(backup_dir),
        "BACKUP_TEST_COMMAND_DIR": str(command_dir),
        "STUB_GATE_OWNER": str(tmp_path / "gate-owner"),
        "STUB_READY": str(ready),
        "STUB_RELEASE": str(release),
    }
    first = subprocess.Popen(
        ["bash", str(BACKUP_SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 5
    while not ready.exists() and first.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)

    second: subprocess.CompletedProcess[str] | None = None
    try:
        assert ready.exists(), "首个备份未进入受控 pg_dump 阶段"
        second = subprocess.run(
            ["bash", str(BACKUP_SCRIPT)],
            cwd=REPO_ROOT,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    finally:
        release.touch()
        first_stdout, first_stderr = first.communicate(timeout=5)

    assert first.returncode == 0, first_stderr or first_stdout
    assert second is not None
    assert second.returncode == 75
    assert "上一次备份仍在运行" in second.stdout + second.stderr
    dumps = list(backup_dir.glob("db-*.dump"))
    checksums = list(backup_dir.glob("db-*.dump.sha256"))
    assert len(dumps) == len(checksums) == 1
    assert not list(backup_dir.glob(".db-*.tmp.*"))


def test_deploy_guide_installs_the_hardened_backup_artifact() -> None:
    """部署手册不能继续复制一份绕过权限保护的旧内联脚本。"""
    guide = DEPLOY_DOC.read_text(encoding="utf-8")

    assert 'install -d -m 700' in guide
    assert guide.index('test ! -L "$BACKUP_DIR"') < guide.index(
        "sudo install -d -m 700"
    )
    assert 'install -m 700 "$APP_DIR/.deploy/backup.sh" "$APP_DIR/backup.sh"' in guide
    assert "cat > ~/apps/it-spareparts/backup.sh" not in guide
    assert 'test "$(stat -c \'%a\' "$BACKUP_DIR")" = 700' in guide
    assert "备份目录必须是 `700`" in guide
    assert "`flock` 拒绝重叠执行" in guide


def test_backup_crontab_install_is_idempotent_and_scoped(tmp_path: Path) -> None:
    """重复安装只能保留一条本应用任务，且不能改动其他 cron。"""
    guide = DEPLOY_DOC.read_text(encoding="utf-8")
    begin = "# BACKUP_CRON_INSTALL_BEGIN"
    end = "# BACKUP_CRON_INSTALL_END"
    assert begin in guide and end in guide
    snippet = guide.split(begin, 1)[1].split(end, 1)[0]
    assert snippet.index("trap cleanup_cron_install EXIT") < snippet.index(
        "CRON_CURRENT=$(mktemp)"
    )

    app_dir = tmp_path / "it-spareparts"
    app_dir.mkdir()
    (app_dir / ".deploy").mkdir()
    _write_executable(app_dir / "backup.sh", "#!/bin/sh\n")
    _write_executable(app_dir / ".deploy" / "backup.sh", "#!/bin/sh\n")

    cron_state = tmp_path / "crontab.txt"
    cron_state.write_text(
        "\n".join(
            [
                "MAILTO=ops@example.test",
                "15 2 * * * /srv/another-product/backup.sh",
                f"0 3 * * * {app_dir}/backup.sh >> {app_dir}/backup.log 2>&1",
                f"0 3 * * * {app_dir}/backup.sh >> {app_dir}/backup.log 2>&1",
                f"0 3 * * * {app_dir}/.deploy/backup.sh",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    command_dir = tmp_path / "bin"
    command_dir.mkdir()
    _write_executable(
        command_dir / "crontab",
        r"""
        #!/usr/bin/env bash
        case "${1:-}" in
          -l)
            if [ ! -f "$FAKE_CRONTAB_STATE" ]; then
              printf 'no crontab for %s\n' "$(/usr/bin/id -un)" >&2
              exit 1
            fi
            cat "$FAKE_CRONTAB_STATE"
            ;;
          -*)
            exit 64
            ;;
          *)
            [ "$#" -eq 1 ] && [ -f "$1" ] || exit 64
            tmp=$(/usr/bin/mktemp)
            cat "$1" > "$tmp"
            mv "$tmp" "$FAKE_CRONTAB_STATE"
            ;;
        esac
        """,
    )
    env = {
        **os.environ,
        "APP_DIR": str(app_dir),
        "FAKE_CRONTAB_STATE": str(cron_state),
        "PATH": f"{command_dir}:{os.environ['PATH']}",
    }

    for _ in range(2):
        result = subprocess.run(
            ["bash", "-eu", "-o", "pipefail", "-c", snippet],
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr or result.stdout

    lines = cron_state.read_text(encoding="utf-8").splitlines()
    expected = (
        f"0 3 * * * umask 077; {app_dir}/backup.sh "
        f">> {app_dir}/backup.log 2>&1"
    )
    assert lines.count(expected) == 1
    assert "MAILTO=ops@example.test" in lines
    assert "15 2 * * * /srv/another-product/backup.sh" in lines
    assert sum(str(app_dir / "backup.sh") in line for line in lines) == 1
    assert not any(str(app_dir / ".deploy" / "backup.sh") in line for line in lines)
    assert _mode(app_dir / "backup.log") == 0o600

    cron_state.write_text("", encoding="utf-8")
    empty_result = subprocess.run(
        ["bash", "-eu", "-o", "pipefail", "-c", snippet],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert empty_result.returncode == 0, empty_result.stderr or empty_result.stdout
    assert cron_state.read_text(encoding="utf-8").splitlines() == [expected]

    cron_state.unlink()
    absent_result = subprocess.run(
        ["bash", "-eu", "-o", "pipefail", "-c", snippet],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert absent_result.returncode == 0, absent_result.stderr or absent_result.stdout
    assert cron_state.read_text(encoding="utf-8").splitlines() == [expected]


def test_backup_crontab_read_failure_is_fail_closed(tmp_path: Path) -> None:
    """无法读取既有 crontab 时不能假装为空，更不能覆盖其他任务。"""
    guide = DEPLOY_DOC.read_text(encoding="utf-8")
    snippet = guide.split("# BACKUP_CRON_INSTALL_BEGIN", 1)[1].split(
        "# BACKUP_CRON_INSTALL_END",
        1,
    )[0]

    app_dir = tmp_path / "it-spareparts"
    app_dir.mkdir()
    _write_executable(app_dir / "backup.sh", "#!/bin/sh\n")
    cron_state = tmp_path / "crontab.txt"
    original = "MAILTO=ops@example.test\n15 2 * * * /srv/another/backup.sh\n"
    cron_state.write_text(original, encoding="utf-8")
    command_dir = tmp_path / "bin"
    command_dir.mkdir()
    _write_executable(
        command_dir / "crontab",
        r"""
        #!/usr/bin/env bash
        case "${1:-}" in
          -l)
            if [ "${STUB_CRONTAB_LIST_STATUS:-0}" -ne 0 ]; then
              printf 'simulated crontab read failure\n' >&2
              exit "$STUB_CRONTAB_LIST_STATUS"
            fi
            cat "$FAKE_CRONTAB_STATE"
            ;;
          -*)
            exit 64
            ;;
          *)
            [ "$#" -eq 1 ] && [ -f "$1" ] || exit 64
            tmp=$(/usr/bin/mktemp)
            cat "$1" > "$tmp"
            /usr/bin/mv "$tmp" "$FAKE_CRONTAB_STATE"
            ;;
        esac
        """,
    )
    result = subprocess.run(
        ["bash", "-eu", "-o", "pipefail", "-c", snippet],
        env={
            **os.environ,
            "APP_DIR": str(app_dir),
            "FAKE_CRONTAB_STATE": str(cron_state),
            "PATH": f"{command_dir}:{os.environ['PATH']}",
            "STUB_CRONTAB_LIST_STATUS": "74",
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert cron_state.read_text(encoding="utf-8") == original

    _write_executable(
        command_dir / "grep",
        "#!/usr/bin/env bash\nexit 2\n",
    )
    filter_failure = subprocess.run(
        ["bash", "-eu", "-o", "pipefail", "-c", snippet],
        env={
            **os.environ,
            "APP_DIR": str(app_dir),
            "FAKE_CRONTAB_STATE": str(cron_state),
            "PATH": f"{command_dir}:{os.environ['PATH']}",
        },
        check=False,
        capture_output=True,
        text=True,
    )
    assert filter_failure.returncode != 0
    assert cron_state.read_text(encoding="utf-8") == original


def test_current_restore_drill_fails_closed_and_compares_stable_project_tables() -> None:
    """后续 schema 发布使用当前恢复门禁；不得污染历史 v1.20 固定控制面。"""
    script = RESTORE_DRILL_SCRIPT.read_text(encoding="utf-8")
    guide = DEPLOY_DOC.read_text(encoding="utf-8")

    assert RESTORE_DRILL_SCRIPT.stat().st_mode & stat.S_IXUSR
    subprocess.run(["bash", "-n", str(RESTORE_DRILL_SCRIPT)], check=True)
    assert "pg_restore -U spareparts -d restore_test --exit-on-error" in script
    assert "maintenance_project|' || count(*) FROM maintenance_project" in script
    assert (
        "maintenance_project_contract|' || count(*) "
        "FROM maintenance_project_contract"
    ) in script
    assert "SOURCE_DB_HEAD" in script
    assert "RESTORED_DB_HEAD" in script
    assert '"$RESTORED_DB_HEAD" = "$SOURCE_DB_HEAD"' in script
    assert "diff -u" in script
    assert "|| true" not in script
    assert '"$APP_DIR/.deploy/restore_drill.sh"' in guide
    assert "两张稳定维保项目表逐表行数" in guide

"""M5-3：v1.23 维保展示板发布链控制（plan v1.3 §2.6）。

模式取自 tests/test_v122_collection_reminders_release_control.py：用 **stub docker**
在 PATH 上真实驱动 .deploy 脚本，断言阶段状态机与三条不可协商规则：
  1. migrate 阶段强制关闭展示板总闸（铁律 7：迁移与开放解耦）；
  2. 翻闸后从运行容器读回核验，失败即紧急复位；
  3. rollback 只关 flag，永不 downgrade。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / ".deploy"
MANIFEST = DEPLOY / "v123_maintenance_boss_manifest.py"
REHEARSE = DEPLOY / "v123_maintenance_boss_rehearse.sh"
RELEASE = DEPLOY / "v123_maintenance_boss_release.sh"
STATIC_TEST = DEPLOY / "v123_maintenance_boss_static_test.py"
AUDIT_DOC = ROOT / "docs" / "releases" / "v1.23-migration-audit.md"

DB_FROM = "c8e2a4f6b1d3"
DB_TO = "c5d9e3f7a2b4"
RELEASE_FLAG = "MAINTENANCE_BOSS_DASHBOARD_ENABLED"
TARGET_SHA = "a" * 40
PARENT_SHA = "b" * 40

ARTIFACTS = (MANIFEST, REHEARSE, RELEASE, STATIC_TEST)


# --------------------------------------------------------------- 工件基线

def test_artifacts_exist_and_are_executable():
    for path in ARTIFACTS:
        assert path.is_file(), path.name
        if path.suffix == ".sh":
            assert path.stat().st_mode & 0o111, f"{path.name} 缺少可执行位"


def test_scripts_pass_syntax_check():
    for script in (REHEARSE, RELEASE):
        result = subprocess.run(["bash", "-n", str(script)], capture_output=True)
        assert result.returncode == 0, result.stderr.decode()


def test_static_self_test_runs_without_production_access():
    result = subprocess.run([sys.executable, str(STATIC_TEST)], capture_output=True)
    assert result.returncode == 0, result.stdout.decode() + result.stderr.decode()


def test_manifest_binds_this_release_not_the_previous_one():
    src = MANIFEST.read_text(encoding="utf-8")
    assert f'DB_FROM = "{DB_FROM}"' in src
    assert f'DB_TO = "{DB_TO}"' in src
    # v1.22 的区间不得残留（复制粘贴发布脚本的典型事故）
    assert "d9f1a3c7e5b2" not in src


def test_manifest_registers_all_frozen_feature_gates():
    """冻结清单四个服务端闸门都必须登记（审计表 §2 的核对依据）。"""
    src = MANIFEST.read_text(encoding="utf-8")
    for flag in (
        "MAINTENANCE_BETA_ENABLED",
        "MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED",
        "REPLENISHMENT_BETA_ENABLED",
        "LLM_MAPPING_EXTERNAL_ENABLED",
    ):
        assert flag in src, flag


def test_migration_audit_doc_covers_every_carried_revision():
    doc = AUDIT_DOC.read_text(encoding="utf-8")
    carried = [
        "e7b3d9f2c1a4", "b1e3f7d9c2a5", "c3b5d9e1f7a2", "d1e3f5a7c2b9",
        "e9f2d4b7a1c6", "f1a2b3c4d5e6", "a7c3e5f9b2d1", "b9d1e7c3f5a8",
        "c3e9d1b7f5a2", "d7f1a3c5e8b2", "e3c5a7f9d1b2", "f1b3d5e7a9c2",
        "b4c8d2e6f1a3", "c5d9e3f7a2b4",
    ]
    for revision in carried:
        assert revision in doc, f"审计表缺少修订 {revision}"
    # 铁律张力必须显式上交而非自行改判
    assert "M0-E" in doc


# --------------------------------------------------------------- 清单 build/verify

def test_manifest_build_and_verify_roundtrip(tmp_path):
    out = tmp_path / "package"
    result = subprocess.run(
        [sys.executable, str(MANIFEST), "build", "--source-dir", str(DEPLOY),
         "--target-sha", TARGET_SHA, "--parent-sha", PARENT_SHA, "--out", str(out)],
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode()
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["db_from"] == DB_FROM and manifest["db_to"] == DB_TO
    assert manifest["migrate_phase_flag_value"] == "false"
    assert manifest["target_sha"] == TARGET_SHA
    verify = subprocess.run(
        [sys.executable, str(MANIFEST), "verify", "--package-dir", str(out)],
        capture_output=True,
    )
    assert verify.returncode == 0, verify.stderr.decode()


def test_manifest_verify_rejects_tampered_artifact(tmp_path):
    out = tmp_path / "package"
    subprocess.run(
        [sys.executable, str(MANIFEST), "build", "--source-dir", str(DEPLOY),
         "--target-sha", TARGET_SHA, "--parent-sha", PARENT_SHA, "--out", str(out)],
        capture_output=True, check=True,
    )
    victim = out / "v123_maintenance_boss_release.sh"
    victim.chmod(0o600)
    victim.write_text(victim.read_text(encoding="utf-8") + "\n# tampered\n",
                      encoding="utf-8")
    verify = subprocess.run(
        [sys.executable, str(MANIFEST), "verify", "--package-dir", str(out)],
        capture_output=True,
    )
    assert verify.returncode != 0
    assert "篡改" in verify.stderr.decode()


def test_manifest_build_rejects_identical_shas(tmp_path):
    result = subprocess.run(
        [sys.executable, str(MANIFEST), "build", "--source-dir", str(DEPLOY),
         "--target-sha", TARGET_SHA, "--parent-sha", TARGET_SHA,
         "--out", str(tmp_path / "pkg")],
        capture_output=True,
    )
    assert result.returncode != 0


# --------------------------------------------------------------- 阶段状态机（stub docker）

def _stub_docker(tmp_path: Path, *, alembic_version: str = DB_FROM,
                 flag_value: str = "false") -> Path:
    """最小 docker 替身：记录调用、模拟 alembic_version 查询与 flag 读回。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "docker.log"
    state = tmp_path / "docker_state"
    state.mkdir(exist_ok=True)
    (state / "alembic_version").write_text(alembic_version)
    (state / "flag").write_text(flag_value)
    script = bin_dir / "docker"
    script.write_text(f"""#!/usr/bin/env bash
echo "$@" >> "{log}"
state="{state}"
# docker compose exec -T db psql ... SELECT version_num FROM alembic_version
if printf '%s' "$*" | grep -q 'alembic_version'; then
  cat "$state/alembic_version"; exit 0
fi
# docker compose exec -T app sh -ceu 'printf %s ${{FLAG:-unset}}'
if printf '%s' "$*" | grep -q '{RELEASE_FLAG}'; then
  # migrate 阶段是 compose run -e FLAG=false ... alembic upgrade
  if printf '%s' "$*" | grep -q 'alembic upgrade'; then
    printf '%s' "$(cat "$state/alembic_version")" > /dev/null
    echo "{DB_TO}" > "$state/alembic_version"
    exit 0
  fi
  cat "$state/flag"; exit 0
fi
if printf '%s' "$*" | grep -q 'pg_dump'; then
  echo "fake-dump"; exit 0
fi
exit 0
""")
    script.chmod(0o755)
    return bin_dir


def _run_release(tmp_path: Path, bin_dir: Path, *args, env_extra=None):
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env.update(env_extra or {})
    return subprocess.run(
        ["bash", str(RELEASE), *args],
        capture_output=True, cwd=tmp_path, env=env,
    )


def test_phase_order_rejects_skipping_without_touching_docker(tmp_path):
    bin_dir = _stub_docker(tmp_path)
    state = tmp_path / "state"
    # 未 preflight 直接 migrate → 拒绝，且不产生任何 docker 调用
    result = _run_release(tmp_path, bin_dir, "migrate", str(state))
    assert result.returncode != 0
    assert "阶段顺序错误" in result.stderr.decode()
    assert not (tmp_path / "docker.log").exists()


def test_phase_order_rejects_repeat_and_regression(tmp_path):
    bin_dir = _stub_docker(tmp_path)
    state = tmp_path / "state"
    state.write_text("phase=migrate\n")
    repeat = _run_release(tmp_path, bin_dir, "migrate", str(state))
    assert repeat.returncode != 0 and "阶段顺序错误" in repeat.stderr.decode()
    regression = _run_release(tmp_path, bin_dir, "backup", str(state), str(tmp_path))
    assert regression.returncode != 0


def test_migrate_forces_release_flag_false_and_checks_revisions(tmp_path):
    bin_dir = _stub_docker(tmp_path)
    state = tmp_path / "state"
    state.write_text("phase=backup\n")
    result = _run_release(tmp_path, bin_dir, "migrate", str(state))
    assert result.returncode == 0, result.stderr.decode()
    calls = (tmp_path / "docker.log").read_text()
    assert f"-e {RELEASE_FLAG}=false" in calls, "migrate 未强制关闭总闸"
    assert f"alembic upgrade {DB_TO}" in calls
    assert "downgrade" not in calls
    assert "phase=migrate" in state.read_text()


def test_migrate_refuses_wrong_production_baseline(tmp_path):
    bin_dir = _stub_docker(tmp_path, alembic_version="deadbeefcafe")
    state = tmp_path / "state"
    state.write_text("phase=backup\n")
    result = _run_release(tmp_path, bin_dir, "migrate", str(state))
    assert result.returncode != 0
    assert "生产基线不是" in result.stderr.decode()
    assert "phase=backup" in state.read_text()   # 状态未推进


def test_canary_reads_flag_back_from_running_container(tmp_path):
    bin_dir = _stub_docker(tmp_path, flag_value="true")
    (tmp_path / ".env").write_text(f"{RELEASE_FLAG}=false\n")
    state = tmp_path / "state"
    state.write_text("phase=deploy\n")
    result = _run_release(tmp_path, bin_dir, "canary", str(state))
    assert result.returncode == 0, result.stderr.decode()
    assert f"{RELEASE_FLAG}=true" in (tmp_path / ".env").read_text()
    assert "phase=canary" in state.read_text()


def test_canary_emergency_restores_flag_when_readback_disagrees(tmp_path):
    # 容器读回仍是 false（翻闸未生效）→ 必须失败并把 .env 复位
    bin_dir = _stub_docker(tmp_path, flag_value="false")
    (tmp_path / ".env").write_text(f"{RELEASE_FLAG}=false\n")
    state = tmp_path / "state"
    state.write_text("phase=deploy\n")
    result = _run_release(tmp_path, bin_dir, "canary", str(state))
    assert result.returncode != 0
    assert f"{RELEASE_FLAG}=false" in (tmp_path / ".env").read_text()
    assert "phase=deploy" in state.read_text()   # 未推进到 canary


def test_rollback_only_closes_flag_and_never_downgrades(tmp_path):
    bin_dir = _stub_docker(tmp_path, alembic_version=DB_TO, flag_value="false")
    (tmp_path / ".env").write_text(f"{RELEASE_FLAG}=true\n")
    result = _run_release(tmp_path, bin_dir, "rollback", str(tmp_path / "state"))
    assert result.returncode == 0, result.stderr.decode()
    assert f"{RELEASE_FLAG}=false" in (tmp_path / ".env").read_text()
    calls = (tmp_path / "docker.log").read_text()
    assert "downgrade" not in calls
    # schema 保持在目标修订（不回退）
    assert (tmp_path / "docker_state" / "alembic_version").read_text() == DB_TO


def test_rehearse_requires_valid_shas_before_touching_docker(tmp_path):
    bin_dir = _stub_docker(tmp_path)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    dump = tmp_path / "dump.sql"
    dump.write_text("-- fake")
    result = subprocess.run(
        ["bash", str(REHEARSE), str(dump), "not-a-sha", PARENT_SHA,
         "sha256:" + "0" * 64, "sha256:" + "1" * 64, str(tmp_path / "out")],
        capture_output=True, cwd=tmp_path, env=env,
    )
    assert result.returncode != 0
    assert "TARGET_SHA 非法" in result.stderr.decode()
    assert not (tmp_path / "docker.log").exists()

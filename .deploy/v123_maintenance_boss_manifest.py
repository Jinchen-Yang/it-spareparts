#!/usr/bin/env python3
"""v1.23 维保展示板发布清单：构建与校验可移植发布包（plan v1.3 M5-3）。

设计沿用 v1.22（`v122_collection_reminders_manifest.py`）的核心不变量，并按本次
发布的实际风险面收敛：

1. **每一字节都被绑定**：清单登记每个工件的 sha256（含发布脚本自身），
   verify 逐个重算；任何篡改导致 verify 失败。
2. **迁移区间显式声明**：DB_FROM/DB_TO 写死在清单里，release.sh 的 migrate 阶段
   会读回 alembic_version 与之比对，防止跑错基线。
3. **运行时 flag 默认关**：包内声明 REQUIRED_RUNTIME_FLAGS，其中展示板总闸
   在迁移阶段必须为 false（铁律 7：回滚=关 flag，不做 downgrade）。
4. **精确 SHA 绑定**：TARGET_SHA/PARENT_PROD_SHA 进清单，独立复审对准 TARGET_SHA。

用法：
    manifest.py build  --source-dir DIR --target-sha SHA --parent-sha SHA --out DIR
    manifest.py verify --package-dir DIR
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

FORMAT = "v123-maintenance-boss-1"

# 迁移区间（docs/releases/v1.23-migration-audit.md 逐修订审计）
DB_FROM = "c8e2a4f6b1d3"
DB_TO = "c5d9e3f7a2b4"

# 本次发布必须显式声明的运行时开关。展示板总闸在 migrate 阶段强制 false，
# 冻结功能的四个闸门在整个发布过程中恒 false（审计表 §2）。
RELEASE_FLAG = "MAINTENANCE_BOSS_DASHBOARD_ENABLED"
FROZEN_FLAGS = (
    "MAINTENANCE_BETA_ENABLED",
    "MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED",
    "REPLENISHMENT_BETA_ENABLED",
    "LLM_MAPPING_EXTERNAL_ENABLED",
)
REQUIRED_RUNTIME_FLAGS = (RELEASE_FLAG, *FROZEN_FLAGS)

# 本次发布新增的权限键（迁移 c5d9e3f7a2b4 回填，一律 false）
RELEASE_PERMISSION_KEYS = (
    "page_maintenance_boss",
    "action_maintenance_wbdd_import",
)

# 发布包必须包含的工件（basename；清单只用 basename，包目录扁平不可变）
REQUIRED_ARTIFACTS = (
    "v123_maintenance_boss_manifest.py",
    "v123_maintenance_boss_rehearse.sh",
    "v123_maintenance_boss_release.sh",
    "v123_maintenance_boss_static_test.py",
)

MANIFEST_NAME = "manifest.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_sha(value: str, label: str) -> str:
    if len(value) != 40 or any(ch not in "0123456789abcdef" for ch in value):
        raise SystemExit(f"FATAL: {label} 必须是 40 位小写十六进制 git SHA")
    return value


def build(source_dir: Path, target_sha: str, parent_sha: str, out_dir: Path,
          now: str | None = None) -> Path:
    target_sha = _validate_sha(target_sha, "TARGET_SHA")
    parent_sha = _validate_sha(parent_sha, "PARENT_PROD_SHA")
    if target_sha == parent_sha:
        raise SystemExit("FATAL: TARGET_SHA 与 PARENT_PROD_SHA 不能相同")
    if out_dir.exists():
        raise SystemExit(f"FATAL: 输出目录已存在：{out_dir}")

    artifacts = {}
    for name in REQUIRED_ARTIFACTS:
        src = source_dir / name
        if not src.is_file():
            raise SystemExit(f"FATAL: 缺少发布工件 {name}")
        artifacts[name] = sha256_file(src)

    out_dir.mkdir(parents=True)
    for name in REQUIRED_ARTIFACTS:
        payload = (source_dir / name).read_bytes()
        dest = out_dir / name
        dest.write_bytes(payload)
        dest.chmod(0o500 if name.endswith(".sh") else 0o400)

    manifest = {
        "format": FORMAT,
        "created_at": now or dt.datetime.now(dt.timezone.utc).isoformat(),
        "target_sha": target_sha,
        "parent_prod_sha": parent_sha,
        "db_from": DB_FROM,
        "db_to": DB_TO,
        "release_flag": RELEASE_FLAG,
        "frozen_flags": list(FROZEN_FLAGS),
        "required_runtime_flags": list(REQUIRED_RUNTIME_FLAGS),
        "release_permission_keys": list(RELEASE_PERMISSION_KEYS),
        # 迁移阶段的强制值：展示板闸必须关（灰度阶段才翻）
        "migrate_phase_flag_value": "false",
        "artifacts": artifacts,
    }
    (out_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (out_dir / MANIFEST_NAME).chmod(0o400)
    return out_dir / MANIFEST_NAME


def verify(package_dir: Path) -> dict:
    manifest_path = package_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        raise SystemExit("FATAL: 发布包缺少 manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    if manifest.get("format") != FORMAT:
        raise SystemExit("FATAL: 清单格式不匹配（不是 v1.23 发布包）")
    if manifest.get("db_from") != DB_FROM or manifest.get("db_to") != DB_TO:
        raise SystemExit(
            f"FATAL: 迁移区间不匹配，期望 {DB_FROM} -> {DB_TO}，"
            f"实为 {manifest.get('db_from')} -> {manifest.get('db_to')}"
        )
    if manifest.get("migrate_phase_flag_value") != "false":
        raise SystemExit("FATAL: 迁移阶段必须强制关闭展示板总闸（铁律 7）")
    if manifest.get("release_flag") != RELEASE_FLAG:
        raise SystemExit("FATAL: 发布 flag 名称不匹配")
    for flag in FROZEN_FLAGS:
        if flag not in manifest.get("frozen_flags", []):
            raise SystemExit(f"FATAL: 冻结功能闸门未登记：{flag}")

    artifacts = manifest.get("artifacts", {})
    missing = [name for name in REQUIRED_ARTIFACTS if name not in artifacts]
    if missing:
        raise SystemExit(f"FATAL: 清单缺少工件条目：{missing}")
    for name, expected in sorted(artifacts.items()):
        path = package_dir / name
        if not path.is_file():
            raise SystemExit(f"FATAL: 发布包缺少文件 {name}")
        actual = sha256_file(path)
        if actual != expected:
            raise SystemExit(f"FATAL: 工件被篡改：{name}")
    _validate_sha(manifest.get("target_sha", ""), "TARGET_SHA")
    _validate_sha(manifest.get("parent_prod_sha", ""), "PARENT_PROD_SHA")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    build_cmd = sub.add_parser("build")
    build_cmd.add_argument("--source-dir", required=True, type=Path)
    build_cmd.add_argument("--target-sha", required=True)
    build_cmd.add_argument("--parent-sha", required=True)
    build_cmd.add_argument("--out", required=True, type=Path)

    verify_cmd = sub.add_parser("verify")
    verify_cmd.add_argument("--package-dir", required=True, type=Path)

    args = parser.parse_args(argv)
    if args.command == "build":
        path = build(args.source_dir, args.target_sha, args.parent_sha, args.out)
        print(f"OK 发布包已生成：{path}")
        return 0
    manifest = verify(args.package_dir)
    print(
        "OK 发布包校验通过："
        f"{manifest['db_from']} -> {manifest['db_to']}，"
        f"target={manifest['target_sha'][:12]}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

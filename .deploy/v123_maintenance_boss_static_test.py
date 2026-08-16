#!/usr/bin/env python3
"""v1.23 发布工件静态自检（无需生产访问，可在任意机器运行）。

只做**静态**校验：语法、清单常量、脚本不变量。真实迁移演练由 rehearse.sh 负责。
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "v123_maintenance_boss_manifest.py"
REHEARSE = HERE / "v123_maintenance_boss_rehearse.sh"
RELEASE = HERE / "v123_maintenance_boss_release.sh"

DB_FROM = "c8e2a4f6b1d3"
DB_TO = "d6e1f4a8c3b5"
RELEASE_FLAG = "MAINTENANCE_BOSS_DASHBOARD_ENABLED"

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        failures.append(message)


def main() -> int:
    for path in (MANIFEST, REHEARSE, RELEASE):
        check(path.is_file(), f"缺少工件：{path.name}")
    if failures:
        for line in failures:
            print("FAIL:", line)
        return 1

    # 1) 语法
    ast.parse(MANIFEST.read_text(encoding="utf-8"))
    for script in (REHEARSE, RELEASE):
        result = subprocess.run(["bash", "-n", str(script)], capture_output=True)
        check(result.returncode == 0, f"{script.name} 语法错误：{result.stderr.decode()}")

    # 2) 可执行位
    for script in (REHEARSE, RELEASE):
        check(script.stat().st_mode & 0o111 != 0, f"{script.name} 缺少可执行位")

    manifest_src = MANIFEST.read_text(encoding="utf-8")
    release_src = RELEASE.read_text(encoding="utf-8")
    rehearse_src = REHEARSE.read_text(encoding="utf-8")

    # 3) 迁移区间三处一致（清单/演练/发布），且不是上一版 v1.22 的区间
    for name, src in (("manifest", manifest_src), ("rehearse", rehearse_src),
                      ("release", release_src)):
        check(DB_FROM in src, f"{name} 未声明起始修订 {DB_FROM}")
        check(DB_TO in src, f"{name} 未声明目标修订 {DB_TO}")
        check("d9f1a3c7e5b2" not in src, f"{name} 仍引用 v1.22 的迁移区间")

    # 4) 迁移阶段强制关闭总闸（铁律 7）
    check(
        re.search(rf"-e\s+\"\$RELEASE_FLAG=false\"", release_src) is not None,
        "release.sh 的 migrate 阶段未强制 RELEASE_FLAG=false",
    )
    check('"migrate_phase_flag_value": "false"' in manifest_src
          or 'migrate_phase_flag_value": "false' in manifest_src,
          "manifest 未声明迁移阶段 flag 必须为 false")

    # 5) 回滚只关 flag，绝不 downgrade。
    #    只禁**可执行的** downgrade 调用；注释与提示文案里的「不做 downgrade」允许出现。
    code_lines = [
        line for line in release_src.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    for line in code_lines:
        check(
            not re.search(r"alembic\s+downgrade|\bdowngrade\s+[\"']?[0-9a-f]{12}", line),
            f"release.sh 出现了可执行的 alembic downgrade —— 违反铁律 7：{line.strip()}",
        )
    check("rollback)" in release_src, "release.sh 缺少 rollback 命令")
    check("upgrade" in release_src, "release.sh 缺少 alembic upgrade 调用")

    # 6) 翻闸后必须从运行容器读回
    check("readback_flag" in release_src, "release.sh 缺少 flag 读回核验")
    check("emergency_close_flag" in release_src, "release.sh 缺少紧急复位 trap")

    # 7) 演练必须验证旧应用兼容新 schema（回滚前提）
    check("PARENT_APP_IMAGE_ID" in rehearse_src,
          "rehearse.sh 未用父生产镜像验证旧应用兼容性")

    if failures:
        for line in failures:
            print("FAIL:", line)
        return 1
    print("OK v1.23 发布工件静态自检通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())

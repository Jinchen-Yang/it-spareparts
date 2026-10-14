"""只读核对：技术员↔销售对接名单 × 稳定项目 salesperson 的覆盖与例外候选。

用途：为「一个技术员对接若干销售名下全部项目」的账号权限配置提供数据底账。
两条配置路线（本脚本只产候选清单，不做任何配置动作）：
  - 账号「对应销售名」+ 行键 own_maintenance_projects_only：项目 salesperson
    与账号销售名**精确相等**才可见（accessible_project_condition 唯一口径），
    新项目自动跟随；行级谓词没有排除语义，项目名例外无法在此路线表达；
  - 项目级可见账号 viewer / 负责人挂靠：逐项目指定，项目名简写例外天然由
    「不勾」表达，但新项目需人工补挂。

输出五部分：
  1) 每位对接人按名单销售名的精确命中项目清单与数量；
  2) 命中集内、项目名含例外简写（如福建联通/中兴）的候选行——映射路线下
     这些项目仍会可见，如需隔离只能走逐项目挂靠；
  3) 客户线对接人按项目名关键字筛出的候选项目；
  4) 未被任何名单认领的销售名残留（开放集合候选 + 写法差异线索）；
  5) salesperson 为空的项目（映射路线天然不可见，只能逐项目挂靠）。

名称相似只是核对线索，不能单独证明归属（CONTEXT.md）；viewer/负责人挂靠以
人工确认后的 project_id 为准。

**安全属性**：本脚本唯一的数据访问是一条 SELECT，且会话建立后立即执行
``SET TRANSACTION READ ONLY`` 把整个事务钉成只读——即使被误改出写语句，
数据库也会拒绝。因此：不产生任何状态变更（无回滚需求），重复执行结果
一致（天然幂等）。报告头部记录连接目标（库/用户/服务器地址）以便审计。

**行数上限**：--max-rows 控制每张表最多展示行数（默认 500，超出截断并
标注总数）；作为逐项目挂靠底账时应确认无截断。

运行示例（需可访问数据库；默认连 config.database_url，可用 DATABASE_URL 覆盖）：
    cd backend && uv run python scripts/audit_salesperson_assignment_coverage.py
    cd backend && uv run python scripts/audit_salesperson_assignment_coverage.py \
      --out /tmp/assignment_coverage.md
    cd backend && uv run python scripts/audit_salesperson_assignment_coverage.py \
      --roster /tmp/my_roster.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from sqlalchemy import select, text

from app.db import SessionLocal
from app.models.maintenance_project import MaintenanceProject

DEFAULT_MAX_ROWS = 500

# 由 main() 依 --max-rows 设置一次后只读；md_table 全部走它，避免逐调用点穿参。
_current_max_rows = DEFAULT_MAX_ROWS

# 内置默认名单 = 2026-10-14 讨论口径。人名/简写以台账实际写法为准，出入大时
# 用 --roster 覆盖（JSON 结构与本字典一致）。
DEFAULT_ROSTER: dict = {
    "assignments": [
        {
            "owner": "陈智玮",
            "salespersons": ["李呈辉"],
            "name_excludes": ["福建联通", "中兴"],
            "fallback_keywords": [],
            "note": "",
        },
        {
            "owner": "郭鸿广",
            "salespersons": ["周晓飞", "尹涛"],
            "name_excludes": [],
            "fallback_keywords": [],
            "note": "",
        },
        {
            "owner": "李冰冰",
            "salespersons": ["王界", "谢广灿", "王源", "尹东东", "马蓉"],
            "name_excludes": [],
            "fallback_keywords": [],
            "note": "名单含「等广州其他销售」，属开放集合，残留区另看",
        },
        {
            "owner": "张佳瑶",
            "salespersons": ["金总"],
            "name_excludes": [],
            "fallback_keywords": ["金"],
            "note": "「金总」若非台账实名写法，零命中时按兜底关键字看线索；含离职人员挂靠单子",
        },
        {
            "owner": "丁艳秋",
            "salespersons": ["雪菲"],
            "name_excludes": ["电信云"],
            "fallback_keywords": [],
            "note": "",
        },
        {
            "owner": "司珂梓",
            "salespersons": ["余俊", "胡杨"],
            "name_excludes": [],
            "fallback_keywords": [],
            "note": "",
        },
        {
            "owner": "（新）CMO（暂替：廖晓娟）",
            "salespersons": ["涂丹琴"],
            "name_excludes": [],
            "fallback_keywords": [],
            "note": "新 CMO 到岗前由廖晓娟暂替，暂替期逐项目负责人挂靠即可",
        },
    ],
    "customer_line": {
        "owner": "廖晓娟",
        "name_keywords": ["电信云", "仁科", "福建联通", "中兴"],
    },
    "open_pool_label": "肖遥部门（其他销售，开放集合）",
}


def norm(value: str | None) -> str:
    """去掉全部空白（含全角空格）后 casefold，用于容纳台账写法的尾空格差异。"""
    return "".join(str(value or "").split()).casefold()


def md_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    max_rows = _current_max_rows
    shown = rows[:max_rows]
    for row in shown:
        lines.append("| " + " | ".join(cell.replace("|", "\\|") for cell in row) + " |")
    if len(rows) > max_rows:
        lines.append(f"（⚠ 仅展示前 {max_rows} 行，共 {len(rows)} 行——配置底账不完整，请提高 --max-rows 重跑）")
    return lines


def project_row(p: dict) -> list[str]:
    active = "是" if p["is_active"] else "否"
    override = "是" if p["salesperson_override_active"] else "否"
    return [
        p["project_code"],
        p["display_name"],
        p["salesperson"] or "",
        p["cmo_name"] or "",
        active,
        override,
    ]


PROJECT_HEADERS = ["项目编码", "项目名", "销售", "CMO", "活跃", "销售名人工覆盖"]


def build_report(projects: list[dict], roster: dict, db_identity: str | None = None) -> str:
    lines: list[str] = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines.append(f"# 技术员↔销售对接核对报告（只读，生成于 {now}）")
    lines.append("")
    if db_identity:
        lines.append(f"连接目标：{db_identity}（只读事务）")
        lines.append("")

    # 预处理：销售名归一 → 认领人；项目名归一用于例外/客户线关键字。
    claimed_owners: dict[str, str] = {}
    conflict_claims: list[str] = []
    for item in roster["assignments"]:
        for name in item["salespersons"]:
            key = norm(name)
            if not key:
                continue
            if key in claimed_owners and claimed_owners[key] != item["owner"]:
                conflict_claims.append(
                    f"销售名「{name}」同时出现在 {claimed_owners[key]} 与 {item['owner']} 的名单里"
                )
                continue
            claimed_owners[key] = item["owner"]

    lines.append(
        f"项目总数 {len(projects)}，其中活跃 "
        f"{sum(1 for p in projects if p['is_active'])}。"
        "以下全部是候选线索；正式配置以人工确认后的 project_id 为准。"
    )
    lines.append("")
    if conflict_claims:
        lines.append("**⚠ 名单冲突（先解决再配置）**：")
        for c in conflict_claims:
            lines.append(f"- {c}")
        lines.append("")

    all_salesperson_values: dict[str, int] = defaultdict(int)
    raw_by_norm: dict[str, str] = {}
    for p in projects:
        key = norm(p["salesperson"])
        if key:
            all_salesperson_values[key] += 1
            raw_by_norm[key] = p["salesperson"]

    # ── 一、逐对接人覆盖 ─────────────────────────────────────────────
    lines.append("## 一、逐对接人覆盖（销售名精确匹配）")
    lines.append("")
    summary: list[list[str]] = []
    claimed_keys: set[str] = set()
    for item in roster["assignments"]:
        names = [n for n in item["salespersons"] if norm(n)]
        hits: dict[str, list[dict]] = defaultdict(list)
        for p in projects:
            key = norm(p["salesperson"])
            if key and key in {norm(n) for n in names}:
                hits[key].append(p)
                claimed_keys.add(key)
        matched = [p for plist in hits.values() for p in plist]
        excludes = [e for e in item.get("name_excludes", []) if norm(e)]
        excluded: list[dict] = []
        if excludes:
            exclude_keys = {norm(e) for e in excludes}
            excluded = [
                p
                for p in matched
                if any(k in norm(p["display_name"]) for k in exclude_keys)
            ]
        note = f"（{item['note']}）" if item.get("note") else ""
        lines.append(
            f"### {item['owner']} ← {'、'.join(names)} {note}\n"
        )
        if not matched:
            lines.append(f"- **精确命中 0 个项目。**")
            variants = [
                value
                for value in all_salesperson_values
                if value not in claimed_owners
                and any(
                    name_key in value or value in name_key
                    for name_key in (norm(n) for n in names)
                    if name_key
                )
            ]
            if variants:
                lines.append(
                    "- 疑似写法差异（台账销售名与名单互相包含但不相等）："
                    + "、".join(
                        f"「{raw_by_norm.get(v, v)}」（{all_salesperson_values[v]} 个项目）"
                        for v in sorted(variants)
                    )
                )
        else:
            lines.append(
                f"- 精确命中 {len(matched)} 个项目"
                + (f"，其中例外候选 {len(excluded)} 个" if excludes else "")
                + "："
            )
            lines.extend(md_table(PROJECT_HEADERS, [project_row(p) for p in matched]))
            raw_mismatch = [
                p
                for p in matched
                if p["salesperson"] not in names  # 行级谓词是裸字符串相等
            ]
            if raw_mismatch:
                detail = "；".join(
                    f"{p['project_code']}（台账写作「{p['salesperson']}」）"
                    for p in raw_mismatch
                )
                lines.append(
                    f"- **写法失配告警**：{detail} 与名单写法存在空白/字符差异。"
                    "真实行级匹配是裸字符串相等，这类项目**不会**进入账号可见集，"
                    "需先修正台账销售名或按原样填账号销售名。"
                )
        if excludes and excluded:
            lines.append(
                "- **例外候选**（项目名含 "
                + "、".join(excludes)
                + "）：销售名映射路线下这些项目**仍会**对该账号可见（行级谓词无排除"
                "语义）；如需隔离，请把这几项改走逐项目挂靠并接受可见重叠，或整人改用 viewer。"
            )
            lines.extend(md_table(PROJECT_HEADERS, [project_row(p) for p in excluded]))
        for kw in item.get("fallback_keywords", []):
            if not norm(kw):
                continue
            clues = [
                p
                for p in projects
                if p not in matched
                and (
                    norm(kw) in norm(p["salesperson"])
                    or norm(kw) in norm(p["cmo_name"])
                )
            ]
            if clues:
                lines.append(
                    f"- 兜底关键字「{kw}」线索（销售/CMO 字段含该字、非精确命中）："
                )
                lines.extend(
                    md_table(PROJECT_HEADERS, [project_row(p) for p in clues])
                )
        summary.append(
            [
                item["owner"],
                str(len(matched)),
                str(len(excluded)),
                "、".join(names),
            ]
        )
        lines.append("")

    # ── 二、客户线（按项目名关键字，不按销售名）──────────────────────
    cl = roster.get("customer_line") or {}
    if cl.get("name_keywords"):
        lines.append(
            f"## 二、客户线 {cl.get('owner', '')}（按项目名关键字筛选，与销售名无关）"
        )
        lines.append("")
        kw_keys = {norm(k): k for k in cl["name_keywords"] if norm(k)}
        grouped: dict[str, list[dict]] = defaultdict(list)
        for p in projects:
            hits = [orig for key, orig in kw_keys.items() if key in norm(p["display_name"])]
            for kw in hits:
                grouped[kw].append(p)
        for orig in (kw_keys[k] for k in kw_keys):
            rows = grouped.get(orig, [])
            lines.append(f"### 项目名含「{orig}」：{len(rows)} 个")
            if rows:
                lines.extend(md_table(PROJECT_HEADERS, [project_row(p) for p in rows]))
            lines.append("")
        multi = [
            p
            for p in projects
            if sum(1 for key in kw_keys if key in norm(p["display_name"])) > 1
        ]
        if multi:
            lines.append(
                "注意：以下项目名同时命中多个关键字（如福建联通+中兴），"
                "挂靠时按 project_id 逐条确认："
            )
            lines.extend(md_table(PROJECT_HEADERS, [project_row(p) for p in multi]))
            lines.append("")

    # ── 三、名单外销售名残留 ─────────────────────────────────────────
    leftover = sorted(
        (v for v in all_salesperson_values if v not in claimed_keys),
        key=lambda v: (-all_salesperson_values[v], v),
    )
    label = roster.get("open_pool_label", "开放池")
    lines.append(f"## 三、未被任何名单认领的销售名残留（{label}候选 + 写法差异线索）")
    lines.append("")
    if leftover:
        lines.extend(
            md_table(
                ["台账销售名", "项目数"],
                [
                    [raw_by_norm.get(v, v), str(all_salesperson_values[v])]
                    for v in leftover
                ],
            )
        )
    else:
        lines.append("无残留。")
    lines.append("")

    # ── 四、销售为空的项目 ───────────────────────────────────────────
    empty = [p for p in projects if not norm(p["salesperson"])]
    lines.append("## 四、salesperson 为空的项目（映射路线天然不可见）")
    lines.append("")
    lines.append(f"共 {len(empty)} 个；这些项目只能走逐项目挂靠（viewer/负责人）。")
    if empty:
        lines.extend(md_table(PROJECT_HEADERS, [project_row(p) for p in empty]))
    lines.append("")

    # ── 五、汇总 ─────────────────────────────────────────────────────
    lines.append("## 五、汇总")
    lines.append("")
    lines.extend(md_table(["对接人", "映射可见项目数", "其中例外候选", "名单销售名"], summary))
    lines.append("")
    lines.append(
        "提醒：负责人（primary_manager）每项目仅一名且含成本/合同额全字段编辑权；"
        "「只看」用项目级可见账号（viewer）。配置顺序建议：先确认本报告清单 → "
        "账号填对应销售名 → 职位模板勾行键 → 例外/残留项目逐条挂 viewer。"
    )
    return "\n".join(lines) + "\n"


def load_projects(db) -> list[dict]:
    rows = db.execute(
        select(
            MaintenanceProject.project_id,
            MaintenanceProject.project_code,
            MaintenanceProject.display_name,
            MaintenanceProject.salesperson,
            MaintenanceProject.cmo_name,
            MaintenanceProject.salesperson_override_active,
            MaintenanceProject.is_active,
        ).order_by(MaintenanceProject.project_code)
    ).all()
    return [dict(row._mapping) for row in rows]


def db_identity_line(db) -> str:
    row = db.execute(
        select(
            text("current_database()"),
            text("current_user"),
            text("coalesce(inet_server_addr()::text, 'local-socket')"),
            text("inet_server_port()"),
        )
    ).one()
    return f"db={row[0]} user={row[1]} server={row[2]}:{row[3]}"


def main() -> int:
    global _current_max_rows
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", help="报告输出文件路径（缺省打印到 stdout）")
    parser.add_argument(
        "--roster", help="名单 JSON 路径，结构同内置 DEFAULT_ROSTER，覆盖默认名单"
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=DEFAULT_MAX_ROWS,
        help=f"每张表最多展示行数（默认 {DEFAULT_MAX_ROWS}，超出截断并标注）",
    )
    args = parser.parse_args()
    _current_max_rows = max(1, args.max_rows)

    roster = DEFAULT_ROSTER
    if args.roster:
        roster = json.loads(Path(args.roster).read_text(encoding="utf-8"))

    db = SessionLocal()
    try:
        # 硬保险：本事务钉死只读——首条语句即 SET TRANSACTION READ ONLY，
        # 之后任何写语句都会被 PostgreSQL 拒绝（错误），而不是悄悄生效。
        db.execute(text("SET TRANSACTION READ ONLY"))
        identity = db_identity_line(db)
        try:
            projects = load_projects(db)
        except Exception as exc:
            import psycopg.errors

            if isinstance(getattr(exc, "orig", None), psycopg.errors.UndefinedTable):
                print(
                    f"错误：{identity} 上没有 maintenance_project 表——"
                    "多半连到了空库/错误库，拒绝生成报告。",
                    file=sys.stderr,
                )
                return 2
            raise
    finally:
        db.close()

    report = build_report(projects, roster, db_identity=identity)
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
        print(f"报告已写入 {args.out}（{identity}，项目 {len(projects)} 个）")
    else:
        sys.stdout.write(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

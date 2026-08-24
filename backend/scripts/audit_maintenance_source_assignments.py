"""只读扫描 WBDD 活跃项目归属与来源项目名的冲突候选。

用途：给人工复核提供候选面，不执行任何改挂。``project_std`` 只是统一 WBDD
自报线索，不足以单独证明当前归属错误；正式更正必须走项目内人工总表回传或既有
来源单归属确认接口。

运行示例：
    uv run python scripts/audit_maintenance_source_assignments.py \
      --order-no WBDD-20260612-0018 --limit 10
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from typing import Any

from sqlalchemy import text

from app.db import SessionLocal


_BASE_CTE = """
WITH active_assignment AS (
    SELECT
        o.raw_order_id AS source_order_id,
        o.order_no,
        o.linked_sales_order_no,
        NULLIF(BTRIM(o.project_std), '') AS project_std,
        LOWER(NULLIF(BTRIM(o.project_std), '')) AS project_key,
        o.maint_start,
        o.maint_end,
        a.assignment_id,
        a.version AS assignment_version,
        a.created_by,
        a.project_id AS current_project_id,
        current_project.display_name AS current_project_name,
        current_project.period_from AS current_period_from,
        current_project.period_to AS current_period_to
    FROM f_maintenance_order AS o
    JOIN maintenance_source_order_assignment AS a
      ON a.source_order_id = o.raw_order_id
     AND a.is_active IS TRUE
    JOIN maintenance_project AS current_project
      ON current_project.project_id = a.project_id
), project_keys AS (
    SELECT project_id, LOWER(BTRIM(display_name)) AS project_key
    FROM maintenance_project
    WHERE is_active IS TRUE
    UNION
    SELECT project_id, LOWER(BTRIM(project_code)) AS project_key
    FROM maintenance_project
    WHERE is_active IS TRUE
), exact_target_counts AS (
    SELECT
        source.source_order_id,
        COUNT(DISTINCT candidate.project_id) AS exact_target_count,
        MIN(candidate.project_id) AS exact_target_project_id
    FROM active_assignment AS source
    LEFT JOIN project_keys AS candidate
      ON candidate.project_key = source.project_key
    GROUP BY source.source_order_id
), classified AS (
    SELECT
        source.*,
        counts.exact_target_count,
        target.project_id AS target_project_id,
        target.display_name AS target_project_name,
        target.period_from AS target_period_from,
        target.period_to AS target_period_to
    FROM active_assignment AS source
    JOIN exact_target_counts AS counts
      ON counts.source_order_id = source.source_order_id
    LEFT JOIN maintenance_project AS target
      ON target.project_id = counts.exact_target_project_id
), conflicts AS (
    SELECT *
    FROM classified
    WHERE exact_target_count = 1
      AND target_project_id <> current_project_id
)
"""


def _row_dicts(result) -> list[dict[str, Any]]:
    return [dict(row._mapping) for row in result]


def audit(*, order_no: str | None, limit: int) -> dict[str, Any]:
    with SessionLocal() as db:
        # 守门：脚本即便误连生产，也只能开启只读事务；后续任何写 SQL 会被数据库拒绝。
        db.execute(text("SET TRANSACTION READ ONLY"))
        read_only = db.execute(text("SHOW transaction_read_only")).scalar_one()
        if read_only != "on":
            raise RuntimeError("database transaction is not read only")

        # 一次取回约两万张活跃单，在 Python 内完成分类；避免把同一 CTE 连续跑五遍，
        # 也避免 GROUP BY 与明细 join 在生产库制造临时文件。
        classified = _row_dicts(db.execute(text(
            _BASE_CTE + "SELECT * FROM classified ORDER BY source_order_id"
        )))
        line_counts = dict(db.execute(text("""
            SELECT orders.raw_order_id, COUNT(lines.id) AS active_lines
            FROM f_maintenance_order AS orders
            LEFT JOIN f_maintenance_line AS lines
              ON lines.order_id = orders.id
             AND lines.is_active IS TRUE
            GROUP BY orders.raw_order_id
        """)).all())

        conflicts = [
            row for row in classified
            if row["exact_target_count"] == 1
            and row["target_project_id"] != row["current_project_id"]
        ]
        summary = {
            "active_assignments": len(classified),
            "source_name_missing": sum(row["project_std"] is None for row in classified),
            "exact_name_consistent": sum(
                row["exact_target_count"] == 1
                and row["target_project_id"] == row["current_project_id"]
                for row in classified
            ),
            "exact_name_conflicts": len(conflicts),
            "exact_name_ambiguous": sum(
                row["exact_target_count"] > 1 for row in classified),
            "no_exact_target": sum(
                row["exact_target_count"] == 0 for row in classified),
        }

        def period_exact(row: dict[str, Any], prefix: str) -> bool:
            period_from = row[f"{prefix}_period_from"]
            period_to = row[f"{prefix}_period_to"]
            return (
                row["maint_start"] is not None
                and row["maint_end"] is not None
                and period_from is not None
                and period_to is not None
                and row["maint_start"] == period_from
                and row["maint_end"] == period_to
            )

        def period_overlaps(row: dict[str, Any], prefix: str) -> bool:
            period_from = row[f"{prefix}_period_from"]
            period_to = row[f"{prefix}_period_to"]
            return (
                row["maint_start"] is not None
                and row["maint_end"] is not None
                and period_from is not None
                and period_to is not None
                and row["maint_start"] <= period_to
                and row["maint_end"] >= period_from
            )

        routes = Counter(
            (row["current_project_name"], row["target_project_name"])
            for row in conflicts
        )
        route_lines: Counter[tuple[str, str]] = Counter()
        for row in conflicts:
            route = (row["current_project_name"], row["target_project_name"])
            route_lines[route] += int(line_counts.get(row["source_order_id"], 0))
        ordered_routes = sorted(
            routes,
            key=lambda route: (-routes[route], -route_lines[route], route[0], route[1]),
        )[:limit]
        top_routes = [{
            "current_project_name": route[0],
            "target_project_name": route[1],
            "affected_orders": routes[route],
            "affected_active_lines": route_lines[route],
        } for route in ordered_routes]

        creator_counts = Counter(row["created_by"] for row in conflicts)
        creators = [{"created_by": creator, "affected_orders": count}
                    for creator, count in sorted(
                        creator_counts.items(), key=lambda item: (-item[1], item[0]))]

        xsdd_projects: dict[str, set[str]] = defaultdict(set)
        xsdd_orders: Counter[str] = Counter()
        for row in classified:
            if row["linked_sales_order_no"]:
                xsdd = row["linked_sales_order_no"]
                xsdd_projects[xsdd].add(row["current_project_id"])
                xsdd_orders[xsdd] += 1
        split_xsdd = [xsdd for xsdd, projects in xsdd_projects.items()
                      if len(projects) > 1]
        xsdd_multi_project = {
            "xsdd_keys": len(split_xsdd),
            "affected_orders": sum(xsdd_orders[xsdd] for xsdd in split_xsdd),
        }

        conflict_profile = {
            "affected_orders": len(conflicts),
            "affected_active_lines": sum(
                int(line_counts.get(row["source_order_id"], 0)) for row in conflicts),
            "affected_xsdd": len({row["linked_sales_order_no"] for row in conflicts
                                  if row["linked_sales_order_no"]}),
            "affected_source_project_names": len({row["project_std"] for row in conflicts
                                                  if row["project_std"]}),
            "assignment_routes": len(routes),
            "order_period_exact_target": sum(
                period_exact(row, "target") for row in conflicts),
            "order_period_exact_current": sum(
                period_exact(row, "current") for row in conflicts),
            "order_period_overlaps_target": sum(
                period_overlaps(row, "target") for row in conflicts),
            "order_period_overlaps_current": sum(
                period_overlaps(row, "current") for row in conflicts),
            "invalid_order_periods": sum(
                row["maint_start"] is not None
                and row["maint_end"] is not None
                and row["maint_start"] > row["maint_end"]
                for row in conflicts),
        }

        selected_fields = (
            "source_order_id", "order_no", "linked_sales_order_no", "project_std",
            "assignment_id", "assignment_version", "created_by",
            "current_project_id", "current_project_name", "exact_target_count",
            "target_project_id", "target_project_name",
        )
        selected_order = [
            {field: row[field] for field in selected_fields}
            for row in classified if order_no and row["order_no"] == order_no
        ]

        db.rollback()
        return {
            "classification": (
                "project_std 精确唯一命中另一个活动项目的只读冲突候选；"
                "候选不等于已确认错误，禁止据此批量改挂"
            ),
            "transaction_read_only": read_only,
            "summary": summary,
            "conflict_profile": conflict_profile,
            "conflict_assignment_creators": creators,
            "xsdd_assigned_to_multiple_projects": xsdd_multi_project,
            "top_conflict_routes": top_routes,
            "selected_order": selected_order,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--order-no", help="同时展开指定 WBDD 单号的活跃归属")
    parser.add_argument("--limit", type=int, default=10, help="最多列出的冲突路线数")
    args = parser.parse_args()
    if args.limit < 1 or args.limit > 100:
        parser.error("--limit 必须在 1..100")
    print(json.dumps(
        audit(order_no=args.order_no, limit=args.limit),
        ensure_ascii=False,
        indent=2,
        default=str,
    ))


if __name__ == "__main__":
    main()

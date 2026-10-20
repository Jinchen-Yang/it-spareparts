"""Read-only D-32 coverage, using the authoritative live/frozen requirement view."""

from collections import defaultdict
from decimal import Decimal
from pathlib import Path
import subprocess
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.models.dimensions import DimPart
from app.models.maintenance_bad_return import MaintenanceReturnObligation
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_project_operations import (
    MaintenanceSiteIssue,
    MaintenanceSiteIssueLine,
)
from app.models.master_data import ProductCategory
from app.services.maintenance_site_return_requirements import line_details


def _tristate(value: bool | None) -> str:
    return "null" if value is None else "true" if value else "false"


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {"line_count": len(rows)}
    for field, output in (
        ("no_return", "raw_no_return_counts"),
        ("effective_no_return", "effective_no_return_counts"),
    ):
        counts = {"true": 0, "false": 0, "null": 0}
        for row in rows:
            counts[_tristate(row[field])] += 1
        summary[output] = counts
        summary[output.replace("_counts", "_project_counts")] = {
            state: len({row["project_id"] for row in rows if _tristate(row[field]) == state})
            for state in counts
        }
    for field in ("quantity", "required_quantity", "exempt_quantity", "pending_quantity"):
        summary[field] = format(sum((Decimal(row[field]) for row in rows), Decimal(0)), ".3f")
    summary["standard_category_counts"] = {
        "matched": sum(row["standard_category_id"] is not None for row in rows),
        "missing": sum(row["standard_category_id"] is None for row in rows),
    }
    summary["category_major_mismatch_count"] = sum(row["category_major_mismatch"] for row in rows)
    summary["requirement_status_counts"] = {
        state: sum(row["requirement_status"] == state for row in rows)
        for state in ("required", "exempt", "pending_category")
    }
    frozen = [row for row in rows if row["requirement_origin"] == "frozen_obligation"]
    summary["frozen_obligation_count"] = len(frozen)
    summary["frozen_obligation_classification_counts"] = {
        state: sum(row["requirement_status"] == state for row in frozen)
        for state in ("required", "exempt", "pending_category")
    }
    summary["frozen_obligation_source_counts"] = {
        source: sum((row["exemption_source"] or "null") == source for row in frozen)
        for source in sorted({row["exemption_source"] or "null" for row in frozen})
    }
    for category_field, output in (
        ("standard_category_major", "standard_hdd_no_return_counts"),
        ("text_category_major", "text_hdd_no_return_counts"),
    ):
        summary[output] = {
            state: sum(row[category_field] == "硬盘" and _tristate(row["no_return"]) == state
                       for row in rows)
            for state in ("true", "false", "null")
        }
    hdd = summary["standard_hdd_no_return_counts"]
    summary["standard_hdd_candidates"] = {
        "explicit_required_count": hdd["false"],
        "inherited_count": hdd["null"],
        "mixed_flags": sum(value > 0 for value in hdd.values()) > 1,
        "all_explicit_required": bool(hdd["false"] and not hdd["true"] and not hdd["null"]),
    }
    return summary


def _grouped_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_source: dict[str, list] = defaultdict(list)
    by_category: dict[tuple, list] = defaultdict(list)
    for row in rows:
        by_source[row["source"]].append(row)
        by_category[(
            row["standard_category_id"], row["standard_category_major"], row["text_category_major"],
        )].append(row)
    return {
        **_summarize(rows),
        "by_source": {source: _summarize(group) for source, group in sorted(by_source.items())},
        "category_major_cross_counts": [
            {
                "standard_category_id": category_id,
                "standard_category_major": standard_major,
                "text_category_major": text_major,
                **_summarize(group),
            }
            for (category_id, standard_major, text_major), group
            in sorted(by_category.items(), key=lambda item: str(item[0]))
        ],
    }


def _code_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[3],
            text=True, capture_output=True, check=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return result.stdout.strip()


def build_coverage_report(
    engine: Engine, *, project_ids: tuple[str, ...] = (),
    environment_label: str = "unspecified",
) -> dict[str, Any]:
    """Own a fresh PostgreSQL snapshot; never reuse a caller's writable session.

    Effective rows mean confirmed/corrected, non-voided headers and active lines.
    An inactive project remains visible with its status, for historical review.
    """
    if engine.dialect.name != "postgresql":
        raise ValueError("覆盖报告必须使用 PostgreSQL 只读一致性事务")
    with engine.connect() as connection, connection.begin():
        connection.exec_driver_sql(
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
        )
        metadata = {
            "schema": "no-return-coverage-v1",
            "environment_label": environment_label,
            "as_of": connection.scalar(text("SELECT CURRENT_TIMESTAMP")).isoformat(),
            "code_sha": _code_sha(),
            "transaction_read_only": connection.scalar(text("SHOW transaction_read_only")) == "on",
            "transaction_isolation": connection.scalar(text("SHOW transaction_isolation")),
            "filters": {
                "project_ids": sorted(set(project_ids)),
                "issue_statuses": ["confirmed", "corrected"],
                "voided_at": "null",
                "line_is_active": True,
                "project_is_active": "all; reported separately",
            },
            "historical_comparison": (
                "2026-10-15 历史核对按 dim_part.category_major 文字识别硬盘；"
                "本报告另列 ProductCategory 标准分类，不将两者混用。"
                "核对候选不是已批准更正项。"
            ),
        }
        with Session(bind=connection, autoflush=False) as db:
            project_query = select(MaintenanceProject).order_by(MaintenanceProject.project_id)
            if project_ids:
                project_query = project_query.where(MaintenanceProject.project_id.in_(project_ids))
            projects = list(db.scalars(project_query))
            selected = {project.project_id: project for project in projects}
            query = (
                select(MaintenanceSiteIssue, MaintenanceSiteIssueLine, DimPart, ProductCategory)
                .join(MaintenanceSiteIssueLine, MaintenanceSiteIssueLine.issue_id == MaintenanceSiteIssue.issue_id)
                .join(DimPart, DimPart.id == MaintenanceSiteIssueLine.part_id)
                .outerjoin(ProductCategory, ProductCategory.id == DimPart.category_id)
                .where(
                    MaintenanceSiteIssue.normalized_status.in_(("confirmed", "corrected")),
                    MaintenanceSiteIssue.voided_at.is_(None),
                    MaintenanceSiteIssueLine.is_active.is_(True),
                    MaintenanceSiteIssue.project_id.in_(selected),
                )
                .order_by(MaintenanceSiteIssue.project_id, MaintenanceSiteIssue.issue_id,
                          MaintenanceSiteIssueLine.line_no, MaintenanceSiteIssueLine.issue_line_id)
            )
            by_project: dict[str, list] = defaultdict(list)
            for issue, line, part, category in db.execute(query):
                by_project[issue.project_id].append((issue, line, part, category))
            delivery_line_ids = [
                line.issue_line_id
                for facts in by_project.values()
                for _issue, line, _part, _category in facts if line.delivery_line_id
            ]
            obligations = {
                row.issue_line_id: row for row in db.scalars(
                    select(MaintenanceReturnObligation).where(
                        MaintenanceReturnObligation.issue_line_id.in_(delivery_line_ids),
                        MaintenanceReturnObligation.is_active.is_(True),
                    )
                )
            } if delivery_line_ids else {}
            rows = []
            summaries = []
            for project in projects:
                facts = by_project[project.project_id]
                requirements = line_details(db, [item[1] for item in facts], project=project)
                project_rows = []
                for issue, line, part, category in facts:
                    requirement = requirements[line.issue_line_id]["return_requirement"]
                    status = requirement["requirement_status"]
                    obligation = obligations.get(line.issue_line_id)
                    row = {
                        "project_id": project.project_id,
                        "project_code": project.project_code,
                        "project_name": project.display_name,
                        "project_is_active": project.is_active,
                        "project_no_return_default": project.no_return_default,
                        "issue_id": issue.issue_id,
                        "issue_no": issue.issue_no,
                        "issue_display_no": issue.display_no_effective,
                        "issue_line_id": line.issue_line_id,
                        "delivery_line_id": line.delivery_line_id,
                        "line_no": line.line_no,
                        "source": issue.source,
                        "expected_issue_version": issue.version,
                        "expected_line_version": line.version,
                        "part_id": part.id,
                        "pn": line.pn,
                        "no_return": line.no_return,
                        "effective_no_return": {"exempt": True, "required": False, "pending_category": None}[status],
                        "requirement_status": status,
                        "requirement_origin": "frozen_obligation" if obligation else "live",
                        "requirement_basis": requirement["basis"],
                        "legacy_obligation_id": obligation.obligation_id if obligation else None,
                        "legacy_obligation_version": obligation.version if obligation else None,
                        "legacy_source_quantity": format(obligation.source_quantity, ".3f") if obligation else None,
                        "exemption_source": requirement["basis"]["exemption_source"],
                        "standard_category_id": category.id if category else None,
                        "standard_category_major": category.category_major if category else None,
                        "standard_category_minor": category.category_minor if category else None,
                        "text_category_major": part.category_major,
                        "text_category_minor": part.category_minor,
                        "category_major_mismatch": bool(category and category.category_major != part.category_major),
                        "quantity": format(line.quantity, ".3f"),
                        "required_quantity": requirement["required_quantity"],
                        "exempt_quantity": requirement["exempt_quantity"],
                        "pending_quantity": requirement["pending_quantity"],
                    }
                    project_rows.append(row)
                rows.extend(project_rows)
                summaries.append({
                    "project_id": project.project_id, "project_code": project.project_code,
                    "project_name": project.display_name, "project_is_active": project.is_active,
                    "project_no_return_default": project.no_return_default,
                    **_grouped_summary(project_rows),
                })
        summary = {
            **_grouped_summary(rows),
            "project_count": len(projects),
            "project_default_counts": {
                "true": sum(project.no_return_default is True for project in projects),
                "false": sum(project.no_return_default is False for project in projects),
            },
        }
        return {"metadata": metadata, "summary": summary, "projects": summaries, "rows": rows}

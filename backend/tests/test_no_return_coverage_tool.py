"""D-32 approval-list coverage CLI, backed by isolated real PostgreSQL."""

import csv
from datetime import UTC, datetime
from decimal import Decimal
import io
import json
from uuid import uuid4

from sqlalchemy import event, inspect, text

from app.db import engine
from app.models.maintenance_bad_return import MaintenanceReturnObligation
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_project_operations import (
    MaintenanceSiteIssue,
    MaintenanceSiteIssueLine,
    MaintenanceSiteIssueReturnEvent,
)
from app.models.master_data import ProductCategory
from tests.test_maintenance_site_issue_facts import _facts
from tests.test_site_issue_v2_api import _delivery_source


def _database_snapshot():
    """Include command/audit/revision tables as well as business facts."""
    with engine.connect() as connection:
        return {
            name: sorted(connection.scalars(text(
                f'SELECT to_jsonb(snapshot_row)::text FROM "{name}" snapshot_row'
            )))
            for name in inspect(connection).get_table_names()
        }


def test_coverage_cli_reports_stable_ids_and_tristates_without_any_write(db, capsys):
    from scripts import audit_no_return_coverage

    project, issue, lines, part = _facts(db)
    category = ProductCategory(category_major="硬盘", category_minor="企业硬盘")
    db.add(category)
    db.flush()
    part.category_id = category.id
    part.category_major = "存储设备"
    lines[0].no_return = False
    db.commit()
    project_id, issue_id = project.project_id, issue.issue_id
    line_id = lines[0].issue_line_id
    before = _database_snapshot()
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        assert audit_no_return_coverage.main([
            "--project-id", project_id, "--environment-label", "isolated-itsp4b-test",
        ]) == 0
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    report = json.loads(capsys.readouterr().out)
    assert _database_snapshot() == before
    assert all(sql.lstrip().split()[0].upper() in {"SELECT", "SHOW", "SET"}
               for sql in statements), statements
    metadata = report["metadata"]
    assert metadata["environment_label"] == "isolated-itsp4b-test"
    assert metadata["transaction_read_only"] is True
    assert metadata["transaction_isolation"] == "repeatable read"
    assert metadata["as_of"] and metadata["code_sha"]
    row = next(row for row in report["rows"] if row["issue_line_id"] == line_id)
    assert (row["project_id"], row["issue_id"], row["source"]) == (
        project_id, issue_id, "page_manual",
    )
    assert (row["expected_issue_version"], row["expected_line_version"]) == (1, 1)
    assert row["no_return"] is False and row["effective_no_return"] is False
    assert row["standard_category_major"] == "硬盘"
    assert row["text_category_major"] == "存储设备"
    assert row["category_major_mismatch"] is True
    summary = report["projects"][0]
    assert summary["raw_no_return_counts"] == {"true": 0, "false": 1, "null": 1}
    assert summary["effective_no_return_counts"] == {"true": 1, "false": 1, "null": 0}
    assert summary["required_quantity"] == "2.000"
    assert summary["exempt_quantity"] == "2.000"


def test_coverage_counts_sources_categories_and_projects_without_guessing_from_text(db, capsys):
    from scripts import audit_no_return_coverage

    first, first_issue, first_lines, first_part = _facts(db, source="legacy")
    first_lines[0].no_return = False
    first_part.category_major = "硬盘"
    db.add(MaintenanceSiteIssueLine(
        issue_line_id=str(uuid4()), issue_id=first_issue.issue_id, line_no=3,
        part_id=first_part.id, pn=first_part.pn_std, quantity=Decimal("3"),
        no_return=True, algorithm_version="coverage-test", is_active=True, version=1,
    ))
    second, _issue, second_lines, second_part = _facts(db, source="workbook")
    category = ProductCategory(category_major="网络设备", category_minor="交换机")
    db.add(category)
    db.flush()
    second_part.category_id = category.id
    second_part.category_major = "网络设备"
    second_lines[0].no_return = True
    db.add(MaintenanceProject(
        project_id=str(uuid4()), project_code="COVERAGE-EMPTY", display_name="无领用项目",
        lifecycle_status="ongoing", no_return_default=True,
    ))
    db.commit()
    before = _database_snapshot()
    assert audit_no_return_coverage.main([]) == 0
    report = json.loads(capsys.readouterr().out)
    assert _database_snapshot() == before
    summary = report["summary"]
    assert summary["line_count"] == 5 and summary["project_count"] == 3
    assert summary["raw_no_return_counts"] == {"true": 2, "false": 1, "null": 2}
    assert summary["effective_no_return_counts"] == {"true": 2, "false": 2, "null": 1}
    assert summary["raw_no_return_project_counts"] == {"true": 2, "false": 1, "null": 2}
    assert summary["project_default_counts"] == {"true": 1, "false": 2}
    assert summary["standard_category_counts"] == {"matched": 2, "missing": 3}
    assert summary["by_source"]["legacy"]["line_count"] == 3
    assert summary["by_source"]["workbook"]["line_count"] == 2
    text_hdd = next(item for item in report["projects"] if item["project_id"] == first.project_id)
    assert text_hdd["standard_hdd_no_return_counts"] == {"true": 0, "false": 0, "null": 0}
    assert text_hdd["text_hdd_no_return_counts"] == {"true": 1, "false": 1, "null": 1}
    assert {row["requirement_origin"] for row in report["rows"]} == {"live"}
    assert {item["project_id"] for item in report["projects"]} >= {first.project_id, second.project_id}


def test_coverage_csv_filters_effective_rows_but_keeps_inactive_project_visible(db, capsys):
    from scripts import audit_no_return_coverage

    project, issue, lines, part = _facts(db)
    project.is_active = False
    project.display_name = "=COVERAGE"
    issue.normalized_status = "corrected"
    lines[0].no_return = False
    lines[1].is_active = False
    for status, voided in (("draft", False), ("void", False), ("confirmed", True)):
        hidden = MaintenanceSiteIssue(
            issue_id=str(uuid4()), project_id=project.project_id, issue_no=f"HIDDEN-{uuid4()}",
            issue_date=issue.issue_date, raw_status=status, normalized_status=status,
            status_mapping_state="mapped", status_mapping_version="coverage-test",
            source="page_manual", version=1,
            voided_at=datetime.now(UTC) if voided else None,
        )
        db.add(hidden)
        db.flush()
        db.add(MaintenanceSiteIssueLine(
            issue_line_id=str(uuid4()), issue_id=hidden.issue_id, line_no=1,
            part_id=part.id, pn=part.pn_std, quantity=Decimal("9"),
            no_return=True, algorithm_version="coverage-test", is_active=True, version=1,
        ))
    db.commit()
    _facts(db)  # Excluded by the explicit stable project filter.
    project_id, line_id = project.project_id, lines[0].issue_line_id
    before = _database_snapshot()
    assert audit_no_return_coverage.main([
        "--project-id", project_id, "--format", "csv", "--environment-label", "test",
    ]) == 0
    records = list(csv.DictReader(io.StringIO(capsys.readouterr().out)))
    assert _database_snapshot() == before
    assert {row["record_type"] for row in records} == {"metadata", "summary", "project", "line"}
    visible = [row for row in records if row["record_type"] == "line"]
    assert len(visible) == 1
    assert visible[0]["issue_line_id"] == line_id
    assert visible[0]["no_return"] == "false"
    assert visible[0]["project_is_active"] == "false"
    assert visible[0]["project_name"] == "'=COVERAGE"


def test_coverage_preserves_active_legacy_obligation_classification_and_quantity(db, capsys):
    from scripts import audit_no_return_coverage

    project, issue, lines, part = _facts(db, source="site_issue_v2")
    delivery = _delivery_source(db, project=project, delivery_line_id=str(uuid4()))
    category = ProductCategory(category_major="硬盘", category_minor="企业硬盘")
    db.add(category)
    db.flush()
    part.category_id = category.id
    line = lines[0]
    line.delivery_line_id = delivery.delivery_line_id
    line.quantity = Decimal("9")
    line.no_return = True
    event_row = MaintenanceSiteIssueReturnEvent(
        event_id=str(uuid4()), project_id=project.project_id, issue_id=issue.issue_id,
        issue_version=1, event_type="return_obligation_created", payload={"schema_version": "test"},
    )
    db.add(event_row)
    db.flush()
    obligation = MaintenanceReturnObligation(
        obligation_id=str(uuid4()), project_id=project.project_id, issue_id=issue.issue_id,
        issue_line_id=line.issue_line_id, delivery_line_id=delivery.delivery_line_id,
        part_id=part.id, pn=part.pn_std, source_quantity=Decimal("2"),
        required_quantity=Decimal("2"), classification="required", exemption_source="none",
        category_id_snapshot=category.id, category_major_snapshot="网络设备",
        category_minor_snapshot="交换机", rule_version="frozen-test-v1", source_issue_version=1,
        last_source_event_id=event_row.event_id, is_active=True, version=1,
    )
    db.add(obligation)
    db.commit()
    line_id, obligation_id = line.issue_line_id, obligation.obligation_id
    before = _database_snapshot()
    assert audit_no_return_coverage.main(["--project-id", project.project_id]) == 0
    report = json.loads(capsys.readouterr().out)
    assert _database_snapshot() == before
    row = next(row for row in report["rows"] if row["issue_line_id"] == line_id)
    assert row["no_return"] is True and row["effective_no_return"] is False
    assert row["quantity"] == "9.000" and row["required_quantity"] == "2.000"
    assert row["standard_category_major"] == "硬盘"
    assert row["requirement_origin"] == "frozen_obligation"
    assert row["legacy_obligation_id"] == obligation_id
    assert row["requirement_basis"]["category_major_snapshot"] == "网络设备"
    assert report["summary"]["frozen_obligation_count"] == 1

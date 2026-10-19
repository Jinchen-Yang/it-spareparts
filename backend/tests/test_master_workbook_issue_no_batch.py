"""06 同号合单、合法互换及事务失败的公开 HTTP / 服务契约。"""

import io
import uuid
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from openpyxl import load_workbook
from sqlalchemy import func, insert, select
from sqlalchemy.orm import Session

from app.api import maintenance_project_master_workbook as workbook_api
from app.models.maintenance_project_operations import (
    MaintenanceProjectOperationAudit,
    MaintenanceProjectWorkbookOperation,
    MaintenanceSiteIssue,
    MaintenanceSiteIssueLine,
    MaintenanceSiteIssueReturnEvent,
)
from app.services import maintenance_project_master_workbook as master
from app.services import maintenance_project_operations as operations
from tests.test_maintenance_expense_collection_workbook import _XLSX, uploader
from tests.test_maintenance_project_master_v2_editable import (
    _append_site_line,
    _make_project_with_line,
    _save,
    _site_issue,
)


@pytest.fixture(autouse=True)
def _enable_v2(monkeypatch):
    monkeypatch.setattr(
        workbook_api.get_settings(), "maintenance_project_master_v2_enabled", True,
    )


def _seed(db, numbers=("ISS-A", "ISS-B")):
    project, part, *_ = _make_project_with_line(db)
    issues = [_site_issue(db, project, part, issue_no=number) for number in numbers]
    book, sheet, columns, rows = _export(db, project)
    return project, part, issues, book, sheet, columns, rows


def _export(db, project):
    book = load_workbook(io.BytesIO(master.build_project_master_v2(
        db, project_id=project.project_id, sheets=(master.V2_SHEET_SITE,),
    )))
    sheet = book[master.V2_SHEET_SITE]
    columns = {cell.value: cell.column for cell in sheet[1]}
    rows = {
        sheet.cell(row, columns["实体ID"]).value: row
        for row in range(2, sheet.max_row + 1)
        if sheet.cell(row, columns["实体ID"]).value
    }
    db.commit()
    return book, sheet, columns, rows


def _post(client, project, book, action, *, token=None, content=None):
    return client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/master-workbook/{action}",
        files={"file": ("synthetic-issue-numbers.xlsx", content or _save(book), _XLSX)},
        data={"site_issue_merge_token": token} if token else {},
    )


def _validate(db, project, book):
    return master.validate_project_master_v2(
        db, project_id=project.project_id, data=_save(book),
    )


def _apply(db, plan):
    return master.apply_project_master_v2(
        db, plan, operated_by="synthetic-number-editor",
        import_batch_id=str(uuid.uuid4()),
    )


def _append_new(sheet, part, *, number=None, serial="SYNTHETIC-NEW", address=None,
                issue_date=date(2026, 8, 10)):
    values = {
        "领用单号": number, "领用日期": issue_date,
        "PN": part.pn_std, "SN": serial, "领用数量": 1,
        "是否应返还": "是", "领用地址": address,
    }
    sheet.append([values.get(cell.value) for cell in sheet[1]])
    return sheet.max_row


def _snapshot(db, project_id):
    db.expire_all()
    return {
        "headers": list(db.execute(select(
            MaintenanceSiteIssue.issue_id, MaintenanceSiteIssue.issue_no,
            MaintenanceSiteIssue.issue_date, MaintenanceSiteIssue.site_location,
            MaintenanceSiteIssue.version, MaintenanceSiteIssue.normalized_status,
        ).where(MaintenanceSiteIssue.project_id == project_id).order_by(
            MaintenanceSiteIssue.issue_id,
        ))),
        "lines": list(db.execute(select(
            MaintenanceSiteIssueLine.issue_line_id, MaintenanceSiteIssueLine.issue_id,
            MaintenanceSiteIssueLine.quantity, MaintenanceSiteIssueLine.version,
            MaintenanceSiteIssueLine.is_active, MaintenanceSiteIssueLine.line_no,
        ).join(MaintenanceSiteIssue).where(
            MaintenanceSiteIssue.project_id == project_id,
        ).order_by(MaintenanceSiteIssueLine.issue_line_id))),
        "revision": operations.get_or_create_workbook_state(
            db, project_id=project_id,
        ).revision,
        "counts": tuple(db.scalar(select(func.count()).select_from(model)) for model in (
            MaintenanceProjectOperationAudit, MaintenanceProjectWorkbookOperation,
            MaintenanceSiteIssueReturnEvent,
        )),
    }


def _assert_duplicate(detail, target, row_numbers):
    assert detail["code"] == "duplicate_issue_no"
    assert target in detail["message"]
    assert "06_领用返还" in detail["message"]
    assert all(str(row) in detail["message"] for row in row_numbers)
    assert sorted(row["row_no"] for row in detail["rows"]) == sorted(row_numbers)


@pytest.mark.parametrize("count", [2, 3])
def test_prod_signed_issues_with_one_final_number_merge_instead_of_failing(db, count):
    """同号为显式合单意图；保留全部稳定明细 ID，并在提交前预览。"""
    project, _, issues, book, sheet, columns, rows = _seed(
        db, tuple(f"ISS-{number}" for number in range(count)),
    )
    target = "WBDD-20261013-0005"
    survivor = min((issue for issue, _ in issues), key=lambda issue: (issue.created_at, issue.issue_id))
    survivor_id = survivor.issue_id
    source_ids = {issue.issue_id for issue, _ in issues}
    original_numbers = {issue.issue_id: issue.issue_no for issue, _ in issues}
    original_versions = {issue.issue_id: issue.version for issue, _ in issues}
    original_line_versions = {line.issue_line_id: line.version for _, line in issues}
    line_ids = {line.issue_line_id for _, line in issues}
    row_numbers = sorted(rows[line.issue_line_id] for _, line in issues)
    for row in row_numbers:
        sheet.cell(row, columns["领用单号"], target)
    client = uploader(db, username="synthetic-duplicate-numbers")
    content = _save(book)
    validated = _post(client, project, book, "validate", content=content)
    assert validated.status_code == 200, validated.text
    previews = validated.json()["site_issue_merges"]
    assert len(previews) == 1
    preview = previews[0]
    assert preview["target_issue_no"] == target
    assert preview["survivor_issue_id"] == survivor_id
    assert preview["total_line_count"] == count
    assert sorted(preview["rows"]) == row_numbers
    assert {source["issue_id"] for source in preview["sources"]} == source_ids
    for source in preview["sources"]:
        issue, line = next(pair for pair in issues if pair[0].issue_id == source["issue_id"])
        assert source["issue_no"] == issue.issue_no
        assert source["line_count"] == 1
        assert source["rows"] == [rows[line.issue_line_id]]
    assert preview["header_conflicts"] == []
    plan = _validate(db, project, book)
    assert plan.summary["site_issue_merges"] == 1
    assert validated.json()["summary"]["site_issue_merges"] == 1
    token = validated.json()["site_issue_merge_token"]
    applied = _post(client, project, book, "apply", token=token, content=content)
    assert applied.status_code == 200, applied.text
    db.expire_all()
    assert survivor.issue_no == target
    for issue, line in issues:
        assert line.issue_id == survivor_id
        assert line.is_active
        assert issue.version > original_versions[issue.issue_id]
        if issue.issue_id != survivor_id:
            assert issue.normalized_status == "void"
            assert issue.issue_no != target
            assert line.version > original_line_versions[line.issue_line_id]
        assert not issue.issue_no.startswith("WB-TEMP-")
    final_lines = list(db.scalars(select(MaintenanceSiteIssueLine).where(
        MaintenanceSiteIssueLine.issue_id == survivor_id,
    ).order_by(MaintenanceSiteIssueLine.line_no)))
    assert {line.issue_line_id for line in final_lines} == line_ids
    assert [line.line_no for line in final_lines] == list(range(1, count + 1))
    audit = db.scalar(select(MaintenanceProjectOperationAudit).where(
        MaintenanceProjectOperationAudit.entity_type == "site_issue",
        MaintenanceProjectOperationAudit.action == "MERGE",
    ))
    assert audit is not None
    assert audit.after_json["survivor_issue_id"] == survivor_id
    assert set(audit.after_json["source_issue_ids"]) == source_ids
    assert audit.after_json["original_issue_nos"] == original_numbers
    assert audit.after_json["moved_line_count"] == count - 1
    assert set(audit.after_json["moved_line_ids"]) == {
        line.issue_line_id for issue, line in issues if issue.issue_id != survivor_id
    }
    assert set(db.scalars(select(MaintenanceProjectOperationAudit.entity_id).where(
        MaintenanceProjectOperationAudit.entity_type == "site_issue",
        MaintenanceProjectOperationAudit.action == "MERGE",
    ))) == source_ids
    before_replay = _snapshot(db, project.project_id)
    db.commit()
    replayed = _post(client, project, book, "apply", token=token, content=content)
    assert replayed.status_code == 200, replayed.text
    assert replayed.json()["replayed"] is True
    assert _snapshot(db, project.project_id) == before_replay
    # Export holds workbook/project row locks; release its own transaction
    # before sending the next HTTP request on a different Session.
    clean_book, *_ = _export(db, project)
    clean_sheet = clean_book[master.V2_SHEET_SITE]
    exported = [row for row in clean_sheet.iter_rows(min_row=2, values_only=True)
                if row[columns["实体ID"] - 1]]
    assert {row[columns["实体ID"] - 1] for row in exported} == line_ids
    assert {row[columns["领用单号"] - 1] for row in exported} == {target}
    fresh = _post(client, project, clean_book, "validate")
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["site_issue_merges"] == []
    assert fresh.json()["changes"] == []
    assert fresh.json()["conflicts"] == []
    assert all(fresh.json()["summary"][key] == 0
               for key in ("site_creates", "site_updates", "site_voids", "site_issue_merges"))
    assert _post(client, project, clean_book, "apply").status_code == 200


@pytest.mark.parametrize("entrypoint", ["service", "http"])
def test_apply_rejects_merge_missing_from_a_replayed_plan(db, monkeypatch, entrypoint):
    project, _, issues, book, sheet, columns, rows = _seed(db)
    for (issue, line), number in zip(issues, ("ISS-FINAL-A", "ISS-FINAL-B")):
        sheet.cell(rows[line.issue_line_id], columns["领用单号"], number)
    plan = _validate(db, project, book)
    forged = replace(plan, site_flags=tuple(
        replace(flag, issue_no="ISS-FINAL-A") for flag in plan.site_flags
    ))
    before = _snapshot(db, project.project_id)
    db.commit()

    if entrypoint == "service":
        with pytest.raises(master.WorkbookError) as raised:
            _apply(db, forged)
        error = raised.value
        assert error.code == "stale_site_issue_merge"
        db.rollback()
    else:
        client = uploader(db, username="synthetic-replayed-numbers")
        monkeypatch.setattr(master, "validate_project_master_v2", lambda *_a, **_kw: forged)
        response = _post(client, project, book, "apply")
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "stale_site_issue_merge"
    assert _snapshot(db, project.project_id) == before


@pytest.mark.parametrize("entrypoint", ["validate", "apply"])
def test_generated_new_header_cannot_share_a_renamed_header_number(db, entrypoint):
    project, part, [(issue, line)], book, sheet, columns, rows = _seed(db, ("ISS-A",))
    new_row = _append_new(sheet, part)
    plan = _validate(db, project, book)
    generated = next(flag for flag in plan.site_flags if flag.is_create)
    assert generated.issue_no.startswith("LY-AUTO-")
    existing_row = rows[line.issue_line_id]
    sheet.cell(existing_row, columns["领用单号"], generated.issue_no)
    if entrypoint == "validate":
        client = uploader(db, username="synthetic-auto-header-collision")
        response = _post(client, project, book, "validate")
        assert response.status_code == 422, response.text
        _assert_duplicate(response.json()["detail"], generated.issue_no,
                          [existing_row, new_row])
    else:
        # Valid plans collected independently can be replayed together; the
        # create without a bound issue_id is a separate header, never an append.
        sheet.delete_rows(new_row)
        renamed = _validate(db, project, book)
        forged = replace(renamed, site_flags=(*renamed.site_flags, generated))
        before = _snapshot(db, project.project_id)
        db.commit()
        with pytest.raises(master.WorkbookError) as raised:
            _apply(db, forged)
        _assert_duplicate({"code": raised.value.code, "message": raised.value.message,
                           **raised.value.details}, generated.issue_no, [existing_row, new_row])
        db.rollback()
        assert _snapshot(db, project.project_id) == before


@pytest.mark.parametrize("targets", [("ISS-B", "ISS-A"), ("ISS-B", "ISS-C")], ids=["swap", "chain"])
@pytest.mark.parametrize("change_quantity", [False, True], ids=["headers", "headers-and-facts"])
def test_unique_final_numbers_succeed_with_original_to_final_audits(db, targets, change_quantity):
    project, _, issues, book, sheet, columns, rows = _seed(db)
    before_revision = operations.get_or_create_workbook_state(db, project_id=project.project_id).revision
    versions = {issue.issue_id: issue.version for issue, _ in issues}
    line_versions = {line.issue_line_id: line.version for _, line in issues}
    original = {issue.issue_id: issue.issue_no for issue, _ in issues}
    for (_, line), target in zip(issues, targets):
        row = rows[line.issue_line_id]
        sheet.cell(row, columns["领用单号"], target)
        if change_quantity:
            sheet.cell(row, columns["领用数量"], 3)
    client = uploader(db, username="synthetic-unique-final-numbers")

    validated = _post(client, project, book, "validate")
    assert validated.status_code == 200, validated.text
    applied = _post(client, project, book, "apply")
    assert applied.status_code == 200, applied.text

    db.expire_all()
    assert [issue.issue_no for issue, _ in issues] == list(targets)
    assert set(db.scalars(select(MaintenanceSiteIssue.issue_no).where(
        MaintenanceSiteIssue.project_id == project.project_id,
    ))) == set(targets)
    for (issue, line), target in zip(issues, targets):
        assert issue.version == versions[issue.issue_id] + 1 + int(change_quantity)
        assert line.version == line_versions[line.issue_line_id] + int(change_quantity)
        assert line.quantity == Decimal("3" if change_quantity else "2")
        audits = list(db.scalars(select(MaintenanceProjectOperationAudit).where(
            MaintenanceProjectOperationAudit.entity_type == "site_issue",
            MaintenanceProjectOperationAudit.entity_id == issue.issue_id,
            MaintenanceProjectOperationAudit.action == "UPDATE",
        )))
        assert len(audits) == 1
        assert audits[0].before_json["issue_no"] == original[issue.issue_id]
        assert audits[0].after_json["issue_no"] == target
    assert operations.get_or_create_workbook_state(db, project_id=project.project_id).revision == before_revision + 1
    assert db.scalar(select(func.count()).select_from(MaintenanceSiteIssueReturnEvent)) == 2 * int(change_quantity)


@pytest.mark.parametrize("number", ["ISS-RENAMED", "WBDD-20261013-0005"])
def test_new_details_preserve_explicit_header_append_and_wbdd_auto_numbering(db, number):
    project, part, [(issue, line)], book, sheet, columns, rows = _seed(db, ("ISS-A",))
    sheet.cell(rows[line.issue_line_id], columns["领用单号"], number)
    _append_new(sheet, part, number=number, address="合成新增明细地址")
    plan = _validate(db, project, book)
    added = next(flag for flag in plan.site_flags if flag.is_create)
    _apply(db, plan)
    db.expire_all()
    new_line = db.get(MaintenanceSiteIssueLine, added.issue_line_id)
    assert issue.issue_no == number
    if number.startswith("WBDD-"):
        assert new_line.issue_id != issue.issue_id
        assert db.get(MaintenanceSiteIssue, new_line.issue_id).issue_no.startswith("LY-AUTO-")
        assert new_line.demand_order_no == number
    else:
        assert new_line.issue_id == issue.issue_id
        assert issue.site_location == "合成新增明细地址"
        assert new_line.line_no == 2
        assert db.scalar(select(func.count()).select_from(MaintenanceSiteIssue)) == 1


def test_number_holder_is_survivor_even_when_newer_than_renamed_source(db):
    project, _, issues, book, sheet, columns, rows = _seed(db)
    sheet.cell(rows[issues[0][1].issue_line_id], columns["领用单号"], "ISS-B")
    client = uploader(db, username="synthetic-occupied-number")
    source, source_line = issues[0]
    holder, holder_line = issues[1]
    assert holder.created_at >= source.created_at
    db.commit()
    content = _save(book)
    validated = _post(client, project, book, "validate", content=content)
    assert validated.status_code == 200, validated.text
    assert validated.json()["site_issue_merges"][0]["survivor_issue_id"] == holder.issue_id
    response = _post(client, project, book, "apply", content=content,
                     token=validated.json()["site_issue_merge_token"])
    assert response.status_code == 200, response.text
    db.expire_all()
    assert holder.issue_no == "ISS-B"
    assert source.normalized_status == "void"
    assert source_line.issue_id == holder_line.issue_id == holder.issue_id
    assert (holder_line.line_no, source_line.line_no) == (1, 2)


def test_apply_rechecks_number_claimed_after_validate(db):
    project, part, [(issue, line)], book, sheet, columns, rows = _seed(db, ("ISS-A",))
    sheet.cell(rows[line.issue_line_id], columns["领用单号"], "ISS-CLAIMED")
    plan = _validate(db, project, book)
    _site_issue(db, project, part, issue_no="ISS-CLAIMED")
    before = _snapshot(db, project.project_id)
    db.commit()
    with pytest.raises(master.WorkbookError) as raised:
        _apply(db, plan)
    assert raised.value.code == "stale_site_issue_merge"
    db.rollback()
    assert _snapshot(db, project.project_id) == before


def test_survivor_tie_breaks_by_stable_id_not_excel_order(db):
    project, _, issues, *_ = _seed(db, ("ISS-A", "ISS-B", "ISS-C"))
    for issue, _ in issues:
        issue.created_at = datetime(2026, 8, 1, tzinfo=timezone.utc)
    db.commit()
    book, sheet, columns, rows = _export(db, project)
    for issue, line in reversed(issues):
        sheet.cell(rows[line.issue_line_id], columns["领用单号"], "ISS-MERGED")
    reverse_id_rows = [
        [cell.value for cell in sheet[rows[line.issue_line_id]]]
        for _, line in sorted(issues, key=lambda pair: pair[0].issue_id, reverse=True)
    ]
    for row_no, values in zip(sorted(rows.values()), reverse_id_rows):
        for column, value in enumerate(values, 1):
            sheet.cell(row_no, column, value)
    plan = _validate(db, project, book)
    expected = min(issue.issue_id for issue, _ in issues)
    assert plan.site_issue_merges[0]["survivor_issue_id"] == expected
    _apply(db, plan)
    db.expire_all()
    assert all(line.issue_id == expected for _, line in issues)


@pytest.mark.parametrize("explicit_header", [False, True])
def test_header_preview_lists_discarded_values_and_explicit_workbook_values_win(db, explicit_header):
    project, _, issues, *_ = _seed(db)
    first, second = (issue for issue, _ in issues)
    for issue, suffix, issue_date in (
        (first, "甲", date(2026, 8, 10)), (second, "乙", date(2026, 8, 11)),
    ):
        issue.issue_date = issue_date
        issue.site_location = f"原地址{suffix}"
        issue.receiver = f"原接收人{suffix}"
        issue.issued_by = f"原发出人{suffix}"
    db.commit()
    book, sheet, columns, rows = _export(db, project)
    for issue, line in issues:
        row = rows[line.issue_line_id]
        sheet.cell(row, columns["领用单号"], "ISS-MERGED")
        if explicit_header:
            sheet.cell(row, columns["领用日期"], date(2026, 8, 20))
            sheet.cell(row, columns["领用地址"], "明确统一的新地址")
    client = uploader(db, username="synthetic-header-preview")
    content = _save(book)
    validated = _post(client, project, book, "validate", content=content)
    assert validated.status_code == 200, validated.text
    preview = validated.json()["site_issue_merges"][0]
    assert preview["survivor_issue_id"] == first.issue_id
    conflicts = {conflict["field"]: conflict for conflict in preview["header_conflicts"]}
    expected = {
        "issue_date": ("2026-08-10", "2026-08-11"),
        "site_location": ("原地址甲", "原地址乙"),
        "receiver": ("原接收人甲", "原接收人乙"),
        "issued_by": ("原发出人甲", "原发出人乙"),
    }
    for field, (survivor_value, discarded_value) in expected.items():
        assert conflicts[field]["survivor_value"] == survivor_value
        discarded = {second.issue_id: discarded_value}
        if explicit_header and field in {"issue_date", "site_location"}:
            discarded[first.issue_id] = survivor_value
        assert {item["issue_id"]: item["value"] for item in conflicts[field]["discarded"]} == discarded
    applied = _post(client, project, book, "apply", content=content,
                    token=validated.json()["site_issue_merge_token"])
    assert applied.status_code == 200, applied.text
    db.refresh(first)
    assert first.issue_date == (date(2026, 8, 20) if explicit_header else date(2026, 8, 10))
    assert first.site_location == ("明确统一的新地址" if explicit_header else "原地址甲")
    assert (first.receiver, first.issued_by) == ("原接收人甲", "原发出人甲")


@pytest.mark.parametrize("field,values", [
    ("领用日期", (date(2026, 8, 20), date(2026, 8, 21))),
    ("领用地址", ("明确新地址甲", "明确新地址乙")),
])
def test_conflicting_explicit_merge_headers_are_422_before_any_write(db, field, values):
    project, _, issues, book, sheet, columns, rows = _seed(db)
    for (_, line), value in zip(issues, values):
        sheet.cell(rows[line.issue_line_id], columns["领用单号"], "ISS-MERGED")
        sheet.cell(rows[line.issue_line_id], columns[field], value)
    client = uploader(db, username="synthetic-explicit-header-conflict")
    before = _snapshot(db, project.project_id)
    db.commit()
    response = _post(client, project, book, "validate")
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "site_issue_merge_conflict"
    assert "ISS-MERGED" in detail["message"]
    assert all(str(row) in detail["message"] for row in rows.values())
    assert detail["rows"]
    assert _snapshot(db, project.project_id) == before


@pytest.mark.parametrize("different_addresses", [False, True])
def test_new_details_for_merged_header_keep_explicit_address_intents(db, different_addresses):
    project, part, issues, book, sheet, columns, rows = _seed(db)
    for _, line in issues:
        sheet.cell(rows[line.issue_line_id], columns["领用单号"], "ISS-MERGED")
    new_rows = [
        _append_new(sheet, part, number="ISS-MERGED", serial="SYNTHETIC-APPEND-A",
                    address="明确新地址甲", issue_date=date(2026, 8, 22)),
        _append_new(sheet, part, number="ISS-MERGED", serial="SYNTHETIC-APPEND-B",
                    address="明确新地址乙" if different_addresses else "明确新地址甲",
                    issue_date=date(2026, 8, 22)),
    ]
    client = uploader(db, username="synthetic-new-merge-address")
    content = _save(book)
    response = _post(client, project, book, "validate", content=content)
    if different_addresses:
        assert response.status_code == 422, response.text
        detail = response.json()["detail"]
        assert detail["code"] == "site_issue_merge_conflict"
        assert "ISS-MERGED" in detail["message"]
        assert all(str(row) in detail["message"] for row in new_rows)
        return
    assert response.status_code == 200, response.text
    survivor_id = response.json()["site_issue_merges"][0]["survivor_issue_id"]
    applied = _post(client, project, book, "apply", content=content,
                    token=response.json()["site_issue_merge_token"])
    assert applied.status_code == 200, applied.text
    db.expire_all()
    survivor = db.get(MaintenanceSiteIssue, survivor_id)
    assert survivor.site_location == "明确新地址甲"
    assert survivor.issue_date == date(2026, 8, 22)
    lines = list(db.scalars(select(MaintenanceSiteIssueLine).where(
        MaintenanceSiteIssueLine.issue_id == survivor_id,
    ).order_by(MaintenanceSiteIssueLine.line_no)))
    assert [line.line_no for line in lines] == [1, 2, 3, 4]
    assert [line.serial_number for line in lines[2:]] == ["SYNTHETIC-APPEND-A", "SYNTHETIC-APPEND-B"]


def test_merge_address_operation_alias_clear_conflicts_with_explicit_address(db):
    project, _, issues, book, sheet, columns, rows = _seed(db)
    operation_column = columns["地址操作"]
    sheet.cell(1, operation_column, "领用地址操作")
    for _, line in issues:
        sheet.cell(rows[line.issue_line_id], columns["领用单号"], "ISS-MERGED")
    sheet.cell(rows[issues[0][1].issue_line_id], operation_column, "清空")
    sheet.cell(rows[issues[1][1].issue_line_id], columns["领用地址"], "明确另一地址")
    client = uploader(db, username="synthetic-merge-alias-clear")
    response = _post(client, project, book, "validate")
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "site_issue_merge_conflict"
    assert "ISS-MERGED" in detail["message"]
    assert all(str(row) in detail["message"] for row in rows.values())


@pytest.mark.parametrize("blocked", ["source_void", "target_void", "draft_source"])
def test_incompatible_merge_participants_are_rejected(db, blocked):
    project, _, issues, book, sheet, columns, rows = _seed(db)
    source, source_line = issues[0]
    target, target_line = issues[1]
    sheet.cell(rows[source_line.issue_line_id], columns["领用单号"], target.issue_no)
    if blocked == "source_void":
        source.normalized_status = "void"
    elif blocked == "target_void":
        target.normalized_status = "void"
    else:
        source.normalized_status = "draft"
    db.commit()
    client = uploader(db, username="synthetic-blocked-merge")
    before = _snapshot(db, project.project_id)
    db.commit()
    response = _post(client, project, book, "validate")
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "site_issue_merge_conflict"
    assert "ISS-B" in detail["message"]
    assert detail["rows"]
    assert _snapshot(db, project.project_id) == before


def test_merge_rejects_duplicate_stable_delivery_identity_before_moving_lines(db):
    from tests.test_site_issue_v2_api import _delivery_source

    project, _, issues, *_ = _seed(db)
    delivery = _delivery_source(db, project=project, delivery_line_id="synthetic-merge-shared-source")
    for _, line in issues:
        line.delivery_line_id = delivery.delivery_line_id
        line.part_id, line.pn = delivery.part_id, delivery.pn
        line.source_order_id, line.source_line_id = delivery.source_order_id, delivery.source_line_id
    db.commit()
    book, sheet, columns, rows = _export(db, project)
    for _, line in issues:
        sheet.cell(rows[line.issue_line_id], columns["领用单号"], "ISS-MERGED")
    client = uploader(db, username="synthetic-merge-delivery-conflict")
    before = _snapshot(db, project.project_id)
    db.commit()
    response = _post(client, project, book, "validate")
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "site_issue_merge_conflict"
    assert "ISS-MERGED" in response.json()["detail"]["message"]
    assert _snapshot(db, project.project_id) == before


def test_merge_never_accepts_a_signed_line_from_another_project(db):
    from app.models.maintenance_project import MaintenanceProject

    project, part, issues, book, sheet, columns, rows = _seed(db)
    other = MaintenanceProject(
        project_id=str(uuid.uuid4()), project_code="SYNTHETIC-MERGE-OTHER",
        display_name="合成另一个项目", lifecycle_status="ongoing",
    )
    db.add(other)
    db.commit()
    _, foreign_line = _site_issue(db, other, part, issue_no="ISS-FOREIGN")
    _, foreign_sheet, _, foreign_rows = _export(db, other)
    foreign_values = [cell.value for cell in foreign_sheet[foreign_rows[foreign_line.issue_line_id]]]
    foreign_values[columns["领用单号"] - 1] = "ISS-MERGED"
    sheet.append(foreign_values)
    foreign_row_no = sheet.max_row
    for _, line in issues:
        sheet.cell(rows[line.issue_line_id], columns["领用单号"], "ISS-MERGED")
    client = uploader(db, username="synthetic-cross-project-merge")
    before = (_snapshot(db, project.project_id), _snapshot(db, other.project_id))
    db.commit()
    response = _post(client, project, book, "validate")
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "project_mismatch"
    assert str(foreign_row_no) in response.json()["detail"]["message"]
    assert (_snapshot(db, project.project_id), _snapshot(db, other.project_id)) == before


def test_merge_moves_inactive_lines_without_changing_identity_or_evidence(db):
    project, part, issues, *_ = _seed(db)
    source, source_line = issues[0]
    holder, _ = issues[1]
    inactive = _append_site_line(db, source, part, qty="4", line_no=7)
    inactive.is_active = False
    inactive.serial_number = "SYNTHETIC-INACTIVE-SN"
    inactive.demand_order_no = "WBDD-SYNTHETIC-DEMAND"
    inactive.source_order_id = "SYNTHETIC-SOURCE-ORDER"
    inactive.source_line_id = "SYNTHETIC-SOURCE-LINE"
    inactive.remark = "作废明细仍保留"
    inactive.no_return = True
    inactive.manual_unit_cost = Decimal("10")
    inactive.manual_unit_cost_inc_tax = Decimal("11.30")
    inactive.manual_evidence = "合成成本证据"
    db.commit()
    fields = ("issue_line_id", "quantity", "serial_number", "demand_order_no", "source_order_id",
              "source_line_id", "remark", "no_return", "manual_unit_cost", "manual_unit_cost_inc_tax",
              "manual_evidence", "is_active")
    preserved = {field: getattr(inactive, field) for field in fields}
    old_version = inactive.version
    book, sheet, columns, rows = _export(db, project)
    sheet.cell(rows[source_line.issue_line_id], columns["领用单号"], holder.issue_no)
    plan = _validate(db, project, book)
    assert plan.site_issue_merges[0]["total_line_count"] == 3
    _apply(db, plan)
    db.refresh(inactive)
    assert {field: getattr(inactive, field) for field in fields} == preserved
    assert inactive.issue_id == holder.issue_id
    assert inactive.line_no == 3
    assert inactive.version > old_version


def test_inactive_signed_line_cannot_set_the_merged_header(db):
    project, part, issues, *_ = _seed(db)
    first, _ = issues[0]
    inactive = _append_site_line(db, first, part, line_no=2)
    book, sheet, columns, rows = _export(db, project)
    inactive.is_active = False
    db.commit()
    for _, line in issues:
        sheet.cell(rows[line.issue_line_id], columns["领用单号"], "ISS-MERGED")
    sheet.cell(rows[inactive.issue_line_id], columns["领用日期"], date(2099, 1, 1))
    sheet.cell(rows[inactive.issue_line_id], columns["领用地址"], "失效签名行不得改变单头")
    plan = _validate(db, project, book)
    assert plan.voided_rows
    assert plan.site_issue_merges[0]["final_header"]["issue_date"] == first.issue_date.isoformat()
    original_header = first.issue_date, first.site_location
    _apply(db, plan)
    db.refresh(first)
    db.refresh(inactive)
    assert (first.issue_date, first.site_location) == original_header
    assert inactive.is_active is False


def test_merge_chain_and_new_detail_use_the_final_header_identity(db):
    project, part, issues, book, sheet, columns, rows = _seed(db, ("ISS-A", "ISS-B", "ISS-D"))
    for (_, line), target in zip(issues, ("ISS-B", "ISS-C", "ISS-B")):
        sheet.cell(rows[line.issue_line_id], columns["领用单号"], target)
    _append_new(sheet, part, number="ISS-B", serial="SYNTHETIC-MERGE-CHAIN-APPEND")
    plan = _validate(db, project, book)
    assert len(plan.site_issue_merges) == 1
    assert plan.site_issue_merges[0]["survivor_issue_id"] == issues[0][0].issue_id
    _apply(db, plan)
    db.expire_all()
    assert issues[0][0].issue_no == "ISS-B"
    assert issues[1][0].issue_no == "ISS-C"
    assert issues[2][0].normalized_status == "void"
    assert issues[2][1].issue_id == issues[0][0].issue_id
    assert issues[1][1].issue_id == issues[1][0].issue_id
    final_lines = list(db.scalars(select(MaintenanceSiteIssueLine).where(
        MaintenanceSiteIssueLine.issue_id == issues[0][0].issue_id,
    ).order_by(MaintenanceSiteIssueLine.line_no)))
    assert [line.line_no for line in final_lines] == [1, 2, 3]
    assert final_lines[2].serial_number == "SYNTHETIC-MERGE-CHAIN-APPEND"


@pytest.mark.parametrize("fault", [
    "missing", "tampered", "unicode_token", "other_actor", "changed_line_set", "changed_header", "changed_line_fact",
])
def test_http_merge_requires_preview_bound_to_current_plan_and_actor(db, fault):
    project, part, issues, book, sheet, columns, rows = _seed(db)
    for _, line in issues:
        sheet.cell(rows[line.issue_line_id], columns["领用单号"], "ISS-MERGED")
    client = uploader(db, username="synthetic-merge-token-owner")
    content = _save(book)
    validated = _post(client, project, book, "validate", content=content)
    assert validated.status_code == 200, validated.text
    token = validated.json()["site_issue_merge_token"]
    if fault == "missing":
        token = None
    elif fault == "tampered":
        token += "x"
    elif fault == "unicode_token":
        token = "合成无效预览令牌"
    elif fault == "other_actor":
        client = uploader(db, username="synthetic-merge-token-other")
    elif fault == "changed_line_set":
        _append_site_line(db, issues[0][0], part, line_no=2)
    elif fault == "changed_header":
        issues[0][0].receiver = "校验后修改接收人"
        issues[0][0].version += 1
        db.commit()
    else:
        issues[0][1].quantity = Decimal("7")
        issues[0][1].version += 1
        db.commit()
    before = _snapshot(db, project.project_id)
    db.commit()
    response = _post(client, project, book, "apply", content=content, token=token)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "stale_site_issue_merge"
    assert _snapshot(db, project.project_id) == before


def test_merge_moves_live_obligations_but_preserves_historical_commands_and_events(db):
    from copy import deepcopy

    from app.models.dimensions import DimPart
    from app.models.maintenance_bad_return import (
        MaintenanceBadReturn, MaintenanceBadReturnLine, MaintenanceReturnObligation,
    )
    from app.models.maintenance_project_operations import MaintenanceSiteIssueCommand
    from app.services import maintenance_bad_returns as returns
    from tests.test_maintenance_bad_returns_api import _category, _client, _confirm_issue
    from tests.test_site_issue_v2_api import _delivery_source

    project, *_ = _make_project_with_line(db)
    client = _client(db, username="synthetic-native-merge")
    category = _category(db, major="服务器配件", minor="电源")
    confirmed = []
    for suffix in ("merge-source", "merge-holder"):
        delivery = _delivery_source(db, project=project, delivery_line_id=f"synthetic-{suffix}")
        db.get(DimPart, delivery.part_id).category_id = category.id
        db.commit()
        confirmed.append(_confirm_issue(
            client, project_id=project.project_id,
            lines=[{"delivery_line_id": delivery.delivery_line_id, "quantity": "3"}],
            suffix=suffix,
        ))
    source, holder = [db.get(MaintenanceSiteIssue, result["issue_id"]) for result in confirmed]
    old_source_event = db.scalar(select(MaintenanceSiteIssueReturnEvent).where(
        MaintenanceSiteIssueReturnEvent.issue_id == source.issue_id,
    ))
    extra_delivery = _delivery_source(db, project=project, delivery_line_id="synthetic-merge-inactive")
    inactive_line = _append_site_line(db, source, db.get(DimPart, extra_delivery.part_id), qty="1", line_no=2)
    inactive_line.delivery_line_id = extra_delivery.delivery_line_id
    inactive_line.is_active = False
    inactive_obligation = MaintenanceReturnObligation(
        obligation_id=str(uuid.uuid4()), project_id=project.project_id, issue_id=source.issue_id,
        issue_line_id=inactive_line.issue_line_id, delivery_line_id=extra_delivery.delivery_line_id,
        part_id=extra_delivery.part_id, pn=extra_delivery.pn,
        source_quantity=Decimal("1"), required_quantity=Decimal("0"),
        classification="pending_category", rule_version="synthetic-test",
        source_issue_version=source.version, last_source_event_id=old_source_event.event_id,
        is_active=False, version=3,
    )
    db.add(inactive_obligation)
    db.commit()
    obligations = list(db.scalars(select(MaintenanceReturnObligation).order_by(
        MaintenanceReturnObligation.issue_id,
    )))
    source_obligation = next(row for row in obligations if row.issue_id == source.issue_id and row.is_active)
    original_obligations = {
        row.obligation_id: (row.issue_line_id, row.required_quantity, row.source_quantity, row.version, row.is_active)
        for row in obligations
    }
    returned = MaintenanceBadReturn(
        return_id=str(uuid.uuid4()), return_no="SYNTHETIC-MERGE-RETURN", project_id=project.project_id,
        status="draft", created_by="synthetic-test", version=1,
    )
    db.add(returned)
    db.flush()
    return_line = MaintenanceBadReturnLine(
        return_line_id=str(uuid.uuid4()), return_id=returned.return_id, line_no=1,
        obligation_id=source_obligation.obligation_id,
        part_id=source_obligation.part_id, pn=source_obligation.pn, quantity=Decimal("1"),
    )
    db.add(return_line)
    inactive_return_line = MaintenanceBadReturnLine(
        return_line_id=str(uuid.uuid4()), return_id=returned.return_id, line_no=2,
        obligation_id=inactive_obligation.obligation_id, part_id=inactive_obligation.part_id,
        pn=inactive_obligation.pn, quantity=Decimal("1"),
    )
    db.add(inactive_return_line)
    # Append an unconsumed high-version event; never rewrite an immutable
    # already-consumed historical event merely to build a pending fixture.
    source.version = 30
    pending = MaintenanceSiteIssueReturnEvent(
        event_id=str(uuid.uuid4()), project_id=project.project_id, issue_id=source.issue_id,
        event_type="return_obligation_corrected", issue_version=30,
        payload=deepcopy(old_source_event.payload),
    )
    db.add(pending)
    db.flush()
    history_events = list(db.scalars(select(MaintenanceSiteIssueReturnEvent)))
    history_commands = list(db.scalars(select(MaintenanceSiteIssueCommand)))
    original_events = {event.event_id: (event.issue_id, deepcopy(event.payload)) for event in history_events}
    original_commands = {
        command.command_id: (command.issue_id, deepcopy(command.response_json)) for command in history_commands
    }
    db.commit()
    book, sheet, columns, rows = _export(db, project)
    source_lines = list(db.scalars(select(MaintenanceSiteIssueLine).where(
        MaintenanceSiteIssueLine.issue_id == source.issue_id,
    )))
    for line in source_lines:
        if line.issue_line_id in rows:
            sheet.cell(rows[line.issue_line_id], columns["领用单号"], holder.issue_no)
    _apply(db, _validate(db, project, book))
    db.expire_all()
    assert return_line.obligation_id == source_obligation.obligation_id
    assert return_line.quantity == Decimal("1")
    assert inactive_return_line.obligation_id == inactive_obligation.obligation_id
    assert inactive_return_line.quantity == Decimal("1")
    for obligation in obligations:
        original_line_id, required, quantity, version, active = original_obligations[obligation.obligation_id]
        assert obligation.issue_id == holder.issue_id
        assert obligation.issue_line_id == original_line_id
        assert (obligation.required_quantity, obligation.source_quantity) == (required, quantity)
        assert obligation.is_active == active
        assert obligation.version >= version
    assert source_obligation.version > original_obligations[source_obligation.obligation_id][3]
    assert inactive_obligation.version > original_obligations[inactive_obligation.obligation_id][3]
    assert {event.event_id: (event.issue_id, event.payload) for event in history_events} == original_events
    assert {command.command_id: (command.issue_id, command.response_json) for command in history_commands} == original_commands
    assert pending.consumed_at is not None
    new_events = list(db.scalars(select(MaintenanceSiteIssueReturnEvent).where(
        MaintenanceSiteIssueReturnEvent.event_id.not_in(original_events),
    )))
    assert len(new_events) == 2
    corrected = next(event for event in new_events if event.issue_id == holder.issue_id)
    voided = next(event for event in new_events if event.issue_id == source.issue_id)
    assert corrected.event_type == "return_obligation_corrected"
    assert {line["issue_line_id"] for line in corrected.payload["lines"]} == {
        row.issue_line_id for row in obligations if row.is_active
    }
    assert voided.event_type == "return_obligation_voided"
    assert voided.payload["lines"] == []
    assert min(corrected.issue_version, voided.issue_version) > 30
    assert all(event.consumed_at is not None and event.downstream_reference for event in new_events)
    # A delayed old source event must not move the live projection back.
    late = MaintenanceSiteIssueReturnEvent(
        event_id=str(uuid.uuid4()), project_id=project.project_id, issue_id=source.issue_id,
        event_type="return_obligation_corrected", issue_version=1,
        payload=deepcopy(original_events[pending.event_id][1]),
    )
    db.add(late)
    db.flush()
    returns.consume_return_event(db, late)
    db.commit()
    assert all(db.get(MaintenanceReturnObligation, key).issue_id == holder.issue_id
               and db.get(MaintenanceReturnObligation, key).is_active == expected[4]
               for key, expected in original_obligations.items())


@pytest.mark.parametrize("append_without_delivery", [False, True])
def test_merging_native_drafts_does_not_create_return_obligations_or_events(db, append_without_delivery):
    from app.models.maintenance_bad_return import MaintenanceReturnObligation
    from tests.test_site_issue_v2_api import _client, _create_draft, _delivery_source

    project, part, *_ = _make_project_with_line(db)
    client = _client(db, username="synthetic-native-draft-merge")
    drafts = []
    for suffix in ("source", "holder"):
        delivery = _delivery_source(db, project=project, delivery_line_id=f"synthetic-draft-merge-{suffix}")
        db.commit()
        drafts.append(_create_draft(
            client, project_id=project.project_id, delivery_line_id=delivery.delivery_line_id,
            quantity="1", key=f"synthetic-draft-merge-{suffix}",
        ))
    source, holder = [db.get(MaintenanceSiteIssue, draft["issue_id"]) for draft in drafts]
    book, sheet, columns, rows = _export(db, project)
    source_line = db.scalar(select(MaintenanceSiteIssueLine).where(
        MaintenanceSiteIssueLine.issue_id == source.issue_id,
    ))
    sheet.cell(rows[source_line.issue_line_id], columns["领用单号"], holder.issue_no)
    if append_without_delivery:
        new_row = _append_new(
            sheet, part, number=holder.issue_no,
            serial="SYNTHETIC-NATIVE-MERGE-MISSING-DELIVERY", issue_date=holder.issue_date,
        )
        before = _snapshot(db, project.project_id)
        db.commit()
        with pytest.raises(master.WorkbookError) as raised:
            _validate(db, project, book)
        assert raised.value.code == "site_issue_merge_conflict"
        assert holder.issue_no in raised.value.message
        assert str(new_row) in raised.value.message
        assert any(row["row_no"] == new_row for row in raised.value.details["rows"])
        assert _snapshot(db, project.project_id) == before
        return
    plan = _validate(db, project, book)
    assert plan.site_issue_merges[0]["survivor_issue_id"] == holder.issue_id
    _apply(db, plan)
    db.expire_all()
    assert holder.normalized_status == "draft"
    assert source.normalized_status == "void"
    assert source_line.issue_id == holder.issue_id
    for model in (MaintenanceReturnObligation, MaintenanceSiteIssueReturnEvent):
        assert db.scalar(select(func.count()).select_from(model)) == 0


def test_merge_supersedes_active_warehouse_links_and_only_updates_open_candidates(db):
    from copy import deepcopy

    from app.models.maintenance_warehouse import MaintenanceWarehouseAmbiguity, MaintenanceWarehouseDocumentLink
    from tests.test_maintenance_warehouse_site_issue_bridge import _seed_exact_shipment

    facts = _seed_exact_shipment(db)
    project, part = facts["project"], facts["part"]
    source, source_line = _site_issue(db, project, part, issue_no="ISS-SOURCE")
    holder, _ = _site_issue(db, project, part, issue_no="ISS-HOLDER")
    link = MaintenanceWarehouseDocumentLink(
        link_id=str(uuid.uuid4()), document_id=facts["document"].document_id,
        link_kind="site_issue", target_type="maintenance_site_issue", target_id=source.issue_id,
        stable_key_kind="issue_id", stable_key_hash="a" * 64, source="manual",
        status="active", version=1, reason="合成领用关联", operated_by="synthetic-test",
    )
    db.add(link)
    ambiguities = []
    for status, fingerprint in (("open", "b"), ("resolved", "c")):
        candidate = {"target_type": "maintenance_site_issue", "target_id": source.issue_id,
                     "label": source.issue_no}
        ambiguity = MaintenanceWarehouseAmbiguity(
            ambiguity_id=str(uuid.uuid4()), import_id=facts["batch"].import_id,
            document_id=facts["document"].document_id,
            ambiguity_type="multiple_candidates", candidates_json=[candidate],
            fingerprint=fingerprint * 64, status=status, version=1,
            **({"resolution_json": candidate, "resolution_reason": "合成历史选择",
                "resolved_by": "synthetic-test", "resolved_at": datetime.now(timezone.utc)}
               if status == "resolved" else {}),
        )
        db.add(ambiguity)
        ambiguities.append(ambiguity)
    db.commit()
    opened, resolved = ambiguities
    opened_candidates = deepcopy(opened.candidates_json)
    resolved_before = (deepcopy(resolved.candidates_json), deepcopy(resolved.resolution_json), resolved.version)
    book, sheet, columns, rows = _export(db, project)
    sheet.cell(rows[source_line.issue_line_id], columns["领用单号"], holder.issue_no)
    _apply(db, _validate(db, project, book))
    db.expire_all()
    assert (link.status, link.version, link.target_id) == ("superseded", 2, source.issue_id)
    successor = db.scalar(select(MaintenanceWarehouseDocumentLink).where(
        MaintenanceWarehouseDocumentLink.supersedes_link_id == link.link_id,
    ))
    assert successor is not None
    assert (successor.status, successor.target_id, successor.version) == ("active", holder.issue_id, 2)
    assert opened.status == "resolved"
    assert opened.candidates_json == opened_candidates
    assert opened.version == 2
    replacement = db.get(MaintenanceWarehouseAmbiguity, opened.resolution_json["new_ambiguity_id"])
    assert replacement is not None
    assert replacement.status == "open"
    assert replacement.candidates_json[0]["target_id"] == holder.issue_id
    assert replacement.fingerprint != opened.fingerprint
    assert (resolved.candidates_json, resolved.resolution_json, resolved.version) == resolved_before


@pytest.mark.parametrize("second_target", ["ISS-A", "ISS-C"], ids=["swap", "chain"])
def test_swap_and_chain_new_detail_attaches_to_final_owner_with_date_and_address(db, second_target):
    project, part, issues, book, sheet, columns, rows = _seed(db)
    for (_, line), number, issue_date in zip(
        issues, ("ISS-B", second_target), (date(2026, 8, 11), date(2026, 8, 12)),
    ):
        sheet.cell(rows[line.issue_line_id], columns["领用单号"], number)
        sheet.cell(rows[line.issue_line_id], columns["领用日期"], issue_date)
    _append_new(sheet, part, number="ISS-B", issue_date=date(2026, 8, 11), address="合成最终 B 地址")
    plan = _validate(db, project, book)
    added = next(flag for flag in plan.site_flags if flag.is_create)
    _apply(db, plan)
    db.expire_all()
    first, second = (issue for issue, _ in issues)
    added_line = db.get(MaintenanceSiteIssueLine, added.issue_line_id)
    assert (first.issue_no, first.issue_date, first.site_location) == (
        "ISS-B", date(2026, 8, 11), "合成最终 B 地址",
    )
    assert (second.issue_no, second.issue_date, second.site_location) == (
        second_target, date(2026, 8, 12), None,
    )
    assert (added_line.issue_id, added_line.line_no) == (first.issue_id, 2)


def test_new_header_reuses_vacated_number_without_old_address_or_line_number(db):
    project, part, [(issue, line)], book, sheet, columns, rows = _seed(db, ("ISS-A",))
    issue.site_location = "合成原领用地址"
    db.commit()
    book = load_workbook(io.BytesIO(master.build_project_master_v2(
        db, project_id=project.project_id, sheets=(master.V2_SHEET_SITE,),
    )))
    sheet = book[master.V2_SHEET_SITE]
    sheet.cell(rows[line.issue_line_id], columns["领用单号"], "ISS-B")
    sheet.cell(rows[line.issue_line_id], columns["领用日期"], date(2026, 8, 11))
    _append_new(sheet, part, number="ISS-A", issue_date=date(2026, 8, 12), address="合成全新 A 地址")
    plan = _validate(db, project, book)
    added = next(flag for flag in plan.site_flags if flag.is_create)
    _apply(db, plan)
    db.expire_all()
    new_line = db.get(MaintenanceSiteIssueLine, added.issue_line_id)
    new_header = db.get(MaintenanceSiteIssue, new_line.issue_id)
    assert (issue.issue_no, issue.issue_date, issue.site_location) == (
        "ISS-B", date(2026, 8, 11), "合成原领用地址",
    )
    assert new_header.issue_id != issue.issue_id
    assert (new_header.issue_no, new_header.issue_date, new_header.site_location) == (
        "ISS-A", date(2026, 8, 12), "合成全新 A 地址",
    )
    assert new_line.line_no == 1


@pytest.mark.parametrize("entrypoint", ["service", "http"])
@pytest.mark.parametrize("phase", ["rename", "create"])
@pytest.mark.parametrize("constraint", ["issue_number", "unknown"])
def test_real_flush_integrity_errors_are_4xx_and_rollback_every_write(
    db, monkeypatch, caplog, entrypoint, phase, constraint,
):
    project, part, [(issue, line)], book, sheet, columns, rows = _seed(db, ("ISS-A",))
    project_id, original_issue_id = project.project_id, issue.issue_id
    target = "ISS-FLUSH-CONFLICT"
    if phase == "rename":
        sheet.cell(rows[line.issue_line_id], columns["领用单号"], target)
    else:
        _append_new(sheet, part, number=target)
    plan = _validate(db, project, book)
    before = _snapshot(db, project_id)
    db.commit()
    client = uploader(db, username="synthetic-flush-conflict") if entrypoint == "http" else None
    # Alembic's fileConfig disables already-imported application loggers while
    # building this test database. Restore this logger for the warning contract.
    monkeypatch.setattr(master.logger, "disabled", False)
    caplog.set_level("WARNING", logger=master.__name__)
    original_flush = Session.flush
    injected = []

    def inject_constraint_failure(session, *args, **kwargs):
        candidates = [obj for obj in (*session.new, *session.dirty)
                      if isinstance(obj, MaintenanceSiteIssue) and obj.issue_no == target]
        if candidates and not injected:
            injected.append(session)
            # Insert through Core to reproduce the actual PG constraint error
            # on the next ORM flush, after the apply-side preflight completed.
            # The competing row belongs to this transaction and rolls back too.
            session.execute(insert(MaintenanceSiteIssue).values(
                issue_id=str(uuid.uuid4()) if constraint == "issue_number" else original_issue_id,
                project_id=project_id, issue_no=target,
                issue_date=date(2026, 8, 10), raw_status="已确认",
                normalized_status="confirmed", status_mapping_state="mapped",
                status_mapping_version="synthetic-test", source="legacy", version=1,
            ))
        return original_flush(session, *args, **kwargs)

    monkeypatch.setattr(Session, "flush", inject_constraint_failure)
    expected_code = "duplicate_issue_no" if constraint == "issue_number" else "apply_conflict"
    if entrypoint == "service":
        with pytest.raises(master.WorkbookError) as raised:
            _apply(db, plan)
        assert raised.value.code == expected_code
        assert db.is_active
        assert db.scalar(select(1)) == 1  # No caller rollback needed after DB failure.
        message = raised.value.message
    else:
        response = _post(client, project, book, "apply")
        assert response.status_code == 422, response.text
        assert response.json()["detail"]["code"] == expected_code
        message = response.json()["detail"]["message"]
    assert injected, "The test must exercise a real write/flush collision"
    if constraint == "issue_number":
        assert target in message
    assert any(record.levelname == "WARNING" and record.exc_info for record in caplog.records)
    assert _snapshot(db, project_id) == before

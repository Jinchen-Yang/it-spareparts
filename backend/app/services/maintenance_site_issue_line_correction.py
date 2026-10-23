"""Partial line/header web commands around the shared fact-editing primitive."""

from collections.abc import Callable, Mapping
from datetime import date
import re
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.maintenance_project_operations import (
    MaintenanceProjectWorkbookState,
    MaintenanceSiteIssue,
    MaintenanceSiteIssueCommand,
    MaintenanceSiteIssueLine,
)
from app.services import maintenance_workbook_state as workbook_state
from app.services import maintenance_site_issue_command_identity as command_identity
from app.services.maintenance_site_issue_facts import (
    SiteIssueEditError,
    apply_locked_site_issue_edits,
)


class SiteIssueLineCorrectionConflict(ValueError):
    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.detail = {"code": code, "message": message, **details}


def correct_site_issue_line(
    db: Session, *, issue_id: str, issue_line_id: str,
    expected_issue_version: int, expected_line_version: int,
    changes: Mapping[str, object], reason: str, idempotency_key: str,
    authorize: Callable[[str], str],
) -> dict:
    """Keep the original line-command receipt and fingerprint compatible."""
    if set(changes) - {
        "no_return", "quantity", "serial_number", "remark",
        "demand_order_no", "pn", "part_id",
    }:
        raise SiteIssueEditError("site_issue_edit_invalid_fields", "包含不可编辑的领用明细字段")
    if ("pn" in changes) != ("part_id" in changes):
        raise SiteIssueEditError("site_issue_edit_invalid_fields", "PN 与备件身份必须成组提交")
    if "no_return" in changes and changes["no_return"] is not None and type(changes["no_return"]) is not bool:
        raise SiteIssueEditError("site_issue_edit_invalid_fields", "返还要求只能为 true、false 或 null")
    return _correct_site_issue(
        db, issue_id=issue_id, issue_line_id=issue_line_id,
        expected_issue_version=expected_issue_version,
        expected_line_version=expected_line_version, changes=changes,
        reason=reason, idempotency_key=idempotency_key, authorize=authorize,
    )


def correct_site_issue_header(
    db: Session, *, issue_id: str, expected_issue_version: int,
    changes: Mapping[str, object], reason: str, idempotency_key: str,
    authorize: Callable[[str], str],
) -> dict:
    return _correct_site_issue(
        db, issue_id=issue_id, issue_line_id=None,
        expected_issue_version=expected_issue_version, expected_line_version=None,
        changes=changes, reason=reason, idempotency_key=idempotency_key,
        authorize=authorize,
    )


def _header_intents(changes: Mapping[str, object]) -> dict:
    if set(changes) - {"issue_no", "issue_date", "receiver", "issued_by", "site_location"}:
        raise SiteIssueEditError("site_issue_edit_invalid_fields", "包含不可编辑的领用单头字段")
    result = dict(changes)
    if "issue_date" in result:
        value = result["issue_date"]
        try:
            if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
                raise ValueError
            result["issue_date"] = date.fromisoformat(value)
        except ValueError as exc:
            raise SiteIssueEditError("site_issue_edit_invalid_fields", "领用日期须为有效 YYYY-MM-DD 日期") from exc
    if isinstance(result.get("issue_no"), str):
        result["issue_no"] = result["issue_no"].strip()
    for key in ("receiver", "issued_by", "site_location"):
        if result.get(key) == "":
            result[key] = None
    return result


def _correct_site_issue(
    db: Session, *, issue_id: str, issue_line_id: str | None,
    expected_issue_version: int, expected_line_version: int | None,
    changes: Mapping[str, object], reason: str, idempotency_key: str,
    authorize: Callable[[str], str],
) -> dict:
    """Own locks/CAS/receipts only; the primitive remains the sole fact writer."""
    is_header = issue_line_id is None
    headers = _header_intents(changes) if is_header else None
    if not reason.strip() or len(reason) > 1000:
        raise SiteIssueEditError("site_issue_edit_actor_required", "请填写不超过 1000 字的更正原因")
    if not idempotency_key.strip() or not 8 <= len(idempotency_key) <= 128:
        raise SiteIssueEditError("site_issue_edit_invalid_fields", "幂等键须为 8 至 128 个字符")

    project_id = db.scalar(select(MaintenanceSiteIssue.project_id).where(
        MaintenanceSiteIssue.issue_id == issue_id,
    ))
    if project_id is None:
        raise SiteIssueEditError("site_issue_edit_identity_conflict", "领用单不存在")
    authorize(project_id)
    command_identity.lock_site_issue_idempotency_key(db, idempotency_key)
    project = workbook_state.lock_project_for_fact_write(db, project_id)
    if project is None:
        raise SiteIssueEditError("site_issue_edit_identity_conflict", "维保项目不存在")
    db.refresh(project)
    state = db.get(MaintenanceProjectWorkbookState, project_id, populate_existing=True)
    issue = db.scalar(select(MaintenanceSiteIssue).where(
        MaintenanceSiteIssue.issue_id == issue_id,
    ).with_for_update().execution_options(populate_existing=True))
    if issue is None or issue.project_id != project_id:
        raise SiteIssueEditError("site_issue_edit_identity_conflict", "领用单归属已变化，请刷新后重试")
    operated_by = authorize(project_id)
    fingerprint = command_identity.site_issue_request_fingerprint({
        "entry_command": "site_issue_header_correction_v1" if is_header else "site_issue_line_correction_v1",
        "project_id": project_id, "issue_id": issue_id,
        "expected_issue_version": expected_issue_version,
        **({} if is_header else {
            "issue_line_id": issue_line_id, "expected_line_version": expected_line_version,
        }),
        "changes": dict(changes), "reason": reason, "operated_by": operated_by,
    })
    command = db.scalar(select(MaintenanceSiteIssueCommand).where(
        MaintenanceSiteIssueCommand.idempotency_key == idempotency_key,
    ))
    if command is not None:
        if (command.action != "correct" or command.project_id != project_id
                or command.issue_id != issue_id or command.request_fingerprint != fingerprint):
            raise SiteIssueLineCorrectionConflict(
                "site_issue_edit_idempotency_conflict", "幂等键已用于不同的操作，请核对原请求",
            )
        return {**command.response_json, "idempotent_replay": True}
    # A successful command keeps its receipt even after a later merge moves
    # its line. Only a first execution validates the current target membership.
    # The scoped query never takes a foreign project's line lock out of order.
    line_query = select(MaintenanceSiteIssueLine).where(
        MaintenanceSiteIssueLine.issue_id == issue_id,
    )
    if is_header:
        line_query = line_query.where(MaintenanceSiteIssueLine.is_active.is_(True))
    else:
        line_query = line_query.where(MaintenanceSiteIssueLine.issue_line_id == issue_line_id)
    lines = list(db.scalars(line_query.order_by(
        MaintenanceSiteIssueLine.issue_line_id,
    ).with_for_update().execution_options(populate_existing=True)))
    line = lines[0] if not is_header and lines else None
    if not is_header and line is None:
        raise SiteIssueEditError("site_issue_edit_identity_conflict", "领用明细不存在或不属于本单")
    authorize(project_id)
    if issue.version != expected_issue_version or (line is not None and line.version != expected_line_version):
        raise SiteIssueLineCorrectionConflict(
            "site_issue_edit_version_conflict", "领用单或明细版本已变化，请刷新核对后重试",
            current_issue_version=issue.version,
            **({"current_line_version": line.version} if line is not None else {}),
        )
    if is_header and any(row.delivery_line_id is not None for row in lines):
        raise SiteIssueEditError("site_issue_edit_source_blocked", "含发货关联明细的领用单须使用原有更正流程")
    result = apply_locked_site_issue_edits(
        db, project=project, issue=issue,
        line_changes={} if is_header else {issue_line_id: changes},
        header_changes=headers, operated_by=operated_by, reason=reason.strip(),
    )
    if result.changed:
        workbook_state.bump_locked_workbook_revision(db, state=state)
    response = {
        "issue_id": issue_id,
        "issue_version": result.issue_version,
        "changed": result.changed,
        "idempotent_replay": False, "workbook_revision": state.revision,
    }
    if is_header:
        response.update(
            line_versions=result.line_versions, scope="whole_issue",
            affected_line_count=len(lines), message="单头修改影响整张领用单全部行",
        )
    else:
        response.update(
            issue_line_id=issue_line_id, line_version=result.line_versions[issue_line_id],
            no_return=line.no_return,
        )
    db.add(MaintenanceSiteIssueCommand(
        command_id=str(uuid4()), idempotency_key=idempotency_key,
        project_id=project_id, issue_id=issue_id, action="correct",
        request_fingerprint=fingerprint, response_json=response,
    ))
    db.flush()
    return response

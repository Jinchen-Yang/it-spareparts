"""Single-row web commands around the shared, locked fact-editing primitive."""

from collections.abc import Callable, Mapping
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.maintenance_project_operations import (
    MaintenanceProjectWorkbookState,
    MaintenanceSiteIssue,
    MaintenanceSiteIssueCommand,
    MaintenanceSiteIssueLine,
)
from app.services import maintenance_project_operations as operations
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
    """Caller supplies current authorization; commits/rollbacks remain at HTTP.

    Restrict this first web consumer to no_return. Extending the HTTP whitelist
    requires the additional field's public contract tests, not another writer.
    """
    if set(changes) - {"no_return"}:
        raise SiteIssueEditError("site_issue_edit_invalid_fields", "此入口仅支持修改领用行返还要求")
    if "no_return" in changes and changes["no_return"] is not None and type(changes["no_return"]) is not bool:
        raise SiteIssueEditError("site_issue_edit_invalid_fields", "返还要求只能为 true、false 或 null")
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
    operations._lock_idempotency_key(db, idempotency_key)
    project = operations._lock_project_for_fact_write(db, project_id)
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
    fingerprint = operations._site_issue_request_fingerprint({
        "entry_command": "site_issue_line_correction_v1", "project_id": project_id,
        "issue_id": issue_id, "issue_line_id": issue_line_id,
        "expected_issue_version": expected_issue_version,
        "expected_line_version": expected_line_version,
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
    line = db.scalar(select(MaintenanceSiteIssueLine).where(
        MaintenanceSiteIssueLine.issue_line_id == issue_line_id,
        MaintenanceSiteIssueLine.issue_id == issue_id,
    ).with_for_update().execution_options(populate_existing=True))
    if line is None:
        raise SiteIssueEditError("site_issue_edit_identity_conflict", "领用明细不存在或不属于本单")
    authorize(project_id)
    if issue.version != expected_issue_version or line.version != expected_line_version:
        raise SiteIssueLineCorrectionConflict(
            "site_issue_edit_version_conflict", "领用单或明细版本已变化，请刷新核对后重试",
            current_issue_version=issue.version, current_line_version=line.version,
        )
    result = apply_locked_site_issue_edits(
        db, project=project, issue=issue, line_changes={issue_line_id: changes},
        header_changes=None, operated_by=operated_by, reason=reason.strip(),
    )
    if result.changed:
        operations.bump_locked_workbook_revision(db, state=state)
    response = {
        "issue_id": issue_id, "issue_line_id": issue_line_id,
        "issue_version": result.issue_version,
        "line_version": result.line_versions[issue_line_id],
        "no_return": line.no_return, "changed": result.changed,
        "idempotent_replay": False, "workbook_revision": state.revision,
    }
    db.add(MaintenanceSiteIssueCommand(
        command_id=str(uuid4()), idempotency_key=idempotency_key,
        project_id=project_id, issue_id=issue_id, action="correct",
        request_fingerprint=fingerprint, response_json=response,
    ))
    db.flush()
    return response

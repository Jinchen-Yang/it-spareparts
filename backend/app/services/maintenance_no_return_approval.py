"""D-32 approved-file adapter; the existing single-line command owns all writes."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import permissions
from app.api.maintenance_project_scope import enforce_maintenance_project_access
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_project_operations import (
    MaintenanceSiteIssue, MaintenanceSiteIssueLine, MaintenanceSiteIssueCommand,
    MaintenanceProjectOperationAudit,
)
from app.models.system import SysUser
from app.security import UserContext, require_action, require_page
from app.services import maintenance_project_operations as operations
from app.services.maintenance_site_issue_facts import SiteIssueEditError
from app.services.maintenance_site_issue_line_correction import (
    SiteIssueLineCorrectionConflict, correct_site_issue_line,
)


class ApprovalError(ValueError):
    def __init__(self, code: str, message: str, *, status: str = "rejected"):
        super().__init__(message)
        self.detail = {"status": status, "code": code, "message": message}


class ApprovedEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    entry_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    project_id: str = Field(min_length=1, max_length=36)
    issue_id: str = Field(min_length=1, max_length=36)
    issue_line_id: str = Field(min_length=1, max_length=64)
    source: str = Field(min_length=1, max_length=32)
    expected_issue_version: StrictInt = Field(ge=1)
    expected_line_version: StrictInt = Field(ge=1)
    expected_current_no_return: StrictBool | None
    target_no_return: StrictBool | None
    reason: str = Field(min_length=1, max_length=1000)
    approver: str = Field(min_length=1, max_length=64)
    approval_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    evidence_reference: str = Field(min_length=1, max_length=500)
    predecessor_entry_id: str | None = None

    @field_validator("*")
    @classmethod
    def nonblank_text(cls, value):
        if isinstance(value, str) and (
            value != value.strip() or not value or any(ord(c) < 32 for c in value)
        ):
            raise ValueError("文本不得为空、含控制字符或首尾空白")
        return value

    @field_validator("approval_date")
    @classmethod
    def valid_date(cls, value):
        date.fromisoformat(value)
        return value

    def audit_reason(self, sha: str) -> str:
        value = "ITSP-4b " + json.dumps({
            "manifest_sha256": sha, "entry_id": self.entry_id,
            "reason": self.reason, "approver": self.approver,
            "approval_date": self.approval_date,
            "evidence_reference": self.evidence_reference,
        }, ensure_ascii=False, separators=(",", ":"))
        if len(value) > 1000:
            raise ApprovalError("manifest_reason_too_long", "原因与批准凭据合计超过审计的 1000 字限制")
        return value


@dataclass(frozen=True)
class ApprovedManifest:
    sha256: str
    entries: tuple[ApprovedEntry, ...]


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON 包含重复字段")
        result[key] = value
    return result


def load_manifest(path: Path) -> ApprovedManifest:
    """SHA binds the exact approved bytes, including CSV formatting/BOM/newlines."""
    try:
        data = path.read_bytes()
        if len(data) > 10_000_000:
            raise ValueError("清单超过 10 MB")
        sha = hashlib.sha256(data).hexdigest()
        content = data.decode("utf-8-sig")
        if path.suffix.lower() == ".csv":
            reader = csv.DictReader(io.StringIO(content, newline=""))
            if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
                raise ValueError("CSV 表头为空或重复")
            rows = list(reader)
            for row in rows:
                if None in row or any(value is None for value in row.values()):
                    raise ValueError("CSV 列数不一致")
                for name in ("expected_current_no_return", "target_no_return"):
                    row[name] = {"true": True, "false": False, "null": None}[row[name]]
                for name in ("expected_issue_version", "expected_line_version"):
                    raw = row[name]
                    if not raw.isascii() or not raw.isdecimal():
                        raise ValueError("CSV 版本须为正整数")
                    row[name] = int(raw)
                if row.get("predecessor_entry_id") == "":
                    row["predecessor_entry_id"] = None
        elif path.suffix.lower() == ".json":
            rows = json.loads(content, object_pairs_hook=_unique_object)
        else:
            raise ValueError("只接受 .json 或 .csv")
        if not isinstance(rows, list) or not 1 <= len(rows) <= 10_000:
            raise ValueError("清单须为含 1 至 10000 项的数组")
        entries = tuple(ApprovedEntry.model_validate(row) for row in rows)
        seen = {}
        lines = set()
        previous_by_issue = {}
        for entry in entries:
            if entry.entry_id in seen or entry.issue_line_id in lines:
                raise ValueError("entry_id 或目标明细重复；撤销须另建清单")
            previous = previous_by_issue.get(entry.issue_id)
            if entry.predecessor_entry_id is not None:
                if previous is None or entry.predecessor_entry_id != previous.entry_id:
                    raise ValueError("前序项须为清单中本单的紧邻前项")
                if (entry.project_id != previous.project_id
                        or entry.expected_issue_version != previous.expected_issue_version):
                    raise ValueError("因果链须使用同项目、同批准单版本")
            seen[entry.entry_id] = entry
            lines.add(entry.issue_line_id)
            previous_by_issue[entry.issue_id] = entry
            entry.audit_reason(sha)
        return ApprovedManifest(sha, entries)
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, csv.Error, RecursionError) as exc:
        if isinstance(exc, ApprovalError):
            raise
        raise ApprovalError("invalid_manifest", f"批准清单无效：{str(exc)[:1000]}") from exc


def _current(db: Session, entry: ApprovedEntry):
    project = db.get(MaintenanceProject, entry.project_id, populate_existing=True)
    issue = db.get(MaintenanceSiteIssue, entry.issue_id, populate_existing=True)
    line = db.get(MaintenanceSiteIssueLine, entry.issue_line_id, populate_existing=True)
    return project, issue, line


def _check_current(entry, project, issue, line, expected_issue_version):
    if (project is None or issue is None or line is None
            or issue.project_id != entry.project_id or line.issue_id != entry.issue_id):
        raise ApprovalError("identity_drift", "稳定身份不存在或归属已变化", status="conflict")
    if not project.is_active or not line.is_active or issue.normalized_status == "void" or issue.voided_at:
        raise ApprovalError("inactive", "项目、领用单或明细已停用／作废")
    if issue.source not in {"page_manual", "workbook", "legacy"} or line.delivery_line_id is not None:
        raise ApprovalError("source_blocked", "不允许更正原生发货或 delivery-backed 明细")
    if issue.normalized_status not in {"confirmed", "corrected"}:
        raise ApprovalError("status_blocked", "只允许已确认／已更正的领用事实")
    if issue.source != entry.source:
        raise ApprovalError("source_drift", "批准来源已变化", status="conflict")
    if (issue.version != expected_issue_version or line.version != entry.expected_line_version
            or line.no_return is not entry.expected_current_no_return):
        raise ApprovalError("approval_drift", "批准前像或版本已漂移，请重新核对", status="conflict")


def dry_run(db: Session, manifest: ApprovedManifest, *, operator: str | None = None) -> dict:
    """Own a fresh, database-enforced read-only snapshot; never create state."""
    db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
    try:
        rows = []
        for entry in manifest.entries:
            project, issue, line = _current(db, entry)
            row = {
                **entry.model_dump(),
                "old_no_return": line.no_return if line is not None else None,
                "current_issue_version": issue.version if issue else None,
                "current_line_version": line.version if line else None,
                "current_source": issue.source if issue else None,
                "current_project_id": issue.project_id if issue else None,
                "current_issue_id": line.issue_id if line else None,
                "project_name": project.display_name if project else None,
                "issue_no": issue.issue_no if issue else None,
                "pn": line.pn if line else None,
            }
            try:
                if operator is not None:
                    authorize_operator(db, operator, entry.project_id)
                saved = _command(db, manifest, entry)
                if saved is not None:
                    meta = _saved_metadata(db, saved, manifest, entry)
                    if operator is not None and meta["operator"] != operator:
                        raise ApprovalError("operator_mismatch", "重试须由原执行账号提交原请求", status="conflict")
                    row.update(
                        status="replay",
                        effective_expected_issue_version=meta["effective_expected_issue_version"],
                        receipt={**saved.response_json, "approved_manifest": meta},
                    )
                else:
                    version = _execution_version(db, manifest, entry)
                    row["effective_expected_issue_version"] = version
                    _check_current(entry, project, issue, line, version)
                    row["status"] = "no-op" if line.no_return is entry.target_no_return else "ready"
            except ApprovalError as exc:
                row.update(exc.detail)
            rows.append(row)
        return {"mode": "dry-run", "manifest_sha256": manifest.sha256,
                "permission_checked": operator is not None, "entries": rows}
    finally:
        db.rollback()


def authorize_operator(db: Session, operator: str, project_id: str | None) -> str:
    """An OS/DB-authorized human selects an existing named account, never a role.

    This is an operations CLI, not password authentication. Reuse the HTTP
    command's live account permission and project-scope guards at every call.
    """
    user = db.scalar(select(SysUser).where(
        SysUser.username == operator,
    ).execution_options(populate_existing=True))
    if user is None or not user.is_active or user.deleted_at is not None:
        raise ApprovalError("operator_required", "请指定当前有效的实名系统账号 username")
    ctx = UserContext(
        user_id=user.username, role=user.role, salesperson_name=user.salesperson_name,
        permissions=permissions.runtime_safe(permissions.effective_for_user(user)),
        is_authenticated=True,
    )
    try:
        require_page("page_maintenance")(ctx)
        require_action("action_maintenance_site_issue_manage", require_data="data_purchase_cost")(ctx)
        if project_id is not None:
            enforce_maintenance_project_access(db, project_id=project_id, ctx=ctx)
    except HTTPException as exc:
        raise ApprovalError("permission_denied", str(exc.detail)) from exc
    return user.username


def command_key(manifest: ApprovedManifest, entry: ApprovedEntry) -> str:
    digest = hashlib.sha256(f"{manifest.sha256}:{entry.entry_id}".encode()).hexdigest()
    return f"no-return-manifest:{digest}"


def _command(db: Session, manifest: ApprovedManifest, entry: ApprovedEntry):
    return db.scalar(select(MaintenanceSiteIssueCommand).where(
        MaintenanceSiteIssueCommand.idempotency_key == command_key(manifest, entry),
    ).execution_options(populate_existing=True))


def _metadata(manifest, entry, operator, expected_version):
    return {
        "schema": "approved-no-return-v1",
        "manifest_sha256": manifest.sha256,
        "entry_id": entry.entry_id, "entry": entry.model_dump(),
        "operator": operator, "effective_expected_issue_version": expected_version,
    }


def _saved_metadata(db, command, manifest, entry):
    response = command.response_json
    audits = list(db.scalars(select(MaintenanceProjectOperationAudit).where(
        MaintenanceProjectOperationAudit.entity_type == "no_return_approved_entry",
        MaintenanceProjectOperationAudit.entity_id == command.command_id,
        MaintenanceProjectOperationAudit.action == "approved_manifest_applied",
    )))
    if len(audits) != 1:
        raise ApprovalError("receipt_mismatch", "缺少唯一的批准执行审计", status="conflict")
    meta = audits[0].after_json or {}
    version = meta.get("effective_expected_issue_version")
    if (type(version) is not int or version < 1
            or meta != _metadata(manifest, entry, meta.get("operator"), version)
            or audits[0].project_id != entry.project_id
            or audits[0].operated_by != meta.get("operator")
            or audits[0].reason != entry.audit_reason(manifest.sha256)
            or command.action != "correct" or command.project_id != entry.project_id
            or command.issue_id != entry.issue_id
            or response.get("issue_id") != entry.issue_id
            or response.get("issue_line_id") != entry.issue_line_id):
        raise ApprovalError("receipt_mismatch", "幂等键未绑定本批准项的完整回执", status="conflict")
    return meta


def _execution_version(db, manifest, entry):
    if entry.predecessor_entry_id is None:
        return entry.expected_issue_version
    predecessor = next(item for item in manifest.entries if item.entry_id == entry.predecessor_entry_id)
    saved = _command(db, manifest, predecessor)
    if saved is None:
        raise ApprovalError("causal_receipt_required", "前序批准项尚无成功回执；不得刷新版本", status="conflict")
    _saved_metadata(db, saved, manifest, predecessor)
    # No lock on the predecessor's command key: its receipt is immutable, and
    # taking another key after the project lock would invert the lock order.
    return saved.response_json["issue_version"]


def apply_one(
    db: Session, manifest: ApprovedManifest, *, entry_id: str,
    manifest_sha256: str, operator: str,
) -> dict:
    """One root transaction, one existing command; no assignment to fact columns."""
    if manifest_sha256 != manifest.sha256:
        raise ApprovalError("manifest_sha256_mismatch", "清单内容与批准 SHA256 不符")
    entry = next((item for item in manifest.entries if item.entry_id == entry_id), None)
    if entry is None:
        raise ApprovalError("unknown_entry", "清单中不存在指定的 entry_id")
    try:
        authorize_operator(db, operator, entry.project_id)
        # Pre-acquire exactly the command's lock prefix. Reentrant locks let the
        # unchanged #423 command enforce its own guards and remain the only writer.
        operations._lock_idempotency_key(db, command_key(manifest, entry))
        project = operations._lock_project_for_fact_write(db, entry.project_id)
        if project is not None:
            db.refresh(project)
        issue = db.scalar(select(MaintenanceSiteIssue).where(
            MaintenanceSiteIssue.issue_id == entry.issue_id,
            MaintenanceSiteIssue.project_id == entry.project_id,
        ).with_for_update().execution_options(populate_existing=True))
        authorize_operator(db, operator, entry.project_id)
        if issue is None:
            raise ApprovalError("identity_drift", "领用单不存在或归属已变化", status="conflict")
        saved = _command(db, manifest, entry)
        if saved is not None:
            meta = _saved_metadata(db, saved, manifest, entry)
            expected_version = meta["effective_expected_issue_version"]
        else:
            expected_version = _execution_version(db, manifest, entry)
            meta = _metadata(manifest, entry, operator, expected_version)
            line = db.scalar(select(MaintenanceSiteIssueLine).where(
                MaintenanceSiteIssueLine.issue_line_id == entry.issue_line_id,
                MaintenanceSiteIssueLine.issue_id == entry.issue_id,
            ).with_for_update().execution_options(populate_existing=True))
            authorize_operator(db, operator, entry.project_id)
            _check_current(entry, project, issue, line, expected_version)
        receipt = correct_site_issue_line(
            db, issue_id=entry.issue_id, issue_line_id=entry.issue_line_id,
            expected_issue_version=expected_version,
            expected_line_version=entry.expected_line_version,
            changes={"no_return": entry.target_no_return},
            reason=entry.audit_reason(manifest.sha256),
            idempotency_key=command_key(manifest, entry),
            authorize=lambda project_id: authorize_operator(db, operator, project_id),
        )
        if not receipt["idempotent_replay"]:
            # The original command receipt is append-only. Store the approval
            # envelope as a separate append-only audit in this same transaction.
            db.add(MaintenanceProjectOperationAudit(
                project_id=entry.project_id, entity_type="no_return_approved_entry",
                entity_id=_command(db, manifest, entry).command_id,
                action="approved_manifest_applied",
                before_json=None, after_json=meta,
                reason=entry.audit_reason(manifest.sha256), operated_by=operator,
            ))
            db.flush()
        db.commit()
        return {
            "mode": "apply-one", "manifest_sha256": manifest.sha256,
            "entry_id": entry.entry_id,
            "status": "replay" if receipt["idempotent_replay"] else "success" if receipt["changed"] else "no-op",
            "receipt": {**receipt, "approved_manifest": meta},
        }
    except SiteIssueLineCorrectionConflict as exc:
        db.rollback()
        raise ApprovalError(exc.detail["code"], exc.detail["message"], status="conflict") from exc
    except SiteIssueEditError as exc:
        db.rollback()
        raise ApprovalError(exc.code, exc.message) from exc
    except IntegrityError as exc:
        db.rollback()
        raise ApprovalError("write_conflict", "数据库完整性冲突；请核对原请求", status="conflict") from exc
    except BaseException:
        db.rollback()
        raise

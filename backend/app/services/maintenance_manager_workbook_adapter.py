"""Database boundary for the project-manager monthly workbook v3 (#206)."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Mapping
from uuid import uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.maintenance_manager import (
    BusinessFile,
    BusinessFileLink,
    MaintenanceAcceptanceDeliverable,
    MaintenanceCollectionMilestone,
    MaintenanceManagerUploadBatch,
    MaintenanceManagerUploadBatchProject,
    MaintenanceServicePeriod,
)
from app.models.maintenance_project import (
    MaintenanceProject,
    MaintenanceProjectContract,
    MaintenanceProjectUserAssignment,
)
from app.models.maintenance_project_operations import (
    MaintenanceCollectionSnapshot,
    MaintenanceProjectOperationAudit,
)
from app.models.system import SysUser
from app.security import UserContext, is_field_hidden
from app.services.maintenance_manager_workbook_v3 import (
    SCHEMA_VERSION,
    TEMPLATE_VERSION,
    AcceptanceDueDateChange,
    ManagerWorkbookExportArtifact,
    ManagerWorkbookValidation,
    MilestoneChange,
    ServicePeriodChange,
    WorkbookIssue,
    build_manager_workbook,
    validate_manager_workbook,
)


VALIDATION_TTL = timedelta(hours=24)


class ManagerWorkbookPermissionError(Exception):
    """The current account cannot use the own-scope manager workbook."""


class ManagerWorkbookConflict(Exception):
    """Scope, version, or validation state changed before atomic apply."""


class ManagerWorkbookNotFound(Exception):
    """The requested validation batch is not visible to this owner."""


class ManagerWorkbookInvalid(Exception):
    """The validation batch cannot be applied."""


def _month(value: date) -> date:
    return value.replace(day=1)


def _json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _stable_hash(value: Any) -> str:
    raw = json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _issue_dict(issue: WorkbookIssue) -> dict:
    return {
        "code": issue.code,
        "message": issue.message,
        "sheet": issue.sheet,
        "row": issue.row,
        "column": issue.column,
        "severity": issue.severity,
    }


def _issue_from_dict(value: Mapping[str, Any]) -> WorkbookIssue:
    return WorkbookIssue(
        code=str(value.get("code") or "workbook_issue"),
        message=str(value.get("message") or "工作簿校验问题"),
        sheet=str(value["sheet"]) if value.get("sheet") is not None else None,
        row=int(value["row"]) if value.get("row") is not None else None,
        column=str(value["column"]) if value.get("column") is not None else None,
        severity=str(value.get("severity") or "error"),
    )


def _change_dict(
    change: ServicePeriodChange | MilestoneChange | AcceptanceDueDateChange,
) -> dict:
    return _json_value(asdict(change))


def _preview_changes(
    validation: ManagerWorkbookValidation,
    snapshot: Mapping[str, Any],
) -> list[dict]:
    projects = {
        str(project.get("project_id")): project
        for project in snapshot.get("projects") or []
    }
    contracts = {
        str(contract.get("project_contract_id")): (project, contract)
        for project in projects.values()
        for contract in project.get("contracts") or []
    }
    items: list[dict] = []
    for change in validation.service_period_changes:
        project = projects.get(change.project_id) or {}
        items.append(
            {
                "kind": "service_period",
                "project_id": change.project_id,
                "project_code": project.get("project_code"),
                "project_name": project.get("project_name"),
                "contract_no": None,
                "sequence": None,
                "before": {
                    "service_start": project.get("service_start"),
                    "service_end": project.get("service_end"),
                    "completeness_state": (
                        "complete"
                        if project.get("service_start") and project.get("service_end")
                        else "start_only"
                        if project.get("service_start")
                        else "end_only"
                        if project.get("service_end")
                        else "empty"
                    ),
                },
                "after": {
                    "service_start": change.service_start,
                    "service_end": change.service_end,
                    "completeness_state": change.completeness_state,
                },
            }
        )
    for change in validation.acceptance_due_date_changes:
        project = projects.get(change.project_id) or {}
        acceptance = project.get("acceptance") or {}
        items.append(
            {
                "kind": "acceptance_due_date",
                "project_id": change.project_id,
                "project_code": project.get("project_code"),
                "project_name": project.get("project_name"),
                "contract_no": None,
                "sequence": None,
                "before": {
                    "due_date": acceptance.get("due_date"),
                    "configuration_state": acceptance.get("configuration_state"),
                },
                "after": {
                    "due_date": change.due_date,
                    "configuration_state": "configured",
                },
            }
        )
    for change in validation.milestone_changes:
        project, contract = contracts.get(
            change.project_contract_id, ({}, {})
        )
        current = next(
            (
                row
                for row in contract.get("planned_milestones") or []
                if int(row.get("sequence") or 0) == change.sequence
            ),
            None,
        )
        items.append(
            {
                "kind": "planned_collection_milestone",
                "project_id": change.project_id,
                "project_code": project.get("project_code"),
                "project_name": project.get("project_name"),
                "project_contract_id": change.project_contract_id,
                "contract_no": contract.get("contract_no"),
                "sequence": change.sequence,
                "before": {
                    "planned_date": current.get("planned_date") if current else None,
                    "planned_amount": current.get("planned_amount") if current else None,
                    "completeness_state": (
                        current.get("completeness_state") if current else None
                    ),
                },
                "after": {
                    "planned_date": change.planned_date,
                    "planned_amount": change.planned_amount,
                    "completeness_state": change.completeness_state,
                },
            }
        )
    return _json_value(items)


def _validation_plan(validation: ManagerWorkbookValidation, snapshot: Mapping[str, Any]) -> dict:
    return {
        "service_period_changes": [
            _change_dict(change) for change in validation.service_period_changes
        ],
        "milestone_changes": [
            _change_dict(change) for change in validation.milestone_changes
        ],
        "acceptance_due_date_changes": [
            _change_dict(change)
            for change in validation.acceptance_due_date_changes
        ],
        "project_scope": [
            {
                "project_id": str(project.get("project_id") or ""),
                "project_version": int(project.get("project_version") or 0),
                "assignment_id": str(project.get("assignment_id") or ""),
                "assignment_version": int(project.get("assignment_version") or 0),
            }
            for project in snapshot.get("projects") or []
        ],
        "warnings": [_issue_dict(issue) for issue in validation.warnings],
        "preview_changes": _preview_changes(validation, snapshot),
    }


def _validation_from_batch(batch: MaintenanceManagerUploadBatch) -> ManagerWorkbookValidation:
    plan = batch.plan_json or {}
    service_changes = tuple(
        ServicePeriodChange(
            project_id=str(row["project_id"]),
            project_version=int(row["project_version"]),
            expected_version=int(row["expected_version"]),
            service_start=date.fromisoformat(row["service_start"]) if row.get("service_start") else None,
            service_end=date.fromisoformat(row["service_end"]) if row.get("service_end") else None,
            completeness_state=str(row["completeness_state"]),
        )
        for row in plan.get("service_period_changes") or []
    )
    milestone_changes = tuple(
        MilestoneChange(
            project_id=str(row["project_id"]),
            project_contract_id=str(row["project_contract_id"]),
            sequence=int(row["sequence"]),
            project_version=int(row["project_version"]),
            contract_version=int(row["contract_version"]),
            expected_version=int(row["expected_version"]),
            planned_date=date.fromisoformat(row["planned_date"]) if row.get("planned_date") else None,
            planned_amount=Decimal(str(row["planned_amount"])) if row.get("planned_amount") is not None else None,
            completeness_state=str(row["completeness_state"]),
        )
        for row in plan.get("milestone_changes") or []
    )
    acceptance_due_date_changes = tuple(
        AcceptanceDueDateChange(
            project_id=str(row["project_id"]),
            project_version=int(row["project_version"]),
            expected_version=int(row["expected_version"]),
            due_date=date.fromisoformat(str(row["due_date"])),
        )
        for row in plan.get("acceptance_due_date_changes") or []
    )
    issues = [_issue_from_dict(row) for row in batch.issues_json or []]
    warnings = tuple(issue for issue in issues if issue.severity == "warning")
    errors = tuple(issue for issue in issues if issue.severity != "warning")
    return ManagerWorkbookValidation(
        validation_id=batch.batch_id,
        export_id=batch.export_id,
        owner_user_id=batch.owner_user_id,
        report_month=batch.report_month,
        scope_version=batch.scope_version,
        data_version=batch.data_version,
        file_sha256=batch.file_sha256,
        service_period_changes=service_changes,
        milestone_changes=milestone_changes,
        acceptance_due_date_changes=acceptance_due_date_changes,
        warnings=warnings,
        errors=errors,
    )


class MaintenanceManagerWorkbookAdapter:
    """Load own-scope facts and apply a persisted validation plan atomically."""

    def __init__(
        self,
        db: Session,
        *,
        user_ctx: UserContext,
        operator: str,
        as_of: date,
    ) -> None:
        self.db = db
        self.user_ctx = user_ctx
        self.operator = str(operator or "").strip()[:64]
        self.as_of = as_of
        if not self.operator:
            raise ManagerWorkbookPermissionError("缺少可审计的操作人")

    def _owner(self, *, lock: bool = False) -> SysUser:
        if not self.user_ctx.is_authenticated or not self.user_ctx.user_id:
            raise ManagerWorkbookPermissionError("请先登录")
        statement = select(SysUser).where(
            SysUser.username == self.user_ctx.user_id,
            SysUser.is_active.is_(True),
        )
        if lock:
            statement = statement.with_for_update()
        owner = self.db.scalar(statement)
        if owner is None:
            raise ManagerWorkbookPermissionError("当前项目经理账号不存在或已停用")
        if is_field_hidden(self.user_ctx, "contract_amount"):
            raise ManagerWorkbookPermissionError(
                "月度工作簿包含全部合同额；当前账号尚未获得对应数据权限"
            )
        return owner

    def load_snapshot(self, report_month: date, *, lock: bool = False) -> dict:
        report_month = _month(report_month)
        owner = self._owner(lock=lock)
        assignment_statement = (
            select(MaintenanceProjectUserAssignment, MaintenanceProject)
            .join(
                MaintenanceProject,
                MaintenanceProject.project_id
                == MaintenanceProjectUserAssignment.project_id,
            )
            .where(
                MaintenanceProjectUserAssignment.user_id == owner.id,
                MaintenanceProjectUserAssignment.responsibility_type == "primary_manager",
                MaintenanceProjectUserAssignment.archived_at.is_(None),
                MaintenanceProject.is_active.is_(True),
            )
            .order_by(
                MaintenanceProject.project_id,
                MaintenanceProjectUserAssignment.assignment_id,
            )
        )
        if lock:
            assignment_statement = assignment_statement.with_for_update()
        assignment_rows = list(self.db.execute(assignment_statement))
        if not assignment_rows:
            raise ManagerWorkbookPermissionError(
                "当前账号未分配任何有效维保项目，不能使用项目经理月度工作簿"
            )
        project_ids = [project.project_id for _assignment, project in assignment_rows]

        contract_rows: list[MaintenanceProjectContract] = []
        if project_ids:
            contract_statement = (
                select(MaintenanceProjectContract)
                .where(
                    MaintenanceProjectContract.project_id.in_(project_ids),
                    MaintenanceProjectContract.included_in_total.is_(True),
                    MaintenanceProjectContract.effective_from <= self.as_of,
                    or_(
                        MaintenanceProjectContract.effective_to.is_(None),
                        MaintenanceProjectContract.effective_to > self.as_of,
                    ),
                )
                .order_by(
                    MaintenanceProjectContract.project_id,
                    MaintenanceProjectContract.contract_no,
                    MaintenanceProjectContract.project_contract_id,
                )
            )
            if lock:
                contract_statement = contract_statement.with_for_update()
            contract_rows = list(self.db.scalars(contract_statement))
        contract_ids = [row.project_contract_id for row in contract_rows]

        service_periods: dict[str, MaintenanceServicePeriod] = {}
        milestones: list[MaintenanceCollectionMilestone] = []
        acceptance_rows: list[MaintenanceAcceptanceDeliverable] = []
        collection_rows: list[MaintenanceCollectionSnapshot] = []
        if project_ids:
            period_statement = (
                select(MaintenanceServicePeriod)
                .where(MaintenanceServicePeriod.project_id.in_(project_ids))
                .order_by(MaintenanceServicePeriod.project_id)
            )
            acceptance_statement = (
                select(MaintenanceAcceptanceDeliverable)
                .where(MaintenanceAcceptanceDeliverable.project_id.in_(project_ids))
                .order_by(
                    MaintenanceAcceptanceDeliverable.project_id,
                    MaintenanceAcceptanceDeliverable.deliverable_type,
                )
            )
            if lock:
                period_statement = period_statement.with_for_update()
                acceptance_statement = acceptance_statement.with_for_update()
            service_periods = {
                row.project_id: row for row in self.db.scalars(period_statement)
            }
            acceptance_rows = list(self.db.scalars(acceptance_statement))
        if contract_ids:
            milestone_statement = (
                select(MaintenanceCollectionMilestone)
                .where(
                    MaintenanceCollectionMilestone.project_contract_id.in_(contract_ids)
                )
                .order_by(
                    MaintenanceCollectionMilestone.project_contract_id,
                    MaintenanceCollectionMilestone.sequence,
                )
            )
            collection_statement = (
                select(MaintenanceCollectionSnapshot)
                .where(
                    MaintenanceCollectionSnapshot.project_contract_id.in_(contract_ids),
                    MaintenanceCollectionSnapshot.status == "confirmed",
                    MaintenanceCollectionSnapshot.report_month <= report_month,
                )
                .order_by(
                    MaintenanceCollectionSnapshot.project_contract_id,
                    MaintenanceCollectionSnapshot.report_month,
                    MaintenanceCollectionSnapshot.collection_id,
                )
            )
            if lock:
                milestone_statement = milestone_statement.with_for_update()
                collection_statement = collection_statement.with_for_update()
            milestones = list(self.db.scalars(milestone_statement))
            collection_rows = list(self.db.scalars(collection_statement))

        attachments_by_deliverable: dict[str, int] = defaultdict(int)
        deliverable_ids = [row.deliverable_id for row in acceptance_rows]
        if deliverable_ids:
            for entity_id, count in self.db.execute(
                select(BusinessFileLink.entity_id, func.count(BusinessFileLink.link_id))
                .join(BusinessFile, BusinessFile.file_id == BusinessFileLink.file_id)
                .where(
                    BusinessFileLink.entity_type
                    == "maintenance_acceptance_deliverable",
                    BusinessFileLink.entity_id.in_(deliverable_ids),
                    BusinessFileLink.archived_at.is_(None),
                    BusinessFile.security_state == "active",
                )
                .group_by(BusinessFileLink.entity_id)
            ):
                attachments_by_deliverable[str(entity_id)] = int(count)

        contracts_by_project: dict[str, list[MaintenanceProjectContract]] = defaultdict(list)
        for contract in contract_rows:
            contracts_by_project[contract.project_id].append(contract)
        milestones_by_contract: dict[str, list[MaintenanceCollectionMilestone]] = defaultdict(list)
        for milestone in milestones:
            milestones_by_contract[milestone.project_contract_id].append(milestone)
        latest_actual: dict[str, MaintenanceCollectionSnapshot] = {}
        for collection in collection_rows:
            latest_actual[collection.project_contract_id] = collection
        acceptance_by_project: dict[str, MaintenanceAcceptanceDeliverable] = {}
        for deliverable in acceptance_rows:
            if deliverable.deliverable_type == "acceptance_report":
                acceptance_by_project[deliverable.project_id] = deliverable

        projects: list[dict] = []
        for assignment, project in assignment_rows:
            period = service_periods.get(project.project_id)
            deliverable = acceptance_by_project.get(project.project_id)
            contract_payload = []
            for contract in contracts_by_project[project.project_id]:
                actual = latest_actual.get(contract.project_contract_id)
                contract_payload.append(
                    {
                        "project_contract_id": contract.project_contract_id,
                        "contract_no": contract.contract_no,
                        "contract_amount": contract.contract_amount,
                        "contract_version": contract.version,
                        "confirmed_received_amount": (
                            actual.cumulative_amount if actual is not None else Decimal("0")
                        ),
                        "confirmed_collection_version": actual.version if actual else 0,
                        "planned_milestones": [
                            {
                                "milestone_id": milestone.milestone_id,
                                "sequence": milestone.sequence,
                                "planned_date": milestone.planned_date,
                                "planned_amount": milestone.planned_amount,
                                "completeness_state": milestone.completeness_state,
                                "version": milestone.version,
                            }
                            for milestone in milestones_by_contract[
                                contract.project_contract_id
                            ]
                        ],
                    }
                )
            projects.append(
                {
                    "project_id": project.project_id,
                    "project_code": project.project_code,
                    "project_name": project.display_name,
                    "project_version": project.version,
                    "assignment_id": assignment.assignment_id,
                    "assignment_version": assignment.version,
                    "service_start": period.service_start if period else None,
                    "service_end": period.service_end if period else None,
                    "service_period_version": period.version if period else 0,
                    "contracts": contract_payload,
                    "acceptance": {
                        "deliverable_id": deliverable.deliverable_id if deliverable else None,
                        "due_date": deliverable.due_date if deliverable else None,
                        "configuration_state": (
                            deliverable.configuration_state
                            if deliverable
                            else "pending_business_configuration"
                        ),
                        "submission_status": (
                            deliverable.submission_status if deliverable else "not_submitted"
                        ),
                        "submitted_at": deliverable.submitted_at if deliverable else None,
                        "approval_status": (
                            deliverable.approval_status if deliverable else "not_reviewed"
                        ),
                        "approved_at": deliverable.approved_at if deliverable else None,
                        "approved_by": deliverable.approved_by if deliverable else None,
                        "attachment_count": (
                            attachments_by_deliverable[deliverable.deliverable_id]
                            if deliverable
                            else 0
                        ),
                        "version": deliverable.version if deliverable else 0,
                    },
                }
            )

        scope_projection = [
            (
                row["project_id"],
                row["project_version"],
                row["assignment_id"],
                row["assignment_version"],
            )
            for row in projects
        ]
        data_projection = [
            {
                "project_id": row["project_id"],
                "project_version": row["project_version"],
                "service_start": row["service_start"],
                "service_end": row["service_end"],
                "service_period_version": row["service_period_version"],
                "contracts": row["contracts"],
                "acceptance": row["acceptance"],
            }
            for row in projects
        ]
        return {
            "owner": {
                "user_id": owner.id,
                "username": owner.username,
                "display_name": owner.display_name,
            },
            "report_month": report_month,
            "scope_version": _stable_hash(scope_projection),
            "data_version": _stable_hash(data_projection),
            "projects": projects,
        }

    def export(
        self,
        report_month: date,
        *,
        hmac_key: bytes,
    ) -> tuple[ManagerWorkbookExportArtifact, dict]:
        snapshot = self.load_snapshot(report_month)
        artifact = build_manager_workbook(snapshot, hmac_key=hmac_key)
        return artifact, snapshot

    def validate(
        self,
        report_month: date,
        content: bytes,
        *,
        hmac_key: bytes,
    ) -> tuple[ManagerWorkbookValidation, MaintenanceManagerUploadBatch]:
        report_month = _month(report_month)
        # Serialize validation per owner so identical uploads cannot race the
        # operation-key uniqueness guard across multiple API workers.
        owner = self._owner(lock=True)
        file_sha256 = hashlib.sha256(content).hexdigest()
        existing = self.db.scalar(
            select(MaintenanceManagerUploadBatch)
            .where(
                MaintenanceManagerUploadBatch.owner_user_id == owner.id,
                MaintenanceManagerUploadBatch.report_month == report_month,
                MaintenanceManagerUploadBatch.file_sha256 == file_sha256,
            )
            .order_by(MaintenanceManagerUploadBatch.created_at.desc())
            .limit(1)
        )
        now = datetime.now(UTC)
        if existing is not None and (
            existing.status == "applied"
            or (existing.status in {"valid", "error"} and existing.expires_at > now)
        ):
            return _validation_from_batch(existing), existing

        snapshot = self.load_snapshot(report_month)
        validation = validate_manager_workbook(
            content,
            snapshot=snapshot,
            hmac_key=hmac_key,
        )
        if existing is not None:
            validation = replace(validation, validation_id=existing.batch_id)
        plan = _validation_plan(validation, snapshot) if validation.can_apply else None
        semantic_hash = _stable_hash(
            {
                "owner_user_id": owner.id,
                "report_month": report_month,
                "data_version": validation.data_version,
                "plan": plan,
            }
        )
        operation_key = _stable_hash(
            {
                "owner_user_id": owner.id,
                "report_month": report_month,
                "export_id": validation.export_id,
                "file_sha256": validation.file_sha256,
            }
        )
        issues = [
            *(_issue_dict(issue) for issue in validation.warnings),
            *(_issue_dict(issue) for issue in validation.errors),
        ]
        if existing is None:
            batch = MaintenanceManagerUploadBatch(
                batch_id=validation.validation_id,
                owner_user_id=owner.id,
                report_month=report_month,
                protocol_version=SCHEMA_VERSION,
                template_version=TEMPLATE_VERSION,
                export_id=validation.export_id,
                file_sha256=validation.file_sha256,
                file_size=len(content),
                operation_key=operation_key,
                semantic_hash=semantic_hash,
                scope_version=validation.scope_version,
                data_version=validation.data_version,
                status="valid" if validation.can_apply else "error",
                plan_json=plan,
                issues_json=issues,
                created_by=self.operator,
                created_at=now,
                expires_at=now + VALIDATION_TTL,
            )
            self.db.add(batch)
        else:
            # A file has one stable operation key. Refresh an expired preview
            # in that same idempotency row instead of attempting a duplicate
            # insert that could never satisfy the database uniqueness guard.
            batch = existing
            batch.protocol_version = SCHEMA_VERSION
            batch.template_version = TEMPLATE_VERSION
            batch.semantic_hash = semantic_hash
            batch.scope_version = validation.scope_version
            batch.data_version = validation.data_version
            batch.status = "valid" if validation.can_apply else "error"
            batch.plan_json = plan
            batch.issues_json = issues
            batch.error_workbook = None
            batch.result_json = None
            batch.created_by = self.operator
            batch.created_at = now
            batch.expires_at = now + VALIDATION_TTL
            batch.applied_by = None
            batch.applied_at = None
        self.db.flush()
        return validation, batch

    def apply(self, batch_id: str, *, data_version: str | None = None) -> dict:
        owner = self._owner(lock=True)
        batch = self.db.scalar(
            select(MaintenanceManagerUploadBatch)
            .where(MaintenanceManagerUploadBatch.batch_id == batch_id)
            .with_for_update()
        )
        if batch is None or batch.owner_user_id != owner.id:
            raise ManagerWorkbookNotFound("校验批次不存在")
        if data_version is not None and data_version != batch.data_version:
            raise ManagerWorkbookConflict("校验数据版本与确认请求不一致")
        if batch.status == "applied":
            return dict(batch.result_json or {})
        if batch.status != "valid" or batch.plan_json is None:
            raise ManagerWorkbookInvalid("校验批次不可应用，请重新上传并校验")
        now = datetime.now(UTC)
        if batch.expires_at <= now:
            raise ManagerWorkbookConflict("校验已过期，请重新上传")

        try:
            snapshot = self.load_snapshot(batch.report_month, lock=True)
        except ManagerWorkbookPermissionError as exc:
            raise ManagerWorkbookConflict(
                "本人负责项目范围已变化，请重新下载"
            ) from exc
        if (
            snapshot["scope_version"] != batch.scope_version
            or snapshot["data_version"] != batch.data_version
        ):
            raise ManagerWorkbookConflict("本人负责项目范围或数据版本已变化，请重新下载")

        plan = batch.plan_json
        changed_rows = 0
        reason = f"项目经理 {batch.report_month:%Y-%m} 月度全量工作簿 v3 应用"
        for value in plan.get("service_period_changes") or []:
            change = ServicePeriodChange(
                project_id=str(value["project_id"]),
                project_version=int(value["project_version"]),
                expected_version=int(value["expected_version"]),
                service_start=date.fromisoformat(value["service_start"]) if value.get("service_start") else None,
                service_end=date.fromisoformat(value["service_end"]) if value.get("service_end") else None,
                completeness_state=str(value["completeness_state"]),
            )
            current = self.db.scalar(
                select(MaintenanceServicePeriod)
                .where(MaintenanceServicePeriod.project_id == change.project_id)
                .with_for_update()
            )
            if change.expected_version == 0:
                if current is not None:
                    raise ManagerWorkbookConflict("维保期限已被其他操作更新")
                before = None
                current = MaintenanceServicePeriod(
                    project_id=change.project_id,
                    service_start=change.service_start,
                    service_end=change.service_end,
                    completeness_state=change.completeness_state,
                    source="manager_workbook_v3",
                    source_batch_id=batch.batch_id,
                    version=1,
                )
                self.db.add(current)
            else:
                if current is None or current.version != change.expected_version:
                    raise ManagerWorkbookConflict("维保期限版本已变化")
                before = {
                    "service_start": current.service_start,
                    "service_end": current.service_end,
                    "completeness_state": current.completeness_state,
                    "version": current.version,
                }
                current.service_start = change.service_start
                current.service_end = change.service_end
                current.completeness_state = change.completeness_state
                current.source = "manager_workbook_v3"
                current.source_batch_id = batch.batch_id
                current.version += 1
            after = {
                "service_start": change.service_start,
                "service_end": change.service_end,
                "completeness_state": change.completeness_state,
                "version": current.version,
            }
            self.db.add(
                MaintenanceProjectOperationAudit(
                    project_id=change.project_id,
                    entity_type="service_period",
                    entity_id=change.project_id,
                    action="manager_workbook_apply",
                    before_json=_json_value(before),
                    after_json=_json_value(after),
                    reason=reason,
                    operated_by=self.operator,
                )
            )
            changed_rows += 1

        for value in plan.get("acceptance_due_date_changes") or []:
            change = AcceptanceDueDateChange(
                project_id=str(value["project_id"]),
                project_version=int(value["project_version"]),
                expected_version=int(value["expected_version"]),
                due_date=date.fromisoformat(str(value["due_date"])),
            )
            current = self.db.scalar(
                select(MaintenanceAcceptanceDeliverable)
                .where(
                    MaintenanceAcceptanceDeliverable.project_id
                    == change.project_id,
                    MaintenanceAcceptanceDeliverable.deliverable_type
                    == "acceptance_report",
                )
                .with_for_update()
            )
            if change.expected_version == 0:
                if current is not None:
                    raise ManagerWorkbookConflict("验收报告截止日已被其他操作创建")
                before = None
                current = MaintenanceAcceptanceDeliverable(
                    deliverable_id=str(uuid4()),
                    project_id=change.project_id,
                    deliverable_type="acceptance_report",
                    due_date=change.due_date,
                    submission_status="not_submitted",
                    submitted_at=None,
                    submitted_by=None,
                    approval_status="not_reviewed",
                    approved_at=None,
                    approved_by=None,
                    rejection_reason=None,
                    configuration_state="configured",
                    version=1,
                )
                self.db.add(current)
            else:
                if current is None or current.version != change.expected_version:
                    raise ManagerWorkbookConflict("验收报告版本已变化")
                before = {
                    "due_date": current.due_date,
                    "configuration_state": current.configuration_state,
                    "version": current.version,
                }
                current.due_date = change.due_date
                current.configuration_state = "configured"
                current.version += 1
            after = {
                "due_date": current.due_date,
                "configuration_state": current.configuration_state,
                "version": current.version,
            }
            self.db.add(
                MaintenanceProjectOperationAudit(
                    project_id=change.project_id,
                    entity_type="acceptance_deliverable",
                    entity_id=current.deliverable_id,
                    action="manager_workbook_apply",
                    before_json=_json_value(before),
                    after_json=_json_value(after),
                    reason=reason,
                    operated_by=self.operator,
                )
            )
            changed_rows += 1

        for value in plan.get("milestone_changes") or []:
            change = MilestoneChange(
                project_id=str(value["project_id"]),
                project_contract_id=str(value["project_contract_id"]),
                sequence=int(value["sequence"]),
                project_version=int(value["project_version"]),
                contract_version=int(value["contract_version"]),
                expected_version=int(value["expected_version"]),
                planned_date=date.fromisoformat(value["planned_date"]) if value.get("planned_date") else None,
                planned_amount=Decimal(str(value["planned_amount"])) if value.get("planned_amount") is not None else None,
                completeness_state=str(value["completeness_state"]),
            )
            current = self.db.scalar(
                select(MaintenanceCollectionMilestone)
                .where(
                    MaintenanceCollectionMilestone.project_contract_id
                    == change.project_contract_id,
                    MaintenanceCollectionMilestone.sequence == change.sequence,
                )
                .with_for_update()
            )
            if change.expected_version == 0:
                if current is not None:
                    raise ManagerWorkbookConflict("计划回款节点已被其他操作创建")
                before = None
                current = MaintenanceCollectionMilestone(
                    milestone_id=str(uuid4()),
                    project_id=change.project_id,
                    project_contract_id=change.project_contract_id,
                    sequence=change.sequence,
                    planned_date=change.planned_date,
                    planned_amount=change.planned_amount,
                    completeness_state=change.completeness_state,
                    source="manager_workbook_v3",
                    source_batch_id=batch.batch_id,
                    version=1,
                )
                self.db.add(current)
            else:
                if current is None or current.version != change.expected_version:
                    raise ManagerWorkbookConflict("计划回款节点版本已变化")
                before = {
                    "planned_date": current.planned_date,
                    "planned_amount": current.planned_amount,
                    "completeness_state": current.completeness_state,
                    "version": current.version,
                }
                current.planned_date = change.planned_date
                current.planned_amount = change.planned_amount
                current.completeness_state = change.completeness_state
                current.source = "manager_workbook_v3"
                current.source_batch_id = batch.batch_id
                current.version += 1
            after = {
                "planned_date": change.planned_date,
                "planned_amount": change.planned_amount,
                "completeness_state": change.completeness_state,
                "version": current.version,
            }
            self.db.add(
                MaintenanceProjectOperationAudit(
                    project_id=change.project_id,
                    entity_type="collection_milestone",
                    entity_id=f"{change.project_contract_id}:{change.sequence}",
                    action="manager_workbook_apply",
                    before_json=_json_value(before),
                    after_json=_json_value(after),
                    reason=reason,
                    operated_by=self.operator,
                )
            )
            changed_rows += 1

        for project in plan.get("project_scope") or []:
            self.db.add(
                MaintenanceManagerUploadBatchProject(
                    batch_id=batch.batch_id,
                    project_id=str(project["project_id"]),
                    assignment_id=str(project["assignment_id"]),
                    assignment_version=int(project["assignment_version"]),
                    project_version=int(project["project_version"]),
                    applied_at=now,
                )
            )
        result = {
            "applied": True,
            "replayed": False,
            "batch_id": batch.batch_id,
            "changed_rows": changed_rows,
            "project_count": len(plan.get("project_scope") or []),
            "warnings": len(plan.get("warnings") or []),
            "report_month": batch.report_month.isoformat(),
        }
        batch.status = "applied"
        batch.applied_by = self.operator
        batch.applied_at = now
        batch.result_json = result
        self.db.flush()
        return result

    def status(self, report_month: date) -> dict:
        report_month = _month(report_month)
        owner = self._owner()
        snapshot = self.load_snapshot(report_month)
        latest = self.db.scalar(
            select(MaintenanceManagerUploadBatch)
            .where(
                MaintenanceManagerUploadBatch.owner_user_id == owner.id,
                MaintenanceManagerUploadBatch.report_month == report_month,
            )
            .order_by(MaintenanceManagerUploadBatch.created_at.desc())
            .limit(1)
        )
        configured = sum(
            1
            for project in snapshot["projects"]
            if (project.get("acceptance") or {}).get("configuration_state")
            == "configured"
        )
        latest_status = latest.status if latest else None
        scope_matches_current = bool(
            latest is not None
            and latest.scope_version == snapshot["scope_version"]
        )
        if (
            latest is not None
            and latest.status in {"valid", "error"}
            and latest.expires_at <= datetime.now(UTC)
        ):
            latest_status = "expired"
        elif latest is not None and latest.status == "applied" and not scope_matches_current:
            latest_status = "stale_scope"
        return {
            "report_month": report_month.isoformat(),
            "project_count": len(snapshot["projects"]),
            "scope_version": snapshot["scope_version"],
            "data_version": snapshot["data_version"],
            "latest_batch": (
                {
                    "batch_id": latest.batch_id,
                    "status": latest_status,
                    "scope_matches_current": scope_matches_current,
                    "created_at": latest.created_at.isoformat(),
                    "expires_at": latest.expires_at.isoformat(),
                    "applied_at": latest.applied_at.isoformat() if latest.applied_at else None,
                    "result": latest.result_json,
                }
                if latest
                else None
            ),
            "acceptance_configuration": (
                "configured"
                if snapshot["projects"] and configured == len(snapshot["projects"])
                else "pending_business_configuration"
            ),
            "attachment_carrier": "controlled_business_file",
            "approval_role": "admin_only_pending_business_configuration",
        }

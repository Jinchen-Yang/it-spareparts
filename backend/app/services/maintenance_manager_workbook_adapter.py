"""Database boundary for the project-manager monthly workbook v3 (#206)."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Mapping

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
    MaintenanceProjectAuditLog,
    MaintenanceProjectContract,
    MaintenanceProjectUserAssignment,
)
from app.models.maintenance_project_operations import (
    MaintenanceCollectionSnapshot,
    MaintenanceProjectOperationAudit,
)
from app.models.system import SysUser
from app.security import UserContext, is_field_hidden
from app.services import maintenance_periods
from app.services import maintenance_project_catalog as catalog
from app.services import maintenance_project_operations as operations
from app.services.maintenance_collection_milestones import write_collection_milestone
from app.services.maintenance_boss_board import _card_contracts
from app.services.maintenance_manager_workbook_v3 import (
    SCHEMA_VERSION,
    TEMPLATE_VERSION,
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
    change: ServicePeriodChange | MilestoneChange,
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

    def _owner(self) -> SysUser:
        if not self.user_ctx.is_authenticated or not self.user_ctx.user_id:
            raise ManagerWorkbookPermissionError("请先登录")
        statement = select(SysUser).where(
            SysUser.username == self.user_ctx.user_id,
            SysUser.is_active.is_(True),
        )
        owner = self.db.scalar(statement)
        if owner is None:
            raise ManagerWorkbookPermissionError("当前项目经理账号不存在或已停用")
        if is_field_hidden(self.user_ctx, "contract_amount"):
            raise ManagerWorkbookPermissionError(
                "月度工作簿包含全部合同额；当前账号尚未获得对应数据权限"
            )
        return owner

    def _lock_owner_operation(self, owner: SysUser) -> None:
        """Serialize validate/apply without taking the ``sys_user`` row early.

        Both operations may write the same upload-batch row.  A dedicated
        transaction advisory lock keeps their order stable while remaining
        independent from direct assignment's canonical
        state→project→assignment→user row-lock chain.
        """

        self.db.execute(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtextextended(
                        f"maintenance-manager-workbook-owner:{owner.id}", 0
                    )
                )
            )
        )

    def _scope_assignment_rows(
        self,
        owner: SysUser,
        *,
        lock: bool,
    ) -> list:
        """Owner scope 的 assignment+project 行。

        lock=True 时按全局锁序冻结 scope：全部 workbook states（project_id
        排序逐行）→ 全部 projects（排序逐行）→ assignments → owner
        user。direct assign 同样是 state → project → assignment → target
        user，因此 manager workbook 不得在 state 之前持有 owner user 锁。

        owner user 锁到手后再无锁复核 scope：若有 direct assign 在首次
        probe 与 owner 锁之间插入了新挂靠，必须 409 fail closed，不能
        将未持有 state 锁的新项目混入本次快照。
        """

        statement = (
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
        if not lock:
            return list(self.db.execute(statement))
        probe = list(self.db.execute(statement))
        if not probe:
            raise ManagerWorkbookPermissionError(
                "当前账号未分配任何有效维保项目，不能使用项目经理月度工作簿"
            )
        project_ids = sorted({project.project_id for _assignment, project in probe})
        operations.lock_workbook_states(self.db, project_ids=project_ids)
        for project_id in project_ids:
            self.db.scalar(
                select(MaintenanceProject)
                .where(MaintenanceProject.project_id == project_id)
                .with_for_update()
            )
        rows = list(self.db.execute(statement.with_for_update()))
        probe_keys = [(a.assignment_id, p.project_id) for a, p in probe]
        if [(a.assignment_id, p.project_id) for a, p in rows] != probe_keys:
            raise ManagerWorkbookConflict("本人负责项目范围已变化，请重新下载")

        # 最后才锁 owner：这与 assign_primary_manager 的 target-user 锁位置
        # 一致。用 id 确认仍是同一个活动账号，避免身份字段并发变化。
        locked_owner = self.db.scalar(
            select(SysUser)
            .where(
                SysUser.id == owner.id,
                SysUser.username == self.user_ctx.user_id,
                SysUser.is_active.is_(True),
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if locked_owner is None:
            raise ManagerWorkbookPermissionError("当前项目经理账号不存在或已停用")

        # 此处不能 FOR UPDATE：若 scope 扩张，新项目的 state/project 尚未
        # 按序加锁；只读比较后直接失败才不会制造新的锁序倒置。
        stable_rows = list(self.db.execute(statement))
        if [
            (assignment.assignment_id, project.project_id)
            for assignment, project in stable_rows
        ] != probe_keys:
            raise ManagerWorkbookConflict("本人负责项目范围已变化，请重新下载")
        return rows

    def load_snapshot(self, report_month: date, *, lock: bool = False) -> dict:
        report_month = _month(report_month)
        # lock=True 时 owner 必须在 scope 的 state/project/assignment 之后
        # 才加锁；_scope_assignment_rows 负责末尾锁定与复核。
        owner = self._owner()
        assignment_rows = self._scope_assignment_rows(owner, lock=lock)
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

        # 锁序（lock=True）：states → projects → assignments（上方已冻结）→
        # contracts → service rows → 其余明细（milestones/collections/acceptance）。
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
            if lock:
                period_statement = period_statement.with_for_update()
            service_periods = {
                row.project_id: row for row in self.db.scalars(period_statement)
            }
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
        if project_ids:
            acceptance_statement = (
                select(MaintenanceAcceptanceDeliverable)
                .where(MaintenanceAcceptanceDeliverable.project_id.in_(project_ids))
                .order_by(
                    MaintenanceAcceptanceDeliverable.project_id,
                    MaintenanceAcceptanceDeliverable.deliverable_type,
                )
            )
            if lock:
                acceptance_statement = acceptance_statement.with_for_update()
            acceptance_rows = list(self.db.scalars(acceptance_statement))

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

        # 项目卡、项目总表与经理月表共用同一套“当前合同事实完整性”：未映射、
        # 缺含税额、同项目重复稳定合同、跨项目共享都不得计算合同总额及回款率。
        contract_facts = _card_contracts(self.db, project_ids)

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
                        # V3 workbook headers and all downstream ratios define
                        # this field as tax-inclusive. contract_amount is the
                        # separate ex-tax reconciliation column.
                        "contract_amount": contract.amount_inc_tax,
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
                    # 期限双源 P1：project.period_* 是唯一业务事实，工作簿快照的
                    # 起止日期读 project；projection 仅提供 OCC 版本号。
                    "service_start": project.period_from,
                    "service_end": project.period_to,
                    "service_period_version": period.version if period else 0,
                    "contract_facts_complete": bool(
                        contract_payload
                        and (fact := contract_facts.get(project.project_id))
                        and not fact.get("contract_incomplete")
                        and fact.get("amount_inc_tax") is not None
                    ),
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
        # Serialize validation/apply per owner without holding sys_user before
        # any project state row (the latter would invert direct-assign order).
        owner = self._owner()
        self._lock_owner_operation(owner)
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
        # 仅做身份 probe；真正的 owner FOR UPDATE 由
        # load_snapshot(lock=True) 在 state/project/assignment 之后获取。
        owner = self._owner()
        self._lock_owner_operation(owner)
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
        # 项目级合并写入登记：每个项目无论 service/milestone 变化多少条，
        # project.version / 审计 / workbook revision 只 bump 一次。
        project_before: dict[str, dict] = {}

        def _locked_project(project_id: str) -> MaintenanceProject:
            # load_snapshot(lock=True) 已按锁序持有全部行锁，此处只是取回实例。
            project = self.db.scalar(
                select(MaintenanceProject)
                .where(MaintenanceProject.project_id == project_id)
                .with_for_update()
            )
            if project is None:
                raise ManagerWorkbookConflict("本人负责项目范围已变化，请重新下载")
            return project

        def _touch(project: MaintenanceProject) -> None:
            if project.project_id not in project_before:
                project_before[project.project_id] = catalog.project_dict(project)

        for value in plan.get("service_period_changes") or []:
            change = ServicePeriodChange(
                project_id=str(value["project_id"]),
                project_version=int(value["project_version"]),
                expected_version=int(value["expected_version"]),
                service_start=date.fromisoformat(value["service_start"]) if value.get("service_start") else None,
                service_end=date.fromisoformat(value["service_end"]) if value.get("service_end") else None,
                completeness_state=str(value["completeness_state"]),
            )
            project = _locked_project(change.project_id)
            # 隐藏的 service_period_version OCC：投影版本必须与世面快照一致。
            current = self.db.scalar(
                select(MaintenanceServicePeriod)
                .where(MaintenanceServicePeriod.project_id == change.project_id)
                .with_for_update()
            )
            if change.expected_version == 0:
                if current is not None:
                    raise ManagerWorkbookConflict("维保期限已被其他操作更新")
            elif current is None or current.version != change.expected_version:
                raise ManagerWorkbookConflict("维保期限版本已变化")
            _touch(project)
            # project.period_* 是唯一事实源：service 变化经 canonical helper
            # 同步 project 日期+lifecycle 与 projection（含互斥 provenance 清理）。
            result = maintenance_periods.apply_canonical_period_locked(
                self.db,
                project=project,
                period_from=change.service_start,
                period_to=change.service_end,
                source=maintenance_periods.SOURCE_MANAGER_WORKBOOK,
                source_batch_id=batch.batch_id,
                as_of=self.as_of,
                operated_by=self.operator,
                reason=reason,
            )
            self.db.add(
                MaintenanceProjectOperationAudit(
                    project_id=change.project_id,
                    entity_type="service_period",
                    entity_id=change.project_id,
                    action="manager_workbook_apply",
                    before_json=_json_value(result["before"]["projection"]),
                    after_json=_json_value(result["after"]["projection"]),
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
                _touch(_locked_project(change.project_id))
                current = write_collection_milestone(
                    self.db,
                    project_id=change.project_id,
                    project_contract_id=change.project_contract_id,
                    sequence=change.sequence,
                    planned_date=change.planned_date,
                    planned_amount=change.planned_amount,
                    completeness_state=change.completeness_state,
                    source="manager_workbook_v3",
                    source_batch_id=batch.batch_id,
                    date_precision="day",
                    operator=self.operator,
                )
            else:
                if current is None or current.version != change.expected_version:
                    raise ManagerWorkbookConflict("计划回款节点版本已变化")
                before = {
                    "planned_date": current.planned_date,
                    "planned_amount": current.planned_amount,
                    "completeness_state": current.completeness_state,
                    "version": current.version,
                }
                _touch(_locked_project(change.project_id))
                current = write_collection_milestone(
                    self.db,
                    project_id=change.project_id,
                    project_contract_id=change.project_contract_id,
                    sequence=change.sequence,
                    planned_date=change.planned_date,
                    planned_amount=change.planned_amount,
                    completeness_state=change.completeness_state,
                    source="manager_workbook_v3",
                    source_batch_id=batch.batch_id,
                    date_precision="day",
                    operator=self.operator,
                )
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

        # 项目级合并 bump：service 与 milestone 变化合并后，每个项目
        # project.version / 主档审计 / workbook revision 本事务只增一次。
        for project_id in sorted(project_before):
            project = _locked_project(project_id)
            project.version += 1
            self.db.flush()
            self.db.add(
                MaintenanceProjectAuditLog(
                    project_id=project_id,
                    entity_type="project",
                    entity_id=project_id,
                    action="update",
                    before_json=project_before[project_id],
                    after_json=catalog.project_dict(project),
                    reason=reason,
                    operated_by=self.operator,
                )
            )
            state = operations.get_or_create_workbook_state(
                self.db,
                project_id=project_id,
                lock=True,
            )
            operations.bump_locked_workbook_revision(self.db, state=state)

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

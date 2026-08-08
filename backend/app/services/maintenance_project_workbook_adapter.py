"""Transactional database adapter for the stable-project workbook v2 protocol.

Validation produces an expiring, server-owned plan.  Apply accepts only that
stored plan and commits the business fact, audit rows, idempotency ledgers and
project revision in one transaction owned by the HTTP endpoint.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import json
from typing import Any, Mapping, Sequence
from uuid import uuid4

from sqlalchemy import delete, func, null, select, update
from sqlalchemy.orm import Session

from app.models.dimensions import DimPart
from app.models.maintenance_project import (
    MaintenanceProject,
    MaintenanceProjectContract,
)
from app.models.maintenance_project_operations import (
    MaintenanceCollectionSnapshot,
    MaintenanceProjectExpenseAttribution,
    MaintenanceProjectWorkbookOperation,
    MaintenanceProjectWorkbookState,
    MaintenanceProjectWorkbookValidation,
    MaintenanceSiteIssue,
    MaintenanceSiteIssueLine,
)
from app.security import UserContext
from app.services import maintenance_project_operations as operations
from app.services.maintenance_project_workbook_v2 import (
    CollectionCreate,
    MAX_ROWS_PER_TABLE,
    ProjectWorkbookV2Error,
    WorkbookApplyResult,
    WorkbookIssue,
    WorkbookValidation,
    PROTOCOL_ID,
    apply_project_workbook,
    build_error_workbook,
    build_project_workbook,
    validate_project_workbook,
)


VALIDATION_TTL = timedelta(minutes=30)
EXPIRED_VALIDATION_RETENTION = timedelta(days=7)
APPLIED_VALIDATION_RETENTION = timedelta(days=30)
VALIDATION_CLEANUP_BATCH_SIZE = 200
_PLAN_VERSION = "maintenance-project-workbook-plan/1"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _excel_date(value: Any) -> date | None:
    """Keep exported dates as real Excel dates instead of ISO text."""

    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ProjectWorkbookV2Error(
            "项目事实包含无效日期，暂不能导出工作簿", status_code=409
        ) from exc


def _issues_payload(issues: Sequence[WorkbookIssue]) -> list[dict[str, Any]]:
    return [
        {
            "code": issue.code,
            "message": issue.message,
            "sheet": issue.sheet,
            "row": issue.row,
            "column": issue.column,
        }
        for issue in issues
    ]


def cleanup_project_workbook_validations(
    db: Session,
    *,
    now: datetime | None = None,
) -> None:
    """Compact expired/applied plans and retire old metadata-only records."""

    cleanup_at = now or _now()
    expiring_ids = (
        select(MaintenanceProjectWorkbookValidation.validation_id)
        .where(
            MaintenanceProjectWorkbookValidation.status.in_(("valid", "error")),
            MaintenanceProjectWorkbookValidation.expires_at <= cleanup_at,
        )
        .order_by(
            MaintenanceProjectWorkbookValidation.expires_at,
            MaintenanceProjectWorkbookValidation.validation_id,
        )
        .limit(VALIDATION_CLEANUP_BATCH_SIZE)
        .with_for_update(skip_locked=True)
    )
    db.execute(
        update(MaintenanceProjectWorkbookValidation)
        .where(
            MaintenanceProjectWorkbookValidation.validation_id.in_(expiring_ids)
        )
        .values(
            status="expired",
            plan_json=null(),
            issues_json=[],
            error_workbook=null(),
        )
        .execution_options(synchronize_session=False)
    )
    applied_plan_ids = (
        select(MaintenanceProjectWorkbookValidation.validation_id)
        .where(
            MaintenanceProjectWorkbookValidation.status == "applied",
            MaintenanceProjectWorkbookValidation.plan_json.is_not(None),
        )
        .order_by(
            MaintenanceProjectWorkbookValidation.applied_at,
            MaintenanceProjectWorkbookValidation.validation_id,
        )
        .limit(VALIDATION_CLEANUP_BATCH_SIZE)
        .with_for_update(skip_locked=True)
    )
    db.execute(
        update(MaintenanceProjectWorkbookValidation)
        .where(
            MaintenanceProjectWorkbookValidation.validation_id.in_(applied_plan_ids)
        )
        .values(plan_json=null())
        .execution_options(synchronize_session=False)
    )
    expired_retention_ids = (
        select(MaintenanceProjectWorkbookValidation.validation_id)
        .where(
            MaintenanceProjectWorkbookValidation.status == "expired",
            MaintenanceProjectWorkbookValidation.expires_at
            <= cleanup_at - EXPIRED_VALIDATION_RETENTION,
        )
        .order_by(
            MaintenanceProjectWorkbookValidation.expires_at,
            MaintenanceProjectWorkbookValidation.validation_id,
        )
        .limit(VALIDATION_CLEANUP_BATCH_SIZE)
        .with_for_update(skip_locked=True)
    )
    db.execute(
        delete(MaintenanceProjectWorkbookValidation).where(
            MaintenanceProjectWorkbookValidation.validation_id.in_(
                expired_retention_ids
            )
        )
        .execution_options(synchronize_session=False)
    )
    applied_retention_ids = (
        select(MaintenanceProjectWorkbookValidation.validation_id)
        .where(
            MaintenanceProjectWorkbookValidation.status == "applied",
            MaintenanceProjectWorkbookValidation.applied_at.is_not(None),
            MaintenanceProjectWorkbookValidation.applied_at
            <= cleanup_at - APPLIED_VALIDATION_RETENTION,
        )
        .order_by(
            MaintenanceProjectWorkbookValidation.applied_at,
            MaintenanceProjectWorkbookValidation.validation_id,
        )
        .limit(VALIDATION_CLEANUP_BATCH_SIZE)
        .with_for_update(skip_locked=True)
    )
    db.execute(
        delete(MaintenanceProjectWorkbookValidation).where(
            MaintenanceProjectWorkbookValidation.validation_id.in_(
                applied_retention_ids
            )
        )
        .execution_options(synchronize_session=False)
    )
    db.flush()


def _serialize_validation(validation: WorkbookValidation) -> dict[str, Any]:
    return {
        "plan_version": _PLAN_VERSION,
        "validation_id": validation.validation_id,
        "project_id": validation.project_id,
        "export_id": validation.export_id,
        "expected_revision": validation.expected_revision,
        "file_sha256": validation.file_sha256,
        "unchanged": validation.unchanged,
        "metadata": dict(validation.metadata),
        "creates": [
            {
                "operation_key": item.operation_key,
                "client_row_id": item.client_row_id,
                "project_contract_id": item.project_contract_id,
                "contract_no": item.contract_no,
                "report_month": item.report_month.isoformat(),
                "cumulative_amount": format(item.cumulative_amount, "f"),
                "voucher_no": item.voucher_no,
                "status": item.status,
                "remark": item.remark,
                "payload_hash": item.payload_hash,
            }
            for item in validation.creates
        ],
    }


def _invalid_stored_plan(message: str = "服务端校验计划损坏，请重新上传") -> None:
    raise ProjectWorkbookV2Error(
        message,
        status_code=409,
        issues=(WorkbookIssue("stored_plan_invalid", message),),
    )


def _deserialize_validation(
    row: MaintenanceProjectWorkbookValidation,
) -> WorkbookValidation:
    payload = row.plan_json
    if not isinstance(payload, dict) or payload.get("plan_version") != _PLAN_VERSION:
        _invalid_stored_plan()
    required = {
        "plan_version",
        "validation_id",
        "project_id",
        "export_id",
        "expected_revision",
        "file_sha256",
        "unchanged",
        "metadata",
        "creates",
    }
    if set(payload) != required:
        _invalid_stored_plan()
    if (
        payload["validation_id"] != row.validation_id
        or payload["project_id"] != row.project_id
        or payload["export_id"] != row.export_id
        or payload["expected_revision"] != row.expected_revision
        or payload["file_sha256"] != row.file_sha256
        or not isinstance(payload["metadata"], dict)
        or not isinstance(payload["creates"], list)
    ):
        _invalid_stored_plan()
    creates: list[CollectionCreate] = []
    create_keys = {
        "operation_key",
        "client_row_id",
        "project_contract_id",
        "contract_no",
        "report_month",
        "cumulative_amount",
        "voucher_no",
        "status",
        "remark",
        "payload_hash",
    }
    try:
        for item in payload["creates"]:
            if not isinstance(item, dict) or set(item) != create_keys:
                _invalid_stored_plan()
            amount = Decimal(str(item["cumulative_amount"]))
            report_month = date.fromisoformat(str(item["report_month"]))
            if (
                not amount.is_finite()
                or amount <= 0
                or amount >= Decimal("1000000000000")
                or report_month.day != 1
                or len(str(item["operation_key"])) > 128
                or len(str(item["project_contract_id"])) > 36
                or len(str(item["contract_no"])) > 64
                or (
                    item["voucher_no"] is not None
                    and len(str(item["voucher_no"])) > 128
                )
                or (item["remark"] is not None and len(str(item["remark"])) > 32767)
                or item["status"] != "已确认"
            ):
                _invalid_stored_plan()
            creates.append(
                CollectionCreate(
                    operation_key=str(item["operation_key"]),
                    client_row_id=str(item["client_row_id"]),
                    project_contract_id=str(item["project_contract_id"]),
                    contract_no=str(item["contract_no"]),
                    report_month=report_month,
                    cumulative_amount=amount,
                    voucher_no=(
                        str(item["voucher_no"])
                        if item["voucher_no"] is not None
                        else None
                    ),
                    status=str(item["status"]),
                    remark=str(item["remark"]) if item["remark"] is not None else None,
                    payload_hash=str(item["payload_hash"]),
                )
            )
    except (InvalidOperation, TypeError, ValueError, KeyError):
        _invalid_stored_plan()
    metadata = {str(key): str(value) for key, value in payload["metadata"].items()}
    if bool(payload["unchanged"]) != (not creates):
        _invalid_stored_plan()
    return WorkbookValidation(
        validation_id=row.validation_id,
        project_id=row.project_id,
        export_id=row.export_id,
        expected_revision=row.expected_revision,
        file_sha256=row.file_sha256,
        creates=tuple(creates),
        unchanged=bool(payload["unchanged"]),
        metadata=metadata,
    )


class MaintenanceProjectWorkbookAdapter:
    """One-request adapter; callers must commit or roll back the session."""

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
        self.operator = operator
        self.as_of = as_of
        self._locked_state: MaintenanceProjectWorkbookState | None = None
        self._locked_project: MaintenanceProject | None = None
        self._validation_row: MaintenanceProjectWorkbookValidation | None = None

    def _state(self, project_id: str) -> MaintenanceProjectWorkbookState:
        if self._locked_state is not None:
            if self._locked_state.project_id != project_id:
                raise ProjectWorkbookV2Error(
                    "一次事务不能处理多个项目工作簿", status_code=409
                )
            return self._locked_state
        if self.db.get(MaintenanceProject, project_id) is None:
            raise ProjectWorkbookV2Error("维保项目不存在", status_code=404)
        self._locked_state = operations.get_or_create_workbook_state(
            self.db, project_id=project_id, lock=True
        )
        return self._locked_state

    def _project(self, project_id: str) -> MaintenanceProject:
        """Lock after workbook state and before any contract/fact row."""

        self._state(project_id)
        if self._locked_project is not None:
            return self._locked_project
        project = self.db.scalar(
            select(MaintenanceProject)
            .where(MaintenanceProject.project_id == project_id)
            .with_for_update()
        )
        if project is None:
            raise ProjectWorkbookV2Error("维保项目不存在", status_code=404)
        self._locked_project = project
        return project

    def load_workspace(self, project_id: str) -> Mapping[str, Any]:
        state = self._state(project_id)
        count_row = self.db.execute(
            select(
                select(func.count())
                .select_from(MaintenanceProjectContract)
                .where(MaintenanceProjectContract.project_id == project_id)
                .scalar_subquery()
                .label("contracts"),
                select(func.count())
                .select_from(MaintenanceCollectionSnapshot)
                .where(
                    MaintenanceCollectionSnapshot.project_id == project_id,
                    MaintenanceCollectionSnapshot.report_month <= self.as_of,
                )
                .scalar_subquery()
                .label("collections"),
                select(func.count())
                .select_from(MaintenanceSiteIssueLine)
                .join(
                    MaintenanceSiteIssue,
                    MaintenanceSiteIssue.issue_id
                    == MaintenanceSiteIssueLine.issue_id,
                )
                .where(
                    MaintenanceSiteIssue.project_id == project_id,
                    MaintenanceSiteIssue.issue_date <= self.as_of,
                    MaintenanceSiteIssue.status_mapping_state == "mapped",
                    MaintenanceSiteIssue.normalized_status == "confirmed",
                )
                .scalar_subquery()
                .label("consumptions"),
                select(func.count())
                .select_from(MaintenanceSiteIssueLine)
                .join(
                    MaintenanceSiteIssue,
                    MaintenanceSiteIssue.issue_id
                    == MaintenanceSiteIssueLine.issue_id,
                )
                .where(
                    MaintenanceSiteIssue.project_id == project_id,
                    MaintenanceSiteIssue.issue_date <= self.as_of,
                )
                .scalar_subquery()
                .label("loaded_consumptions"),
                select(func.count())
                .select_from(MaintenanceProjectExpenseAttribution)
                .where(
                    MaintenanceProjectExpenseAttribution.project_id == project_id,
                    MaintenanceProjectExpenseAttribution.expense_date <= self.as_of,
                    MaintenanceProjectExpenseAttribution.status_mapping_state
                    == "mapped",
                    MaintenanceProjectExpenseAttribution.normalized_status
                    == "approved",
                )
                .scalar_subquery()
                .label("expenses"),
                select(func.count())
                .select_from(MaintenanceProjectExpenseAttribution)
                .where(
                    MaintenanceProjectExpenseAttribution.project_id == project_id,
                    MaintenanceProjectExpenseAttribution.expense_date <= self.as_of,
                )
                .scalar_subquery()
                .label("loaded_expenses"),
            )
        ).one()
        labels = {
            "contracts": "合同",
            "collections": "回款",
            "consumptions": "已确认现场领用明细",
            "expenses": "已审批报销",
            "loaded_consumptions": "现场领用全量事实",
            "loaded_expenses": "报销全量事实",
        }
        oversized = [
            f"{labels[key]}={int(getattr(count_row, key))}"
            for key in labels
            if int(getattr(count_row, key)) > MAX_ROWS_PER_TABLE
        ]
        if oversized:
            message = (
                f"项目工作簿超过单表 {MAX_ROWS_PER_TABLE} 行导出上限："
                + "、".join(oversized)
            )
            raise ProjectWorkbookV2Error(
                message,
                issues=(WorkbookIssue("export_row_limit", message),),
            )
        raw = operations.project_workbook_workspace(
            self.db,
            project_id=project_id,
            as_of=self.as_of,
            user_ctx=self.user_ctx,
        )
        if raw is None:
            raise ProjectWorkbookV2Error("维保项目不存在", status_code=404)
        part_ids = {
            int(row["part_id"])
            for row in raw["confirmed_site_consumptions"]
            if row.get("part_id") is not None
        }
        descriptions = (
            {
                part.id: part.description
                for part in self.db.scalars(
                    select(DimPart).where(DimPart.id.in_(part_ids))
                )
            }
            if part_ids
            else {}
        )
        contract_no_by_id = dict(
            self.db.execute(
                select(
                    MaintenanceProjectContract.project_contract_id,
                    MaintenanceProjectContract.contract_no,
                ).where(MaintenanceProjectContract.project_id == project_id)
            ).all()
        )
        collections = [
            {
                "collection_id": item["collection_id"],
                "project_contract_id": item["project_contract_id"],
                "contract_no": contract_no_by_id.get(item["project_contract_id"], ""),
                "report_month": _excel_date(item["report_month"]),
                "cumulative_amount": item["cumulative_amount"],
                "voucher_no": item["receipt_reference"],
                "status": {
                    "confirmed": "已确认",
                    "unconfirmed": "未确认",
                    "void": "已作废",
                }.get(item["status"], item["status"]),
                "remark": item["remark"],
                "version": item["version"],
            }
            for item in raw["collection_snapshots"]
        ]
        consumptions = [
            {
                "consumption_id": item["issue_line_id"],
                "issue_no": item["issue_no"],
                "issue_date": _excel_date(item["issue_date"]),
                "part_no": item["pn"],
                "part_name": descriptions.get(item.get("part_id")),
                "quantity": item["quantity"],
                "unit_cost": item["unit_cost"],
                "cost_amount": item["cost_amount"],
                "cost_status": (
                    "缺少价格成本"
                    if item["unit_cost"] is None or item["cost_amount"] is None
                    else "成本完整"
                ),
                "cost_source": item["cost_source"],
            }
            for item in raw["confirmed_site_consumptions"]
        ]
        expenses = [
            {
                "expense_id": item["expense_id"],
                "expense_no": item["expense_ref"],
                "expense_date": _excel_date(item["expense_date"]),
                "applicant": item.get("applicant"),
                "category": item.get("category"),
                "amount": item["amount_ex_tax"],
                "approval_status": (
                    "已审批"
                    if item["normalized_status"] == "approved"
                    else item["normalized_status"]
                ),
                "remark": item.get("expense_reason"),
            }
            for item in raw["approved_expenses"]
        ]
        tasks = [
            {
                "task_id": item["task_id"],
                "task_type": item.get("task_type") or item.get("rule_key"),
                "title": item["title"],
                "due_date": _excel_date(item.get("due_date")),
                "status": item.get("status") or "待处理",
                "owner": item.get("owner") or raw["project"].get("project_manager_id"),
                "detail": item.get("detail"),
            }
            for item in raw["derived_tasks"]
        ]
        return {
            "project": {
                "project_id": raw["project"]["project_id"],
                "project_code": raw["project"]["project_code"],
                "project_name": raw["project"]["display_name"],
                "manager_name": raw["project"].get("project_manager_id"),
                "is_active": raw["project"].get("is_active"),
                "version": raw["project"]["version"],
            },
            "workbook_revision": state.revision,
            "as_of": raw["as_of"],
            # The workbook displays every linked contract and labels whether it
            # participates in the current denominator. Financial summaries still
            # use only current, included relations.
            "contracts": list(raw["all_contracts"]),
            "collections": collections,
            "consumptions": consumptions,
            "expenses": expenses,
            "tasks": tasks,
            "canonical_metrics": dict(raw["canonical_metrics"]),
            "canonical_completeness": dict(raw["canonical_completeness"]),
            "data_version": state.data_version,
        }

    def export(self, project_id: str, *, hmac_key: bytes):
        state = self._state(project_id)
        exported_at = _now()
        artifact = build_project_workbook(
            self.load_workspace(project_id),
            hmac_key=hmac_key,
            exported_by=self.operator,
            exported_at=exported_at,
        )
        file_sha256 = hashlib.sha256(artifact.content).hexdigest()
        state.last_export_id = artifact.export_id
        state.last_exported_at = exported_at
        self.db.add(
            MaintenanceProjectWorkbookOperation(
                project_id=project_id,
                export_id=artifact.export_id,
                file_sha256=file_sha256,
                operation_key=f"export:{artifact.export_id}",
                payload_hash=file_sha256,
                operation_type="file_export",
                operated_by=self.operator,
            )
        )
        self.db.flush()
        return artifact

    def validate(
        self,
        project_id: str,
        content: bytes,
        *,
        hmac_key: bytes,
    ) -> tuple[WorkbookValidation | None, tuple[WorkbookIssue, ...], str]:
        state = self._state(project_id)
        try:
            validation = validate_project_workbook(
                content,
                workspace=self.load_workspace(project_id),
                hmac_key=hmac_key,
            )
            if validation.project_id != project_id:
                raise ProjectWorkbookV2Error(
                    "工作簿项目与 URL 项目不一致", status_code=409
                )
        except ProjectWorkbookV2Error as exc:
            if exc.status_code == 409:
                raise
            validation_id = str(uuid4())
            report = build_error_workbook(
                exc.issues,
                hmac_key=hmac_key,
                project_id=project_id,
                source_sha256=hashlib.sha256(content).hexdigest(),
            )
            self.db.add(
                MaintenanceProjectWorkbookValidation(
                    validation_id=validation_id,
                    project_id=project_id,
                    export_id=f"error-{validation_id[:36]}",
                    expected_revision=state.revision,
                    file_sha256=hashlib.sha256(content).hexdigest(),
                    # JSONB would otherwise encode Python None as JSON ``null``;
                    # the database invariant intentionally requires SQL NULL.
                    plan_json=null(),
                    status="error",
                    issues_json=_issues_payload(exc.issues),
                    error_workbook=report,
                    created_by=self.operator,
                    expires_at=_now() + VALIDATION_TTL,
                )
            )
            self.db.flush()
            return None, tuple(exc.issues), validation_id
        self.db.add(
            MaintenanceProjectWorkbookValidation(
                validation_id=validation.validation_id,
                project_id=validation.project_id,
                export_id=validation.export_id,
                expected_revision=validation.expected_revision,
                file_sha256=validation.file_sha256,
                plan_json=_serialize_validation(validation),
                status="valid",
                issues_json=[],
                error_workbook=None,
                created_by=self.operator,
                expires_at=_now() + VALIDATION_TTL,
            )
        )
        self.db.flush()
        return validation, (), validation.validation_id

    def apply_validation(
        self,
        project_id: str,
        validation_id: str,
        *,
        data_version: str,
    ) -> tuple[WorkbookApplyResult, MaintenanceProjectWorkbookState]:
        state = self._state(project_id)
        self._project(project_id)
        row = self.db.scalar(
            select(MaintenanceProjectWorkbookValidation)
            .where(MaintenanceProjectWorkbookValidation.validation_id == validation_id)
            .with_for_update()
        )
        if row is None:
            raise ProjectWorkbookV2Error("validation_token 不存在", status_code=404)
        self._validation_row = row
        if row.project_id != project_id:
            raise ProjectWorkbookV2Error(
                "validation_token 不属于当前项目", status_code=409
            )
        if not hmac.compare_digest(row.created_by, self.operator):
            raise ProjectWorkbookV2Error(
                "validation_token 不属于当前用户", status_code=409
            )
        if row.status == "applied":
            raise ProjectWorkbookV2Error(
                "该校验计划已应用，拒绝重复提交", status_code=409
            )
        if row.status != "valid" or row.plan_json is None:
            raise ProjectWorkbookV2Error("该校验计划不可应用", status_code=409)
        if row.expires_at <= _now():
            raise ProjectWorkbookV2Error("校验计划已过期，请重新上传", status_code=409)
        if not hmac.compare_digest(data_version, state.data_version):
            raise ProjectWorkbookV2Error(
                "项目数据版本已变化，请重新下载", status_code=409
            )
        validation = _deserialize_validation(row)
        if validation.unchanged:
            if self.applied_file(validation.file_sha256):
                raise ProjectWorkbookV2Error(
                    "工作簿已应用，拒绝重复提交", status_code=409
                )
            if state.revision != validation.expected_revision:
                raise ProjectWorkbookV2Error(
                    "项目数据已更新，应用计划已过期；请重新下载",
                    status_code=409,
                )
            self.apply_collections_atomically(validation, ())
            result = WorkbookApplyResult(
                status="applied",
                created=0,
                replayed=0,
                validation_id=validation.validation_id,
            )
        else:
            result = apply_project_workbook(validation, repository=self)
        if result.status != "applied":
            raise ProjectWorkbookV2Error("工作簿已应用，拒绝重复提交", status_code=409)
        row.status = "applied"
        row.plan_json = null()
        row.applied_at = _now()
        state.last_applied_at = row.applied_at
        self.db.flush()
        return result, state

    # ProjectWorkbookApplyRepository implementation.  The state lock serializes
    # every method below for one project.
    def current_revision(self, project_id: str) -> int:
        return self._state(project_id).revision

    def applied_file(self, file_sha256: str) -> bool:
        return (
            self.db.scalar(
                select(MaintenanceProjectWorkbookOperation.id).where(
                    MaintenanceProjectWorkbookOperation.file_sha256 == file_sha256,
                    MaintenanceProjectWorkbookOperation.operation_type == "file_apply",
                )
            )
            is not None
        )

    def applied_operation(self, operation_key: str) -> str | None:
        payload_hash = self.db.scalar(
            select(MaintenanceProjectWorkbookOperation.payload_hash).where(
                MaintenanceProjectWorkbookOperation.operation_key == operation_key
            )
        )
        if payload_hash is not None:
            raise ProjectWorkbookV2Error(
                "工作簿操作已应用，拒绝重复提交",
                status_code=409,
                issues=(WorkbookIssue("operation_replay", "工作簿操作键已存在"),),
            )
        return None

    def apply_collections_atomically(
        self,
        validation: WorkbookValidation,
        creates: Sequence[CollectionCreate],
    ) -> None:
        state = self._state(validation.project_id)
        if state.revision != validation.expected_revision:
            raise ProjectWorkbookV2Error(
                "项目数据已更新，应用计划已过期；请重新下载",
                status_code=409,
            )
        relationships = {
            row.project_contract_id: row
            for row in self.db.scalars(
                select(MaintenanceProjectContract)
                .where(
                    MaintenanceProjectContract.project_id == validation.project_id,
                    MaintenanceProjectContract.project_contract_id.in_(
                        [item.project_contract_id for item in creates]
                    ),
                )
                .with_for_update()
            )
        }
        seen_months: set[tuple[str, date]] = set()
        latest_allowed_month = self.as_of.replace(day=1)
        for item in creates:
            relation = relationships.get(item.project_contract_id)
            month_end = item.report_month.replace(
                day=monthrange(item.report_month.year, item.report_month.month)[1]
            )
            if (
                relation is None
                or relation.contract_no != item.contract_no
                or relation.status_mapping_state != "mapped"
                or item.report_month > latest_allowed_month
                or relation.effective_from > month_end
                or (
                    relation.effective_to is not None
                    and relation.effective_to <= item.report_month
                )
            ):
                raise ProjectWorkbookV2Error(
                    "新增回款关联合同已变化或在报告月份无效", status_code=409
                )
            month_key = (item.project_contract_id, item.report_month)
            if month_key in seen_months:
                raise ProjectWorkbookV2Error(
                    "校验计划包含重复合同月份", status_code=409
                )
            seen_months.add(month_key)
            existing = self.db.scalar(
                select(MaintenanceCollectionSnapshot.collection_id)
                .where(
                    MaintenanceCollectionSnapshot.project_contract_id
                    == item.project_contract_id,
                    MaintenanceCollectionSnapshot.report_month == item.report_month,
                )
                .with_for_update()
            )
            if existing is not None:
                raise ProjectWorkbookV2Error(
                    "同一合同月份的回款已存在，拒绝重复应用", status_code=409
                )
            if (
                self.db.scalar(
                    select(MaintenanceProjectWorkbookOperation.id).where(
                        MaintenanceProjectWorkbookOperation.operation_key
                        == item.operation_key
                    )
                )
                is not None
            ):
                raise ProjectWorkbookV2Error("工作簿操作已存在", status_code=409)

        for item in creates:
            created = operations.create_collection(
                self.db,
                project_id=validation.project_id,
                project_contract_id=item.project_contract_id,
                report_month=item.report_month,
                cumulative_amount=item.cumulative_amount,
                status="confirmed",
                receipt_reference=item.voucher_no,
                remark=item.remark,
                reason=f"项目工作簿回填 {validation.validation_id}",
                operated_by=self.operator,
                bump_revision=False,
                source="workbook",
                import_batch_id=validation.file_sha256,
            )
            if created is None:
                raise ProjectWorkbookV2Error("维保项目不存在", status_code=404)
            self.db.add(
                MaintenanceProjectWorkbookOperation(
                    project_id=validation.project_id,
                    export_id=validation.export_id,
                    file_sha256=validation.file_sha256,
                    operation_key=item.operation_key,
                    payload_hash=item.payload_hash,
                    operation_type="collection_create",
                    entity_id=created["collection_id"],
                    operated_by=self.operator,
                )
            )
        self.db.add(
            MaintenanceProjectWorkbookOperation(
                project_id=validation.project_id,
                export_id=validation.export_id,
                file_sha256=validation.file_sha256,
                operation_key=f"workbook-file:{validation.file_sha256}",
                payload_hash=hashlib.sha256(
                    json.dumps(
                        _serialize_validation(validation),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
                operation_type="file_apply",
                operated_by=self.operator,
            )
        )
        state.revision += 1
        state.data_version = hashlib.sha256(
            f"{validation.project_id}:{state.revision}".encode("utf-8")
        ).hexdigest()
        self.db.flush()

    def load_error_workbook(self, validation_id: str) -> bytes | None:
        row = self.db.get(MaintenanceProjectWorkbookValidation, validation_id)
        if (
            row is None
            or row.status != "error"
            or row.error_workbook is None
            or row.expires_at <= _now()
            or not hmac.compare_digest(row.created_by, self.operator)
        ):
            return None
        return bytes(row.error_workbook)


def workbook_preview(
    workspace: Mapping[str, Any],
    *,
    data_version: str,
    exported_at: str | None,
) -> dict[str, Any]:
    as_of = str(workspace.get("as_of") or "")
    return {
        "protocol_version": PROTOCOL_ID,
        "sheets": [
            {
                "code": "overview",
                "name": "01_总览",
                "row_count": len(workspace.get("contracts") or [])
                + len(workspace.get("collections") or []),
                "ownership": "append_only",
            },
            {
                "code": "site_requisitions",
                "name": "02_备件消耗",
                "row_count": len(workspace.get("consumptions") or []),
                "ownership": "system",
            },
            {
                "code": "approved_expenses",
                "name": "03_报销单",
                "row_count": len(workspace.get("expenses") or []),
                "ownership": "system",
            },
            {
                "code": "manager_tracking",
                "name": "04_项目经理追踪与提醒",
                "row_count": len(workspace.get("tasks") or []),
                "ownership": "system",
            },
        ],
        "latest_tracking_month": as_of[:7] if len(as_of) >= 7 else None,
        "last_exported_at": exported_at,
        "data_version": data_version,
    }

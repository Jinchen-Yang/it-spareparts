"""Admin-only maintenance cutover dry-run and approval endpoints."""

from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import current_identity
from app.config import get_settings
from app.db import get_db
from app.models.system import SysUser
from app.security import UserContext, get_current_user_context, record_access_log
from app.services import maintenance_migration_runs as runs
from app.services import maintenance_migration_controls as controls
from app.services.maintenance_migration_warehouse import (
    load_project_inventory_movements,
)
from app.services.maintenance_migration_legacy import load_project_legacy_truth


router = APIRouter(prefix="/maintenance/migration-runs", tags=["maintenance"])

_MONEY_LIMIT = Decimal("1000000000000")
_QTY_LIMIT = Decimal("1000000000000")


class HistoricalBaselineInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount_ex_tax: Decimal = Field(
        ge=0, lt=_MONEY_LIMIT, max_digits=14, decimal_places=2
    )
    amount_inc_tax: Decimal = Field(
        ge=0, lt=_MONEY_LIMIT, max_digits=14, decimal_places=2
    )
    evidence_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    coverage_from: date
    coverage_through: date
    scope: str = Field(pattern=r"^site_issue_parts_only$")
    excludes_expenses: bool
    source_artifact_locator: str = Field(min_length=1, max_length=512)
    source_row_count: int = Field(ge=0, le=10_000_000)
    aggregation_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_machine_contract(self):
        if self.amount_inc_tax != (self.amount_ex_tax * Decimal("1.13")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        ):
            raise ValueError("历史基线含税金额必须按固定 13% 由未税金额复算")
        if self.coverage_from > self.coverage_through:
            raise ValueError("历史基线覆盖区间不能为空或倒置")
        if self.excludes_expenses is not True:
            raise ValueError("历史基线必须明确排除报销费用")
        expected = controls.historical_baseline_aggregation_fingerprint(
            self.model_dump(mode="json", exclude={"aggregation_fingerprint"})
        )
        if self.aggregation_fingerprint != expected:
            raise ValueError("历史基线聚合指纹与金额及覆盖范围不一致")
        return self


class OpeningBalanceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    balance_key: str = Field(min_length=1, max_length=256)
    pn: str | None = Field(default=None, max_length=256)
    quantity: Decimal = Field(ge=0, lt=_QTY_LIMIT, max_digits=14, decimal_places=3)
    evidence_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class ProjectCutoverInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=36)
    cutover_date: date
    warehouse_ready_through: date | None = None
    historical_mode: str = Field(
        pattern=r"^(approved_cost_baseline|stable_site_issues)$"
    )
    historical_baseline: HistoricalBaselineInput | None = None
    opening_balances: list[OpeningBalanceInput] = Field(
        default_factory=list, max_length=runs.MAX_OPENINGS_PER_PROJECT
    )

    @model_validator(mode="after")
    def validate_baseline_boundary(self):
        if self.historical_baseline is not None and (
            self.historical_baseline.coverage_through
            != self.cutover_date - timedelta(days=1)
        ):
            raise ValueError("历史基线覆盖截止日必须精确为切换日前一日")
        return self


class MigrationPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=96)
    reason: str = Field(min_length=1, max_length=1000)
    projects: list[ProjectCutoverInput] = Field(
        min_length=1, max_length=runs.MAX_MIGRATION_PROJECTS
    )

    @model_validator(mode="after")
    def validate_total_openings(self):
        if sum(len(project.opening_balances) for project in self.projects) > (
            runs.MAX_TOTAL_OPENINGS
        ):
            raise ValueError("库存期初候选总数超过安全上限")
        return self


class MigrationSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statuses: list[str] = Field(default_factory=list, max_length=3)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)


class MigrationCommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    operation_key: str = Field(min_length=1, max_length=110)
    reason: str = Field(min_length=1, max_length=1000)


class HistoricalBaselineSignoffInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    baseline_id: str = Field(min_length=1, max_length=36)
    expected_version: int = Field(ge=1)


class OpeningBalanceSignoffInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    opening_balance_id: str = Field(min_length=1, max_length=36)
    expected_version: int = Field(ge=1)


class ProjectReconcileSignoffInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=36)
    expected_plan_version: int = Field(ge=1)
    expected_truth_comparison_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    reason: str = Field(min_length=1, max_length=1000)
    historical_baseline: HistoricalBaselineSignoffInput | None = None
    opening_balances: list[OpeningBalanceSignoffInput] = Field(
        default_factory=list, max_length=runs.MAX_OPENINGS_PER_PROJECT
    )


class MigrationReconcileRequest(MigrationCommandRequest):
    project_signoffs: list[ProjectReconcileSignoffInput] = Field(
        min_length=1, max_length=runs.MAX_MIGRATION_PROJECTS
    )

    @model_validator(mode="after")
    def validate_total_signoffs(self):
        if sum(len(item.opening_balances) for item in self.project_signoffs) > (
            runs.MAX_TOTAL_OPENINGS
        ):
            raise ValueError("库存期初签字总数超过安全上限")
        return self


class MigrationApproveRequest(MigrationCommandRequest):
    supplied_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


def _real_operator(db: Session, ident: dict) -> str:
    if ident.get("authn") != "sys_user" or ident.get("fb"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "维保迁移对账与审批必须使用实名系统账号",
        )
    username = str(ident.get("sub") or "").strip()
    user = db.scalar(
        select(SysUser).where(
            SysUser.username == username,
            SysUser.is_active.is_(True),
        )
    )
    if not username or user is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "维保迁移对账与审批必须使用实名系统账号",
        )
    return username


def _migration_operator(
    db: Session = Depends(get_db), ident: dict = Depends(current_identity)
) -> str:
    operator = _real_operator(db, ident)
    permissions = ident.get("perms")
    required = (
        "page_maintenance",
        "data_purchase_cost",
        "data_profit",
        "action_maintenance_migration_review",
    )
    if not isinstance(permissions, dict) or any(
        permissions.get(key) is not True for key in required
    ):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "实名账号缺少维保迁移复核或敏感数据权限",
        )
    return operator


def _project_payloads(body: MigrationPreviewRequest) -> list[dict]:
    return [
        project.model_dump(mode="json", exclude_none=False) for project in body.projects
    ]


def _raise_service_error(exc: Exception) -> None:
    if isinstance(exc, runs.MaintenanceMigrationRunNotFound):
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    if isinstance(exc, runs.MaintenanceMigrationRunConflict):
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    if isinstance(exc, runs.MaintenanceMigrationRunError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    raise exc


@router.post("/preview", status_code=status.HTTP_201_CREATED)
def create_preview(
    body: MigrationPreviewRequest,
    db: Session = Depends(get_db),
    operator: str = Depends(_migration_operator),
) -> dict:
    try:
        result = runs.create_preview_run(
            db,
            idempotency_key=body.idempotency_key,
            projects=_project_payloads(body),
            reason=body.reason,
            operated_by=operator,
            warehouse_loader=load_project_inventory_movements,
            legacy_loader=load_project_legacy_truth,
        )
        db.commit()
        return result
    except Exception as exc:
        db.rollback()
        _raise_service_error(exc)


@router.post("/search")
def search_migration_runs(
    body: MigrationSearchRequest,
    db: Session = Depends(get_db),
    _operator: str = Depends(_migration_operator),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    try:
        result = runs.search_runs(
            db,
            statuses=body.statuses,
            page=body.page,
            page_size=body.page_size,
        )
        record_access_log(
            ctx,
            "migration_runs_search",
            "maintenance_migration",
            {"statuses": body.statuses, "page": body.page, "page_size": body.page_size},
        )
        return result
    except Exception as exc:
        _raise_service_error(exc)


@router.get("/{run_id}")
def get_migration_run(
    run_id: str = Path(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
    _operator: str = Depends(_migration_operator),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    try:
        result = runs.get_run_detail(db, run_id=run_id)
        record_access_log(
            ctx, "migration_run_detail", "maintenance_migration", {"run_id": run_id}
        )
        return result
    except Exception as exc:
        _raise_service_error(exc)


@router.get("/{run_id}/projects/{project_id}/evidence")
def get_migration_project_evidence(
    run_id: str = Path(..., min_length=1, max_length=36),
    project_id: str = Path(..., min_length=1, max_length=36),
    section: str = Query(..., min_length=1, max_length=64),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
    _operator: str = Depends(_migration_operator),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    try:
        result = runs.get_project_evidence(
            db,
            run_id=run_id,
            project_id=project_id,
            section=section,
            page=page,
            page_size=page_size,
        )
        record_access_log(
            ctx,
            "migration_project_evidence",
            "maintenance_migration",
            {
                "run_id": run_id,
                "project_id": project_id,
                "section": section,
                "page": page,
                "page_size": page_size,
            },
        )
        return result
    except Exception as exc:
        _raise_service_error(exc)


@router.get("/{run_id}/manifest")
def get_migration_manifest(
    run_id: str = Path(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
    _operator: str = Depends(_migration_operator),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    try:
        result = runs.get_signed_manifest(
            db,
            run_id=run_id,
            verification_keys=get_settings().maintenance_manifest_verification_keys(),
            warehouse_loader=load_project_inventory_movements,
            legacy_loader=load_project_legacy_truth,
        )
        record_access_log(
            ctx, "migration_manifest", "maintenance_migration", {"run_id": run_id}
        )
        return result
    except Exception as exc:
        _raise_service_error(exc)


@router.post("/{run_id}/reconcile")
def reconcile_migration_run(
    body: MigrationReconcileRequest,
    run_id: str = Path(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
    operator: str = Depends(_migration_operator),
) -> dict:
    try:
        result = runs.reconcile_run(
            db,
            run_id=run_id,
            expected_version=body.expected_version,
            operation_key=body.operation_key,
            reason=body.reason,
            operated_by=operator,
            project_signoffs=[
                signoff.model_dump(mode="json") for signoff in body.project_signoffs
            ],
            warehouse_loader=load_project_inventory_movements,
            legacy_loader=load_project_legacy_truth,
        )
        db.commit()
        return result
    except Exception as exc:
        db.rollback()
        _raise_service_error(exc)


@router.post("/{run_id}/approve")
def approve_migration_run(
    body: MigrationApproveRequest,
    run_id: str = Path(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
    operator: str = Depends(_migration_operator),
) -> dict:
    try:
        signing_key_id, signing_key = (
            get_settings().maintenance_manifest_signing_material()
        )
        result = runs.approve_run(
            db,
            run_id=run_id,
            expected_version=body.expected_version,
            supplied_fingerprint=body.supplied_fingerprint,
            operation_key=body.operation_key,
            reason=body.reason,
            operated_by=operator,
            signing_key=signing_key,
            signing_key_id=signing_key_id,
            warehouse_loader=load_project_inventory_movements,
            legacy_loader=load_project_legacy_truth,
        )
        db.commit()
        return result
    except Exception as exc:
        db.rollback()
        _raise_service_error(exc)

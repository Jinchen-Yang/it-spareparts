"""Admin-only maintenance cutover dry-run and approval endpoints."""

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import current_identity
from app.config import get_settings
from app.db import get_db
from app.models.system import SysUser
from app.security import require_action, require_page
from app.services import maintenance_migration_runs as runs
from app.services.maintenance_migration_warehouse import (
    load_project_inventory_movements,
)


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
    historical_mode: str = Field(
        pattern=r"^(approved_cost_baseline|stable_site_issues)$"
    )
    historical_baseline: HistoricalBaselineInput | None = None
    opening_balances: list[OpeningBalanceInput] = Field(
        default_factory=list, max_length=5000
    )


class MigrationPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=96)
    reason: str = Field(min_length=1, max_length=1000)
    projects: list[ProjectCutoverInput] = Field(min_length=1, max_length=500)


class MigrationSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statuses: list[str] = Field(default_factory=list, max_length=3)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)


class MigrationReconcileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    operation_key: str = Field(min_length=1, max_length=110)
    reason: str = Field(min_length=1, max_length=1000)


class MigrationApproveRequest(MigrationReconcileRequest):
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


_access = [
    Depends(require_page("page_maintenance")),
    Depends(
        require_action(
            "action_maintenance_migration_review", require_data="data_profit"
        )
    ),
]


@router.post("/preview", status_code=status.HTTP_201_CREATED, dependencies=_access)
def create_preview(
    body: MigrationPreviewRequest,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
) -> dict:
    operator = _real_operator(db, ident)
    try:
        result = runs.create_preview_run(
            db,
            idempotency_key=body.idempotency_key,
            projects=_project_payloads(body),
            reason=body.reason,
            operated_by=operator,
            warehouse_loader=load_project_inventory_movements,
        )
        db.commit()
        return result
    except Exception as exc:
        db.rollback()
        _raise_service_error(exc)


@router.post("/search", dependencies=_access)
def search_migration_runs(
    body: MigrationSearchRequest,
    db: Session = Depends(get_db),
) -> dict:
    try:
        return runs.search_runs(
            db,
            statuses=body.statuses,
            page=body.page,
            page_size=body.page_size,
        )
    except Exception as exc:
        _raise_service_error(exc)


@router.get("/{run_id}", dependencies=_access)
def get_migration_run(
    run_id: str = Path(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
) -> dict:
    try:
        return runs.get_run_detail(db, run_id=run_id)
    except Exception as exc:
        _raise_service_error(exc)


@router.post("/{run_id}/reconcile", dependencies=_access)
def reconcile_migration_run(
    body: MigrationReconcileRequest,
    run_id: str = Path(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
) -> dict:
    operator = _real_operator(db, ident)
    try:
        result = runs.reconcile_run(
            db,
            run_id=run_id,
            expected_version=body.expected_version,
            operation_key=body.operation_key,
            reason=body.reason,
            operated_by=operator,
            warehouse_loader=load_project_inventory_movements,
        )
        db.commit()
        return result
    except Exception as exc:
        db.rollback()
        _raise_service_error(exc)


@router.post("/{run_id}/approve", dependencies=_access)
def approve_migration_run(
    body: MigrationApproveRequest,
    run_id: str = Path(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
) -> dict:
    operator = _real_operator(db, ident)
    try:
        result = runs.approve_run(
            db,
            run_id=run_id,
            expected_version=body.expected_version,
            supplied_fingerprint=body.supplied_fingerprint,
            operation_key=body.operation_key,
            reason=body.reason,
            operated_by=operator,
            signing_key=get_settings().secret_key.encode("utf-8"),
            warehouse_loader=load_project_inventory_movements,
        )
        db.commit()
        return result
    except Exception as exc:
        db.rollback()
        _raise_service_error(exc)

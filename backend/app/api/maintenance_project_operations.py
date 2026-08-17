"""Stable-project contract, payment, consumption, expense and task APIs."""

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.maintenance_project_scope import (
    enforce_maintenance_project_access,
    require_maintenance_project_access,
)
from app.auth import current_identity, current_role
from app.business_time import business_today
from app.db import get_db
from app.models.maintenance_project import MaintenanceProjectContract
from app.models.maintenance_project_operations import (
    MaintenanceCollectionSnapshot,
    MaintenanceProjectExpenseAttribution,
    MaintenanceSiteIssue,
)
from app.models.system import SysUser
from app.security import (
    UserContext,
    get_current_user_context,
    is_field_hidden,
    record_access_log,
    require_action,
    require_page,
)
from app.services import maintenance_project_assignments as assignments
from app.services import maintenance_project_operations as operations
from app.services import maintenance_project_procurement as procurement


router = APIRouter(prefix="/maintenance/projects/stable", tags=["maintenance"])
site_issue_router = APIRouter(prefix="/maintenance/site-issues", tags=["maintenance"])


class ProjectOperationsSearch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    q: str = ""
    as_of: date | None = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=24, ge=1, le=200)
    lifecycle: str = Field(
        default="all",
        pattern="^(ongoing|ended|missing|all)$",
    )
    reminder: str | None = None
    include_inactive: bool = False
    owner_scope: str | None = Field(default=None, pattern="^(me|all)$")
    task_type: str | None = None
    task_status: str | None = Field(
        default=None,
        pattern="^(open|pending|completed)$",
    )
    due_from: date | None = None
    due_to: date | None = None


class ContractCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_id: str = Field(min_length=1, max_length=64)
    contract_no: str = Field(min_length=1, max_length=64)
    contract_amount: Decimal | None = Field(default=None, ge=0)
    contract_status: str | None = Field(default=None, max_length=64)
    status_mapping_state: str
    status_mapping_version: str = Field(min_length=1, max_length=64)
    included_in_total: bool = False
    effective_from: date
    effective_to: date | None = None
    source: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=1000)


class ContractPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    contract_no: str | None = Field(default=None, min_length=1, max_length=64)
    contract_amount: Decimal | None = Field(default=None, ge=0)
    contract_status: str | None = Field(default=None, max_length=64)
    status_mapping_state: str | None = None
    status_mapping_version: str | None = Field(default=None, min_length=1, max_length=64)
    included_in_total: bool | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    source: str | None = Field(default=None, min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=1000)


class ContractArchive(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    effective_to: date
    reason: str = Field(min_length=1, max_length=1000)


class CollectionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_contract_id: str = Field(min_length=1, max_length=36)
    report_month: date
    cumulative_amount: Decimal = Field(ge=0)
    status: str
    receipt_reference: str | None = Field(default=None, max_length=128)
    remark: str | None = Field(default=None, max_length=32767)
    reason: str = Field(min_length=1, max_length=1000)


class CollectionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    report_month: date | None = None
    cumulative_amount: Decimal | None = Field(default=None, ge=0)
    status: str | None = None
    receipt_reference: str | None = Field(default=None, max_length=128)
    remark: str | None = Field(default=None, max_length=32767)
    reason: str = Field(min_length=1, max_length=1000)


class SiteIssueLineCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issue_line_id: str = Field(min_length=1, max_length=64)
    line_no: int = Field(ge=1)
    part_id: int = Field(ge=1)
    pn: str = Field(min_length=1, max_length=128)
    quantity: Decimal = Field(gt=0)
    no_return: bool | None = None
    linked_purchase_line_id: int | None = Field(default=None, ge=1)


class SiteIssueCreate(BaseModel):
    """Legacy direct-entry contract kept stable for existing API callers."""

    model_config = ConfigDict(extra="forbid")

    issue_no: str = Field(min_length=1, max_length=64)
    issue_date: date
    raw_status: str = Field(min_length=1, max_length=64)
    status_mapping_state: str
    normalized_status: str
    status_mapping_version: str = Field(min_length=1, max_length=64)
    lines: list[SiteIssueLineCreate] = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=1000)


class SiteIssueDraftLineCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    delivery_line_id: str = Field(min_length=1, max_length=64)
    quantity: Decimal = Field(gt=0)
    # 行级不返还：True/False 覆盖项目默认；留空继承项目默认
    no_return: bool | None = None


class SiteIssueDraftCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=8, max_length=128)
    issue_date: date
    receiver: str = Field(min_length=1, max_length=128)
    issued_by: str = Field(min_length=1, max_length=128)
    site_location: str = Field(min_length=1, max_length=256)
    lines: list[SiteIssueDraftLineCreate] = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=1000)


class SiteIssueCandidateSearch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    q: str | None = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=200)


class SiteIssuePreview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=36)
    version: int = Field(ge=1)


class SiteIssueCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=36)
    version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=128)
    reason: str = Field(min_length=1, max_length=1000)


class SiteIssuePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=36)
    version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=128)
    issue_date: date | None = None
    receiver: str | None = Field(default=None, min_length=1, max_length=128)
    issued_by: str | None = Field(default=None, min_length=1, max_length=128)
    site_location: str | None = Field(default=None, min_length=1, max_length=256)
    lines: list[SiteIssueDraftLineCreate] | None = Field(
        default=None,
        min_length=1,
        max_length=200,
    )
    reason: str = Field(min_length=1, max_length=1000)


class SiteIssueSearch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=36)
    q: str | None = None
    workflow_statuses: list[str] = Field(
        default_factory=lambda: ["draft", "confirmed", "corrected", "void"],
        min_length=1,
        max_length=4,
    )
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)


class SiteIssueStatusPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    raw_status: str = Field(min_length=1, max_length=64)
    normalized_status: str
    status_mapping_version: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=1000)


class ManualCostPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line_id: str = Field(min_length=1, max_length=64)
    version: int = Field(ge=1)
    unit_cost_ex_tax: Decimal = Field(ge=0)
    evidence: str = Field(min_length=1, max_length=1000)
    reason: str = Field(min_length=1, max_length=1000)


class CostGapRecompute(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=1000)


class ExpenseCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expense_id: str = Field(min_length=1, max_length=64)
    project_contract_id: str | None = Field(default=None, max_length=36)
    expense_ref: str = Field(min_length=1, max_length=128)
    expense_date: date
    applicant: str | None = Field(default=None, max_length=64)
    category: str | None = Field(default=None, max_length=64)
    expense_reason: str | None = Field(default=None, max_length=32767)
    amount_ex_tax: Decimal = Field(ge=0)
    raw_status: str = Field(min_length=1, max_length=64)
    status_mapping_state: str
    normalized_status: str
    status_mapping_version: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=1000)


class ExpenseReadinessMark(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ready_through: date
    expected_version: int | None = Field(default=None, ge=0)
    correction_reason: str | None = Field(default=None, min_length=1, max_length=1000)
    reason: str = Field(min_length=1, max_length=1000)


class ExpenseStatusPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    raw_status: str = Field(min_length=1, max_length=64)
    normalized_status: str
    status_mapping_version: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=1000)


def _real_operator(db: Session, ident: dict) -> str:
    if ident.get("authn") != "sys_user" or ident.get("fb"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "经营事实写入必须使用实名系统账号")
    username = str(ident.get("sub") or "").strip()
    user = db.scalar(
        select(SysUser).where(
            SysUser.username == username,
            SysUser.is_active.is_(True),
        )
    )
    if not username or user is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "经营事实写入必须使用实名系统账号")
    return username


def _enforce_site_issue_access(
    db: Session,
    *,
    issue_id: str,
    ctx: UserContext,
) -> None:
    """Apply manager row scope to entity-id routes before reading or writing."""

    project_id = db.scalar(
        select(MaintenanceSiteIssue.project_id).where(
            MaintenanceSiteIssue.issue_id == issue_id
        )
    )
    if project_id is not None:
        enforce_maintenance_project_access(db, project_id=project_id, ctx=ctx)


@router.post("/{project_id}/contracts", status_code=status.HTTP_201_CREATED)
def create_project_contract(
    body: ContractCreate,
    project_id: str = Path(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(
        require_action("action_maintenance_project_manage", require_data="data_profit")
    ),
    _scope: None = Depends(require_maintenance_project_access),
) -> dict:
    operator = _real_operator(db, ident)
    try:
        payload = operations.create_contract(
            db,
            project_id=project_id,
            **body.model_dump(exclude={"reason"}),
            reason=body.reason,
            operated_by=operator,
        )
        if payload is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "维保项目不存在")
        db.commit()
        return payload
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "项目合同关系重复或冲突") from exc
    except operations.MaintenanceOperationConflict as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except operations.MaintenanceOperationError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except Exception:
        db.rollback()
        raise


def _contract_write_result(
    operation,
    *,
    db: Session,
    ident: dict,
    not_found_message: str = "项目合同关系不存在",
    **kwargs,
) -> dict:
    operator = _real_operator(db, ident)
    try:
        payload = operation(db, operated_by=operator, **kwargs)
        if payload is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, not_found_message)
        db.commit()
        return payload
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "项目合同关系重复或冲突") from exc
    except operations.MaintenanceOperationConflict as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except operations.MaintenanceOperationError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except Exception:
        db.rollback()
        raise


@router.patch("/contracts/{project_contract_id}")
def patch_project_contract(
    body: ContractPatch,
    project_contract_id: str = Path(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(
        require_action("action_maintenance_project_manage", require_data="data_profit")
    ),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    project_id = db.scalar(
        select(MaintenanceProjectContract.project_id).where(
            MaintenanceProjectContract.project_contract_id == project_contract_id
        )
    )
    if project_id is not None:
        enforce_maintenance_project_access(db, project_id=project_id, ctx=ctx)
    return _contract_write_result(
        operations.update_contract,
        db=db,
        ident=ident,
        project_contract_id=project_contract_id,
        version=body.version,
        updates=body.model_dump(exclude_unset=True, exclude={"version", "reason"}),
        reason=body.reason,
    )


@router.post("/contracts/{project_contract_id}/archive")
def archive_project_contract(
    body: ContractArchive,
    project_contract_id: str = Path(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(
        require_action("action_maintenance_project_manage", require_data="data_profit")
    ),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    project_id = db.scalar(
        select(MaintenanceProjectContract.project_id).where(
            MaintenanceProjectContract.project_contract_id == project_contract_id
        )
    )
    if project_id is not None:
        enforce_maintenance_project_access(db, project_id=project_id, ctx=ctx)
    return _contract_write_result(
        operations.archive_contract,
        db=db,
        ident=ident,
        project_contract_id=project_contract_id,
        version=body.version,
        effective_to=body.effective_to,
        reason=body.reason,
    )


@router.post("/{project_id}/collections", status_code=status.HTTP_201_CREATED)
def create_project_collection(
    body: CollectionCreate,
    project_id: str = Path(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(
        require_action("action_maintenance_roundtrip_apply", require_data="data_profit")
    ),
    _scope: None = Depends(require_maintenance_project_access),
) -> dict:
    operator = _real_operator(db, ident)
    try:
        payload = operations.create_collection(
            db,
            project_id=project_id,
            **body.model_dump(exclude={"reason"}),
            reason=body.reason,
            operated_by=operator,
            source="direct_api",
            import_batch_id=None,
        )
        if payload is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "维保项目不存在")
        db.commit()
        return payload
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "同一合同同一月份只能有一条累计回款") from exc
    except operations.MaintenanceOperationError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except Exception:
        db.rollback()
        raise


@router.patch("/collections/{collection_id}")
def patch_project_collection(
    body: CollectionPatch,
    collection_id: str = Path(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(
        require_action("action_maintenance_roundtrip_apply", require_data="data_profit")
    ),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    project_id = db.scalar(
        select(MaintenanceCollectionSnapshot.project_id).where(
            MaintenanceCollectionSnapshot.collection_id == collection_id
        )
    )
    if project_id is not None:
        enforce_maintenance_project_access(db, project_id=project_id, ctx=ctx)
    return _contract_write_result(
        operations.update_collection,
        db=db,
        ident=ident,
        collection_id=collection_id,
        version=body.version,
        updates=body.model_dump(exclude_unset=True, exclude={"version", "reason"}),
        reason=body.reason,
    )


@site_issue_router.post("/projects/{project_id}/candidates/search")
def search_project_site_issue_candidates(
    body: SiteIssueCandidateSearch,
    project_id: str = Path(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    enforce_maintenance_project_access(db, project_id=project_id, ctx=ctx)
    try:
        payload = operations.search_site_issue_candidates(
            db,
            project_id=project_id,
            q_text=body.q,
            page=body.page,
            page_size=body.page_size,
        )
        if payload is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "维保项目不存在")
    except HTTPException:
        raise
    except operations.MaintenanceOperationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    record_access_log(
        ctx,
        "site_issue_delivery_candidates",
        "maintenance_project",
        {
            "project_id": project_id,
            "searched": bool(body.q and body.q.strip()),
            "adapter_state": payload["adapter"]["state"],
            "page": body.page,
            "page_size": body.page_size,
            "total": payload["total"],
        },
    )
    return payload


@router.post("/{project_id}/site-issues", status_code=status.HTTP_201_CREATED)
def create_project_site_issue(
    body: SiteIssueCreate,
    project_id: str = Path(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(
        require_action(
            "action_maintenance_site_issue_manage",
            require_data="data_purchase_cost",
        )
    ),
    _scope: None = Depends(require_maintenance_project_access),
) -> dict:
    operator = _real_operator(db, ident)
    try:
        payload = operations.create_site_issue(
            db,
            project_id=project_id,
            **body.model_dump(exclude={"reason"}),
            reason=body.reason,
            operated_by=operator,
            source="direct_api",
            import_batch_id=None,
        )
        if payload is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "维保项目不存在")
        db.commit()
        return payload
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "现场领用单或明细重复") from exc
    except operations.MaintenanceOperationError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except Exception:
        db.rollback()
        raise


@site_issue_router.post("/projects/{project_id}", status_code=status.HTTP_201_CREATED)
def create_project_site_issue_draft(
    body: SiteIssueDraftCreate,
    project_id: str = Path(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(
        require_action(
            "action_maintenance_site_issue_manage",
            require_data="data_purchase_cost",
        )
    ),
    _scope: None = Depends(require_maintenance_project_access),
) -> dict:
    operator = _real_operator(db, ident)
    try:
        payload = operations.create_site_issue_draft(
            db,
            project_id=project_id,
            **body.model_dump(exclude={"reason"}),
            reason=body.reason,
            operated_by=operator,
        )
        if payload is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "维保项目不存在")
        db.commit()
        return payload
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "现场领用单或明细重复") from exc
    except operations.MaintenanceOperationConflict as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except operations.MaintenanceOperationPermissionError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except operations.MaintenanceOperationError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except Exception:
        db.rollback()
        raise


@site_issue_router.post("/{issue_id}/preview")
def preview_project_site_issue(
    body: SiteIssuePreview,
    issue_id: str = Path(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(
        require_action(
            "action_maintenance_site_issue_manage",
            require_data="data_purchase_cost",
        )
    ),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    _enforce_site_issue_access(db, issue_id=issue_id, ctx=ctx)
    try:
        payload = operations.preview_site_issue(
            db,
            issue_id=issue_id,
            project_id=body.project_id,
            version=body.version,
        )
        if payload is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "现场领用单不存在")
        return payload
    except HTTPException:
        raise
    except operations.MaintenanceOperationConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except operations.MaintenanceOperationPermissionError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except operations.MaintenanceOperationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@site_issue_router.post("/{issue_id}/confirm")
def confirm_project_site_issue(
    body: SiteIssueCommand,
    issue_id: str = Path(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(
        require_action(
            "action_maintenance_site_issue_manage",
            require_data="data_purchase_cost",
        )
    ),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    _enforce_site_issue_access(db, issue_id=issue_id, ctx=ctx)
    operator = _real_operator(db, ident)
    try:
        payload = operations.confirm_site_issue(
            db,
            issue_id=issue_id,
            project_id=body.project_id,
            version=body.version,
            idempotency_key=body.idempotency_key,
            reason=body.reason,
            operated_by=operator,
        )
        if payload is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "现场领用单或项目不存在")
        db.commit()
        return payload
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "现场领用确认发生并发冲突") from exc
    except operations.MaintenanceOperationConflict as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except operations.MaintenanceOperationPermissionError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except operations.MaintenanceOperationError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except Exception:
        db.rollback()
        raise


@site_issue_router.patch("/{issue_id}")
def patch_project_site_issue_v2(
    body: SiteIssuePatch,
    issue_id: str = Path(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(
        require_action(
            "action_maintenance_site_issue_manage",
            require_data="data_purchase_cost",
        )
    ),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    _enforce_site_issue_access(db, issue_id=issue_id, ctx=ctx)
    operator = _real_operator(db, ident)
    try:
        payload = operations.patch_site_issue(
            db,
            issue_id=issue_id,
            project_id=body.project_id,
            version=body.version,
            idempotency_key=body.idempotency_key,
            issue_date=body.issue_date,
            receiver=body.receiver,
            issued_by=body.issued_by,
            site_location=body.site_location,
            lines=(
                [line.model_dump() for line in body.lines]
                if body.lines is not None
                else None
            ),
            reason=body.reason,
            operated_by=operator,
        )
        if payload is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "现场领用单或项目不存在")
        db.commit()
        return payload
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "现场领用编辑发生并发冲突") from exc
    except operations.MaintenanceOperationConflict as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except operations.MaintenanceOperationPermissionError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except operations.MaintenanceOperationError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except Exception:
        db.rollback()
        raise


@site_issue_router.post("/{issue_id}/void")
def void_project_site_issue(
    body: SiteIssueCommand,
    issue_id: str = Path(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(
        require_action(
            "action_maintenance_site_issue_manage",
            require_data="data_purchase_cost",
        )
    ),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    _enforce_site_issue_access(db, issue_id=issue_id, ctx=ctx)
    operator = _real_operator(db, ident)
    try:
        payload = operations.void_site_issue(
            db,
            issue_id=issue_id,
            project_id=body.project_id,
            version=body.version,
            idempotency_key=body.idempotency_key,
            reason=body.reason,
            operated_by=operator,
        )
        if payload is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "现场领用单或项目不存在")
        db.commit()
        return payload
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "现场领用作废发生并发冲突") from exc
    except operations.MaintenanceOperationConflict as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except operations.MaintenanceOperationPermissionError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except operations.MaintenanceOperationError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except Exception:
        db.rollback()
        raise


@site_issue_router.post("/search")
def search_project_site_issues(
    body: SiteIssueSearch,
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    if is_field_hidden(ctx, "unit_cost"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "无权查看现场领用成本")
    enforce_maintenance_project_access(db, project_id=body.project_id, ctx=ctx)
    try:
        payload = operations.search_site_issues(
            db,
            project_id=body.project_id,
            q_text=body.q,
            workflow_statuses=body.workflow_statuses,
            page=body.page,
            page_size=body.page_size,
        )
        if payload is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "维保项目不存在")
    except HTTPException:
        raise
    except operations.MaintenanceOperationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    record_access_log(
        ctx,
        "site_issue_search",
        "maintenance_project",
        {
            "project_id": body.project_id,
            "searched": bool(body.q and body.q.strip()),
            "workflow_statuses": body.workflow_statuses,
            "page": body.page,
            "page_size": body.page_size,
            "total": payload["total"],
        },
    )
    return payload


@router.patch("/site-issues/{issue_id}/status")
def patch_project_site_issue_status(
    body: SiteIssueStatusPatch,
    issue_id: str = Path(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(
        require_action(
            "action_maintenance_site_issue_manage",
            require_data="data_purchase_cost",
        )
    ),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    project_id = db.scalar(
        select(MaintenanceSiteIssue.project_id).where(
            MaintenanceSiteIssue.issue_id == issue_id
        )
    )
    if project_id is not None:
        enforce_maintenance_project_access(db, project_id=project_id, ctx=ctx)
    operator = _real_operator(db, ident)
    try:
        payload = operations.update_site_issue_status(
            db,
            issue_id=issue_id,
            version=body.version,
            raw_status=body.raw_status,
            normalized_status=body.normalized_status,
            status_mapping_version=body.status_mapping_version,
            reason=body.reason,
            operated_by=operator,
        )
        if payload is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "现场领用单不存在")
        db.commit()
        return payload
    except HTTPException:
        db.rollback()
        raise
    except operations.MaintenanceOperationConflict as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except operations.MaintenanceOperationError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except Exception:
        db.rollback()
        raise


@router.get("/{project_id}/cost-gaps")
def project_cost_gaps(
    project_id: str = Path(..., min_length=1, max_length=36),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
    _scope: None = Depends(require_maintenance_project_access),
) -> dict:
    if is_field_hidden(ctx, "unit_cost"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "无权查看或回填采购成本")
    payload = operations.list_cost_gaps(
        db,
        project_id=project_id,
        page=page,
        page_size=page_size,
    )
    if payload is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "维保项目不存在")
    record_access_log(
        ctx,
        "stable_project_cost_gaps",
        "maintenance_project",
        {
            "project_id": project_id,
            "page": page,
            "page_size": page_size,
            "total": payload["total"],
        },
    )
    return payload


@router.post("/{project_id}/cost-gaps/recompute")
def recompute_project_cost_gaps(
    body: CostGapRecompute,
    project_id: str = Path(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(
        require_action(
            "action_maintenance_project_manage",
            require_data="data_purchase_cost",
        )
    ),
    _scope: None = Depends(require_maintenance_project_access),
) -> dict:
    return _contract_write_result(
        operations.recompute_cost_gaps,
        db=db,
        ident=ident,
        not_found_message="维保项目不存在",
        project_id=project_id,
        reason=body.reason,
    )


@router.post("/{project_id}/expenses", status_code=status.HTTP_201_CREATED)
def create_project_expense(
    body: ExpenseCreate,
    project_id: str = Path(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(
        require_action("action_maintenance_roundtrip_apply", require_data="data_profit")
    ),
    _scope: None = Depends(require_maintenance_project_access),
) -> dict:
    operator = _real_operator(db, ident)
    try:
        payload = operations.create_expense(
            db,
            project_id=project_id,
            **body.model_dump(exclude={"reason"}),
            reason=body.reason,
            operated_by=operator,
        )
        if payload is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "维保项目不存在")
        db.commit()
        return payload
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "报销单重复或冲突") from exc
    except operations.MaintenanceOperationError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except Exception:
        db.rollback()
        raise


@router.put("/{project_id}/expenses/readiness")
def mark_project_expense_readiness(
    body: ExpenseReadinessMark,
    project_id: str = Path(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(
        require_action(
            "action_maintenance_roundtrip_apply",
            require_data="data_profit",
        )
    ),
    _scope: None = Depends(require_maintenance_project_access),
) -> dict:
    return _contract_write_result(
        operations.mark_expense_readiness,
        db=db,
        ident=ident,
        not_found_message="维保项目不存在",
        project_id=project_id,
        ready_through=body.ready_through,
        expected_version=body.expected_version,
        correction_reason=body.correction_reason,
        reason=body.reason,
    )


@router.patch("/expenses/{expense_id}/status")
def patch_project_expense_status(
    body: ExpenseStatusPatch,
    expense_id: str = Path(..., min_length=1, max_length=64),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(
        require_action(
            "action_maintenance_roundtrip_apply",
            require_data="data_profit",
        )
    ),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    project_id = db.scalar(
        select(MaintenanceProjectExpenseAttribution.project_id).where(
            MaintenanceProjectExpenseAttribution.expense_id == expense_id
        )
    )
    if project_id is not None:
        enforce_maintenance_project_access(db, project_id=project_id, ctx=ctx)
    operator = _real_operator(db, ident)
    try:
        payload = operations.update_expense_status(
            db,
            expense_id=expense_id,
            version=body.version,
            raw_status=body.raw_status,
            normalized_status=body.normalized_status,
            status_mapping_version=body.status_mapping_version,
            reason=body.reason,
            operated_by=operator,
        )
        if payload is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "报销归集事实不存在")
        db.commit()
        return payload
    except HTTPException:
        db.rollback()
        raise
    except operations.MaintenanceOperationConflict as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except operations.MaintenanceOperationError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except Exception:
        db.rollback()
        raise


@router.patch("/{project_id}/cost-gaps")
def patch_project_cost_gap(
    body: ManualCostPatch,
    project_id: str = Path(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(
        require_action("action_maintenance_project_manage", require_data="data_purchase_cost")
    ),
    _scope: None = Depends(require_maintenance_project_access),
) -> dict:
    return _contract_write_result(
        operations.fill_manual_cost,
        db=db,
        ident=ident,
        project_id=project_id,
        issue_line_id=body.line_id,
        version=body.version,
        manual_unit_cost=body.unit_cost_ex_tax,
        evidence=body.evidence,
        reason=body.reason,
    )


@router.get("/{project_id}/workspace")
def stable_project_workspace(
    project_id: str = Path(..., min_length=1, max_length=36),
    as_of: date | None = None,
    collection_page: int = Query(1, ge=1),
    collection_page_size: int = Query(20, ge=1, le=100),
    requisition_page: int = Query(1, ge=1),
    requisition_page_size: int = Query(20, ge=1, le=100),
    expense_page: int = Query(1, ge=1),
    expense_page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _scope: None = Depends(require_maintenance_project_access),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    effective_as_of = as_of or business_today()
    payload = operations.project_workspace(
        db,
        project_id=project_id,
        as_of=effective_as_of,
        user_ctx=ctx,
        collection_page=collection_page,
        collection_page_size=collection_page_size,
        requisition_page=requisition_page,
        requisition_page_size=requisition_page_size,
        expense_page=expense_page,
        expense_page_size=expense_page_size,
    )
    if payload is None:
        db.rollback()
        raise HTTPException(status.HTTP_404_NOT_FOUND, "维保项目不存在")
    db.commit()
    record_access_log(
        ctx,
        "stable_project_workspace",
        "maintenance_project",
        {
            "project_id": project_id,
            "as_of": effective_as_of.isoformat(),
            "requisition_total": payload["requisitions"]["total"],
            "approved_expense_total": payload["approved_expenses"]["total"],
            "reminder_total": len(payload["reminders"]),
        },
    )
    return payload


@router.get("/{project_id}/tasks")
def stable_project_tasks(
    project_id: str = Path(..., min_length=1, max_length=36),
    as_of: date | None = None,
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _scope: None = Depends(require_maintenance_project_access),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    effective_as_of = as_of or business_today()
    payload = operations.project_tasks(
        db,
        project_id=project_id,
        as_of=effective_as_of,
        user_ctx=ctx,
    )
    if payload is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "维保项目不存在")
    record_access_log(
        ctx,
        "stable_project_tasks",
        "maintenance_project",
        {
            "project_id": project_id,
            "as_of": effective_as_of.isoformat(),
            "total": payload["total"],
        },
    )
    return payload


@router.get("/{project_id}/purchases")
def stable_project_purchases(
    project_id: str = Path(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _scope: None = Depends(require_maintenance_project_access),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    rows = procurement.get_project_procurement_chain(db, project_id)
    # 采购成本字段脱敏：无 data_purchase_cost 权限时单价结构性置空，
    # 与同文件 unit_cost 遮罩（is_field_hidden "unit_cost"）同一口径。
    if is_field_hidden(ctx, "unit_price"):
        for order in rows:
            for line in order.get("lines", []):
                line["unit_price"] = None
    record_access_log(
        ctx,
        "stable_project_purchases",
        "maintenance_project",
        {
            "project_id": project_id,
            "result_count": len(rows),
            "cost_masked": is_field_hidden(ctx, "unit_price"),
        },
    )
    return {"project_id": project_id, "purchases": rows}


def _stable_project_operations_response(
    *,
    as_of: date | None,
    page: int,
    page_size: int,
    q: str | None,
    lifecycle: str,
    reminder: str | None,
    include_inactive: bool,
    owner_scope: str | None,
    task_type: str | None,
    task_status: str | None,
    due_from: date | None,
    due_to: date | None,
    db: Session,
    ctx: UserContext,
) -> dict:
    effective_as_of = as_of or business_today()
    try:
        effective_owner_scope = assignments.resolve_owner_scope(ctx, owner_scope)
        payload = operations.project_operations(
            db,
            as_of=effective_as_of,
            user_ctx=ctx,
            q_text=q,
            lifecycle=lifecycle,
            reminder=reminder,
            include_inactive=include_inactive,
            page=page,
            page_size=page_size,
            owner_scope=effective_owner_scope,
            task_type=task_type,
            task_status=task_status,
            due_from=due_from,
            due_to=due_to,
        )
    except assignments.MaintenanceProjectAssignmentPermissionError as exc:
        db.rollback()
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "当前账号只能查看本人负责的维保项目",
        ) from exc
    except operations.MaintenanceOperationPermissionError as exc:
        db.rollback()
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "当前账号无权使用该提醒筛选",
        ) from exc
    except operations.MaintenanceOperationError as exc:
        db.rollback()
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            str(exc),
        ) from exc
    except Exception:
        db.rollback()
        raise
    db.commit()
    log_detail = {
        "as_of": effective_as_of.isoformat(),
        "searched": bool(q and q.strip()),
        "lifecycle": lifecycle,
        "reminder": reminder,
        "include_inactive": include_inactive,
        "page": page,
        "page_size": page_size,
    }
    if task_type or task_status or due_from or due_to:
        log_detail.update(
            {
                # Never persist the free-text task-type selector itself.
                "task_type_filtered": bool(task_type and task_type.strip()),
                "task_status": task_status,
                "due_from": due_from.isoformat() if due_from else None,
                "due_to": due_to.isoformat() if due_to else None,
            }
        )
    record_access_log(
        ctx,
        "stable_project_operations",
        "maintenance",
        log_detail,
    )
    return payload


@router.get("/operations")
def stable_project_operations(
    as_of: date | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=1, le=200),
    q: str | None = Query(default=None),
    lifecycle: str = Query(
        "all", pattern="^(ongoing|ended|missing|all)$"
    ),
    reminder: str | None = Query(default=None),
    include_inactive: bool = False,
    owner_scope: str | None = Query(default=None, pattern="^(me|all)$"),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    if q is not None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "项目搜索请使用安全搜索接口",
        )
    return _stable_project_operations_response(
        as_of=as_of,
        page=page,
        page_size=page_size,
        q=None,
        lifecycle=lifecycle,
        reminder=reminder,
        include_inactive=include_inactive,
        owner_scope=owner_scope,
        task_type=None,
        task_status=None,
        due_from=None,
        due_to=None,
        db=db,
        ctx=ctx,
    )


@router.post("/operations/search")
def search_stable_project_operations(
    body: ProjectOperationsSearch,
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    q = body.q.strip()
    if len(q) > 256:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "项目搜索条件无效",
        )
    if body.task_type is not None and len(body.task_type.strip()) > 64:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "任务筛选条件无效",
        )
    return _stable_project_operations_response(
        as_of=body.as_of,
        page=body.page,
        page_size=body.page_size,
        q=q,
        lifecycle=body.lifecycle,
        reminder=body.reminder,
        include_inactive=body.include_inactive,
        owner_scope=body.owner_scope,
        task_type=body.task_type.strip() if body.task_type else None,
        task_status=body.task_status,
        due_from=body.due_from,
        due_to=body.due_to,
        db=db,
        ctx=ctx,
    )

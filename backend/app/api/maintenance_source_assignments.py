"""Manual source maintenance order assignment endpoints (#201)."""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from app.api.maintenance_projects import _real_operator
from app.auth import current_identity, current_role
from app.db import get_db
from app.security import (
    UserContext,
    get_current_user_context,
    record_access_log,
    require_action,
    require_page,
)
from app.services import maintenance_source_assignments as assignments


router = APIRouter(
    prefix="/maintenance/project-assignments/orders",
    tags=["maintenance"],
)


class AssignmentExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_order_id: str = Field(min_length=1, max_length=64)
    expected_assignment_id: str | None = Field(default=None, max_length=36)
    expected_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def expectation_pair_is_complete(self):
        if (self.expected_assignment_id is None) != (self.expected_version is None):
            raise ValueError("expected_assignment_id 与 expected_version 必须同时提供")
        return self


class AssignSourceOrders(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=36)
    items: list[AssignmentExpectation] = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=1000)


class UnassignmentExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assignment_id: str = Field(min_length=1, max_length=36)
    expected_version: int = Field(ge=1)


class UnassignSourceOrders(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[UnassignmentExpectation] = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=1000)


@router.get("")
def source_order_directory(
    q: str | None = Query(None, max_length=128),
    source_order_id: list[str] | None = Query(None),
    assignment_status: str = Query("unassigned"),
    project_id: str | None = Query(None, max_length=36),
    include_candidates: bool = Query(False),   # plan v1.3 M2-1：只读候选，不自动写
    # #48：给定项目时，命中该项目 XSDD 集合的未归属单排最前（排序，不过滤）
    xsdd_project_id: str | None = Query(None, max_length=36),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    if source_order_id:
        if len(source_order_id) > 100:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "一次最多刷新 100 张来源维保单",
            )
        if any(not value.strip() or len(value) > 64 for value in source_order_id):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "来源维保单 ID 必须为 1 至 64 个字符",
            )
    record_access_log(
        ctx,
        "source_order_assignment_directory",
        "maintenance",
        {
            "assignment_status": assignment_status,
            "project_id": project_id,
            "source_order_count": len(source_order_id or []),
            "page": page,
            "page_size": page_size,
        },
    )
    try:
        return assignments.list_source_orders(
            db,
            q_text=q,
            source_order_ids=source_order_id,
            assignment_status=assignment_status,
            project_id=project_id,
            page=page,
            page_size=page_size,
            user_ctx=ctx,
            include_candidates=include_candidates,
            xsdd_project_id=xsdd_project_id,
        )
    except assignments.SourceAssignmentError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.post("/assign")
def assign_source_orders(
    body: AssignSourceOrders,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(
        require_action("action_maintenance_project_manage", require_data="data_profit")
    ),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    operated_by = _real_operator(db, ident)
    try:
        payload = assignments.assign_source_orders(
            db,
            project_id=body.project_id,
            items=[item.model_dump() for item in body.items],
            reason=body.reason,
            operated_by=operated_by,
            user_ctx=ctx,
        )
        db.commit()
        return {"assignments": payload}
    except assignments.SourceAssignmentConflict as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except assignments.SourceAssignmentError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except assignments.SourceAssignmentPermissionError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except Exception:
        db.rollback()
        raise


@router.post("/unassign")
def unassign_source_orders(
    body: UnassignSourceOrders,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(
        require_action("action_maintenance_project_manage", require_data="data_profit")
    ),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    operated_by = _real_operator(db, ident)
    try:
        payload = assignments.unassign_source_orders(
            db,
            items=[item.model_dump() for item in body.items],
            reason=body.reason,
            operated_by=operated_by,
            user_ctx=ctx,
        )
        db.commit()
        return {"assignments": payload}
    except assignments.SourceAssignmentConflict as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except assignments.SourceAssignmentError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except assignments.SourceAssignmentPermissionError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except Exception:
        db.rollback()
        raise

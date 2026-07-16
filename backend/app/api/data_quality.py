"""行级采购/销售数据疑点队列 API（DEV-05A）。"""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth import current_role
from app.db import get_db
from app.security import (
    UserContext,
    apply_field_visibility,
    get_current_user_context,
    is_field_hidden,
    require_action,
    require_page,
)
from app.services import data_quality as svc

router = APIRouter(
    prefix="/data-quality/issues",
    tags=["data-quality"],
    dependencies=[Depends(current_role), Depends(require_page("page_governance"))],
)


class DecisionBody(BaseModel):
    decision: Literal["confirmed_valid", "confirmed_source_error"]
    version: int = Field(ge=1)
    note: str


class ReopenBody(BaseModel):
    version: int = Field(ge=1)
    note: str


def _run(fn, **kwargs):
    try:
        return fn(**kwargs)
    except svc.DataQualityValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except svc.DataQualityNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except svc.DataQualityConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


def _visible(data: dict, ctx: UserContext, *, detail: bool = False) -> dict:
    # evidence 是规则定义的自由 JSON，字段名不稳定；成本受限时整体收起，避免换键名绕过。
    restricted = is_field_hidden(ctx, "unit_price")
    data["price_restricted"] = restricted
    if detail and restricted:
        data["evidence"] = None
        data["evidence_restricted"] = True
        for entry in data.get("audit", []):
            # before/after 含 evidence 快照，受限时不能把历史证据作为旁路。
            entry["before"] = None
            entry["after"] = None
    elif detail:
        data["evidence_restricted"] = False
    return apply_field_visibility(data, ctx)


@router.get("")
def list_issues(
    status_: str | None = Query(None, alias="status",
                                pattern="^(open|confirmed_valid|confirmed_source_error|source_changed)$"),
    side: str | None = Query(None, pattern="^(purchase|sales)$"),
    rule_code: str | None = None,
    q: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    data = _run(svc.list_issues, db=db, status=status_, side=side,
                rule_code=rule_code, q=q, page=page, page_size=page_size)
    return _visible(data, ctx)


@router.get("/{issue_id}")
def get_issue(
    issue_id: int,
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    data = svc.get_issue(db, issue_id)
    if data is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "疑点不存在")
    return _visible(data, ctx, detail=True)


@router.post("/{issue_id}/decision")
def decide_issue(
    issue_id: int,
    body: DecisionBody,
    db: Session = Depends(get_db),
    _action: None = Depends(require_action(
        "action_data_quality_review", require_data="data_purchase_cost")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    data = _run(
        svc.decide_issue, db=db, issue_id=issue_id, decision=body.decision,
        version=body.version, note=body.note, operated_by=ctx.user_id or ctx.role,
    )
    return _visible(data, ctx, detail=True)


@router.post("/{issue_id}/reopen")
def reopen_issue(
    issue_id: int,
    body: ReopenBody,
    db: Session = Depends(get_db),
    _action: None = Depends(require_action(
        "action_data_quality_review", require_data="data_purchase_cost")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    data = _run(
        svc.reopen_issue, db=db, issue_id=issue_id, version=body.version,
        note=body.note, operated_by=ctx.user_id or ctx.role,
    )
    return _visible(data, ctx, detail=True)

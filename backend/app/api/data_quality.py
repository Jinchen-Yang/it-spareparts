"""采购/销售质量队列 transport：governance_access 按实际 side 授权。

仅资源尚未切换时，protected service 的 legacy adapter 保留旧权限语义。
"""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.authz.principal import VerifiedIdentity, current_verified_identity
from app.services import governance_access
from app.security import UserContext, get_current_user_context
from app.services import data_quality as svc

router = APIRouter(
    prefix="/data-quality/issues",
    tags=["data-quality"],
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
    identity: VerifiedIdentity = Depends(current_verified_identity),
) -> dict:
    return _run(governance_access.read_quality_issues, db=db, identity=identity, legacy_context=ctx,
        status=status_, side=side, rule_code=rule_code, q=q, page=page, page_size=page_size)


@router.get("/{issue_id}")
def get_issue(
    issue_id: int,
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
    identity: VerifiedIdentity = Depends(current_verified_identity),
) -> dict:
    data = governance_access.read_quality_issue(db, identity, issue_id, legacy_context=ctx)
    if data is None:
        raise HTTPException(404, "疑点不存在")
    return data


@router.post("/{issue_id}/decision")
def decide_issue(
    issue_id: int,
    body: DecisionBody,
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
    identity: VerifiedIdentity = Depends(current_verified_identity),
) -> dict:
    return _run(governance_access.decide_quality_issue, db=db, identity=identity, issue_id=issue_id,
        legacy_context=ctx, **body.model_dump())


@router.post("/{issue_id}/reopen")
def reopen_issue(
    issue_id: int,
    body: ReopenBody,
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
    identity: VerifiedIdentity = Depends(current_verified_identity),
) -> dict:
    return _run(governance_access.reopen_quality_issue, db=db, identity=identity, issue_id=issue_id,
        legacy_context=ctx, **body.model_dump())

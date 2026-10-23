"""治理、型号身份与质量队列 transport。具名动作、字段投影和锁后授权由 governance_access 实施。

仅资源尚未切换时，protected service 的 legacy adapter 保留旧权限语义。
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.authz.principal import VerifiedIdentity, current_verified_identity
from app.services import governance_access
from app.security import UserContext, get_current_user_context
from app.services import governance, master_data, match_candidates, merge, part_rename

router = APIRouter(
    prefix="/governance",
    tags=["governance"],
)


class ExcludeRequest(BaseModel):
    pn_std: str
    excluded: bool = True
    reason: str | None = None


class MergeRequest(BaseModel):
    source_pn: str
    target_pn: str
    reason: str | None = None
    recompute: bool = True   # 合并后自动重算利润/库存成本


class UnmergeRequest(BaseModel):
    merge_log_id: int
    recompute: bool = True


class RenameRequest(BaseModel):
    pn_std: str            # 当前名（被标准化的型号）
    new_pn_std: str        # 厂商规范写法
    reason: str | None = None


class CandidateReview(BaseModel):
    action: str              # merge | reject | independent
    reason: str | None = None


class ConfirmBatchRequest(BaseModel):
    pn_stds: list[str] | None = None   # 指定型号；为空时按 scope
    scope: str | None = None           # no_candidates = 全部无候选的待审型号


class IssueResolve(BaseModel):
    action: str = "resolved"           # resolved | dismissed
    note: str | None = None


class AliasReview(BaseModel):
    action: str                        # approve | reject | reassign
    target_pn: str | None = None


@router.get("/summary")
def summary(
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
    identity: VerifiedIdentity = Depends(current_verified_identity),
) -> dict:
    try:
        result = governance_access.read_summary(db, identity, legacy_context=ctx)
    except (governance.GovernanceError, merge.MergeError, part_rename.RenameError,
            match_candidates.CandidateError, master_data.AliasError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    if result is None:
        raise HTTPException(404, "记录不存在")
    return result


@router.get("/metrics")
def metrics(
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
    identity: VerifiedIdentity = Depends(current_verified_identity),
) -> dict:
    try:
        result = governance_access.read_metrics(db, identity, legacy_context=ctx)
    except (governance.GovernanceError, merge.MergeError, part_rename.RenameError,
            match_candidates.CandidateError, master_data.AliasError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    if result is None:
        raise HTTPException(404, "记录不存在")
    return result


@router.post("/refresh")
def refresh(
    with_candidates: bool = Query(True),
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
    identity: VerifiedIdentity = Depends(current_verified_identity),
) -> dict:
    try:
        result = governance_access.refresh(db, identity, legacy_context=ctx, with_candidates=with_candidates)
    except (governance.GovernanceError, merge.MergeError, part_rename.RenameError,
            match_candidates.CandidateError, master_data.AliasError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    if result is None:
        raise HTTPException(404, "记录不存在")
    return result


@router.post("/classify-backfill")
def classify_backfill(
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
    identity: VerifiedIdentity = Depends(current_verified_identity),
) -> dict:
    try:
        result = governance_access.classify_backfill(db, identity, legacy_context=ctx)
    except (governance.GovernanceError, merge.MergeError, part_rename.RenameError,
            match_candidates.CandidateError, master_data.AliasError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    if result is None:
        raise HTTPException(404, "记录不存在")
    return result


@router.get("/parts")
def parts(
    kind: str = Query("nonstd"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
    identity: VerifiedIdentity = Depends(current_verified_identity),
) -> dict:
    try:
        result = governance_access.read_parts(db, identity, legacy_context=ctx, kind=kind, page=page, page_size=page_size)
    except (governance.GovernanceError, merge.MergeError, part_rename.RenameError,
            match_candidates.CandidateError, master_data.AliasError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    if result is None:
        raise HTTPException(404, "记录不存在")
    return result


@router.put("/exclude")
def exclude(
    body: ExcludeRequest,
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
    identity: VerifiedIdentity = Depends(current_verified_identity),
) -> dict:
    try:
        result = governance_access.exclude_parts(db, identity, legacy_context=ctx, **body.model_dump())
    except (governance.GovernanceError, merge.MergeError, part_rename.RenameError,
            match_candidates.CandidateError, master_data.AliasError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    if result is None:
        raise HTTPException(404, "记录不存在")
    return result


# ---------------- 合并 ----------------

@router.post("/parts/merge")
def merge_parts(
    body: MergeRequest,
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
    identity: VerifiedIdentity = Depends(current_verified_identity),
) -> dict:
    try:
        result = governance_access.merge_parts(db, identity, legacy_context=ctx, **body.model_dump())
    except (governance.GovernanceError, merge.MergeError, part_rename.RenameError,
            match_candidates.CandidateError, master_data.AliasError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    if result is None:
        raise HTTPException(404, "记录不存在")
    return result


@router.post("/parts/rename")
def rename_part(
    body: RenameRequest,
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
    identity: VerifiedIdentity = Depends(current_verified_identity),
) -> dict:
    try:
        result = governance_access.rename_part(db, identity, legacy_context=ctx, **body.model_dump())
    except (governance.GovernanceError, merge.MergeError, part_rename.RenameError,
            match_candidates.CandidateError, master_data.AliasError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    if result is None:
        raise HTTPException(404, "记录不存在")
    return result


@router.get("/merges")
def list_merges(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
    identity: VerifiedIdentity = Depends(current_verified_identity),
) -> dict:
    try:
        result = governance_access.read_merges(db, identity, legacy_context=ctx, page=page, page_size=page_size)
    except (governance.GovernanceError, merge.MergeError, part_rename.RenameError,
            match_candidates.CandidateError, master_data.AliasError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    if result is None:
        raise HTTPException(404, "记录不存在")
    return result


@router.post("/parts/unmerge")
def unmerge_parts(
    body: UnmergeRequest,
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
    identity: VerifiedIdentity = Depends(current_verified_identity),
) -> dict:
    try:
        result = governance_access.unmerge_parts(db, identity, legacy_context=ctx, **body.model_dump())
    except (governance.GovernanceError, merge.MergeError, part_rename.RenameError,
            match_candidates.CandidateError, master_data.AliasError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    if result is None:
        raise HTTPException(404, "记录不存在")
    return result


@router.post("/parts/confirm-batch")
def confirm_batch(
    body: ConfirmBatchRequest,
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
    identity: VerifiedIdentity = Depends(current_verified_identity),
) -> dict:
    try:
        result = governance_access.confirm_parts(db, identity, legacy_context=ctx, **body.model_dump())
    except (governance.GovernanceError, merge.MergeError, part_rename.RenameError,
            match_candidates.CandidateError, master_data.AliasError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    if result is None:
        raise HTTPException(404, "记录不存在")
    return result


# ---------------- 候选审核 ----------------

@router.get("/candidates")
def candidates(
    status_: str = Query("pending", alias="status"),
    order: str = Query("volume"),       # volume | score | age
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
    identity: VerifiedIdentity = Depends(current_verified_identity),
) -> dict:
    try:
        result = governance_access.read_candidates(db, identity, legacy_context=ctx, status=status_, order=order, page=page, page_size=page_size)
    except (governance.GovernanceError, merge.MergeError, part_rename.RenameError,
            match_candidates.CandidateError, master_data.AliasError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    if result is None:
        raise HTTPException(404, "记录不存在")
    return result


@router.post("/candidates/{candidate_id}/review")
def review_candidate(
    candidate_id: int,
    body: CandidateReview,
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
    identity: VerifiedIdentity = Depends(current_verified_identity),
) -> dict:
    try:
        result = governance_access.review_candidate(db, identity, candidate_id, legacy_context=ctx, **body.model_dump())
    except (governance.GovernanceError, merge.MergeError, part_rename.RenameError,
            match_candidates.CandidateError, master_data.AliasError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    if result is None:
        raise HTTPException(404, "记录不存在")
    return result


# ---------------- 质量问题 ----------------

@router.get("/issues")
def issues(
    issue_type: str | None = Query(None),
    status_: str = Query("open", alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
    identity: VerifiedIdentity = Depends(current_verified_identity),
) -> dict:
    try:
        result = governance_access.read_governance_issues(db, identity, legacy_context=ctx, issue_type=issue_type, status=status_, page=page, page_size=page_size)
    except (governance.GovernanceError, merge.MergeError, part_rename.RenameError,
            match_candidates.CandidateError, master_data.AliasError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    if result is None:
        raise HTTPException(404, "记录不存在")
    return result


@router.post("/issues/{issue_id}/resolve")
def resolve_issue(
    issue_id: int,
    body: IssueResolve,
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
    identity: VerifiedIdentity = Depends(current_verified_identity),
) -> dict:
    try:
        result = governance_access.resolve_governance_issue(db, identity, issue_id, legacy_context=ctx, **body.model_dump())
    except (governance.GovernanceError, merge.MergeError, part_rename.RenameError,
            match_candidates.CandidateError, master_data.AliasError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    if result is None:
        raise HTTPException(404, "记录不存在")
    return result


# ---------------- 别名审核 ----------------

@router.get("/aliases")
def aliases(
    status_: str = Query("pending", alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
    identity: VerifiedIdentity = Depends(current_verified_identity),
) -> dict:
    try:
        result = governance_access.read_aliases(db, identity, legacy_context=ctx, status=status_, page=page, page_size=page_size)
    except (governance.GovernanceError, merge.MergeError, part_rename.RenameError,
            match_candidates.CandidateError, master_data.AliasError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    if result is None:
        raise HTTPException(404, "记录不存在")
    return result


@router.post("/aliases/{alias_id}/review")
def review_alias(
    alias_id: int,
    body: AliasReview,
    db: Session = Depends(get_db),
    ctx: UserContext = Depends(get_current_user_context),
    identity: VerifiedIdentity = Depends(current_verified_identity),
) -> dict:
    try:
        result = governance_access.review_alias(db, identity, alias_id, legacy_context=ctx, **body.model_dump())
    except (governance.GovernanceError, merge.MergeError, part_rename.RenameError,
            match_candidates.CandidateError, master_data.AliasError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    if result is None:
        raise HTTPException(404, "记录不存在")
    return result

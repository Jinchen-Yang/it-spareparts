"""数据治理 API（报告#25/#11 + 整改 P0/P4 主数据工作流）。全部需管理员；写操作留审计。"""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.db import get_db
from app.services import governance, master_data, match_candidates, merge
from app.services import inventory as inventory_svc
from app.services import profit as profit_svc

router = APIRouter(prefix="/governance", tags=["governance"])

_KINDS = ("nonstd", "needs_review", "excluded")


class ExcludeRequest(BaseModel):
    pn_std: str
    excluded: bool = True
    reason: str | None = None


class MergeRequest(BaseModel):
    source_pn: str
    target_pn: str
    reason: str | None = None
    recompute: bool = True   # 合并后自动重算利润/库存成本


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
def summary(db: Session = Depends(get_db), _: str = Depends(require_admin)) -> dict:
    return governance.summary(db)


@router.get("/metrics")
def metrics(db: Session = Depends(get_db), _: str = Depends(require_admin)) -> dict:
    """主数据建设量化指标（审核说明 §7）：覆盖率/归一率/完整率/积压/可追溯率。"""
    return master_data.metrics(db)


@router.post("/refresh")
def refresh(
    with_candidates: bool = Query(True),
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
) -> dict:
    """一键回填+扫描（幂等）：字典/别名/规格/质量问题，可选生成合并候选。"""
    stats = master_data.refresh(db)
    if with_candidates:
        stats["candidates"] = match_candidates.generate(db)
    return stats


@router.get("/parts")
def parts(
    kind: str = Query("nonstd"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
) -> dict:
    k = kind if kind in _KINDS else "nonstd"
    return governance.list_parts(db, k, page, page_size)


@router.put("/exclude")
def exclude(
    body: ExcludeRequest,
    db: Session = Depends(get_db),
    role: str = Depends(require_admin),
) -> dict:
    try:
        res = governance.set_excluded(db, body.pn_std, body.excluded, body.reason, operated_by=role)
    except governance.GovernanceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if res is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"型号不存在: {body.pn_std}")
    return res


# ---------------- 合并 ----------------

@router.post("/parts/merge")
def merge_parts(
    body: MergeRequest,
    db: Session = Depends(get_db),
    role: str = Depends(require_admin),
) -> dict:
    try:
        result = merge.merge_parts(db, body.source_pn, body.target_pn, body.reason, role)
    except merge.MergeError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if body.recompute:
        # 合并已提交；重算各自管理事务。失败不回滚合并，提示手动重算。
        try:
            result["recompute"] = profit_svc.recompute(db)
            result["inventory_costs"] = inventory_svc.backfill_costs(db)
        except Exception as exc:  # noqa: BLE001
            result["recompute_failed"] = str(exc)
    return result


@router.post("/parts/confirm-batch")
def confirm_batch(
    body: ConfirmBatchRequest,
    db: Session = Depends(get_db),
    role: str = Depends(require_admin),
) -> dict:
    """批量确认独立型号（清待审标记）。pn_stds 与 scope=no_candidates 二选一。"""
    if body.pn_stds:
        from sqlalchemy import select

        from app.models.dimensions import DimPart
        ids = list(db.execute(
            select(DimPart.id).where(DimPart.pn_std.in_(body.pn_stds))).scalars().all())
    elif body.scope == "no_candidates":
        ids = match_candidates.parts_without_candidates(db)
    else:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "需提供 pn_stds 或 scope=no_candidates")
    if not ids:
        return {"confirmed": 0, "obsoleted_candidates": 0}
    return match_candidates.confirm_independent(db, ids, role)


# ---------------- 候选审核 ----------------

@router.get("/candidates")
def candidates(
    status_: str = Query("pending", alias="status"),
    order: str = Query("volume"),       # volume | score | age
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
) -> dict:
    return match_candidates.list_candidates(db, status_, page, page_size, order)


@router.post("/candidates/{candidate_id}/review")
def review_candidate(
    candidate_id: int,
    body: CandidateReview,
    db: Session = Depends(get_db),
    role: str = Depends(require_admin),
) -> dict:
    try:
        return match_candidates.review(db, candidate_id, body.action, body.reason, role)
    except (match_candidates.CandidateError, merge.MergeError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


# ---------------- 质量问题 ----------------

@router.get("/issues")
def issues(
    issue_type: str | None = Query(None),
    status_: str = Query("open", alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
) -> dict:
    return master_data.list_issues(db, issue_type, status_, page, page_size)


@router.post("/issues/{issue_id}/resolve")
def resolve_issue(
    issue_id: int,
    body: IssueResolve,
    db: Session = Depends(get_db),
    role: str = Depends(require_admin),
) -> dict:
    try:
        res = master_data.resolve_issue(db, issue_id, body.action, body.note, role)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if res is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"问题不存在: {issue_id}")
    return res


# ---------------- 别名审核 ----------------

@router.get("/aliases")
def aliases(
    status_: str = Query("pending", alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
) -> dict:
    return master_data.list_aliases(db, status_, page, page_size)


@router.post("/aliases/{alias_id}/review")
def review_alias(
    alias_id: int,
    body: AliasReview,
    db: Session = Depends(get_db),
    role: str = Depends(require_admin),
) -> dict:
    try:
        return master_data.review_alias(db, alias_id, body.action, body.target_pn, role)
    except master_data.AliasError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

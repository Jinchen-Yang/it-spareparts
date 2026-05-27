"""数据治理 API（报告#25/#11）。查看需管理员；排除操作写审计。"""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.db import get_db
from app.services import governance

router = APIRouter(prefix="/governance", tags=["governance"])

_KINDS = ("nonstd", "needs_review", "excluded")


class ExcludeRequest(BaseModel):
    pn_std: str
    excluded: bool = True
    reason: str | None = None


@router.get("/summary")
def summary(db: Session = Depends(get_db), _: str = Depends(require_admin)) -> dict:
    return governance.summary(db)


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
    res = governance.set_excluded(db, body.pn_std, body.excluded, body.reason, operated_by=role)
    if res is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"型号不存在: {body.pn_std}")
    return res

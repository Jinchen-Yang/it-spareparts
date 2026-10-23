"""替代关系 transport：part_substitute.read/manage，由 part_access 验证当前授权。

仅资源尚未切换时，protected service 的 legacy adapter 保留旧权限语义。
"""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.authz.principal import VerifiedIdentity, current_verified_identity
from app.security import UserContext, get_current_user_context
from app.services import part_access
from app.services import substitute


router = APIRouter(prefix="/substitutes", tags=["substitutes"])


class SubstituteCreate(BaseModel):
    pn_a: str
    pn_b: str
    note: str | None = None
    direction: str = "both"            # both=互替 | one_way=pn_b 可替代 pn_a
    substitute_type: str | None = None  # original/compatible/same_spec/downgrade/upgrade/conditional


@router.get("")
def list_(
    pn_std: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
    identity: VerifiedIdentity = Depends(current_verified_identity),
    ctx: UserContext = Depends(get_current_user_context),
) -> list[dict]:
    return part_access.read_substitutes(db, identity, legacy_context=ctx, pn_std=pn_std)


@router.post("")
def create(
    body: SubstituteCreate,
    db: Session = Depends(get_db),
    identity: VerifiedIdentity = Depends(current_verified_identity),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    try:
        return part_access.add_substitute(db, identity, legacy_context=ctx, pn_a=body.pn_a, pn_b=body.pn_b, note=body.note,
                                         direction=body.direction,
                                         substitute_type=body.substitute_type)
    except substitute.SubstituteError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.delete("")
def delete_(
    pn_a: str = Query(..., min_length=1),
    pn_b: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
    identity: VerifiedIdentity = Depends(current_verified_identity),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """解除两型号间的直连替代关系（管理员或采购，写审计）。加错了纠正用。"""
    try:
        return part_access.delete_substitute(db, identity, legacy_context=ctx, pn_a=pn_a, pn_b=pn_b)
    except substitute.SubstituteError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

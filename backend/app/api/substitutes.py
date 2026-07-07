"""替代料 API（§9）：查（登录）、增/删（管理员或采购，写审计）。"""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import current_role, require_roles
from app.db import get_db
from app.services import substitute

# 替代料维护（增/删）开放给采购：一线最了解通用号互替关系。admin 恒可，写审计留痕。
require_sub_editor = require_roles("purchaser")

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
    _: str = Depends(current_role),
) -> list[dict]:
    return substitute.list_substitutes(db, pn_std)


@router.post("")
def create(
    body: SubstituteCreate,
    db: Session = Depends(get_db),
    role: str = Depends(require_sub_editor),
) -> dict:
    try:
        return substitute.add_substitute(db, body.pn_a, body.pn_b, body.note, operated_by=role,
                                         direction=body.direction,
                                         substitute_type=body.substitute_type)
    except substitute.SubstituteError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.delete("")
def delete_(
    pn_a: str = Query(..., min_length=1),
    pn_b: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
    role: str = Depends(require_sub_editor),
) -> dict:
    """解除两型号间的直连替代关系（管理员或采购，写审计）。加错了纠正用。"""
    try:
        return substitute.remove_substitute(db, pn_a, pn_b, operated_by=role)
    except substitute.SubstituteError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

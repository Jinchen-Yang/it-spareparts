"""替代料 API（§9）：查（登录）、增（管理员，写审计）。"""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import current_role, require_admin
from app.db import get_db
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
    _: str = Depends(current_role),
) -> list[dict]:
    return substitute.list_substitutes(db, pn_std)


@router.post("")
def create(
    body: SubstituteCreate,
    db: Session = Depends(get_db),
    role: str = Depends(require_admin),
) -> dict:
    try:
        return substitute.add_substitute(db, body.pn_a, body.pn_b, body.note, operated_by=role,
                                         direction=body.direction,
                                         substitute_type=body.substitute_type)
    except substitute.SubstituteError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

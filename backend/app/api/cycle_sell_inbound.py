"""循环可售入站（D-0057 §10）。服务凭据，不是用户 JWT。"""
from __future__ import annotations

import hashlib
import hmac
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.services import cycle_sell_inbound as inbound

router = APIRouter(prefix="/integrations/cycle", tags=["cycle-sell-inbound"])
LINE_ID = r"^[A-Za-z0-9._:-]{1,64}$"


class RequeueInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deliveryId: UUID


class SoldInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schemaVersion: Literal[1]
    deliveryId: UUID
    shopOrderLineId: str = Field(pattern=LINE_ID)
    qty: Literal[1]


def require_callback_token(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    expected = get_settings().cycle_sell_callback_token_sha256.get_secret_value()
    if not expected:
        raise HTTPException(status_code=404, detail="not_enabled")
    scheme, separator, token = (authorization or "").partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not 32 <= len(token) <= 256:
        raise HTTPException(status_code=401, detail="credential_invalid")
    provided = hashlib.sha256(token.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="credential_invalid")


def _finish(db: Session, fn):
    try:
        payload = fn()
        db.commit()
        return payload
    except inbound.InboundError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except DBAPIError as exc:
        db.rollback()
        if getattr(exc.orig, "sqlstate", None) in {"55P03", "40001", "40P01"}:
            raise HTTPException(status_code=409, detail="remote_conflict") from exc
        raise


@router.post("/requeue", dependencies=[Depends(require_callback_token)])
def requeue(
    body: RequeueInput,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    return _finish(db, lambda: inbound.requeue(db, body.deliveryId))


def _phase_deferred(body: SoldInput, response: Response) -> None:
    """合法 v1 售出体本期零写入。解析通过后直接拒绝，不查库存、不改意图。"""
    del body
    response.headers["Cache-Control"] = "no-store"
    raise HTTPException(status_code=409, detail="phase_deferred")


@router.post("/mark-sold", dependencies=[Depends(require_callback_token)])
def mark_sold(body: SoldInput, response: Response) -> None:
    _phase_deferred(body, response)


@router.post("/revert-sold", dependencies=[Depends(require_callback_token)])
def revert_sold(body: SoldInput, response: Response) -> None:
    _phase_deferred(body, response)

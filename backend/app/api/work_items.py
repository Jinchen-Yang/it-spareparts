"""通用待办和时间线接口，业务权限由 SubjectSpec 提供。"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import current_identity
from app.db import get_db
from app.models.system import SysUser
from app.security import UserContext, record_access_log
from app.services.workflow import events, work_items
from app.services.workflow.registry import Subject, WorkflowError

router = APIRouter(tags=["workflow"])
ItemId = Annotated[int, Path(gt=0, le=2**63 - 1)]


def workflow_context(ident: dict = Depends(current_identity), db: Session = Depends(get_db)) -> UserContext:
    # 外键、领取和归属均依赖 sys_user.id；共享口令身份不能代替实名账号。
    if ident.get("fb") or ident.get("authn") == "shared" or not ident.get("sub"):
        raise HTTPException(403, "待办操作需要实名账号")
    ctx = UserContext(user_id=ident.get("sub"), role=ident.get("role", "guest"),
                      permissions=ident.get("perms"), is_authenticated=True)
    try:
        return work_items.actor_context(db, ctx)[1]
    except WorkflowError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc


Ctx = Annotated[UserContext, Depends(workflow_context)]
Db = Annotated[Session, Depends(get_db)]


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except WorkflowError as exc:
        raise HTTPException(exc.status_code, getattr(exc, "detail", str(exc))) from exc


@router.get("/work-items")
def list_work_items(
    db: Db, ctx: Ctx,
    tab: Literal["assigned", "pool", "claimed", "done", "all"] = "assigned",
    subject_type: str | None = None,
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
):
    return _call(work_items.visible_items, db, ctx, tab, subject_type, page=page, page_size=page_size)


@router.get("/work-items/badge")
def work_badge(db: Db, ctx: Ctx):
    return _call(work_items.badge, db, ctx)


@router.get("/work-items/candidates")
def pool_candidates(db: Db, ctx: Ctx, pool_key: str = Query(min_length=1, max_length=32)):
    return _call(work_items.candidates, db, ctx, pool_key)


class ActionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1, le=2**31 - 1)


class ReasonInput(ActionInput):
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def require_reason(self):
        self.reason = self.reason.strip()
        if not self.reason:
            raise ValueError("必须填写原因")
        return self


class TransferInput(ReasonInput):
    to_user_id: int = Field(gt=0, le=2**31 - 1)


class ReassignInput(ReasonInput):
    to_user_id: int | None = Field(gt=0, le=2**31 - 1)


def _act(db, ctx, item_id, action, body):
    fn = getattr(work_items, action)
    kwargs = body.model_dump()
    try:
        item = _call(fn, db, item_id, ctx, **kwargs)
        user, fresh_ctx = work_items.actor_context(db, ctx)
        result = work_items.serialize_items(db, [item], user.id, fresh_ctx)[0]
        db.commit()
    except Exception:
        db.rollback()
        raise
    record_access_log(ctx, f"work_item_{action}", "work_item",
                      {"work_item_id": item_id, **kwargs})
    return result


@router.post("/work-items/{item_id}/claim")
def claim(item_id: ItemId, body: ActionInput, db: Db, ctx: Ctx):
    return _act(db, ctx, item_id, "claim", body)


@router.post("/work-items/{item_id}/start")
def start(item_id: ItemId, body: ActionInput, db: Db, ctx: Ctx):
    return _act(db, ctx, item_id, "start", body)


@router.post("/work-items/{item_id}/release")
def release(item_id: ItemId, body: ReasonInput, db: Db, ctx: Ctx):
    return _act(db, ctx, item_id, "release", body)


@router.post("/work-items/{item_id}/transfer")
def transfer(item_id: ItemId, body: TransferInput, db: Db, ctx: Ctx):
    return _act(db, ctx, item_id, "transfer", body)


@router.post("/work-items/{item_id}/reassign")
def reassign(item_id: ItemId, body: ReassignInput, db: Db, ctx: Ctx):
    return _act(db, ctx, item_id, "reassign", body)


@router.post("/work-items/{item_id}/complete")
def complete(item_id: ItemId, body: ActionInput, db: Db, ctx: Ctx):
    return _act(db, ctx, item_id, "complete", body)


@router.post("/work-items/{item_id}/decline")
def decline(item_id: ItemId, body: ReasonInput, db: Db, ctx: Ctx):
    return _act(db, ctx, item_id, "decline", body)


@router.get("/subjects/{subject_type}/{subject_id}/events")
def subject_events(
    subject_type: str, subject_id: ItemId, db: Db, ctx: Ctx,
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
):
    rows = _call(events.list_events, db, Subject(subject_type, subject_id), ctx)
    rows.sort(key=lambda event: (event.created_at, event.id), reverse=True)
    total = len(rows)
    rows = rows[(page - 1) * page_size:page * page_size]
    actor_ids = {event.actor for event in rows if event.actor is not None}
    names = {user.id: user.display_name or user.username for user in db.scalars(
        select(SysUser).where(SysUser.id.in_(actor_ids))).all()} if actor_ids else {}
    return {"items": [{**{col.name: getattr(event, col.name) for col in event.__table__.columns},
                        "actor_name": names.get(event.actor)} for event in rows],
            "total": total, "page": page, "page_size": page_size}

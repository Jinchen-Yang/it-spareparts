"""坏件循环池 HTTP API（D-0057）。前缀 /cycle，与维保 page_maintenance 脱钩。"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import permissions as _perm
from app.auth import current_identity, current_role
from app.db import get_db
from app.models.maintenance_cycle import MaintenanceCycleUnit
from app.models.system import SysUser
from app.security import UserContext, get_current_user_context
from app.services import maintenance_cycle as cycle

router = APIRouter(prefix="/cycle", tags=["cycle"])


class HandoffBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    receipt_id: str = Field(min_length=1, max_length=36)
    qty: int = Field(ge=1, le=1000)
    expected_receipt_version: int = Field(ge=1)
    serial_numbers: list[str] | None = Field(default=None, max_length=1000)


class IntakeExternalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    qty: int = Field(ge=1, le=1000)
    part_id: int | None = Field(default=None, ge=1)
    pn: str | None = Field(default=None, min_length=1, max_length=128)
    note: str | None = Field(default=None, max_length=1000)
    intake_condition: str | None = Field(default=None, max_length=64)


class Versioned(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)


class DispatchBody(Versioned):
    to: Literal["testing", "repairing", "outsource"]
    assignee_username: str | None = Field(default=None, max_length=64)
    vendor_name: str | None = Field(default=None, max_length=128)


class CompleteTestBody(Versioned):
    result: Literal["good", "bad"]
    next: Literal["ready", "repairing", "scrap"]


class CompleteRepairBody(Versioned):
    result: Literal["fixed", "unfixed"]
    next: Literal["testing", "ready", "scrap"]


class OutsourceReturnBody(Versioned):
    next: Literal["testing", "repairing", "to_shop", "to_company", "scrap"]
    condition_bucket: Literal["good", "bad"] | None = None


class CloseBody(Versioned):
    outcome: Literal["to_shop", "to_company", "scrap"]
    condition_bucket: Literal["good", "bad"] | None = None
    note: str | None = Field(default=None, max_length=1000)


def _graph(db: Session, ctx: UserContext) -> dict[str, bool]:
    if not ctx.is_authenticated:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "请先登录")
    user = db.scalar(
        select(SysUser).where(
            SysUser.username == ctx.user_id,
            SysUser.is_active.is_(True),
        )
    )
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "请先登录")
    return _perm.runtime_safe(_perm.effective_for_user(user))


def _require_any(graph: dict[str, bool], *keys: str) -> None:
    if not any(graph.get(key, False) for key in keys):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "无循环池权限")


def _is_manage(graph: dict[str, bool]) -> bool:
    return bool(graph.get("page_cycle_manage", False))


def _real_operator(db: Session, ident: dict) -> str:
    if ident.get("authn") != "sys_user" or ident.get("fb"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "循环池写入必须使用实名系统账号")
    username = str(ident.get("sub") or "").strip()
    user = db.scalar(
        select(SysUser).where(
            SysUser.username == username,
            SysUser.is_active.is_(True),
        )
    )
    if not username or user is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "循环池写入必须使用实名系统账号")
    return username


def _raise(exc: Exception) -> None:
    if isinstance(exc, cycle.CycleConflict):
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    if isinstance(exc, cycle.CycleForbidden):
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    if isinstance(exc, cycle.CycleError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    raise exc


def _unit_visible(graph: dict[str, bool], row) -> bool:
    if _is_manage(graph) or graph.get("page_cycle_recycle", False):
        return True
    if row.station == "testing" and graph.get("page_cycle_test", False):
        return True
    if row.station == "repairing" and graph.get("page_cycle_repair", False):
        return True
    return False


@router.post("/units/handoff-from-receipt", status_code=status.HTTP_201_CREATED)
def handoff_from_receipt(
    body: HandoffBody,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[UserContext, Depends(get_current_user_context)],
    ident: Annotated[dict, Depends(current_identity)],
    _role: Annotated[str, Depends(current_role)],
):
    graph = _graph(db, ctx)
    _require_any(graph, "page_cycle_recycle", "page_cycle_manage")
    actor = _real_operator(db, ident)
    try:
        units = cycle.handoff_from_receipt(
            db,
            receipt_id=body.receipt_id,
            qty=body.qty,
            expected_receipt_version=body.expected_receipt_version,
            actor=actor,
            serial_numbers=body.serial_numbers,
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        _raise(exc)
    manage = _is_manage(graph)
    return {"items": [cycle.serialize_unit(u, manage=manage) for u in units]}


@router.post("/units/intake-external", status_code=status.HTTP_201_CREATED)
def intake_external(
    body: IntakeExternalBody,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[UserContext, Depends(get_current_user_context)],
    ident: Annotated[dict, Depends(current_identity)],
    _role: Annotated[str, Depends(current_role)],
):
    graph = _graph(db, ctx)
    _require_any(graph, "page_cycle_recycle", "page_cycle_manage")
    actor = _real_operator(db, ident)
    try:
        units = cycle.intake_external(
            db,
            actor=actor,
            qty=body.qty,
            part_id=body.part_id,
            pn=body.pn,
            note=body.note,
            intake_condition=body.intake_condition,
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        _raise(exc)
    manage = _is_manage(graph)
    return {"items": [cycle.serialize_unit(u, manage=manage) for u in units]}


@router.get("/units")
def list_units(
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[UserContext, Depends(get_current_user_context)],
    _role: Annotated[str, Depends(current_role)],
    station: str | None = Query(default=None),
    assignee: str | None = Query(default=None, max_length=64),
    part_id: int | None = Query(default=None, ge=1),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
):
    response.headers["Cache-Control"] = "no-store"
    graph = _graph(db, ctx)
    _require_any(
        graph,
        "page_cycle_recycle",
        "page_cycle_test",
        "page_cycle_repair",
        "page_cycle_manage",
    )
    stations: list[str] | None = None
    if station:
        stations = [s.strip() for s in station.split(",") if s.strip()]
    elif not _is_manage(graph) and not graph.get("page_cycle_recycle", False):
        allowed = []
        if graph.get("page_cycle_test", False):
            allowed.append("testing")
        if graph.get("page_cycle_repair", False):
            allowed.append("repairing")
        stations = allowed or ["__none__"]
    assignee_username = None if assignee in (None, "", "all") else assignee
    if assignee == "me":
        assignee_username = str(ctx.user_id)
    rows, total = cycle.list_units(
        db,
        stations=stations,
        assignee_username=assignee_username,
        part_id=part_id,
        limit=page_size,
        offset=(page - 1) * page_size,
    )
    manage = _is_manage(graph)
    return {
        "items": [cycle.serialize_unit(row, manage=manage) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/units/{unit_id}")
def get_unit(
    unit_id: int,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[UserContext, Depends(get_current_user_context)],
    _role: Annotated[str, Depends(current_role)],
):
    response.headers["Cache-Control"] = "no-store"
    graph = _graph(db, ctx)
    row = db.get(MaintenanceCycleUnit, unit_id)
    if row is None or not _unit_visible(graph, row):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "循环单位不存在")
    return cycle.serialize_unit(row, manage=_is_manage(graph))


def _mutate(db, ctx, ident, unit_id: int, fn):
    graph = _graph(db, ctx)
    actor = _real_operator(db, ident)
    try:
        row = fn(db, actor=actor, graph=graph)
        db.commit()
        db.refresh(row)
    except Exception as exc:
        db.rollback()
        _raise(exc)
    return cycle.serialize_unit(row, manage=_is_manage(graph))


@router.post("/units/{unit_id}/dispatch")
def dispatch_unit(
    unit_id: int,
    body: DispatchBody,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[UserContext, Depends(get_current_user_context)],
    ident: Annotated[dict, Depends(current_identity)],
    _role: Annotated[str, Depends(current_role)],
):
    def run(db, *, actor, graph):
        _require_any(graph, "page_cycle_recycle", "page_cycle_manage")
        return cycle.dispatch(
            db,
            unit_id=unit_id,
            expected_version=body.expected_version,
            actor=actor,
            to=body.to,
            assignee_username=body.assignee_username,
            vendor_name=body.vendor_name,
        )

    return _mutate(db, ctx, ident, unit_id, run)


@router.post("/units/{unit_id}/self-pass")
def self_pass_unit(
    unit_id: int,
    body: Versioned,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[UserContext, Depends(get_current_user_context)],
    ident: Annotated[dict, Depends(current_identity)],
    _role: Annotated[str, Depends(current_role)],
):
    def run(db, *, actor, graph):
        _require_any(graph, "page_cycle_recycle", "page_cycle_manage")
        return cycle.self_pass(
            db, unit_id=unit_id, expected_version=body.expected_version, actor=actor
        )

    return _mutate(db, ctx, ident, unit_id, run)


@router.post("/units/{unit_id}/complete-test")
def complete_test_unit(
    unit_id: int,
    body: CompleteTestBody,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[UserContext, Depends(get_current_user_context)],
    ident: Annotated[dict, Depends(current_identity)],
    _role: Annotated[str, Depends(current_role)],
):
    def run(db, *, actor, graph):
        _require_any(graph, "page_cycle_test", "page_cycle_manage")
        return cycle.complete_test(
            db,
            unit_id=unit_id,
            expected_version=body.expected_version,
            actor=actor,
            result=body.result,
            next_step=body.next,
        )

    return _mutate(db, ctx, ident, unit_id, run)


@router.post("/units/{unit_id}/complete-repair")
def complete_repair_unit(
    unit_id: int,
    body: CompleteRepairBody,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[UserContext, Depends(get_current_user_context)],
    ident: Annotated[dict, Depends(current_identity)],
    _role: Annotated[str, Depends(current_role)],
):
    def run(db, *, actor, graph):
        _require_any(graph, "page_cycle_repair", "page_cycle_manage")
        return cycle.complete_repair(
            db,
            unit_id=unit_id,
            expected_version=body.expected_version,
            actor=actor,
            result=body.result,
            next_step=body.next,
        )

    return _mutate(db, ctx, ident, unit_id, run)


@router.post("/units/{unit_id}/outsource-return")
def outsource_return_unit(
    unit_id: int,
    body: OutsourceReturnBody,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[UserContext, Depends(get_current_user_context)],
    ident: Annotated[dict, Depends(current_identity)],
    _role: Annotated[str, Depends(current_role)],
):
    def run(db, *, actor, graph):
        _require_any(graph, "page_cycle_recycle", "page_cycle_manage")
        return cycle.outsource_return(
            db,
            unit_id=unit_id,
            expected_version=body.expected_version,
            actor=actor,
            next_step=body.next,
            condition_bucket=body.condition_bucket,
        )

    return _mutate(db, ctx, ident, unit_id, run)


@router.post("/units/{unit_id}/close")
def close_unit(
    unit_id: int,
    body: CloseBody,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[UserContext, Depends(get_current_user_context)],
    ident: Annotated[dict, Depends(current_identity)],
    _role: Annotated[str, Depends(current_role)],
):
    def run(db, *, actor, graph):
        _require_any(graph, "page_cycle_recycle", "page_cycle_manage")
        return cycle.close(
            db,
            unit_id=unit_id,
            expected_version=body.expected_version,
            actor=actor,
            outcome=body.outcome,
            condition_bucket=body.condition_bucket,
            note=body.note,
        )

    return _mutate(db, ctx, ident, unit_id, run)


@router.post("/units/{unit_id}/mark-sold")
def mark_sold_unit(
    unit_id: int,
    body: Versioned,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[UserContext, Depends(get_current_user_context)],
    ident: Annotated[dict, Depends(current_identity)],
    _role: Annotated[str, Depends(current_role)],
):
    def run(db, *, actor, graph):
        _require_any(graph, "page_cycle_manage")
        return cycle.mark_sold(
            db, unit_id=unit_id, expected_version=body.expected_version, actor=actor
        )

    return _mutate(db, ctx, ident, unit_id, run)


@router.get("/units/{unit_id}/events")
def unit_events(
    unit_id: int,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[UserContext, Depends(get_current_user_context)],
    _role: Annotated[str, Depends(current_role)],
):
    response.headers["Cache-Control"] = "no-store"
    graph = _graph(db, ctx)
    row = db.get(MaintenanceCycleUnit, unit_id)
    if row is None or not _unit_visible(graph, row):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "循环单位不存在")
    return {"items": cycle.list_events(db, unit_id=unit_id, manage=_is_manage(graph))}


@router.get("/wip")
def wip(
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[UserContext, Depends(get_current_user_context)],
    _role: Annotated[str, Depends(current_role)],
    part_ids: str = Query(..., min_length=1, max_length=2000),
):
    response.headers["Cache-Control"] = "no-store"
    graph = _graph(db, ctx)
    _require_any(
        graph,
        "page_cycle_wip",
        "page_cycle_manage",
        "page_cycle_recycle",
    )
    raw = [item.strip() for item in part_ids.split(",") if item.strip()]
    if not raw or len(raw) > 100:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "part_ids 1–100 个")
    try:
        ids = [int(item) for item in raw]
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "part_ids 必须为正整数") from exc
    if any(item < 1 for item in ids):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "part_ids 必须为正整数")
    return {"items": cycle.wip_by_parts(db, ids)}

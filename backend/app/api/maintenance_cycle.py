"""坏件循环池 API（D-0057 T2）。

- 前缀 ``/api/cycle``；内部 JSON 用 snake_case（与 #520 的 camelCase 示例不同，
  跨仓投递才用 camelCase）。
- 所有写操作必须带 ``expected_version``；版本不匹配 409 且零写入。
- 五个页键都是「实名账号快照 ⊕ 覆盖」的显式门（T1 已纳入 ACCOUNT_SCOPED_BETA_PAGE_KEYS）：
  共享口令回退、旧 token 一律不满足，未授权访问返回 403。
- 读接口按允许工位集合过滤；不可见单位返回 404（不泄露存在性）。
- 待办领取入口（``/units/{id}/claim``）在 T3 接线，本模块不实现。
"""
from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app import permissions
from app.auth import current_identity
from app.db import get_db
from app.models.system import SysUser
from app.security import UserContext, record_access_log
from app.services import maintenance_cycle as cycle

router = APIRouter(tags=["cycle"])
UnitId = Annotated[int, Path(gt=0, le=2**63 - 1)]


def cycle_actor(
    ident: dict = Depends(current_identity),
    db: Session = Depends(get_db),
) -> UserContext:
    """实名系统账号身份：坏件循环不接受共享口令回退或旧 token。"""
    if ident.get("authn") != "sys_user" or ident.get("fb") or not ident.get("sub"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "坏件循环操作需要实名系统账号")
    username = str(ident.get("sub")).strip()
    user = db.scalar(
        select(SysUser).where(
            SysUser.username == username,
            SysUser.is_active.is_(True),
            SysUser.deleted_at.is_(None),
        )
    )
    if not username or user is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "坏件循环操作需要实名系统账号")
    return UserContext(
        user_id=username,
        role=str(ident.get("role") or "guest"),
        permissions=ident.get("perms") if isinstance(ident.get("perms"), dict) else None,
        is_authenticated=True,
    )


def require_cycle_pages(*page_keys: str):
    """任一页键即放行（A-01 的 `page_cycle_recycle` 或 `page_cycle_manage` 口径）。"""

    def _dep(ctx: UserContext = Depends(cycle_actor)) -> None:
        graph = (
            ctx.permissions
            if isinstance(ctx.permissions, dict)
            else permissions.effective(ctx.role, None)
        )
        if any(
            permissions.page_permission_allowed(
                role=ctx.role, permission_map=graph, page_key=key
            )
            for key in page_keys
        ):
            return
        raise HTTPException(status.HTTP_403_FORBIDDEN, "无权访问坏件循环页面")

    return _dep


Ctx = Annotated[UserContext, Depends(cycle_actor)]
Db = Annotated[Session, Depends(get_db)]


def _raise_cycle_error(exc: cycle.CycleError) -> None:
    if isinstance(exc, cycle.CycleNotFound):
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    if isinstance(exc, cycle.CycleConflict):
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    if isinstance(exc, cycle.CycleForbidden):
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


def _raise_dbapi(exc: DBAPIError) -> None:
    if getattr(exc.orig, "sqlstate", None) in {"55P03", "40001", "40P01"}:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "该循环单位正在被其他操作处理，请稍后重试"
        ) from exc
    raise exc


def _write(db: Session, fn, /, **kwargs) -> dict:
    try:
        payload = fn(db, **kwargs)
        db.commit()
    except cycle.CycleError as exc:
        db.rollback()
        _raise_cycle_error(exc)
        raise
    except DBAPIError as exc:
        db.rollback()
        _raise_dbapi(exc)
        raise
    return payload


def _read(fn, /, **kwargs) -> dict:
    try:
        return fn(**kwargs)
    except cycle.CycleError as exc:
        _raise_cycle_error(exc)
        raise


# ---------------------------------------------------------------------------
# 请求模型（extra=forbid）
# ---------------------------------------------------------------------------


class HandoffInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    receipt_id: str = Field(min_length=1, max_length=36)
    # 1–1000 的整数在 service 层校验，越界按任务书 §7.3 返回 400（非 422）。
    qty: int
    expected_receipt_version: int = Field(ge=1, le=2**31 - 1)
    serial_numbers: list[str] | None = None


class ExternalIntakeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    part_id: int | None = Field(default=None, gt=0, le=2**31 - 1)
    pn: str | None = Field(default=None, max_length=128)
    qty: int
    intake_condition: str | None = Field(default=None, max_length=64)
    note: str | None = Field(default=None, max_length=cycle.MAX_NOTE_LENGTH)
    serial_numbers: list[str] | None = None


class VersionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1, le=2**31 - 1)


class DispatchInput(VersionInput):
    to: Literal["testing", "repairing", "outsource"]
    assignee_username: str | None = Field(default=None, max_length=64)
    vendor_name: str | None = Field(default=None, max_length=cycle.MAX_VENDOR_LENGTH)


class CompleteTestInput(VersionInput):
    result: Literal["good", "bad"]
    next: Literal["ready", "repairing", "scrap"] | None = None


class CompleteRepairInput(VersionInput):
    result: Literal["fixed", "unfixed"]
    next: Literal["testing", "ready", "scrap"] | None = None


class OutsourceReturnInput(VersionInput):
    next: Literal["testing", "repairing", "to_shop", "to_company", "scrap"]
    condition_bucket: Literal["good", "bad"] | None = None


class CloseInput(VersionInput):
    outcome: Literal["to_shop", "to_company", "scrap"]
    condition_bucket: Literal["good", "bad"] | None = None
    note: str | None = Field(default=None, max_length=cycle.MAX_NOTE_LENGTH)


# ---------------------------------------------------------------------------
# 入池
# ---------------------------------------------------------------------------


@router.post(
    "/cycle/units/handoff-from-receipt",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_cycle_pages(*cycle.WRITE_PAGE_KEYS["handoff_from_receipt"]))],
)
def handoff_from_receipt(body: HandoffInput, db: Db, ctx: Ctx) -> dict:
    try:
        units = cycle.handoff_from_receipt(
            db,
            receipt_id=body.receipt_id,
            qty=body.qty,
            expected_receipt_version=body.expected_receipt_version,
            serial_numbers=body.serial_numbers,
            actor=str(ctx.user_id),
        )
        db.commit()
    except cycle.CycleError as exc:
        db.rollback()
        _raise_cycle_error(exc)
        raise
    except DBAPIError as exc:
        db.rollback()
        _raise_dbapi(exc)
        raise
    record_access_log(
        ctx, "cycle_handoff_from_receipt", "maintenance_cycle_unit",
        {"receipt_id": body.receipt_id, "qty": body.qty},
    )
    return {"items": cycle.serialize_units(units, ctx)}


@router.post(
    "/cycle/units/intake-external",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_cycle_pages(*cycle.WRITE_PAGE_KEYS["intake_external"]))],
)
def intake_external(body: ExternalIntakeInput, db: Db, ctx: Ctx) -> dict:
    try:
        units = cycle.intake_external(
            db,
            part_id=body.part_id,
            pn=body.pn,
            qty=body.qty,
            intake_condition=body.intake_condition,
            note=body.note,
            serial_numbers=body.serial_numbers,
            actor=str(ctx.user_id),
        )
        db.commit()
    except cycle.CycleError as exc:
        db.rollback()
        _raise_cycle_error(exc)
        raise
    except DBAPIError as exc:
        db.rollback()
        _raise_dbapi(exc)
        raise
    record_access_log(
        ctx, "cycle_intake_external", "maintenance_cycle_unit",
        {"part_id": body.part_id, "qty": body.qty},
    )
    return {"items": cycle.serialize_units(units, ctx)}


# ---------------------------------------------------------------------------
# 工位转移（每步 expected_version；409 零写入）
# ---------------------------------------------------------------------------


@router.post(
    "/cycle/units/{unit_id}/dispatch",
    dependencies=[Depends(require_cycle_pages(*cycle.WRITE_PAGE_KEYS["dispatch"]))],
)
def dispatch(unit_id: UnitId, body: DispatchInput, db: Db, ctx: Ctx) -> dict:
    payload = _write(
        db,
        cycle.dispatch,
        unit_id=unit_id,
        expected_version=body.expected_version,
        to=body.to,
        assignee_username=body.assignee_username,
        vendor_name=body.vendor_name,
        actor=str(ctx.user_id),
        ctx=ctx,
    )
    record_access_log(ctx, "cycle_dispatch", "maintenance_cycle_unit",
                      {"unit_id": unit_id, "to": body.to})
    return payload


@router.post(
    "/cycle/units/{unit_id}/self-pass",
    dependencies=[Depends(require_cycle_pages(*cycle.WRITE_PAGE_KEYS["self_pass"]))],
)
def self_pass(unit_id: UnitId, body: VersionInput, db: Db, ctx: Ctx) -> dict:
    payload = _write(
        db,
        cycle.self_pass,
        unit_id=unit_id,
        expected_version=body.expected_version,
        actor=str(ctx.user_id),
        ctx=ctx,
    )
    record_access_log(ctx, "cycle_self_pass", "maintenance_cycle_unit", {"unit_id": unit_id})
    return payload


@router.post(
    "/cycle/units/{unit_id}/complete-test",
    dependencies=[Depends(require_cycle_pages(*cycle.WRITE_PAGE_KEYS["complete_test"]))],
)
def complete_test(unit_id: UnitId, body: CompleteTestInput, db: Db, ctx: Ctx) -> dict:
    payload = _write(
        db,
        cycle.complete_test,
        unit_id=unit_id,
        expected_version=body.expected_version,
        result=body.result,
        next_station=body.next,
        actor=str(ctx.user_id),
        ctx=ctx,
    )
    record_access_log(ctx, "cycle_complete_test", "maintenance_cycle_unit",
                      {"unit_id": unit_id, "result": body.result})
    return payload


@router.post(
    "/cycle/units/{unit_id}/complete-repair",
    dependencies=[Depends(require_cycle_pages(*cycle.WRITE_PAGE_KEYS["complete_repair"]))],
)
def complete_repair(unit_id: UnitId, body: CompleteRepairInput, db: Db, ctx: Ctx) -> dict:
    payload = _write(
        db,
        cycle.complete_repair,
        unit_id=unit_id,
        expected_version=body.expected_version,
        result=body.result,
        next_station=body.next,
        actor=str(ctx.user_id),
        ctx=ctx,
    )
    record_access_log(ctx, "cycle_complete_repair", "maintenance_cycle_unit",
                      {"unit_id": unit_id, "result": body.result})
    return payload


@router.post(
    "/cycle/units/{unit_id}/outsource-return",
    dependencies=[Depends(require_cycle_pages(*cycle.WRITE_PAGE_KEYS["outsource_return"]))],
)
def outsource_return(unit_id: UnitId, body: OutsourceReturnInput, db: Db, ctx: Ctx) -> dict:
    payload = _write(
        db,
        cycle.outsource_return,
        unit_id=unit_id,
        expected_version=body.expected_version,
        next_station=body.next,
        condition_bucket=body.condition_bucket,
        actor=str(ctx.user_id),
        ctx=ctx,
    )
    record_access_log(ctx, "cycle_outsource_return", "maintenance_cycle_unit",
                      {"unit_id": unit_id, "next": body.next})
    return payload


@router.post(
    "/cycle/units/{unit_id}/close",
    dependencies=[Depends(require_cycle_pages(*cycle.WRITE_PAGE_KEYS["close"]))],
)
def close(unit_id: UnitId, body: CloseInput, db: Db, ctx: Ctx) -> dict:
    payload = _write(
        db,
        cycle.close,
        unit_id=unit_id,
        expected_version=body.expected_version,
        outcome=body.outcome,
        condition_bucket=body.condition_bucket,
        note=body.note,
        actor=str(ctx.user_id),
        ctx=ctx,
    )
    record_access_log(ctx, "cycle_close", "maintenance_cycle_unit",
                      {"unit_id": unit_id, "outcome": body.outcome})
    return payload


@router.post(
    "/cycle/units/{unit_id}/withdraw",
    dependencies=[Depends(require_cycle_pages(*cycle.WRITE_PAGE_KEYS["withdraw"]))],
)
def withdraw(unit_id: UnitId, body: VersionInput, db: Db, ctx: Ctx) -> dict:
    payload = _write(
        db,
        cycle.withdraw,
        unit_id=unit_id,
        expected_version=body.expected_version,
        actor=str(ctx.user_id),
        ctx=ctx,
    )
    record_access_log(ctx, "cycle_withdraw", "maintenance_cycle_unit", {"unit_id": unit_id})
    return payload


@router.post(
    "/cycle/units/{unit_id}/mark-sold",
    dependencies=[Depends(require_cycle_pages(*cycle.WRITE_PAGE_KEYS["mark_sold"]))],
)
def mark_sold(unit_id: UnitId, body: VersionInput, db: Db, ctx: Ctx) -> dict:
    payload = _write(
        db,
        cycle.mark_sold,
        unit_id=unit_id,
        expected_version=body.expected_version,
        actor=str(ctx.user_id),
        ctx=ctx,
    )
    record_access_log(ctx, "cycle_mark_sold", "maintenance_cycle_unit", {"unit_id": unit_id})
    return payload


# ---------------------------------------------------------------------------
# 读接口
# ---------------------------------------------------------------------------


@router.get(
    "/cycle/units",
    dependencies=[Depends(require_cycle_pages(*cycle.READ_PAGE_KEYS))],
)
def list_units(
    db: Db,
    ctx: Ctx,
    station: str | None = Query(default=None, max_length=128),
    assignee: str | None = Query(default=None, max_length=64),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict:
    return _read(
        cycle.list_units,
        db=db,
        ctx=ctx,
        station=station,
        assignee=assignee,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/cycle/units/{unit_id}",
    dependencies=[Depends(require_cycle_pages(*cycle.READ_PAGE_KEYS))],
)
def get_unit(unit_id: UnitId, db: Db, ctx: Ctx) -> dict:
    return _read(cycle.get_unit, db=db, unit_id=unit_id, ctx=ctx)


@router.get(
    "/cycle/units/{unit_id}/events",
    dependencies=[Depends(require_cycle_pages(*cycle.READ_PAGE_KEYS))],
)
def list_events(unit_id: UnitId, db: Db, ctx: Ctx) -> dict:
    return _read(cycle.list_events, db=db, unit_id=unit_id, ctx=ctx)


@router.get(
    "/cycle/wip",
    dependencies=[Depends(require_cycle_pages(*cycle.WRITE_PAGE_KEYS["wip"]))],
)
def wip(
    part_ids: str = Query(min_length=1, max_length=1200),
    db: Session = Depends(get_db),
    _ctx: UserContext = Depends(cycle_actor),
) -> dict:
    raw_ids = [token.strip() for token in part_ids.split(",")]
    tokens = [token for token in raw_ids if token]
    parsed: list[int] = []
    for token in tokens:
        if not token.isdigit():
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "part_ids 必须为 1–100 个正整数"
            )
        value = int(token)
        if value <= 0:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "part_ids 必须为 1–100 个正整数"
            )
        if value not in parsed:
            parsed.append(value)
    if not 1 <= len(parsed) <= 100:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "part_ids 必须为 1–100 个正整数"
        )
    return cycle.wip_counts(db, part_ids=parsed)

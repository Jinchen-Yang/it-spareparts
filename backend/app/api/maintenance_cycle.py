"""坏件循环待修池 API（D-0058）。

内部 JSON 用 snake_case。错误体 ``detail`` 是字符串。
页键默认全关。共享口令、旧 token、未登录不能进入。
"""
from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app import permissions
from app.auth import current_identity
from app.db import get_db
from app.models.system import SysUser
from app.security import UserContext, record_access_log
from app.services import cycle_sell_delivery, maintenance_repair as repair

router = APIRouter(tags=["cycle"])

REPAIR_PAGE = "page_cycle_repair"
MANAGE_PAGE = "page_cycle_manage"
WIP_PAGE = "page_cycle_wip"
IDEMPOTENCY_DETAIL = "Idempotency-Key 须为 1–128 位无空白 ASCII"
PART_IDS_DETAIL = "part_ids 必须为 1–100 个正整数"


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


def _allowed(ctx: UserContext, page_key: str) -> bool:
    graph = (
        ctx.permissions
        if isinstance(ctx.permissions, dict)
        else permissions.effective(ctx.role, None)
    )
    return permissions.page_permission_allowed(
        role=ctx.role, permission_map=graph, page_key=page_key
    )


Ctx = Annotated[UserContext, Depends(cycle_actor)]
Db = Annotated[Session, Depends(get_db)]


def _valid_idempotency_key(value: str | None) -> str:
    if (
        value is None
        or not 1 <= len(value) <= 128
        or any(not 33 <= ord(char) <= 126 for char in value)
    ):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, IDEMPOTENCY_DETAIL)
    return value


def _raise_repair(exc: repair.RepairError) -> None:
    raise HTTPException(exc.status_code, exc.detail) from exc


def _raise_dbapi(exc: DBAPIError) -> None:
    sqlstate = getattr(exc.orig, "sqlstate", None)
    if sqlstate == "23514":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "修好数与废品数之和超过当前待修件数"
        ) from exc
    if sqlstate in {"55P03", "40001", "40P01"}:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "该型号正在被其他操作处理，请稍后重试"
        ) from exc
    raise exc


def _finish(db: Session, fn) -> repair.RepairResult:
    try:
        result = fn()
        db.commit()
        return result
    except repair.RepairError as exc:
        db.rollback()
        _raise_repair(exc)
        raise
    except DBAPIError as exc:
        db.rollback()
        _raise_dbapi(exc)
        raise


def _replay(response: Response, result: repair.RepairResult) -> dict:
    if result.replayed:
        response.headers["Idempotent-Replayed"] = "true"
    response.status_code = result.status_code
    return result.body


def _parse_part_ids(raw: str) -> list[int]:
    tokens = [token.strip() for token in raw.split(",") if token.strip()]
    parsed: list[int] = []
    for token in tokens:
        if not token.isdigit():
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, PART_IDS_DETAIL)
        value = int(token)
        if value <= 0:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, PART_IDS_DETAIL)
        if value not in parsed:
            parsed.append(value)
    if not 1 <= len(parsed) <= 100:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, PART_IDS_DETAIL)
    return parsed


class SubmitInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    part_id: int = Field(gt=0, le=2**31 - 1, strict=True)
    fixed_qty: int = Field(ge=0, le=1000, strict=True)
    scrap_qty: int = Field(ge=0, le=1000, strict=True)

    @model_validator(mode="after")
    def at_least_one(self):
        if self.fixed_qty + self.scrap_qty < 1:
            raise ValueError("修好数与废品数至少填写一件")
        return self


class VoidInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1, le=2**31 - 1, strict=True)
    reason: str = Field(min_length=1, max_length=256)
    shop_stock_manually_reduced: bool = False

    @field_validator("reason")
    @classmethod
    def strip_reason(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned or len(cleaned) > 256:
            raise ValueError("必须填写作废原因")
        return cleaned


class AttestInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1, le=2**31 - 1, strict=True)


@router.get(
    "/cycle/pool",
    dependencies=[Depends(require_cycle_pages(REPAIR_PAGE, MANAGE_PAGE))],
)
def pool(
    db: Db,
    q: str | None = Query(default=None, max_length=128),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict:
    return repair.list_pool(db, q=q, page=page, page_size=page_size)


@router.post(
    "/cycle/repair-records",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_cycle_pages(REPAIR_PAGE, MANAGE_PAGE))],
)
def submit_record(
    body: SubmitInput,
    response: Response,
    db: Db,
    ctx: Ctx,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict:
    key = _valid_idempotency_key(idempotency_key)
    result = _finish(
        db,
        lambda: repair.submit_result(
            db,
            actor=ctx.user_id,
            part_id=body.part_id,
            fixed_qty=body.fixed_qty,
            scrap_qty=body.scrap_qty,
            idempotency_key=key,
        ),
    )
    if not result.replayed:
        record_access_log(
            ctx,
            "cycle_repair_submit",
            "maintenance_repair_record",
            {
                "part_id": body.part_id,
                "fixed_qty": body.fixed_qty,
                "scrap_qty": body.scrap_qty,
            },
        )
    return _replay(response, result)


@router.get(
    "/cycle/repair-records",
    dependencies=[Depends(require_cycle_pages(REPAIR_PAGE, MANAGE_PAGE))],
)
def list_records(
    db: Db,
    ctx: Ctx,
    q: str | None = Query(default=None, max_length=128),
    status_filter: Literal["active", "voided"] | None = Query(default=None, alias="status"),
    part_id: int | None = Query(default=None, gt=0, le=2**31 - 1),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict:
    return repair.list_records(
        db,
        actor=ctx.user_id,
        as_supervisor=_allowed(ctx, MANAGE_PAGE),
        q=q,
        status=status_filter,
        part_id=part_id,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/cycle/repair-records/{record_id}",
    dependencies=[Depends(require_cycle_pages(REPAIR_PAGE, MANAGE_PAGE))],
)
def get_record(record_id: UUID, db: Db, ctx: Ctx) -> dict:
    try:
        return repair.get_record(
            db,
            actor=ctx.user_id,
            as_supervisor=_allowed(ctx, MANAGE_PAGE),
            record_id=record_id,
        )
    except repair.RepairError as exc:
        _raise_repair(exc)
        raise


@router.post(
    "/cycle/repair-records/{record_id}/void",
    dependencies=[Depends(require_cycle_pages(REPAIR_PAGE, MANAGE_PAGE))],
)
def void_record(
    record_id: UUID,
    body: VoidInput,
    response: Response,
    db: Db,
    ctx: Ctx,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict:
    key = _valid_idempotency_key(idempotency_key)
    supervisor = _allowed(ctx, MANAGE_PAGE)
    result = _finish(
        db,
        lambda: repair.void_result(
            db,
            actor=ctx.user_id,
            record_id=record_id,
            expected_version=body.expected_version,
            reason=body.reason,
            shop_stock_manually_reduced=body.shop_stock_manually_reduced,
            idempotency_key=key,
            as_supervisor=supervisor,
        ),
    )
    if not result.replayed:
        record_access_log(
            ctx,
            "cycle_repair_void",
            "maintenance_repair_record",
            {"record_id": str(record_id)},
        )
    return _replay(response, result)


@router.get(
    "/cycle/repair-records/{record_id}/sell-intent",
    dependencies=[Depends(require_cycle_pages(MANAGE_PAGE))],
)
def sell_intent(record_id: UUID, db: Db, ctx: Ctx) -> dict:
    try:
        repair.get_record(
            db, actor=ctx.user_id, as_supervisor=True, record_id=record_id
        )
    except repair.RepairError as exc:
        _raise_repair(exc)
        raise
    return cycle_sell_delivery.read_status(db, record_id)


@router.post(
    "/cycle/repair-records/{record_id}/attest-shop-adjustment",
    dependencies=[Depends(require_cycle_pages(MANAGE_PAGE))],
)
def attest_shop_adjustment(
    record_id: UUID,
    body: AttestInput,
    response: Response,
    db: Db,
    ctx: Ctx,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict:
    key = _valid_idempotency_key(idempotency_key)
    result = _finish(
        db,
        lambda: repair.attest_manual_shop_reduction(
            db,
            actor=ctx.user_id,
            record_id=record_id,
            expected_version=body.expected_version,
            idempotency_key=key,
        ),
    )
    if not result.replayed:
        record_access_log(
            ctx,
            "cycle_repair_attest_shop_adjustment",
            "maintenance_repair_record",
            {"record_id": str(record_id)},
        )
    return _replay(response, result)


@router.get(
    "/cycle/summary",
    dependencies=[Depends(require_cycle_pages(WIP_PAGE, MANAGE_PAGE))],
)
def summary(
    db: Db,
    q: str | None = Query(default=None, max_length=128),
    part_ids: str | None = Query(default=None, max_length=1200),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict:
    parsed = _parse_part_ids(part_ids) if part_ids is not None else None
    return repair.list_summary(
        db, q=q, part_ids=parsed, page=page, page_size=page_size
    )

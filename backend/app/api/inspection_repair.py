"""检测与维修正式入口。账号角色与操作请求分别校验。"""
from typing import Annotated, Callable
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Response
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.auth import current_identity
from app.db import get_db
from app.models.maintenance_repair_orders import MaintenanceRepairOrder, MaintenanceRepairOrderCommand
from app.models.system import SysUser
from app.schemas.inspection_repair import CreateRepairOrder, RepairOrderAction, RepairRole
from app.services import maintenance_repair_orders as orders
from app.services.maintenance_repair import RepairError, request_fingerprint
from app.services.repair_order_export import export_fixed_dispositions

router = APIRouter(prefix="/inspection-repair", tags=["inspection-repair"])

Db = Annotated[Session, Depends(get_db)]
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]


def authenticated_account(db: Db, ident: Annotated[dict, Depends(current_identity)]) -> SysUser:
    username = ident.get("sub")
    if ident.get("authn") != "sys_user" or ident.get("fb") or not username:
        raise HTTPException(403, "检测与维修需要实名系统账号")
    account = db.scalar(orders.active_user_query().where(SysUser.username == username))
    if account is None:
        raise HTTPException(403, "检测与维修需要实名系统账号")
    return account


Account = Annotated[SysUser, Depends(authenticated_account)]


def require_role(account: Account, role: Annotated[RepairRole, Query()]) -> RepairRole:
    if role not in orders.roles_for(account):
        raise HTTPException(403, "当前账号没有所选维修岗位权限")
    return role


Role = Annotated[RepairRole, Depends(require_role)]


@router.get("/session")
def role_session(db: Db, account: Account, response: Response) -> dict:
    response.headers["Cache-Control"] = "no-store"
    return orders.session_payload(db, account)


@router.get("/orders")
def list_orders(db: Db, account: Account, role: Role, response: Response) -> list[dict]:
    response.headers["Cache-Control"] = "no-store"
    return orders.list_orders(db, account, role)


@router.get("/orders/export", response_class=Response)
def export_orders(db: Db, role: Role) -> Response:
    if role != "supervisor":
        raise HTTPException(403, "只有维修主管可导出已登记去向的修好件")
    filename = quote("已登记去向的修好件.xlsx")
    return Response(
        content=export_fixed_dispositions(db),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="repair-results.xlsx"; filename*=UTF-8\'\'{filename}',
            "Cache-Control": "no-store",
        },
    )


@router.get("/return-candidates")
def return_candidates(
    db: Db,
    account: Account,
    role: Role,
    response: Response,
    q: Annotated[str, Query(max_length=128)] = "",
    page: Annotated[int, Query(ge=1, le=10000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 30,
) -> dict:
    if role != "supervisor":
        raise HTTPException(403, "只有维修主管可查看维保返还来源")
    from app.services.repair_order_sources import return_candidates

    response.headers["Cache-Control"] = "no-store"
    try:
        return return_candidates(db, orders.account_context(account), q=q, page=page, page_size=page_size)
    except RepairError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


def _write(
    db: Session,
    account: SysUser,
    role: RepairRole,
    response: Response,
    key: str,
    fingerprint: str,
    operation: Callable[[], MaintenanceRepairOrder],
) -> dict:
    if any(not 33 <= ord(char) <= 126 for char in key):
        raise HTTPException(400, "Idempotency-Key 须为无空白 ASCII")
    response.headers["Cache-Control"] = "no-store"
    try:
        db.execute(text("SET LOCAL lock_timeout = '5s'"))
        db.execute(
            select(func.pg_advisory_xact_lock(func.hashtextextended(f"repair-order:{account.id}:{key}", 0)))
        )
        prior = db.get(MaintenanceRepairOrderCommand, (account.id, key))
        if prior is not None:
            if prior.request_fingerprint != fingerprint:
                raise HTTPException(409, "该提交标识已用于不同的维修操作")
            response.headers["Idempotency-Replayed"] = "true"
            return orders.replay_payload(db, prior.response_json, account, role)
        order = operation()
        db.flush()
        result = orders.order_payloads(db, [order], account, role)[0]
        db.add(MaintenanceRepairOrderCommand(
            actor_id=account.id, idempotency_key=key, order_id=order.id,
            request_fingerprint=fingerprint, response_json=result,
        ))
        db.commit()
        return result
    except HTTPException:
        db.rollback()
        raise
    except RepairError as exc:
        db.rollback()
        raise HTTPException(exc.status_code, exc.detail) from exc
    except DBAPIError as exc:
        db.rollback()
        sqlstate = getattr(exc.orig, "sqlstate", None)
        if sqlstate in {"55P03", "40001", "40P01"}:
            raise HTTPException(409, "相关维修任务或返还正在处理中，请刷新后重试") from exc
        if sqlstate == "23514":
            raise HTTPException(409, "维修任务或返还数量已变化，请刷新后重试") from exc
        raise


@router.post("/orders", status_code=201)
def create_order(
    body: CreateRepairOrder, db: Db, account: Account, role: Role, response: Response, key: IdempotencyKey,
) -> dict:
    fingerprint = request_fingerprint(f"{role}:create", None, body.model_dump(mode="json", exclude_unset=True))
    return _write(db, account, role, response, key, fingerprint, lambda: orders.create_order(db, account, role, body))


@router.post("/orders/{order_id}/actions")
def apply_action(
    order_id: Annotated[str, Path(min_length=1, max_length=36)],
    body: RepairOrderAction,
    db: Db,
    account: Account,
    role: Role,
    response: Response,
    key: IdempotencyKey,
) -> dict:
    fingerprint = request_fingerprint(f"{role}:{body.type}", order_id, body.model_dump(mode="json", exclude_unset=True))
    return _write(db, account, role, response, key, fingerprint, lambda: orders.apply_action(db, account, role, order_id, body))

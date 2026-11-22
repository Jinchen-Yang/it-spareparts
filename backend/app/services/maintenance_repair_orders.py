"""维修任务单：责任流转、送修来源与结果数量。"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, joinedload, selectinload

from app import permissions
from app.api.maintenance_project_scope import resolve_visible_project_ids
from app.business_time import BUSINESS_TZ
from app.models.maintenance_doc_import import MaintenanceRkdReturnLine
from app.models.maintenance_repair_orders import (
    MaintenanceRepairOrder as Order,
    MaintenanceRepairOrderLine as Line,
    repair_order_no_sequence,
)
from app.models.system import SysUser
from app.schemas.inspection_repair import (
    AssignAction,
    ClaimAction,
    CreateLine,
    CreateRepairOrder,
    DispositionCorrection,
    DisposeAction,
    EditAction,
    RepairOrderAction,
    RepairRole,
    ResultAction,
    ReturnAction,
)
from app.security import UserContext

ROLE_PAGES: dict[RepairRole, str] = {
    "supervisor": "page_repair_order_supervisor",
    "sales": "page_repair_order_sales",
    "technician": "page_repair_order_technician",
    "admin": "page_repair_order_disposition",
}
_RETRY_NOTE_PREFIX = "二次维修 · 来源维修单 "
_RETRY_EVENT_PREFIX = "登记二次维修，来源单 "
_ORDER_NUMBER = re.compile(r"WX-[0-9]{8}-[0-9]{3,}")


def roles_for(account: SysUser) -> list[RepairRole]:
    graph = permissions.effective_for_user(account)
    return [role for role, key in ROLE_PAGES.items() if graph.get(key, False)]


def account_context(account: SysUser) -> UserContext:
    return UserContext(
        user_id=account.username,
        role=account.role,
        salesperson_name=account.salesperson_name,
        permissions=permissions.effective_for_user(account),
        is_authenticated=True,
    )


def active_user_query():
    return select(SysUser).where(SysUser.is_active.is_(True), SysUser.deleted_at.is_(None))


def display_name(account: SysUser) -> str:
    return account.display_name or account.username


def session_payload(db: Session, account: SysUser) -> dict:
    roles = roles_for(account)
    if not roles:
        raise HTTPException(403, "无权访问检测与维修")
    people = []
    if "supervisor" in roles or "sales" in roles:
        users = db.scalars(active_user_query().order_by(SysUser.display_name, SysUser.username))
        people = [
            {"username": user.username, "displayName": display_name(user)}
            for user in users if "technician" in roles_for(user)
        ]
    return {"username": account.username, "displayName": display_name(account), "roles": roles, "technicians": people}


def _authorized_person(db: Session, username: str, needed: set[RepairRole]) -> SysUser:
    user = db.scalar(active_user_query().where(SysUser.username == username).with_for_update(read=True))
    if user is None or needed.isdisjoint(roles_for(user)):
        raise HTTPException(422, "请选择具备相应维修权限的实名账号")
    return user


def _pending(line: Line, result: str) -> int:
    count = line.fixed if result == "fixed" else line.unfixed
    return count - sum(entry["qty"] for entry in line.dispositions if entry["result"] == result)


def _has_pending(line: Line) -> bool:
    return _pending(line, "fixed") > 0 or _pending(line, "unfixed") > 0


def _unfinished(order: Order) -> bool:
    return any(line.fixed + line.unfixed < line.qty for line in order.lines)


def _in_pool(order: Order) -> bool:
    return (
        order.route == "pool" and not order.returned and order.assignee_id is None
        and all(not (line.fixed or line.unfixed) for line in order.lines)
    )


def _can_see(order: Order, account: SysUser, role: RepairRole) -> bool:
    if role == "supervisor":
        return True
    if role == "sales":
        return order.creator_id == account.id
    if role == "technician":
        return _in_pool(order) or order.assignee_id == account.id
    return any(_has_pending(line) for line in order.lines)


def _event(account: SysUser, event: str, at: datetime, *, kind: str = "handoff") -> dict:
    return {
        "username": account.username, "name": display_name(account),
        "event": event, "at": at.isoformat(), "kind": kind,
    }


def _append_event(order: Order, account: SysUser, event: str, at: datetime, *, kind: str = "handoff") -> None:
    order.history = [*order.history, _event(account, event, at, kind=kind)]


def _new_order(
    db: Session,
    account: SysUser,
    *,
    creator_id: int,
    mode: str,
    note: str,
    lines: list[Line],
    at: datetime,
    route: str = "pool",
    assignee: SysUser | None = None,
    parent: Line | None = None,
) -> Order:
    serial = db.scalar(select(repair_order_no_sequence.next_value()))
    order = Order(
        id=f"WX-{at:%Y%m%d}-{serial:03d}", creator_id=creator_id,
        assignee_id=assignee.id if assignee else None, mode=mode, route=route,
        returned=False, note=note, version=1, created_at=at,
        history=[_event(account, f"{_RETRY_EVENT_PREFIX}{parent.order_id}" if parent else "创建维修单", at)],
        repair_source_line_id=parent.id if parent else None, lines=lines,
    )
    if assignee:
        _append_event(order, account, f"派给{display_name(assignee)}", at)
        _append_event(order, assignee, "接办派发任务", at)
    db.add(order)
    return order


def _new_line(position: int, *, pn: str, description: str, qty: int) -> Line:
    return Line(
        position=position, pn=pn, description=description, qty=qty,
        fixed=0, unfixed=0, previously_handled=False, dispositions=[],
    )


def _sources_for_write(
    db: Session, account: SysUser, role: RepairRole, expected: dict[str, int | None],
    quantities: dict[str, int], existing: dict[str, int],
) -> dict[str, dict]:
    if not (quantities or existing):
        return {}
    if role != "supervisor":
        raise HTTPException(403, "只有维修主管可选择维保返还来源")
    from app.services.repair_order_sources import reserve_return_sources

    return reserve_return_sources(db, account_context(account), expected, quantities, existing)


def _creation_sources(specs: list[CreateLine]) -> tuple[dict[str, int], dict[str, int]]:
    expected: dict[str, int] = {}
    quantities: Counter[str] = Counter()
    for spec in specs:
        receipt_id, version = spec.sourceReceiptId, spec.sourceVersion
        if receipt_id is None:
            if version is not None:
                raise HTTPException(422, "返还来源与版本必须同时提交")
            continue
        if version is None:
            raise HTTPException(422, "请刷新返还来源后重试")
        if expected.setdefault(receipt_id, version) != version:
            raise HTTPException(422, "同一返还来源不能使用不同版本")
        quantities[receipt_id] += spec.qty
    return expected, dict(quantities)


def create_order(db: Session, account: SysUser, role: RepairRole, body: CreateRepairOrder) -> Order:
    if role not in {"sales", "supervisor"}:
        raise HTTPException(403, "只有销售与维修主管可新建维修单")
    if role == "supervisor" and not body.assignee:
        raise HTTPException(422, "请选择承接的维修人员")
    assignee = _authorized_person(db, body.assignee, {"technician"}) if body.assignee else None
    versions, quantities = _creation_sources(body.lines)
    sources = _sources_for_write(db, account, role, versions, quantities, {})
    lines = []
    for position, spec in enumerate(body.lines):
        source = sources.get(spec.sourceReceiptId)
        line = _new_line(
            position, pn=source["pn"] if source else spec.pn,
            description=(source["description"] or "") if source else spec.description, qty=spec.qty,
        )
        if source:
            line.source_receipt_id = spec.sourceReceiptId
            line.source_version = source["version"]
            line.source_head_no = source["headNo"]
        lines.append(line)
    route = ("direct" if role == "sales" else "assigned") if assignee else "pool"
    return _new_order(
        db, account, creator_id=account.id, mode=body.mode, note=body.note, lines=lines,
        route=route, assignee=assignee, at=datetime.now(BUSINESS_TZ),
    )


def _check_quantities(qty: int, fixed: int, unfixed: int, dispositions: list[dict]) -> None:
    if fixed + unfixed > qty:
        raise HTTPException(409, "修好与没修好数量合计不能超过该条维修数量")
    totals = Counter()
    for entry in dispositions:
        totals[entry["result"]] += entry["qty"]
    if totals["fixed"] > fixed or totals["unfixed"] > unfixed:
        raise HTTPException(409, "已登记去向超出对应结果，请同时更正去向数量")


def _correct_dispositions(prior: list[dict], corrections: list[DispositionCorrection] | None) -> list[dict]:
    if corrections is None:
        return prior
    if len(corrections) != len(prior):
        raise HTTPException(422, "请保留原去向记录，可将主公司或电商数量更正为零")
    result = []
    for before, after in zip(prior, corrections, strict=True):
        if before["kind"] != after.kind or before["result"] != after.result:
            raise HTTPException(422, "请保留原去向与结果类别")
        if before["kind"] == "retry" and (
            before["qty"] != after.qty
            or ("retryOrderId" in after.model_fields_set and before.get("retryOrderId") != after.retryOrderId)
        ):
            raise HTTPException(422, "请保留原二次维修单的关联和数量")
        result.append({**before, "qty": after.qty})
    return result


def _edit_order(db: Session, order: Order, account: SysUser, body: EditAction, at: datetime) -> None:
    old_lines = list(order.lines)
    indices = body.sourceIndices
    if indices is None:
        indices = [i if i < len(old_lines) else None for i in range(len(body.lines))]
    original_indices = [i for i in indices if i is not None]
    if (
        len(indices) != len(body.lines) or len(set(original_indices)) != len(original_indices)
        or any(i >= len(old_lines) for i in original_indices)
    ):
        raise HTTPException(422, "任务条来源无效或重复")
    removed = set(range(len(old_lines))) - set(original_indices)
    if any(
        old_lines[i].previously_handled or old_lines[i].fixed or old_lines[i].unfixed or old_lines[i].dispositions
        for i in removed
    ):
        raise HTTPException(422, "已办理的任务条须保留，可更正数量")

    # 二次维修量已经从上一轮对应结果中扣除，保持父条与子单守恒。
    if order.repair_source_line_id and (
        indices != [0] or len(old_lines) != 1 or body.lines[0].qty != old_lines[0].qty
        or body.lines[0].pn != old_lines[0].pn
    ):
        raise HTTPException(422, "二次维修须保留原单关联的 PN 与件数")

    existing: Counter[str] = Counter()
    for line in old_lines:
        if line.source_receipt_id:
            existing[line.source_receipt_id] += line.qty
    quantities: Counter[str] = Counter()
    expected: dict[str, int | None] = {}
    lines = []
    for position, (spec, old_index) in enumerate(zip(body.lines, indices, strict=True)):
        old = old_lines[old_index] if old_index is not None else None
        receipt_id = old.source_receipt_id if old is not None else None
        if "sourceReceiptId" in spec.model_fields_set and spec.sourceReceiptId != receipt_id:
            raise HTTPException(422, "更正任务条时须保留原返还来源")
        if not receipt_id and spec.sourceVersion is not None:
            raise HTTPException(422, "缺少对应的返还来源")
        if receipt_id:
            if spec.pn != old.pn:
                raise HTTPException(422, "来源任务条的 PN 须与原返还一致")
            quantities[receipt_id] += spec.qty
            if spec.sourceVersion is not None:
                if expected.setdefault(receipt_id, spec.sourceVersion) != spec.sourceVersion:
                    raise HTTPException(422, "同一返还来源不能使用不同版本")
        if old and spec.pn != old.pn and any(entry["kind"] == "retry" for entry in old.dispositions):
            raise HTTPException(422, "已送二次维修的任务条须保留关联 PN")
        dispositions = _correct_dispositions(old.dispositions if old else [], spec.dispositions)
        _check_quantities(spec.qty, spec.fixed, spec.unfixed, dispositions)
        lines.append((position, spec, old, dispositions))

    changed = {receipt_id for receipt_id in existing.keys() | quantities.keys() if existing[receipt_id] != quantities[receipt_id]}
    _sources_for_write(
        db, account, "supervisor", {key: expected[key] for key in changed if key in expected},
        {key: quantities[key] for key in changed if quantities[key]},
        {key: existing[key] for key in changed if existing[key]},
    )
    was_pool = _in_pool(order)
    former_assignee = db.get(SysUser, order.assignee_id) if order.assignee_id else None
    assignee = None
    if body.assignee:
        assignee = (
            former_assignee if former_assignee and former_assignee.username == body.assignee
            else _authorized_person(db, body.assignee, {"technician"})
        )
    old_assignee_id = order.assignee_id
    if body.creator is not None:
        former_creator = db.get(SysUser, order.creator_id)
        if former_creator.username != body.creator:
            order.creator_id = _authorized_person(db, body.creator, {"sales", "supervisor"}).id
    updated = []
    for position, spec, old, dispositions in lines:
        line = old if old else _new_line(position, pn=spec.pn, description=spec.description, qty=spec.qty)
        line.previously_handled = bool(line.previously_handled or line.fixed or line.unfixed or spec.fixed or spec.unfixed)
        line.position, line.pn, line.description = position, spec.pn, spec.description
        line.qty, line.fixed, line.unfixed = spec.qty, spec.fixed, spec.unfixed
        line.dispositions = dispositions
        updated.append(line)
    order.lines = updated
    order.note, order.mode = body.note, body.mode
    order.assignee_id = assignee.id if assignee else None
    order.returned = not assignee and (not was_pool or any(line.fixed or line.unfixed for line in updated))
    _append_event(order, account, "更正维修单", at)
    if old_assignee_id != order.assignee_id:
        if assignee:
            order.route = "assigned" if order.route == "pool" else order.route
            _append_event(order, account, f"派给{display_name(assignee)}", at)
            _append_event(order, assignee, "接办维修任务", at)
        else:
            _append_event(order, account, "接回维修任务", at)


def apply_action(db: Session, account: SysUser, role: RepairRole, order_id: str, body: RepairOrderAction) -> Order:
    order = db.scalar(
        select(Order).where(Order.id == order_id).options(selectinload(Order.lines)).with_for_update()
    )
    if order is None:
        raise HTTPException(404, "维修单不存在或不在可见范围内")
    if not _can_see(order, account, role):
        # 领取后从对其他维修人员可见的公共池消失。仅已公开过的单可返回领取竞争。
        if isinstance(body, ClaimAction) and role == "technician" and order.route == "claimed":
            raise HTTPException(409, "任务已离开公共池")
        raise HTTPException(404, "维修单不存在或不在可见范围内")
    if order.version != body.version:
        raise HTTPException(409, "维修单已变化，请刷新后重试")
    at = datetime.now(BUSINESS_TZ)
    if isinstance(body, EditAction):
        if role != "supervisor":
            raise HTTPException(403, "只有维修主管可更正维修单")
        _edit_order(db, order, account, body, at)
    elif isinstance(body, ClaimAction):
        if role != "technician":
            raise HTTPException(403, "只有维修人员可以领取任务")
        if not _in_pool(order):
            raise HTTPException(409, "任务已离开公共池")
        order.assignee_id, order.route = account.id, "claimed"
        _append_event(order, account, "从公共池领取", at)
    elif isinstance(body, AssignAction):
        if role != "supervisor":
            raise HTTPException(403, "只有维修主管可以派发任务")
        if order.assignee_id is not None or not _unfinished(order):
            raise HTTPException(409, "主管可派发待领或待接回的任务")
        assignee = _authorized_person(db, body.assignee, {"technician"})
        order.assignee_id, order.returned = assignee.id, False
        order.route = "assigned" if order.route == "pool" else order.route
        _append_event(order, account, f"派给{display_name(assignee)}", at)
        _append_event(order, assignee, "接办维修任务", at)
    elif isinstance(body, ReturnAction):
        _require_assignee(order, account, role)
        if not _unfinished(order):
            raise HTTPException(409, "结果已登记完毕，无法退回")
        order.assignee_id, order.returned = None, True
        _append_event(order, account, "退回维修主管", at)
    elif isinstance(body, ResultAction):
        _require_assignee(order, account, role)
        if not (body.fixed or body.unfixed):
            raise HTTPException(422, "请至少登记一件维修结果")
        line = _get_line(order, body.line)
        new_fixed, new_unfixed = line.fixed + body.fixed, line.unfixed + body.unfixed
        _check_quantities(line.qty, new_fixed, new_unfixed, line.dispositions)
        line.fixed, line.unfixed, line.previously_handled = new_fixed, new_unfixed, True
        _append_event(
            order, account, f"登记第 {body.line + 1} 条结果，修好 {body.fixed} 件，没修好 {body.unfixed} 件",
            at, kind="result",
        )
    elif isinstance(body, DisposeAction):
        if role != "admin":
            raise HTTPException(403, "只有去向管理员可以登记去向")
        _set_disposition(db, order, account, body, at)
    order.version += 1
    return order


def _require_assignee(order: Order, account: SysUser, role: RepairRole) -> None:
    if role != "technician" or order.assignee_id != account.id:
        raise HTTPException(403, "只有当前维修承接人可以办理任务")


def _get_line(order: Order, index: int) -> Line:
    if index >= len(order.lines):
        raise HTTPException(404, "维修任务条不存在")
    return order.lines[index]


def _set_disposition(db: Session, order: Order, account: SysUser, body: DisposeAction, at: datetime) -> None:
    line = _get_line(order, body.line)
    if body.qty > _pending(line, body.result):
        raise HTTPException(409, "去向数量超过该结果剩余待分配的数量")
    entry: dict[str, Any] = {"kind": body.kind, "result": body.result, "qty": body.qty}
    if body.kind == "retry":
        if body.result != "unfixed":
            raise HTTPException(422, "只有没修好的备件可以进入二次维修")
        retry = _new_order(
            db, account, creator_id=order.creator_id, mode=order.mode, at=at,
            note=f"{_RETRY_NOTE_PREFIX}{order.id}",
            lines=[_new_line(0, pn=line.pn, description=line.description, qty=body.qty)],
            parent=line,
        )
        entry["retryOrderId"] = retry.id
    line.dispositions = [*line.dispositions, entry]
    _append_event(order, account, f"第 {body.line + 1} 条登记去向 {body.qty} 件", at, kind="disposition")


def list_orders(db: Session, account: SysUser, role: RepairRole) -> list[dict]:
    # 版本、承接人和任务条在同一个数据库读取快照中取得。
    stmt = select(Order).options(joinedload(Order.lines))
    if role == "sales":
        stmt = stmt.where(Order.creator_id == account.id)
    if role == "technician":
        stmt = stmt.where(or_(
            Order.assignee_id == account.id,
            and_(Order.route == "pool", Order.assignee_id.is_(None), Order.returned.is_(False)),
        ))
    if role == "admin":
        stmt = stmt.where(Order.lines.any(or_(Line.fixed > 0, Line.unfixed > 0)))
    rows = db.scalars(stmt.order_by(Order.created_at.desc(), Order.id.desc())).unique().all()
    return order_payloads(db, [row for row in rows if _can_see(row, account, role)], account, role)


def _source_rows(db: Session, ids: set[str], account: SysUser, role: RepairRole) -> dict:
    if role != "supervisor" or not ids:
        return {}
    ctx = account_context(account)
    if not permissions.page_permission_allowed(role=ctx.role, permission_map=ctx.permissions, page_key="page_maintenance"):
        return {}
    scope = resolve_visible_project_ids(db, ctx)
    stmt = select(MaintenanceRkdReturnLine).where(MaintenanceRkdReturnLine.rkd_line_id.in_(ids))
    if scope is not None:
        stmt = stmt.where(MaintenanceRkdReturnLine.project_id.in_(scope))
    return {row.rkd_line_id: row for row in db.scalars(stmt)}


def replay_payload(db: Session, response: dict, account: SysUser, role: RepairRole) -> dict:
    if role != "supervisor":
        order = db.scalars(
            select(Order).where(Order.id == response["id"]).options(joinedload(Order.lines))
        ).unique().one_or_none()
        if order is None or not _can_see(order, account, role):
            # 成功回执可重试。负责人或可见结果已变化时只报成功的订单号、版本。
            return {"id": response["id"], "version": response["version"], "lines": []}
        if role == "admin":
            return order_payloads(db, [order], account, role)[0]
    ids = {line["sourceReceiptId"] for line in response["lines"] if "sourceReceiptId" in line}
    if not ids:
        return response
    sources = _source_rows(db, ids, account, role)
    source_fields = {"sourceReceiptId", "sourceHeadNo", "sourceVersion", "sourceAvailableQty"}
    return {
        **response,
        "lines": [
            line if line.get("sourceReceiptId") in sources else
            {key: value for key, value in line.items() if key not in source_fields}
            for line in response["lines"]
        ],
    }


def _hide_related_numbers(value: str, hidden: set[str], prefix: str, replacement: str) -> str:
    source = _ORDER_NUMBER.match(value, len(prefix)) if value.startswith(prefix) else None
    if source and source.group() in hidden:
        value = replacement + value[source.end():]
    return _ORDER_NUMBER.sub(lambda match: "已隐藏" if match.group() in hidden else match.group(), value)


def order_payloads(db: Session, orders: list[Order], account: SysUser, role: RepairRole) -> list[dict]:
    if not orders:
        return []
    people_ids = {user_id for order in orders for user_id in (order.creator_id, order.assignee_id) if user_id}
    people = {row.id: row for row in db.scalars(select(SysUser).where(SysUser.id.in_(people_ids)))}
    # 销售列表已用同一条查询选出全部本人单据。关系的归属沿用该快照，
    # 避免主管同时换建单人时混用两个时点的权限。
    visible_order_ids = {order.id for order in orders} if role == "sales" else set()
    parents_ids = {order.repair_source_line_id for order in orders if order.repair_source_line_id}
    parents = {}
    if parents_ids:
        parent_query = select(Line.id, Line.order_id, Line.position).where(Line.id.in_(parents_ids))
        parents = {
            key: {"orderId": order_id, "line": position}
            for key, order_id, position in db.execute(parent_query)
        }
    source_ids = {
        line.source_receipt_id for order in orders for line in order.lines if line.source_receipt_id
    }
    sources = _source_rows(db, source_ids, account, role)
    payload = []
    for order in orders:
        creator, assignee = people[order.creator_id], people.get(order.assignee_id)
        parent = parents.get(order.repair_source_line_id)
        note = order.note if role != "admin" else ""
        history = [
            entry for entry in order.history
            if role != "admin" or entry.get("kind", "handoff") == "handoff"
        ]
        if role == "sales":
            hidden = {
                retry_id
                for line in order.lines
                for entry in line.dispositions
                if isinstance(retry_id := entry.get("retryOrderId"), str) and retry_id not in visible_order_ids
            }
            if parent and parent["orderId"] not in visible_order_ids:
                hidden.add(parent["orderId"])
                parent = None
            if hidden:
                note = _hide_related_numbers(note, hidden, _RETRY_NOTE_PREFIX, "二次维修")
                history = [
                    {
                        **entry,
                        "event": _hide_related_numbers(entry["event"], hidden, _RETRY_EVENT_PREFIX, "登记二次维修"),
                    }
                    if isinstance(entry.get("event"), str) else entry
                    for entry in history
                ]
        item = {
            "id": order.id, "creator": creator.username, "creatorName": display_name(creator),
            "assignee": assignee.username if assignee else None,
            "assigneeName": display_name(assignee) if assignee else None,
            "mode": order.mode, "route": order.route, "returned": order.returned,
            "note": note,
            "version": order.version, "createdAt": order.created_at.isoformat(),
            "history": history,
            "lines": [],
        }
        if parent is not None:
            item["repairSource"] = parent
        for line in order.lines:
            if role == "admin" and not _has_pending(line):
                continue
            fixed, unfixed = line.fixed, line.unfixed
            qty, dispositions = line.qty, line.dispositions
            if role == "admin":
                fixed, unfixed = _pending(line, "fixed"), _pending(line, "unfixed")
                qty, dispositions = fixed + unfixed, []
            elif role == "sales":
                masked_dispositions = []
                for entry in dispositions:
                    retry_id = entry.get("retryOrderId")
                    if "retryOrderId" in entry and (
                        not isinstance(retry_id, str) or retry_id not in visible_order_ids
                    ):
                        entry = {key: value for key, value in entry.items() if key != "retryOrderId"}
                    masked_dispositions.append(entry)
                dispositions = masked_dispositions
            row = {
                "pn": line.pn, "description": line.description, "qty": qty, "fixed": fixed, "unfixed": unfixed,
                "lineIndex": line.position, "dispositions": dispositions,
                "previouslyHandled": line.previously_handled,
            }
            if line.source_receipt_id in sources:
                receipt = sources[line.source_receipt_id]
                row.update(
                    sourceReceiptId=line.source_receipt_id, sourceHeadNo=line.source_head_no,
                    sourceVersion=receipt.version,
                )
            item["lines"].append(row)
        payload.append(item)
    return payload

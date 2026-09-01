"""WBDD demand search and server-enforced safe logical deletion.

The source header and lines remain immutable business evidence.  An active
``MaintenanceDemandTombstone`` removes a demand from effective reads without
physically deleting either source rows or downstream assignment history.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Iterable
from uuid import uuid4

from sqlalchemy import exists, func, or_, select, text, true
from sqlalchemy.orm import Session

from app.config import DATA_CHANGE_ADVISORY_LOCK_KEY, get_settings
from app.models.maintenance import (
    FMaintenanceLine,
    FMaintenanceOrder,
    MaintenanceDemandDeleteEvent,
    MaintenanceDemandDeleteIntent,
    MaintenanceDemandDeleteIntentItem,
    MaintenanceDemandTombstone,
)
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.services import maintenance_cost_invalidation

if TYPE_CHECKING:
    from app.models.maintenance_project_operations import (
        MaintenanceProjectWorkbookState,
    )


MAX_DELETE_HEADERS = 1_000
MAX_DELETE_LINES = 20_000
DELETE_WAIT_SECONDS = 7
INTENT_TTL_MINUTES = 15
MAX_REASON_LENGTH = 1_000
MAX_SOURCE_ORDER_ID_LENGTH = 64


class MaintenanceDemandError(ValueError):
    pass


class MaintenanceDemandNotFound(MaintenanceDemandError):
    pass


class MaintenanceDemandForbidden(MaintenanceDemandError):
    pass


class DeleteIntentConflict(MaintenanceDemandError):
    pass


class DeleteIntentTooEarly(MaintenanceDemandError):
    def __init__(self, not_before: datetime, server_now: datetime):
        super().__init__("服务端七秒安全等待尚未结束")
        self.not_before = not_before
        self.server_now = server_now


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _validate_source_order_id(value: Any) -> str:
    """Validate a private stable ID without ever embedding it in an error."""

    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > MAX_SOURCE_ORDER_ID_LENGTH
    ):
        raise MaintenanceDemandError("维保需求单选择无效")
    return value


def _validate_source_order_ids(values: Any) -> list[str]:
    """Validate a complete selection before hashing, locking, or persistence."""

    if not isinstance(values, list) or not values or len(values) > MAX_DELETE_HEADERS:
        raise MaintenanceDemandError("维保需求单选择无效")
    validated = [_validate_source_order_id(value) for value in values]
    if len(set(validated)) != len(validated):
        raise DeleteIntentConflict("选择列表包含重复 WBDD，整批已拒绝")
    return validated


def _json_value(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    return value


def beta_active_demand_condition(order=FMaintenanceOrder):
    """SQL condition for the Beta shadow-deletion view of WBDD facts."""

    return ~exists(
        select(1).where(
            MaintenanceDemandTombstone.source_order_id == order.raw_order_id,
            MaintenanceDemandTombstone.restored_at.is_(None),
        )
    )


def active_demand_condition(order=FMaintenanceOrder):
    """Stable WBDD boundary, switched only by the independent cutover flag.

    Beta tombstones are intentionally invisible to stable cost, inventory and
    export readers until the separately approved business cutover.  Keeping the
    switch here lets those readers retain their established contract while the
    Beta workbench can exercise deletion against the same database.
    """

    if not get_settings().maintenance_cutover_enabled:
        return true()
    return beta_active_demand_condition(order)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _order_version_payload(
    order: FMaintenanceOrder,
    lines: Iterable[FMaintenanceLine],
    assignment: MaintenanceSourceOrderAssignment | None,
) -> dict:
    order_payload = {
        column.name: _json_value(getattr(order, column.name))
        for column in FMaintenanceOrder.__table__.columns
        if column.name not in {"id", "created_at"}
    }
    line_payloads = [
        {
            column.name: _json_value(getattr(line, column.name))
            for column in FMaintenanceLine.__table__.columns
            if column.name not in {"id", "order_id"}
        }
        for line in lines
    ]
    line_payloads.sort(key=lambda row: str(row.get("raw_line_id") or ""))
    assignment_payload = (
        {
            "assignment_id": assignment.assignment_id,
            "project_id": assignment.project_id,
            "version": assignment.version,
            "is_active": assignment.is_active,
        }
        if assignment is not None
        else None
    )
    return {
        "header": order_payload,
        "lines": line_payloads,
        "active_project_assignment": assignment_payload,
    }


def _snapshot(
    order: FMaintenanceOrder,
    lines: list[FMaintenanceLine],
    assignment: MaintenanceSourceOrderAssignment | None,
) -> dict:
    downstream = []
    if order.linked_sales_order_no:
        downstream.append(
            {
                "kind": "sales_order",
                "label": f"关联销售单 {order.linked_sales_order_no}",
                "reference_id": order.linked_sales_order_no,
            }
        )
    assignment_payload = None
    if assignment is not None:
        assignment_payload = {
            "assignment_id": assignment.assignment_id,
            "project_id": assignment.project_id,
            "version": assignment.version,
        }
        downstream.append(
            {
                "kind": "stable_project",
                "label": "已确认稳定项目归属",
                "reference_id": assignment.project_id,
            }
        )
    return {
        "source_order_id": order.raw_order_id,
        "order_no": order.order_no,
        "order_date": order.order_date.isoformat() if order.order_date else None,
        "project": order.project_std or order.project_raw,
        "project_raw": order.project_raw,
        "linked_sales_order_no": order.linked_sales_order_no,
        "line_count": len(lines),
        "downstream_references": downstream,
        "active_project_assignment": assignment_payload,
        "version_digest": _digest(
            _order_version_payload(order, lines, assignment)
        ),
    }


def _load_snapshots(
    db: Session,
    source_order_ids: list[str],
    *,
    lock: bool = False,
    active_only: bool = True,
) -> dict[str, dict]:
    if not source_order_ids:
        return {}
    statement = select(FMaintenanceOrder).where(
        FMaintenanceOrder.raw_order_id.in_(source_order_ids)
    )
    if active_only:
        statement = statement.where(beta_active_demand_condition())
    statement = statement.order_by(
        FMaintenanceOrder.raw_order_id,
        FMaintenanceOrder.id,
    )
    if lock:
        statement = statement.with_for_update()
    orders = list(db.scalars(statement))
    internal_ids = [order.id for order in orders]
    assignment_statement = (
        select(MaintenanceSourceOrderAssignment)
        .where(
            MaintenanceSourceOrderAssignment.source_order_id.in_(
                [order.raw_order_id for order in orders]
            ),
            MaintenanceSourceOrderAssignment.is_active.is_(True),
        )
        .order_by(MaintenanceSourceOrderAssignment.source_order_id)
    )
    if lock:
        assignment_statement = assignment_statement.with_for_update()
    assignments_by_source = {
        assignment.source_order_id: assignment
        for assignment in db.scalars(assignment_statement)
    }
    # All maintenance writers use the same hierarchy:
    # order -> active assignment -> detail line.  Keep the snapshot path in
    # that order as well; global refill locks the same rows before overrides.
    lines_by_order: dict[int, list[FMaintenanceLine]] = defaultdict(list)
    if internal_ids:
        line_statement = (
            select(FMaintenanceLine)
            .where(
                FMaintenanceLine.order_id.in_(internal_ids),
                # 2026-08-19：用户作废的明细行不出现在需求快照/搜索/详情（#55）
                FMaintenanceLine.is_active.is_(True),
            )
            .order_by(FMaintenanceLine.order_id, FMaintenanceLine.raw_line_id)
        )
        if lock:
            line_statement = line_statement.with_for_update()
        for line in db.scalars(line_statement):
            lines_by_order[line.order_id].append(line)
    return {
        order.raw_order_id: _snapshot(
            order,
            lines_by_order.get(order.id, []),
            assignments_by_source.get(order.raw_order_id),
        )
        for order in orders
    }


def search_demands(
    db: Session,
    *,
    q: str | None,
    page: int,
    page_size: int,
    allowed_project_ids: set[str] | None = None,
    include_voided: bool = False,
) -> dict:
    """Search WBDD headers; joins never duplicate a header row.

    When *allowed_project_ids* is a set (non-admin), only demands with an
    active assignment to one of those projects are returned.  Unassigned
    demands are visible only in full scope (admin).

    include_voided=True（#268 场景一「含已作废」视图）：不作墓碑过滤，item 带
    is_voided 标记，供恢复入口使用。
    """

    predicates = [] if include_voided else [beta_active_demand_condition()]
    if allowed_project_ids is not None:
        if not allowed_project_ids:
            return {"items": [], "page": page, "page_size": page_size, "total": 0}
        predicates.append(
            exists(
                select(1).where(
                    MaintenanceSourceOrderAssignment.source_order_id
                    == FMaintenanceOrder.raw_order_id,
                    MaintenanceSourceOrderAssignment.project_id.in_(
                        allowed_project_ids
                    ),
                    MaintenanceSourceOrderAssignment.is_active.is_(True),
                )
            )
        )
    term = (q or "").strip()
    if term:
        pattern = f"%{_escape_like(term)}%"
        line_match = exists(
            select(1).where(
                FMaintenanceLine.order_id == FMaintenanceOrder.id,
                # 2026-08-19：作废行不参与订单搜索命中（#55）
                FMaintenanceLine.is_active.is_(True),
                or_(
                    FMaintenanceLine.pn_std.ilike(pattern, escape="\\"),
                    FMaintenanceLine.pn_raw.ilike(pattern, escape="\\"),
                    FMaintenanceLine.description.ilike(pattern, escape="\\"),
                ),
            )
        )
        predicates.append(
            or_(
                FMaintenanceOrder.raw_order_id.ilike(pattern, escape="\\"),
                FMaintenanceOrder.order_no.ilike(pattern, escape="\\"),
                FMaintenanceOrder.project_raw.ilike(pattern, escape="\\"),
                FMaintenanceOrder.project_std.ilike(pattern, escape="\\"),
                FMaintenanceOrder.linked_sales_order_no.ilike(pattern, escape="\\"),
                line_match,
            )
        )

    total = int(
        db.scalar(
            select(func.count()).select_from(FMaintenanceOrder).where(*predicates)
        )
        or 0
    )
    source_ids = list(
        db.scalars(
            select(FMaintenanceOrder.raw_order_id)
            .where(*predicates)
            .order_by(
                FMaintenanceOrder.order_date.desc().nullslast(),
                FMaintenanceOrder.order_no,
                FMaintenanceOrder.raw_order_id,
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    snapshots = _load_snapshots(db, source_ids, active_only=not include_voided)
    if include_voided:
        voided_ids = set(
            db.execute(
                select(MaintenanceDemandTombstone.source_order_id).where(
                    MaintenanceDemandTombstone.source_order_id.in_(source_ids),
                    MaintenanceDemandTombstone.restored_at.is_(None),
                )
            ).scalars().all()
        )
        for source_id in source_ids:
            snapshots.setdefault(
                source_id,
                {"source_order_id": source_id, "order_no": None, "is_voided": True},
            )
            snapshots[source_id]["is_voided"] = source_id in voided_ids
    return {
        "items": [snapshots[source_id] for source_id in source_ids],
        "page": page,
        "page_size": page_size,
        "total": total,
    }


def _request_digest(
    *,
    source_order_ids: list[str],
    reason: str,
    operated_by: str,
) -> str:
    return _digest(
        {
            "source_order_ids": source_order_ids,
            "reason": reason,
            "operated_by": operated_by,
        }
    )


def _selection_digest(items: list[dict], reason: str, operated_by: str) -> str:
    return _digest(
        {
            "items": [
                {
                    "source_order_id": item["source_order_id"],
                    "version_digest": item["version_digest"],
                    "line_count": item["line_count"],
                }
                for item in items
            ],
            "reason": reason,
            "operated_by": operated_by,
        }
    )


def _intent_items(db: Session, intent_id: str) -> list[MaintenanceDemandDeleteIntentItem]:
    return list(
        db.scalars(
            select(MaintenanceDemandDeleteIntentItem)
            .where(MaintenanceDemandDeleteIntentItem.intent_id == intent_id)
            .order_by(MaintenanceDemandDeleteIntentItem.ordinal)
        )
    )


def _intent_payload(db: Session, intent: MaintenanceDemandDeleteIntent) -> dict:
    items = [row.snapshot_json for row in _intent_items(db, intent.intent_id)]
    return {
        "intent_id": intent.intent_id,
        "status": intent.status,
        "selection_digest": intent.selection_digest,
        "reason": intent.reason,
        "operated_by": intent.operated_by,
        "header_count": intent.header_count,
        "line_count": intent.line_count,
        "created_at": intent.created_at,
        "not_before": intent.not_before,
        "expires_at": intent.expires_at,
        "executed_at": intent.executed_at,
        "items": items,
        "result": intent.result_json,
    }


def create_delete_intent(
    db: Session,
    *,
    source_order_ids: list[str],
    reason: str,
    idempotency_key: str,
    operated_by: str,
    allowed_project_ids: set[str] | None = None,
    now: datetime | None = None,
) -> dict:
    now = now or _utc_now()
    normalized_reason = reason.strip()
    if not normalized_reason:
        raise MaintenanceDemandError("删除理由不能为空")
    if len(reason) > MAX_REASON_LENGTH:
        raise MaintenanceDemandError("删除理由无效")
    source_order_ids = _validate_source_order_ids(source_order_ids)

    request_digest = _request_digest(
        source_order_ids=source_order_ids,
        reason=normalized_reason,
        operated_by=operated_by,
    )
    # Serialize the check/insert pair per key.  The unique constraint remains
    # the database backstop; this lock makes concurrent retries return the one
    # existing intent instead of exposing an IntegrityError race.
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": f"maintenance-demand-delete-intent:{idempotency_key}"},
    )
    existing = db.scalar(
        select(MaintenanceDemandDeleteIntent).where(
            MaintenanceDemandDeleteIntent.idempotency_key == idempotency_key
        )
    )
    if existing is not None:
        if existing.request_digest != request_digest:
            raise DeleteIntentConflict("幂等键已被另一份删除请求使用")
        return _intent_payload(db, existing)

    # Imports hold the exclusive form of this lock.  Taking the shared form
    # prevents the header and line queries below from observing two different
    # source revisions while still allowing independent read snapshots to run
    # concurrently.
    db.execute(
        text("SELECT pg_advisory_xact_lock_shared(:k)"),
        {"k": DATA_CHANGE_ADVISORY_LOCK_KEY},
    )
    snapshots = _load_snapshots(db, source_order_ids, lock=False, active_only=True)
    if set(snapshots) != set(source_order_ids):
        raise DeleteIntentConflict("所选 WBDD 已不存在、已删除或状态发生变化")
    items = [snapshots[source_id] for source_id in source_order_ids]
    if allowed_project_ids is not None:
        for item in items:
            assignment = item.get("active_project_assignment")
            project_id = assignment.get("project_id") if assignment else None
            if project_id is None or project_id not in allowed_project_ids:
                raise MaintenanceDemandForbidden(
                    "只能删除本人负责项目下的维保需求单"
                )
    line_count = sum(int(item["line_count"]) for item in items)
    if line_count > MAX_DELETE_LINES:
        raise DeleteIntentConflict(f"一次最多涉及 {MAX_DELETE_LINES} 行备件")
    selection_digest = _selection_digest(items, normalized_reason, operated_by)
    intent = MaintenanceDemandDeleteIntent(
        intent_id=str(uuid4()),
        idempotency_key=idempotency_key,
        request_digest=request_digest,
        selection_digest=selection_digest,
        status="reviewed",
        reason=normalized_reason,
        operated_by=operated_by,
        header_count=len(items),
        line_count=line_count,
        created_at=now,
        expires_at=now + timedelta(minutes=INTENT_TTL_MINUTES),
    )
    db.add(intent)
    db.flush()
    for ordinal, item in enumerate(items):
        db.add(
            MaintenanceDemandDeleteIntentItem(
                intent_id=intent.intent_id,
                source_order_id=item["source_order_id"],
                ordinal=ordinal,
                version_digest=item["version_digest"],
                snapshot_json=item,
            )
        )
    db.flush()
    return _intent_payload(db, intent)


def get_delete_intent(db: Session, *, intent_id: str, operated_by: str) -> dict:
    intent = db.get(MaintenanceDemandDeleteIntent, intent_id)
    if intent is None:
        raise MaintenanceDemandNotFound("删除意图不存在")
    if intent.operated_by != operated_by:
        raise DeleteIntentConflict("删除意图只能由原操作人继续")
    return _intent_payload(db, intent)


def _event(
    db: Session,
    *,
    event_type: str,
    idempotency_key: str,
    operated_by: str,
    reason: str,
    payload: dict,
    occurred_at: datetime,
    intent_id: str | None = None,
    source_order_id: str | None = None,
) -> None:
    existing = db.scalar(
        select(MaintenanceDemandDeleteEvent.event_id).where(
            MaintenanceDemandDeleteEvent.idempotency_key == idempotency_key
        )
    )
    if existing is not None:
        return
    db.add(
        MaintenanceDemandDeleteEvent(
            event_id=str(uuid4()),
            intent_id=intent_id,
            source_order_id=source_order_id,
            event_type=event_type,
            idempotency_key=idempotency_key,
            operated_by=operated_by,
            reason=reason,
            payload_json=payload,
            occurred_at=occurred_at,
        )
    )


def _lock_intent(db: Session, intent_id: str) -> MaintenanceDemandDeleteIntent:
    intent = db.scalar(
        select(MaintenanceDemandDeleteIntent)
        .where(MaintenanceDemandDeleteIntent.intent_id == intent_id)
        .with_for_update()
    )
    if intent is None:
        raise MaintenanceDemandNotFound("删除意图不存在")
    return intent


def _verify_actor_and_digest(
    intent: MaintenanceDemandDeleteIntent,
    *,
    digest: str,
    operated_by: str,
) -> None:
    if intent.operated_by != operated_by:
        raise DeleteIntentConflict("删除意图只能由原操作人继续")
    if intent.selection_digest != digest:
        raise DeleteIntentConflict("复核清单摘要不匹配，整批已拒绝")


def _expire_if_needed(
    db: Session,
    intent: MaintenanceDemandDeleteIntent,
    *,
    now: datetime,
) -> None:
    if now <= intent.expires_at or intent.status in {
        "executed", "cancelled", "conflicted", "expired"
    }:
        return
    intent.status = "expired"
    intent.terminal_at = now
    _event(
        db,
        event_type="expired",
        idempotency_key=f"intent:{intent.intent_id}:expired",
        operated_by=intent.operated_by,
        reason=intent.reason,
        payload={"intent_id": intent.intent_id, "header_count": intent.header_count},
        occurred_at=now,
        intent_id=intent.intent_id,
    )
    db.flush()


def arm_delete_intent(
    db: Session,
    *,
    intent_id: str,
    digest: str,
    operated_by: str,
    now: datetime | None = None,
) -> dict:
    now = now or _utc_now()
    intent = _lock_intent(db, intent_id)
    _expire_if_needed(db, intent, now=now)
    _verify_actor_and_digest(intent, digest=digest, operated_by=operated_by)
    if intent.status == "armed_wait":
        return _intent_payload(db, intent)
    if intent.status != "reviewed":
        raise DeleteIntentConflict(f"当前状态 {intent.status} 不能开始安全等待")
    intent.status = "armed_wait"
    intent.not_before = now + timedelta(seconds=DELETE_WAIT_SECONDS)
    _event(
        db,
        event_type="armed",
        idempotency_key=f"intent:{intent.intent_id}:armed",
        operated_by=operated_by,
        reason=intent.reason,
        payload={
            "intent_id": intent.intent_id,
            "selection_digest": intent.selection_digest,
            "header_count": intent.header_count,
            "line_count": intent.line_count,
            "not_before": intent.not_before.isoformat(),
        },
        occurred_at=now,
        intent_id=intent.intent_id,
    )
    db.flush()
    return _intent_payload(db, intent)


def _mark_conflicted(
    db: Session,
    intent: MaintenanceDemandDeleteIntent,
    *,
    operated_by: str,
    now: datetime,
    cause: str,
) -> None:
    intent.status = "conflicted"
    intent.terminal_at = now
    _event(
        db,
        event_type="conflicted",
        idempotency_key=f"intent:{intent.intent_id}:conflicted",
        operated_by=operated_by,
        reason=intent.reason,
        payload={
            "intent_id": intent.intent_id,
            "header_count": intent.header_count,
            "line_count": intent.line_count,
            "cause": cause,
        },
        occurred_at=now,
        intent_id=intent.intent_id,
    )
    db.flush()


def _lock_workbook_states_for_owners(
    db: Session,
    source_order_ids: list[str],
    *,
    prelocked_states: dict[str, MaintenanceProjectWorkbookState] | None = None,
) -> tuple[dict[str, MaintenanceProjectWorkbookState], set[str]]:
    """OCC 写者失效：无锁 probe 当前 active 归属 → 按序锁工作簿状态。

    全局写锁顺序：data-change advisory → workbook states(sorted) → order →
    assignment → line。probe 不加任何事实锁；锁后重读若出现 probe 之外的
    归属，调用方必须以 Conflict 零写拒绝，绝不在持锁后补拿新 state
    （与 assign_source_orders 同一约定）。

    调用方（master V2 级联）已持 global+states 时通过 ``prelocked_states``
    传入，此时只复用并做覆盖校验，不拿新 state。返回
    ``(locked_states, probed_owner_ids)``。
    """

    probed_owner_ids: set[str] = set()
    if source_order_ids:
        probed_owner_ids = {
            value
            for value in db.scalars(
                select(MaintenanceSourceOrderAssignment.project_id).where(
                    MaintenanceSourceOrderAssignment.source_order_id.in_(
                        source_order_ids
                    ),
                    MaintenanceSourceOrderAssignment.is_active.is_(True),
                )
            ).all()
            if value
        }
    from app.services import maintenance_project_operations as operations

    if prelocked_states is None:
        locked_states = operations.lock_workbook_states(
            db, project_ids=probed_owner_ids
        )
    else:
        locked_states = prelocked_states
        if not probed_owner_ids.issubset(locked_states):
            raise DeleteIntentConflict("需求单项目归属已变化，请刷新后重试")
    return locked_states, probed_owner_ids


def _bump_workbook_revisions(
    db: Session,
    locked_states: dict[str, MaintenanceProjectWorkbookState],
    changed_project_ids: set[str],
) -> None:
    """只为本次实际 tombstone/挂靠停用的项目 +1；state 必须已预锁。

    bump_locked_workbook_revision 已按当前根事务去重，幂等重放/already_voided
    路径根本不会走到这里（changed 为空 → +0）。
    """

    if not changed_project_ids:
        return
    from app.services import maintenance_project_operations as operations

    for project_id in sorted(changed_project_ids):
        operations.bump_locked_workbook_revision(
            db, state=locked_states[project_id]
        )


def execute_delete_intent(
    db: Session,
    *,
    intent_id: str,
    digest: str,
    operated_by: str,
    allowed_project_ids: set[str] | None = None,
    now: datetime | None = None,
) -> dict:
    now = now or _utc_now()
    # Same exclusive transaction lock as imports/cost writes: snapshot validation
    # and tombstone materialization see one coherent source state.
    db.execute(
        text("SELECT pg_advisory_xact_lock(:k)"),
        {"k": DATA_CHANGE_ADVISORY_LOCK_KEY},
    )
    intent = _lock_intent(db, intent_id)
    _expire_if_needed(db, intent, now=now)
    _verify_actor_and_digest(intent, digest=digest, operated_by=operated_by)
    if intent.status == "executed":
        return dict(intent.result_json or {})
    if intent.status != "armed_wait" or intent.not_before is None:
        raise DeleteIntentConflict(f"当前状态 {intent.status} 不能执行删除")
    if now < intent.not_before:
        raise DeleteIntentTooEarly(intent.not_before, now)

    expected_items = _intent_items(db, intent.intent_id)
    source_order_ids = [item.source_order_id for item in expected_items]
    # OCC 写者失效：无锁 probe 当前归属 → 按序锁工作簿状态 → 再加锁重读
    # 订单/挂靠（全局顺序 advisory → states → order → assignment → line）。
    locked_states, probed_owner_ids = _lock_workbook_states_for_owners(
        db, source_order_ids
    )
    current = _load_snapshots(
        db,
        source_order_ids,
        lock=True,
        active_only=True,
    )
    conflict_cause = None
    if set(current) != set(source_order_ids):
        conflict_cause = "missing_or_tombstoned"
    else:
        for expected in expected_items:
            if current[expected.source_order_id]["version_digest"] != expected.version_digest:
                conflict_cause = f"version_changed:{expected.source_order_id}"
                break
        if conflict_cause is None:
            # probe 之后新出现的归属：其 state 未预锁，整批冲突零写，
            # 不允许持锁后补拿新 state。
            for source_id in source_order_ids:
                assignment = current[source_id].get("active_project_assignment")
                owner = assignment.get("project_id") if assignment else None
                if owner is not None and owner not in probed_owner_ids:
                    conflict_cause = f"assignment_changed:{source_id}"
                    break
    if conflict_cause:
        _mark_conflicted(
            db,
            intent,
            operated_by=operated_by,
            now=now,
            cause=conflict_cause,
        )
        raise DeleteIntentConflict("复核后有 WBDD 数据发生变化，整批未删除")

    # TOCTOU 防护：digest 校验之后、tombstone 物化之前，在当前加锁快照上
    # 重新核验操作者项目范围。admin/全量范围（None）跳过；任一需求单当前
    # 归属不在本人范围内（含未归属）→ 整批 403 零删除。
    if allowed_project_ids is not None:
        for source_id in source_order_ids:
            assignment = current[source_id].get("active_project_assignment")
            project_id = assignment.get("project_id") if assignment else None
            if project_id is None or project_id not in allowed_project_ids:
                raise MaintenanceDemandForbidden(
                    "执行时项目范围已变化，整批删除已取消"
                )

    for item in expected_items:
        _upsert_active_tombstone(
            db,
            source_order_id=item.source_order_id,
            delete_intent_id=intent.intent_id,
            version_digest=item.version_digest,
            deleted_by=operated_by,
            delete_reason=intent.reason,
            deleted_at=now,
        )

    # 两阶段执行同样停用挂靠（#267 读侧修复 2）：否则墓碑单继续通过
    # assignment join 出现在项目总表/概览。
    changed_project_ids = _deactivate_assignments(
        db,
        source_order_ids=source_order_ids,
        operated_by=operated_by,
        reason=intent.reason,
        now=now,
    )
    # 只为本次实际停用了挂靠的项目 bump OCC 版本（重放路径提前返回，+0）。
    _bump_workbook_revisions(db, locked_states, changed_project_ids)

    result = {
        "intent_id": intent.intent_id,
        "status": "executed",
        "header_count": intent.header_count,
        "line_count": intent.line_count,
        "source_order_ids": source_order_ids,
        "executed_at": now.isoformat(),
    }
    intent.status = "executed"
    intent.executed_at = now
    intent.terminal_at = now
    intent.result_json = result
    _event(
        db,
        event_type="executed",
        idempotency_key=f"intent:{intent.intent_id}:executed",
        operated_by=operated_by,
        reason=intent.reason,
        payload=result,
        occurred_at=now,
        intent_id=intent.intent_id,
    )
    db.flush()
    from app.services import maintenance_warehouse

    maintenance_warehouse.reconcile_project_assignment_links(
        db,
        operated_by=operated_by,
        reason=intent.reason,
        source_order_ids=set(source_order_ids),
    )
    return result


def cancel_delete_intent(
    db: Session,
    *,
    intent_id: str,
    digest: str,
    operated_by: str,
    now: datetime | None = None,
) -> dict:
    now = now or _utc_now()
    intent = _lock_intent(db, intent_id)
    _expire_if_needed(db, intent, now=now)
    _verify_actor_and_digest(intent, digest=digest, operated_by=operated_by)
    if intent.status == "cancelled":
        return _intent_payload(db, intent)
    if intent.status not in {"reviewed", "armed_wait"}:
        raise DeleteIntentConflict(f"当前状态 {intent.status} 不能取消")
    intent.status = "cancelled"
    intent.terminal_at = now
    _event(
        db,
        event_type="cancelled",
        idempotency_key=f"intent:{intent.intent_id}:cancelled",
        operated_by=operated_by,
        reason=intent.reason,
        payload={"intent_id": intent.intent_id, "header_count": intent.header_count},
        occurred_at=now,
        intent_id=intent.intent_id,
    )
    db.flush()
    return _intent_payload(db, intent)


def _cascade_candidate(db: Session, source_order_id: str) -> bool:
    """无锁 probe：该单是否满足级联打墓碑条件（存在、未墓碑、活动行归零）。"""

    order_id = db.scalar(
        select(FMaintenanceOrder.id).where(
            FMaintenanceOrder.raw_order_id == source_order_id)
    )
    if order_id is None:
        return False
    already = db.get(MaintenanceDemandTombstone, source_order_id)
    if already is not None and already.restored_at is None:
        return False
    active_lines = int(db.scalar(
        select(func.count(FMaintenanceLine.id)).where(
            FMaintenanceLine.order_id == order_id,
            FMaintenanceLine.is_active.is_(True),
        )
    ) or 0)
    return active_lines == 0


def cascade_tombstone_orders(
    db: Session,
    *,
    source_order_ids: list[str],
    operated_by: str,
    reason: str,
    now: datetime | None = None,
    _prelocked_states: dict[str, MaintenanceProjectWorkbookState] | None = None,
) -> list[str]:
    """总表行级作废的级联：某需求单的活动行归零 → 整单打墓碑（#264 增强）。

    与 void-fast 同构（intent 锚 + 墓碑 + 挂靠停用 + 事件），但不做行数/范围
    校验——调用方（工作簿 apply）已完成行级校验，这里只负责收尾整单状态。
    返回实际打了墓碑的单（本来就没有活动行的跳过）。

    OCC 写者失效：DATA_CHANGE advisory → 无锁 probe 新作废单归属 →
    lock_workbook_states(sorted) → 锁后重读 order/assignment。master V2 在
    已持 global+states 后调用本函数时应传入 ``_prelocked_states``；传入时
    只复用并做覆盖校验，锁后出现未预锁 owner 立即 Conflict 零写，绝不补拿
    新 state。默认（None）保持独立调用兼容。
    """
    now = now or _utc_now()
    reason = (reason or "总表行全部作废").strip()[:MAX_REASON_LENGTH]
    tombstoned_now: list[str] = []
    if not source_order_ids:
        return tombstoned_now
    db.execute(
        text("SELECT pg_advisory_xact_lock(:k)"),
        {"k": DATA_CHANGE_ADVISORY_LOCK_KEY},
    )
    candidates = [
        source_id
        for source_id in source_order_ids
        if _cascade_candidate(db, source_id)
    ]
    if not candidates:
        return tombstoned_now
    locked_states, probed_owner_ids = _lock_workbook_states_for_owners(
        db, candidates, prelocked_states=_prelocked_states
    )
    # 锁后按层级重读（order -> assignment）：probe 之后新出现的归属直接
    # Conflict 零写，绝不在持锁后补拿新 state。
    locked_orders = {
        order.raw_order_id: order
        for order in db.scalars(
            select(FMaintenanceOrder)
            .where(FMaintenanceOrder.raw_order_id.in_(candidates))
            .order_by(FMaintenanceOrder.raw_order_id)
            .with_for_update()
        )
    }
    locked_assignments = list(db.scalars(
        select(MaintenanceSourceOrderAssignment)
        .where(
            MaintenanceSourceOrderAssignment.source_order_id.in_(candidates),
            MaintenanceSourceOrderAssignment.is_active.is_(True),
        )
        .order_by(MaintenanceSourceOrderAssignment.source_order_id)
        .with_for_update()
    ))
    if any(
        assignment.project_id not in probed_owner_ids
        for assignment in locked_assignments
    ):
        raise DeleteIntentConflict("需求单项目归属已变化，级联作废已取消")
    for source_id in candidates:
        order = locked_orders.get(source_id)
        if order is None:
            continue
        # 锁后复核：并发探头期的候选可能在持锁后已墓碑或重新有活动行。
        already = db.get(MaintenanceDemandTombstone, source_id)
        if already is not None and already.restored_at is None:
            continue
        active_lines = int(db.scalar(
            select(func.count(FMaintenanceLine.id)).where(
                FMaintenanceLine.order_id == order.id,
                FMaintenanceLine.is_active.is_(True),
            )
        ) or 0)
        if active_lines > 0:
            continue
        intent = MaintenanceDemandDeleteIntent(
            intent_id=str(uuid4()),
            idempotency_key=f"workbook-cascade:{source_id}:{now.strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:6]}",
            request_digest=_digest({"cascade": source_id, "reason": reason,
                                    "operated_by": operated_by}),
            selection_digest=_digest({"cascade": source_id}),
            status="executed",
            reason=reason,
            operated_by=operated_by,
            header_count=1,
            line_count=0,
            created_at=now,
            not_before=now,
            expires_at=now,
        )
        db.add(intent)
        db.flush()
        _upsert_active_tombstone(
            db,
            source_order_id=source_id,
            delete_intent_id=intent.intent_id,
            version_digest=_digest({"cascade": source_id, "at": now.isoformat()}),
            deleted_by=operated_by,
            delete_reason=reason,
            deleted_at=now,
        )
        result = {"intent_id": intent.intent_id, "status": "executed",
                  "mode": "workbook_cascade", "source_order_id": source_id,
                  "order_no": order.order_no, "executed_at": now.isoformat()}
        intent.result_json = result
        _event(
            db,
            event_type="executed",
            idempotency_key=f"workbook-cascade:{intent.intent_id}",
            operated_by=operated_by,
            reason=reason,
            payload=result,
            occurred_at=now,
            intent_id=intent.intent_id,
            source_order_id=source_id,
        )
        tombstoned_now.append(source_id)
    db.flush()
    if tombstoned_now:
        changed_project_ids = _deactivate_assignments(
            db, source_order_ids=tombstoned_now,
            operated_by=operated_by, reason=reason, now=now,
        )
        # 只为本次实际打墓碑/停用挂靠的项目 bump OCC 版本。
        _bump_workbook_revisions(db, locked_states, changed_project_ids)
        from app.services import maintenance_warehouse

        maintenance_warehouse.reconcile_project_assignment_links(
            db, operated_by=operated_by, reason=reason,
            source_order_ids=set(tombstoned_now),
        )
    return tombstoned_now


def _deactivate_assignments(
    db: Session,
    *,
    source_order_ids: list[str],
    operated_by: str,
    reason: str,
    now: datetime,
) -> set[str]:
    """作废/删除时同步停用挂靠关系（#267 读侧修复 2）。

    未停用的 assignment 会绕过墓碑过滤（项目总表 join 只看 is_active），
    导致已作废单的行继续出现在总表/概览。restore 不逆向复活挂靠——
    重新挂靠走正常 assign 流程，保持「恢复后以人工重挂为准」。

    返回本次实际停用了挂靠的归属项目集合（调用方只为这些项目 bump OCC
    版本；已是墓碑/无活动挂靠的单返回空集 → +0）。
    """
    if not source_order_ids:
        return set()
    from app.models.maintenance_project import MaintenanceProjectAuditLog

    assignments = db.scalars(
        select(MaintenanceSourceOrderAssignment).where(
            MaintenanceSourceOrderAssignment.source_order_id.in_(source_order_ids),
            MaintenanceSourceOrderAssignment.is_active.is_(True),
        )
    )
    deactivated_projects: set[str] = set()
    for assignment in assignments:
        before = {
            "assignment_id": assignment.assignment_id,
            "source_order_id": assignment.source_order_id,
            "project_id": assignment.project_id,
            "is_active": True,
            "version": assignment.version,
        }
        assignment.is_active = False
        assignment.version += 1
        assignment.archived_by = operated_by
        # archived_at 必须 >= created_at（ck_..._archive_state）：调用方传入的
        # now 可能被测试冻结在 created_at 之前，取 max 保证约束恒成立。
        assignment.archived_at = (
            now if assignment.created_at is None or now >= assignment.created_at
            else assignment.created_at
        )
        db.add(
            MaintenanceProjectAuditLog(
                project_id=assignment.project_id,
                entity_type="source_order_assignment",
                entity_id=assignment.assignment_id,
                action="void_out",
                before_json=before,
                after_json={
                    **before,
                    "is_active": False,
                    "version": before["version"] + 1,
                },
                reason=reason,
                operated_by=operated_by,
            )
        )
        deactivated_projects.add(assignment.project_id)
    db.flush()
    return deactivated_projects


def _upsert_active_tombstone(
    db: Session,
    *,
    source_order_id: str,
    delete_intent_id: str,
    version_digest: str,
    deleted_by: str,
    delete_reason: str,
    deleted_at: datetime,
) -> MaintenanceDemandTombstone:
    """创建墓碑，或把已恢复墓碑推进为新一代 active tombstone。

    restore→revoid 不能 INSERT 同一主键，也不能保留 restored_*；三条作废
    入口统一走这里，确保 delete intent、摘要、审计人与版本一起换代。
    """
    tombstone = db.get(MaintenanceDemandTombstone, source_order_id)
    if tombstone is None:
        tombstone = MaintenanceDemandTombstone(
            source_order_id=source_order_id,
            delete_intent_id=delete_intent_id,
            version_digest=version_digest,
            deleted_by=deleted_by,
            delete_reason=delete_reason,
            deleted_at=deleted_at,
            version=1,
        )
        db.add(tombstone)
        return tombstone
    tombstone.delete_intent_id = delete_intent_id
    tombstone.version_digest = version_digest
    tombstone.deleted_by = deleted_by
    tombstone.delete_reason = delete_reason
    tombstone.deleted_at = deleted_at
    tombstone.restored_by = None
    tombstone.restore_reason = None
    tombstone.restored_at = None
    tombstone.version += 1
    return tombstone


def void_fast(
    db: Session,
    *,
    source_order_ids: list[str],
    reason: str,
    operated_by: str,
    allowed_project_ids: set[str] | None = None,
    idempotency_key: str | None = None,
    now: datetime | None = None,
) -> dict:
    """一键批量作废（#264/#267 契约）：一次事务内完成快照校验→墓碑→挂靠停用。

    与两阶段 delete-intents 的差别：跳过 arm/7 秒窗口，同事务内创建一条
    ``status='executed'`` 的 intent 作为墓碑外键与审计锚。仍保留：
    独占 data-change 锁、行级快照（版本 digest 存档）、行数上限、项目范围
    TOCTOU 校验、整批零删除（任何一张单不在活跃态即整批拒绝——已墓碑的单
    视为已完成，返回 already_voided 而非报错，允许前端安全重放点击）。
    """
    now = now or _utc_now()
    normalized_reason = (reason or "").strip()
    if not normalized_reason:
        raise MaintenanceDemandError("作废理由不能为空")
    if len(reason) > MAX_REASON_LENGTH:
        raise MaintenanceDemandError("作废理由无效")
    source_order_ids = _validate_source_order_ids(source_order_ids)

    request_digest = _request_digest(
        source_order_ids=source_order_ids,
        reason=normalized_reason,
        operated_by=operated_by,
    )
    if idempotency_key:
        # 幂等重放（P2，Codex review #272）：同键同请求 → 返回首次结果；
        # 同键异请求 → 冲突。与 create_delete_intent 共用每键串行锁。
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"maintenance-demand-delete-intent:{idempotency_key}"},
        )
        existing = db.scalar(
            select(MaintenanceDemandDeleteIntent).where(
                MaintenanceDemandDeleteIntent.idempotency_key == idempotency_key
            )
        )
        if existing is not None:
            if existing.request_digest != request_digest:
                raise DeleteIntentConflict("幂等键已被另一份作废请求使用")
            replay = dict(existing.result_json or {})
            if existing.status != "executed" or replay.get("mode") != "void_fast":
                raise DeleteIntentConflict("幂等键已被另一种删除流程使用")
            # 与 restore 共用全局数据变更锁后再判断当前状态。旧作废成功后若
            # 已被恢复，盲目返回历史 result 会向前端谎报“仍已作废”；同键只能
            # 重放同一代状态，恢复后的新一代作废必须使用新幂等键。
            db.execute(
                text("SELECT pg_advisory_xact_lock(:k)"),
                {"k": DATA_CHANGE_ADVISORY_LOCK_KEY},
            )
            replay_source_ids = {
                str(value)
                for value in replay.get("source_order_ids", ())
                if value
            }
            active_tombstones = set(
                db.scalars(
                    select(MaintenanceDemandTombstone.source_order_id).where(
                        MaintenanceDemandTombstone.source_order_id.in_(
                            sorted(replay_source_ids)
                        ),
                        MaintenanceDemandTombstone.restored_at.is_(None),
                    )
                )
            ) if replay_source_ids else set()
            if active_tombstones != replay_source_ids:
                raise DeleteIntentConflict(
                    "该幂等键对应的历史作废已被恢复；如需再次作废请使用新幂等键"
                )
            replay.setdefault("intent_id", existing.intent_id)
            replay.setdefault("status", existing.status)
            replay["replayed"] = True
            return replay

    db.execute(
        text("SELECT pg_advisory_xact_lock(:k)"),
        {"k": DATA_CHANGE_ADVISORY_LOCK_KEY},
    )

    # 已墓碑（未恢复）的单幂等放行；不存在/未知的单整批拒绝（零写入）。
    tombstoned = set(
        db.execute(
            select(MaintenanceDemandTombstone.source_order_id).where(
                MaintenanceDemandTombstone.source_order_id.in_(source_order_ids),
                MaintenanceDemandTombstone.restored_at.is_(None),
            )
        ).scalars().all()
    )
    unknown = [sid for sid in source_order_ids if sid not in tombstoned]
    # OCC 写者失效：无锁 probe 新作废单的当前归属 → 按序锁工作簿状态 →
    # 加锁重读订单/挂靠（全局顺序 advisory → states → order → assignment）。
    # already_voided 的单不 probe 也不补停用——幂等路径必须 +0。
    locked_states, probed_owner_ids = _lock_workbook_states_for_owners(db, unknown)
    snapshots = _load_snapshots(db, unknown, lock=True, active_only=True) if unknown else {}
    missing = [sid for sid in unknown if sid not in snapshots]
    if missing:
        raise MaintenanceDemandNotFound(
            "所选 WBDD 已不存在或状态发生变化，整批未作废"
        )
    # probe 之后新出现的归属：其 state 未预锁，整批冲突零写，
    # 不允许持锁后补拿新 state。
    for item in snapshots.values():
        assignment = item.get("active_project_assignment")
        owner = assignment.get("project_id") if assignment else None
        if owner is not None and owner not in probed_owner_ids:
            raise DeleteIntentConflict("所选 WBDD 项目归属已变化，整批未作废")

    items = [snapshots[sid] for sid in unknown]
    if allowed_project_ids is not None:
        for item in items:
            assignment = item.get("active_project_assignment")
            project_id = assignment.get("project_id") if assignment else None
            if project_id is None or project_id not in allowed_project_ids:
                raise MaintenanceDemandForbidden(
                    "只能作废本人负责项目下的维保需求单"
                )
    line_count = sum(int(item["line_count"]) for item in items)
    if line_count > MAX_DELETE_LINES:
        raise DeleteIntentConflict(f"一次最多涉及 {MAX_DELETE_LINES} 行备件")

    # 同事务落一条已执行的 intent：墓碑 FK（delete_intent_id）与既有审计链不变。
    idem = idempotency_key or f"void-fast:{uuid4()}"
    intent = MaintenanceDemandDeleteIntent(
        intent_id=str(uuid4()),
        idempotency_key=idem,
        request_digest=request_digest,
        selection_digest=_selection_digest(items, normalized_reason, operated_by),
        status="executed",
        reason=normalized_reason,
        operated_by=operated_by,
        header_count=len(source_order_ids),
        line_count=line_count,
        created_at=now,
        not_before=now,
        expires_at=now,
    )
    db.add(intent)
    db.flush()
    for ordinal, item in enumerate(items):
        db.add(
            MaintenanceDemandDeleteIntentItem(
                intent_id=intent.intent_id,
                source_order_id=item["source_order_id"],
                ordinal=ordinal,
                version_digest=item["version_digest"],
                snapshot_json=item,
            )
        )
    db.flush()

    for item in items:
        _upsert_active_tombstone(
            db,
            source_order_id=item["source_order_id"],
            delete_intent_id=intent.intent_id,
            version_digest=item["version_digest"],
            deleted_by=operated_by,
            delete_reason=normalized_reason,
            deleted_at=now,
        )

    # 作废同步停用挂靠关系：未停用的 assignment 会绕过墓碑过滤（读侧 join
    # 只看 is_active），且 restore 依赖挂靠重建归属（#267 读侧修复 2）。
    # 只对本次新作废的单停用——already_voided 的幂等路径不产生任何 bump。
    changed_project_ids = _deactivate_assignments(
        db,
        source_order_ids=unknown,
        operated_by=operated_by,
        reason=normalized_reason,
        now=now,
    )
    _bump_workbook_revisions(db, locked_states, changed_project_ids)

    order_no_by_source = {
        item["source_order_id"]: item.get("order_no") for item in items
    }
    result = {
        "intent_id": intent.intent_id,
        "status": "executed",
        "mode": "void_fast",
        "header_count": len(source_order_ids),
        "line_count": line_count,
        "voided": len(unknown),
        "already_voided": len(tombstoned),
        # #265 冻结契约：逐单结果（前端逐行反馈 + 幂等命中不算错误）。
        "results": [
            {
                "source_order_id": source_id,
                "order_no": order_no_by_source.get(source_id),
                "status": ("voided" if source_id in snapshots else "already_voided"),
            }
            for source_id in source_order_ids
        ],
        "source_order_ids": source_order_ids,
        "executed_at": now.isoformat(),
    }
    intent.result_json = result
    # event_type 复用 CHECK 约束内的 'executed'——不新增枚举（零迁移），
    # 来源由 payload.mode 与幂等键前缀 void-fast: 区分。
    _event(
        db,
        event_type="executed",
        idempotency_key=f"void-fast:{intent.intent_id}",
        operated_by=operated_by,
        reason=normalized_reason,
        payload=result,
        occurred_at=now,
        intent_id=intent.intent_id,
    )
    db.flush()
    from app.services import maintenance_warehouse

    maintenance_warehouse.reconcile_project_assignment_links(
        db,
        operated_by=operated_by,
        reason=normalized_reason,
        source_order_ids=set(source_order_ids),
    )
    return result


def restore_demand(
    db: Session,
    *,
    source_order_id: str,
    reason: str,
    operated_by: str,
    now: datetime | None = None,
) -> dict:
    now = now or _utc_now()
    normalized_reason = reason.strip()
    if not normalized_reason:
        raise MaintenanceDemandError("恢复理由不能为空")
    if len(reason) > MAX_REASON_LENGTH:
        raise MaintenanceDemandError("恢复理由无效")
    source_order_id = _validate_source_order_id(source_order_id)
    db.execute(
        text("SELECT pg_advisory_xact_lock(:k)"),
        {"k": DATA_CHANGE_ADVISORY_LOCK_KEY},
    )
    tombstone = db.scalar(
        select(MaintenanceDemandTombstone)
        .where(MaintenanceDemandTombstone.source_order_id == source_order_id)
        .with_for_update()
    )
    if tombstone is None or tombstone.restored_at is not None:
        raise MaintenanceDemandNotFound("WBDD 不在已删除状态")
    order_id = db.scalar(
        select(FMaintenanceOrder.id)
        .where(FMaintenanceOrder.raw_order_id == source_order_id)
        .with_for_update()
    )
    if order_id is None:
        raise MaintenanceDemandNotFound("WBDD 来源单不存在")
    # restore 明确不复活 assignment（重挂走正常 assign 流程），正常路径
    # 不动任何项目工作簿版本（+0）。若发现历史脏数据——已墓碑单上仍存在
    # active 挂靠——fail closed 拒绝恢复，而不是带病放行出双口径。
    dirty_assignment = db.scalar(
        select(MaintenanceSourceOrderAssignment.assignment_id).where(
            MaintenanceSourceOrderAssignment.source_order_id == source_order_id,
            MaintenanceSourceOrderAssignment.is_active.is_(True),
        )
    )
    if dirty_assignment is not None:
        raise MaintenanceDemandError(
            "该 WBDD 仍存在有效项目挂靠，恢复已拒绝，请先核对挂靠数据"
        )
    cutover_enabled = get_settings().maintenance_cutover_enabled
    if cutover_enabled:
        # Once tombstones are the canonical production boundary, a restored
        # order may have been reimported or its price evidence may have changed
        # while it was hidden.  Clear every derived field before making it
        # effective again so an old snapshot cannot masquerade as current cost.
        # Release invariant: while any line is ``cost_recompute_pending``, the
        # cutover flag is not a rollback switch.  Recompute and validate first,
        # then a false rollback can safely expose the same canonical columns.
        invalidated_line_count = maintenance_cost_invalidation.invalidate_line_costs(
            db,
            condition=FMaintenanceLine.order_id == order_id,
            pending_recompute=True,
        )
        cost_state = "pending_recompute"
    else:
        # During the Beta trial tombstones are shadow facts: stable readers
        # never stopped consuming this order.  Invalidating here would mutate
        # the stable cost/project/export truth even though the Beta action was
        # explicitly isolated from it.
        invalidated_line_count = 0
        cost_state = "stable_unchanged"
    tombstone.restored_by = operated_by
    tombstone.restore_reason = normalized_reason
    tombstone.restored_at = now
    tombstone.version += 1
    result = {
        "source_order_id": source_order_id,
        "status": "restored",
        "restored_at": now.isoformat(),
        "tombstone_version": tombstone.version,
        "cost_state": cost_state,
        "invalidated_line_count": invalidated_line_count,
    }
    _event(
        db,
        event_type="restored",
        idempotency_key=f"restore:{source_order_id}:{tombstone.version}",
        operated_by=operated_by,
        reason=normalized_reason,
        payload=result,
        occurred_at=now,
        intent_id=tombstone.delete_intent_id,
        source_order_id=source_order_id,
    )
    db.flush()
    from app.services import maintenance_warehouse

    maintenance_warehouse.reconcile_project_assignment_links(
        db,
        operated_by=operated_by,
        reason=normalized_reason,
        source_order_ids={source_order_id},
    )
    return result

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
from typing import Any, Iterable
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
    if lock:
        statement = statement.with_for_update()
    orders = list(db.scalars(statement))
    internal_ids = [order.id for order in orders]
    lines_by_order: dict[int, list[FMaintenanceLine]] = defaultdict(list)
    if internal_ids:
        line_statement = (
            select(FMaintenanceLine)
            .where(FMaintenanceLine.order_id.in_(internal_ids))
            .order_by(FMaintenanceLine.order_id, FMaintenanceLine.raw_line_id)
        )
        if lock:
            line_statement = line_statement.with_for_update()
        for line in db.scalars(line_statement):
            lines_by_order[line.order_id].append(line)
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
) -> dict:
    """Search active WBDD headers; joins never duplicate a header row."""

    predicates = [beta_active_demand_condition()]
    term = (q or "").strip()
    if term:
        pattern = f"%{_escape_like(term)}%"
        line_match = exists(
            select(1).where(
                FMaintenanceLine.order_id == FMaintenanceOrder.id,
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
    snapshots = _load_snapshots(db, source_ids)
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


def execute_delete_intent(
    db: Session,
    *,
    intent_id: str,
    digest: str,
    operated_by: str,
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
    if conflict_cause:
        _mark_conflicted(
            db,
            intent,
            operated_by=operated_by,
            now=now,
            cause=conflict_cause,
        )
        raise DeleteIntentConflict("复核后有 WBDD 数据发生变化，整批未删除")

    for item in expected_items:
        tombstone = db.get(MaintenanceDemandTombstone, item.source_order_id)
        if tombstone is None:
            tombstone = MaintenanceDemandTombstone(
                source_order_id=item.source_order_id,
                delete_intent_id=intent.intent_id,
                version_digest=item.version_digest,
                deleted_by=operated_by,
                delete_reason=intent.reason,
                deleted_at=now,
                version=1,
            )
            db.add(tombstone)
        else:
            tombstone.delete_intent_id = intent.intent_id
            tombstone.version_digest = item.version_digest
            tombstone.deleted_by = operated_by
            tombstone.delete_reason = intent.reason
            tombstone.deleted_at = now
            tombstone.restored_by = None
            tombstone.restore_reason = None
            tombstone.restored_at = None
            tombstone.version += 1

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
        "cutover_enabled": cutover_enabled,
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

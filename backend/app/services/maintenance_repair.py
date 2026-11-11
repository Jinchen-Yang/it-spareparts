"""坏件循环待修池（D-0058）。

待修 = 合格坏返件数 − 有效扣数。已上电商 = 有效结果的修好数。
废品 = 有效结果的废品数。本模块不写 inventory、前置库、采购、销售、氚云或变卖台账，
也不调用商城。``fixed_qty > 0`` 时只在同一事务插入一条 pending 可售意图。
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Iterable
from uuid import UUID, uuid4

from sqlalchemy import Column, Integer, MetaData, Table, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.business_time import BUSINESS_TZ
from app.models.dimensions import DimPart
from app.models.maintenance_cycle import MaintenanceCycleSellIntent
from app.models.maintenance_doc_import import MaintenanceRkdReturnLine
from app.models.maintenance_repair import (
    MaintenanceRepairCommand,
    MaintenanceRepairRecord,
    MaintenanceRepairRecordSource,
)

log = logging.getLogger(__name__)

DEDUCTION_MESSAGE = "该返还收货已有维修扣数，变更后将少于已扣件数"
BAD_CONDITIONS = frozenset({"坏品", "坏件", "故障"})
UNSTOCKED_BLOCK_CODES = frozenset({
    "missing_catalog_identity",
    "missing_formal_image",
    "missing_category_mapping",
    "missing_list_price",
    "publish_api_missing_stock_update",
})
FAILED_UNSTOCKED_ERRORS = frozenset({
    "invalid_request",
    "manual_conflict",
    "conflict",
})
REPAIRER_RECORD_KEYS = frozenset({
    "record_id",
    "record_no",
    "part_id",
    "pn",
    "description",
    "fixed_qty",
    "scrap_qty",
    "status",
    "version",
    "created_at",
    "operator",
    "voided_at",
    "void_reason",
})
CREATE_RECORD_KEYS = REPAIRER_RECORD_KEYS - {"voided_at", "void_reason"}

v_repair_pool = Table(
    "v_repair_pool",
    MetaData(),
    Column("part_id", Integer),
    Column("pending_qty", Integer),
    Column("shop_qty", Integer),
    Column("scrap_qty", Integer),
)


class RepairError(Exception):
    status_code = 400

    def __init__(self, detail: str, status_code: int | None = None):
        super().__init__(detail)
        self.detail = detail
        if status_code is not None:
            self.status_code = status_code


class RepairConflict(RepairError):
    status_code = 409


class RepairNotFound(RepairError):
    status_code = 404


class RepairForbidden(RepairError):
    status_code = 403


class RepairDeductionExceeded(Exception):
    def __init__(self, detail: str = DEDUCTION_MESSAGE):
        super().__init__(detail)
        self.detail = detail


@dataclass
class RepairResult:
    status_code: int
    body: dict
    replayed: bool = False


def format_record_no(wire_unit_id: int) -> str:
    return f"WX{wire_unit_id:08d}"


def eligible_qty(line: MaintenanceRkdReturnLine) -> int:
    return eligible_from_values(
        line.line_status, line.test_result, line.part_id, line.qty
    )


def eligible_from_values(
    line_status: str | None,
    test_result: str | None,
    part_id: int | None,
    qty: Decimal | int | None,
) -> int:
    if line_status != "active" or test_result not in BAD_CONDITIONS or part_id is None:
        return 0
    if qty is None:
        return 0
    amount = Decimal(qty)
    if not amount.is_finite() or amount != amount.to_integral_value():
        return 0
    if amount < 1 or amount > 2147483647:
        return 0
    return int(amount)


def request_fingerprint(action: str, record_id: str | None, body: dict) -> str:
    payload = {"action": action, "record_id": record_id, "body": body}
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _bound(db: Session) -> None:
    db.execute(text("SET LOCAL statement_timeout = '5000ms'"))
    db.execute(text("SET LOCAL lock_timeout = '1000ms'"))


def _lock_timeout(db: Session, timeout: str) -> None:
    db.execute(text(f"SET LOCAL lock_timeout = '{timeout}'"))


def lock_parts(
    db: Session, part_ids: Iterable[int | None], *, keep_lock_timeout: bool = False
) -> None:
    ids = sorted({int(part_id) for part_id in part_ids if part_id is not None})
    if not ids:
        return
    _lock_timeout(db, "1000ms")
    for part_id in ids:
        db.execute(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtextextended(f"repair-part:{part_id}", 0)
                )
            )
        )
    if not keep_lock_timeout:
        _lock_timeout(db, "30000ms")


def try_lock_part(db: Session, part_id: int) -> bool:
    locked = db.scalar(
        text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"repair-part:{part_id}"},
    )
    return bool(locked)


def _lock_idempotency(db: Session, actor: str, key: str) -> None:
    db.execute(
        select(
            func.pg_advisory_xact_lock(
                func.hashtextextended(f"{actor}:{key}", 0)
            )
        )
    )


def _receipt_context(db: Session) -> None:
    from app.services.maintenance_return_receipts import lock_receipt_context

    lock_receipt_context(db)


def lock_receipt_lines(db: Session, part_id: int) -> list[MaintenanceRkdReturnLine]:
    return list(
        db.scalars(
            select(MaintenanceRkdReturnLine)
            .where(MaintenanceRkdReturnLine.part_id == part_id)
            .order_by(MaintenanceRkdReturnLine.rkd_line_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    )


def lock_sources_and_intents(db: Session, receipt_ids: Iterable[str]) -> None:
    ids = sorted({receipt_id for receipt_id in receipt_ids if receipt_id})
    if not ids:
        return
    db.scalars(
        select(MaintenanceRepairRecordSource)
        .where(MaintenanceRepairRecordSource.receipt_id.in_(ids))
        .order_by(
            MaintenanceRepairRecordSource.receipt_id,
            MaintenanceRepairRecordSource.record_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    record_ids = list(
        db.scalars(
            select(MaintenanceRepairRecordSource.record_id)
            .where(MaintenanceRepairRecordSource.receipt_id.in_(ids))
            .order_by(MaintenanceRepairRecordSource.record_id)
        ).all()
    )
    if record_ids:
        db.scalars(
            select(MaintenanceCycleSellIntent)
            .where(MaintenanceCycleSellIntent.record_id.in_(record_ids))
            .order_by(MaintenanceCycleSellIntent.record_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()


def _active_deducted_map(db: Session, receipt_ids: Iterable[str]) -> dict[str, int]:
    ids = [receipt_id for receipt_id in receipt_ids if receipt_id]
    if not ids:
        return {}
    rows = db.execute(
        select(
            MaintenanceRepairRecordSource.receipt_id,
            func.coalesce(func.sum(MaintenanceRepairRecordSource.qty), 0),
        )
        .join(
            MaintenanceRepairRecord,
            MaintenanceRepairRecord.record_id == MaintenanceRepairRecordSource.record_id,
        )
        .where(
            MaintenanceRepairRecordSource.receipt_id.in_(ids),
            MaintenanceRepairRecord.status == "active",
        )
        .group_by(MaintenanceRepairRecordSource.receipt_id)
    ).all()
    return {receipt_id: int(qty) for receipt_id, qty in rows}


def active_deducted(db: Session, receipt_id: str) -> int:
    return _active_deducted_map(db, [receipt_id]).get(receipt_id, 0)


def assert_receipt_change(
    db: Session,
    row: MaintenanceRkdReturnLine,
    *,
    new_qty: Decimal | int,
    new_part_id: int | None,
    new_test_result: str | None,
    new_line_status: str,
) -> None:
    deducted = active_deducted(db, row.rkd_line_id)
    if deducted <= 0:
        return
    if new_part_id != row.part_id:
        raise RepairDeductionExceeded(DEDUCTION_MESSAGE)
    eligible = eligible_from_values(
        new_line_status, new_test_result, new_part_id, new_qty
    )
    if deducted > eligible:
        raise RepairDeductionExceeded(DEDUCTION_MESSAGE)


def _fifo_key(line: MaintenanceRkdReturnLine) -> tuple:
    if line.occurred_at is None:
        return (1, "", line.rkd_line_id)
    return (0, line.occurred_at.isoformat(), line.rkd_line_id)


def allocate_fifo(
    lines: list[MaintenanceRkdReturnLine], deducted: dict[str, int], qty: int
) -> list[tuple[str, int]]:
    remaining = qty
    allocated: list[tuple[str, int]] = []
    for line in sorted(lines, key=_fifo_key):
        if remaining <= 0:
            break
        room = eligible_qty(line) - deducted.get(line.rkd_line_id, 0)
        if room <= 0:
            continue
        take = min(room, remaining)
        allocated.append((line.rkd_line_id, take))
        remaining -= take
    if remaining > 0:
        raise RepairConflict("修好数与废品数之和超过当前待修件数")
    return allocated


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=BUSINESS_TZ).isoformat()
    return value.isoformat()


def _description(value: str | None) -> str | None:
    if value is None:
        return None
    text_value = value.strip()
    return text_value or None


def _part_description(db: Session, part_id: int) -> str | None:
    description = db.scalar(select(DimPart.description).where(DimPart.id == part_id))
    return _description(description)


def _stocked(intent: MaintenanceCycleSellIntent | None) -> bool:
    if intent is None:
        return False
    if intent.state == "delivered":
        return True
    if intent.receipt_status in {"applied", "unchanged"}:
        return True
    return intent.block_code == "publish_disabled"


def _certainly_unstocked(intent: MaintenanceCycleSellIntent | None) -> bool:
    if intent is None:
        return True
    if _stocked(intent):
        return False
    if intent.state == "pending" and intent.attempts == 0:
        return True
    if (
        intent.receipt_status == "blocked"
        and intent.block_code in UNSTOCKED_BLOCK_CODES
    ):
        return True
    if intent.last_error_code == "remote_busy":
        return True
    return (
        intent.state == "failed"
        and intent.receipt_status != "applied"
        and intent.block_code != "publish_disabled"
        and intent.last_error_code in FAILED_UNSTOCKED_ERRORS
    )


def _missing_operator_deferred(intent: MaintenanceCycleSellIntent | None) -> bool:
    return (
        intent is not None
        and intent.block_code == "missing_operator"
        and not _stocked(intent)
    )


def _needs_shop_checkbox(
    intent: MaintenanceCycleSellIntent | None, *, now: datetime
) -> bool:
    if intent is None or _missing_operator_deferred(intent) or _certainly_unstocked(intent):
        return False
    if _stocked(intent):
        return True
    lease_expired = (
        intent.state == "delivering"
        and intent.lease_until is not None
        and intent.lease_until <= now
    )
    return intent.attempts > 0 and (intent.state == "pending" or lease_expired)


def shop_closeout(
    record: MaintenanceRepairRecord, intent: MaintenanceCycleSellIntent | None
) -> str | None:
    if (
        record.status == "voided"
        and not record.shop_adjusted_manually
        and _missing_operator_deferred(intent)
    ):
        return "awaiting_shop_reversal"
    return None


def _intent_for(db: Session, record_id: UUID) -> MaintenanceCycleSellIntent | None:
    return db.scalar(
        select(MaintenanceCycleSellIntent).where(
            MaintenanceCycleSellIntent.record_id == record_id
        )
    )


def intent_payload(
    intent: MaintenanceCycleSellIntent | None,
    record: MaintenanceRepairRecord | None,
    *,
    with_record_id: bool,
) -> dict | None:
    if intent is None:
        if with_record_id and record is not None:
            return {"record_id": str(record.record_id), "status": "not_queued"}
        return None
    body = {
        "delivery_id": str(intent.delivery_id),
        "unit_id": int(intent.wire_unit_id),
        "status": intent.state,
        "qty": int(intent.qty),
        "attempts": int(intent.attempts),
        "block_code": intent.block_code,
        "last_error_code": intent.last_error_code,
        "receipt_status": intent.receipt_status,
        "next_attempt_at": (
            _iso(intent.available_at) if intent.state in {"pending", "blocked"} else None
        ),
        "shop_closeout": shop_closeout(record, intent) if record is not None else None,
    }
    if with_record_id:
        return {"record_id": str(intent.record_id), **body}
    return body


def _sources_payload(db: Session, record_id: UUID) -> list[dict]:
    rows = db.execute(
        select(MaintenanceRepairRecordSource, MaintenanceRkdReturnLine)
        .join(
            MaintenanceRkdReturnLine,
            MaintenanceRkdReturnLine.rkd_line_id
            == MaintenanceRepairRecordSource.receipt_id,
        )
        .where(MaintenanceRepairRecordSource.record_id == record_id)
    ).all()
    rows.sort(
        key=lambda pair: (
            pair[1].occurred_at is None,
            pair[1].occurred_at.isoformat() if pair[1].occurred_at else "",
            pair[1].rkd_line_id,
        )
    )
    payload = []
    for source, line in rows:
        line_qty = line.qty
        payload.append(
            {
                "receipt_id": source.receipt_id,
                "qty": int(source.qty),
                "occurred_at": _iso(line.occurred_at),
                "line_qty": int(line_qty) if line_qty == line_qty.to_integral_value() else float(line_qty),
                "test_result": line.test_result,
                "project_id": line.project_id,
            }
        )
    return payload


def _record_body(
    db: Session,
    record: MaintenanceRepairRecord,
    *,
    audience: str,
    include_void_fields: bool,
) -> dict:
    body = {
        "record_id": str(record.record_id),
        "record_no": record.record_no,
        "part_id": record.part_id,
        "pn": record.pn,
        "description": _part_description(db, record.part_id),
        "fixed_qty": int(record.fixed_qty),
        "scrap_qty": int(record.scrap_qty),
        "status": record.status,
        "version": int(record.version),
        "created_at": _iso(record.created_at),
        "operator": record.operator,
    }
    if include_void_fields:
        body["voided_at"] = _iso(record.voided_at)
        body["void_reason"] = record.void_reason
    if audience == "supervisor":
        intent = _intent_for(db, record.record_id)
        body["sources"] = _sources_payload(db, record.record_id)
        body["sell_intent"] = intent_payload(intent, record, with_record_id=False)
    return body


def _remember(
    db: Session,
    *,
    actor: str,
    key: str,
    action: str,
    record_id: UUID | None,
    fingerprint: str,
    status_code: int,
    body: dict,
) -> None:
    db.add(
        MaintenanceRepairCommand(
            actor=actor,
            idempotency_key=key,
            action=action,
            record_id=record_id,
            request_fingerprint=fingerprint,
            status_code=status_code,
            response_json=body,
        )
    )
    try:
        db.flush()
    except IntegrityError as exc:
        constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", "")
        if constraint == "uq_maintenance_repair_command_key":
            log.info("repair idempotency conflict actor=%s action=%s", actor, action)
            raise RepairConflict("幂等键已被用于不同的请求") from exc
        raise


def _begin_command(
    db: Session,
    *,
    actor: str,
    key: str,
    action: str,
    record_id: str | None,
    body: dict,
) -> RepairResult | str:
    _bound(db)
    _lock_idempotency(db, actor, key)
    _receipt_context(db)
    fingerprint = request_fingerprint(action, record_id, body)
    other = db.scalar(
        select(MaintenanceRepairCommand.actor).where(
            MaintenanceRepairCommand.idempotency_key == key,
            MaintenanceRepairCommand.actor != actor,
        )
    )
    if other is not None:
        log.info("repair idempotency conflict actor=%s action=%s", actor, action)
        raise RepairConflict("幂等键已被用于不同的请求")
    stored = db.scalar(
        select(MaintenanceRepairCommand).where(
            MaintenanceRepairCommand.actor == actor,
            MaintenanceRepairCommand.idempotency_key == key,
        )
    )
    if stored is None:
        return fingerprint
    stored_record = str(stored.record_id) if stored.record_id is not None else None
    if (
        stored.action != action
        or stored_record != record_id
        or stored.request_fingerprint != fingerprint
    ):
        log.info("repair idempotency conflict actor=%s action=%s", actor, action)
        raise RepairConflict("幂等键已被用于不同的请求")
    return RepairResult(stored.status_code, dict(stored.response_json), replayed=True)


def _pending_qty(lines: list[MaintenanceRkdReturnLine], deducted: dict[str, int]) -> int:
    return sum(
        max(eligible_qty(line) - deducted.get(line.rkd_line_id, 0), 0) for line in lines
    )


def submit_result(
    db: Session,
    *,
    actor: str,
    part_id: int,
    fixed_qty: int,
    scrap_qty: int,
    idempotency_key: str,
) -> RepairResult:
    body = {"part_id": part_id, "fixed_qty": fixed_qty, "scrap_qty": scrap_qty}
    begun = _begin_command(
        db,
        actor=actor,
        key=idempotency_key,
        action="submit",
        record_id=None,
        body=body,
    )
    if isinstance(begun, RepairResult):
        return begun
    lock_parts(db, [part_id], keep_lock_timeout=True)
    lines = lock_receipt_lines(db, part_id)
    lock_sources_and_intents(db, [line.rkd_line_id for line in lines])
    part = db.get(DimPart, part_id)
    deducted = _active_deducted_map(db, [line.rkd_line_id for line in lines])
    pending = _pending_qty(lines, deducted)
    eligible_total = sum(eligible_qty(line) for line in lines)
    requested = fixed_qty + scrap_qty
    if part is None or part.status != "active" or eligible_total <= 0:
        raise RepairError("该型号当前没有待修件数", 400)
    if requested > pending:
        raise RepairConflict("修好数与废品数之和超过当前待修件数")
    allocation = allocate_fifo(lines, deducted, requested)
    now = db.scalar(select(func.clock_timestamp()))
    wire_unit_id = int(
        db.scalar(text("SELECT nextval('maintenance_repair_record_no_seq')"))
    )
    record = MaintenanceRepairRecord(
        record_id=uuid4(),
        wire_unit_id=wire_unit_id,
        record_no=format_record_no(wire_unit_id),
        part_id=part.id,
        pn=part.pn_std,
        fixed_qty=fixed_qty,
        scrap_qty=scrap_qty,
        operator=actor,
        status="active",
        version=1,
        created_at=now,
    )
    db.add(record)
    db.flush()
    for receipt_id, qty in allocation:
        db.add(
            MaintenanceRepairRecordSource(
                record_id=record.record_id, receipt_id=receipt_id, qty=qty
            )
        )
    if fixed_qty > 0:
        db.add(
            MaintenanceCycleSellIntent(
                record_id=record.record_id,
                part_id=part.id,
                qty=fixed_qty,
                wire_unit_id=wire_unit_id,
                condition_bucket="good",
                state="pending",
                attempts=0,
                available_at=now,
                created_at=now,
                updated_at=now,
            )
        )
    db.flush()
    payload = _record_body(db, record, audience="repairer", include_void_fields=False)
    _remember(
        db,
        actor=actor,
        key=idempotency_key,
        action="submit",
        record_id=None,
        fingerprint=begun,
        status_code=201,
        body=payload,
    )
    return RepairResult(201, payload)


def _visible_record(
    record: MaintenanceRepairRecord | None, *, actor: str, as_supervisor: bool
) -> MaintenanceRepairRecord:
    if record is None or (not as_supervisor and record.operator != actor):
        raise RepairNotFound("记录不存在")
    return record


def _lock_record_for_write(
    db: Session, record_id: UUID, *, actor: str, as_supervisor: bool
) -> MaintenanceRepairRecord:
    peek = db.get(MaintenanceRepairRecord, record_id)
    _visible_record(peek, actor=actor, as_supervisor=as_supervisor)
    assert peek is not None
    lock_parts(db, [peek.part_id], keep_lock_timeout=True)
    record = db.scalar(
        select(MaintenanceRepairRecord)
        .where(MaintenanceRepairRecord.record_id == record_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    record = _visible_record(record, actor=actor, as_supervisor=as_supervisor)
    if record.part_id != peek.part_id and not try_lock_part(db, record.part_id):
        raise RepairConflict("该型号正在被其他操作处理，请稍后重试")
    db.scalars(
        select(MaintenanceRepairRecordSource)
        .where(MaintenanceRepairRecordSource.record_id == record.record_id)
        .order_by(MaintenanceRepairRecordSource.receipt_id)
        .with_for_update()
    ).all()
    db.scalar(
        select(MaintenanceCycleSellIntent)
        .where(MaintenanceCycleSellIntent.record_id == record.record_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return record


def void_result(
    db: Session,
    *,
    actor: str,
    record_id: UUID,
    expected_version: int,
    reason: str,
    shop_stock_manually_reduced: bool,
    idempotency_key: str,
    as_supervisor: bool,
) -> RepairResult:
    body = {
        "expected_version": expected_version,
        "reason": reason,
        "shop_stock_manually_reduced": shop_stock_manually_reduced,
    }
    begun = _begin_command(
        db,
        actor=actor,
        key=idempotency_key,
        action="void",
        record_id=str(record_id),
        body=body,
    )
    if isinstance(begun, RepairResult):
        return begun
    record = _lock_record_for_write(
        db, record_id, actor=actor, as_supervisor=as_supervisor
    )
    if record.status == "voided":
        raise RepairConflict("该记录已作废")
    if record.version != expected_version:
        raise RepairConflict("版本冲突：记录已被他人处理，请刷新后重试")
    now = db.scalar(select(func.clock_timestamp()))
    if not as_supervisor:
        created = record.created_at
        if created.astimezone(BUSINESS_TZ).date() != now.astimezone(BUSINESS_TZ).date():
            raise RepairForbidden("已超过当天，只有主管可以作废")
    intent = _intent_for(db, record.record_id)
    if (
        intent is not None
        and intent.state == "delivering"
        and intent.lease_until is not None
        and intent.lease_until > now
    ):
        raise RepairConflict("推送进行中，请稍后再作废")
    if _missing_operator_deferred(intent):
        if shop_stock_manually_reduced:
            raise RepairConflict(
                "这笔推送还没入账，作废时不要勾选。请等商城补上经办人并盘点减回、核对在手后，再登记"
            )
        adjusted = False
    elif _needs_shop_checkbox(intent, now=now):
        if not shop_stock_manually_reduced:
            raise RepairConflict(
                "修好数量可能已计入商城库存，请先在商城手工减库存并勾选确认"
            )
        adjusted = True
    else:
        adjusted = bool(shop_stock_manually_reduced)
    record.status = "voided"
    record.voided_by = actor
    record.voided_at = now
    record.void_reason = reason
    record.shop_adjusted_manually = adjusted
    record.version += 1
    if intent is not None and intent.state != "failed":
        intent.state = "failed"
        intent.last_error_code = "record_voided"
        intent.lease_token = None
        intent.lease_until = None
        intent.completed_at = now
        intent.updated_at = now
    db.flush()
    audience = "supervisor" if as_supervisor else "repairer"
    payload = _record_body(db, record, audience=audience, include_void_fields=True)
    _remember(
        db,
        actor=actor,
        key=idempotency_key,
        action="void",
        record_id=record.record_id,
        fingerprint=begun,
        status_code=200,
        body=payload,
    )
    return RepairResult(200, payload)


def attest_manual_shop_reduction(
    db: Session,
    *,
    actor: str,
    record_id: UUID,
    expected_version: int,
    idempotency_key: str,
) -> RepairResult:
    body = {"expected_version": expected_version}
    begun = _begin_command(
        db,
        actor=actor,
        key=idempotency_key,
        action="attest_shop_adjustment",
        record_id=str(record_id),
        body=body,
    )
    if isinstance(begun, RepairResult):
        return begun
    record = _lock_record_for_write(
        db, record_id, actor=actor, as_supervisor=True
    )
    if record.version != expected_version:
        raise RepairConflict("版本冲突：记录已被他人处理，请刷新后重试")
    intent = _intent_for(db, record.record_id)
    if not (
        record.status == "voided"
        and not record.shop_adjusted_manually
        and _missing_operator_deferred(intent)
    ):
        raise RepairConflict("这笔记录不用登记商城减库存")
    record.shop_adjusted_manually = True
    record.version += 1
    db.flush()
    payload = _record_body(db, record, audience="supervisor", include_void_fields=True)
    _remember(
        db,
        actor=actor,
        key=idempotency_key,
        action="attest_shop_adjustment",
        record_id=record.record_id,
        fingerprint=begun,
        status_code=200,
        body=payload,
    )
    return RepairResult(200, payload)


def _search(column, q: str | None):
    if q is None:
        return None
    text_q = q.strip()
    if not text_q:
        return None
    return column.icontains(text_q, autoescape=True)


def _page(page: int, page_size: int, total: int, items: list) -> dict:
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def list_pool(
    db: Session, *, q: str | None, page: int, page_size: int
) -> dict:
    clause = _search(DimPart.pn_std, q)
    description_clause = _search(DimPart.description, q)
    filters = [DimPart.status == "active", v_repair_pool.c.pending_qty > 0]
    if clause is not None and description_clause is not None:
        filters.append(clause | description_clause)
    stmt = (
        select(
            DimPart.id,
            DimPart.pn_std,
            DimPart.description,
            v_repair_pool.c.pending_qty,
        )
        .join(v_repair_pool, v_repair_pool.c.part_id == DimPart.id)
        .where(*filters)
    )
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = db.execute(
        stmt.order_by(DimPart.pn_std, DimPart.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    items = [
        {
            "part_id": part_id,
            "pn": pn,
            "description": _description(description),
            "pending_qty": int(pending_qty),
        }
        for part_id, pn, description, pending_qty in rows
    ]
    return _page(page, page_size, total, items)


def list_records(
    db: Session,
    *,
    actor: str,
    as_supervisor: bool,
    q: str | None,
    status: str | None,
    part_id: int | None,
    page: int,
    page_size: int,
) -> dict:
    filters = []
    if not as_supervisor:
        filters.append(MaintenanceRepairRecord.operator == actor)
    if status is not None:
        filters.append(MaintenanceRepairRecord.status == status)
    if part_id is not None:
        filters.append(MaintenanceRepairRecord.part_id == part_id)
    pn_clause = _search(DimPart.pn_std, q)
    description_clause = _search(DimPart.description, q)
    stmt = select(MaintenanceRepairRecord).join(
        DimPart, DimPart.id == MaintenanceRepairRecord.part_id
    )
    if filters:
        stmt = stmt.where(*filters)
    if pn_clause is not None and description_clause is not None:
        stmt = stmt.where(pn_clause | description_clause)
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = list(
        db.scalars(
            stmt.order_by(
                MaintenanceRepairRecord.created_at.desc(),
                MaintenanceRepairRecord.record_id.desc(),
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
    )
    # 列表不展开来源。主管列表与维修员列表字段相同；范围已在查询里收紧。
    items = [
        _record_body(db, row, audience="repairer", include_void_fields=True)
        for row in rows
    ]
    return _page(page, page_size, total, items)


def get_record(
    db: Session, *, actor: str, as_supervisor: bool, record_id: UUID
) -> dict:
    record = _visible_record(
        db.get(MaintenanceRepairRecord, record_id),
        actor=actor,
        as_supervisor=as_supervisor,
    )
    audience = "supervisor" if as_supervisor else "repairer"
    return _record_body(db, record, audience=audience, include_void_fields=True)


def list_summary(
    db: Session,
    *,
    q: str | None,
    part_ids: list[int] | None,
    page: int,
    page_size: int,
) -> dict:
    if part_ids is not None:
        parts = {
            part.id: part
            for part in db.scalars(select(DimPart).where(DimPart.id.in_(part_ids))).all()
        }
        counts = {
            part_id: (int(pending), int(shop), int(scrap))
            for part_id, pending, shop, scrap in db.execute(
                select(
                    v_repair_pool.c.part_id,
                    v_repair_pool.c.pending_qty,
                    v_repair_pool.c.shop_qty,
                    v_repair_pool.c.scrap_qty,
                ).where(v_repair_pool.c.part_id.in_(part_ids))
            ).all()
        }
        items = []
        for part_id in part_ids:
            part = parts.get(part_id)
            pending, shop, scrap = counts.get(part_id, (0, 0, 0))
            items.append(
                {
                    "part_id": part_id,
                    "pn": part.pn_std if part is not None else None,
                    "description": _description(part.description) if part is not None else None,
                    "pending_qty": pending,
                    "shop_qty": shop,
                    "scrap_qty": scrap,
                }
            )
        return _page(1, len(items), len(items), items)

    filters = [
        DimPart.status == "active",
        (
            (v_repair_pool.c.pending_qty > 0)
            | (v_repair_pool.c.shop_qty > 0)
            | (v_repair_pool.c.scrap_qty > 0)
        ),
    ]
    pn_clause = _search(DimPart.pn_std, q)
    description_clause = _search(DimPart.description, q)
    if pn_clause is not None and description_clause is not None:
        filters.append(pn_clause | description_clause)
    stmt = (
        select(
            DimPart.id,
            DimPart.pn_std,
            DimPart.description,
            v_repair_pool.c.pending_qty,
            v_repair_pool.c.shop_qty,
            v_repair_pool.c.scrap_qty,
        )
        .join(v_repair_pool, v_repair_pool.c.part_id == DimPart.id)
        .where(*filters)
    )
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = db.execute(
        stmt.order_by(DimPart.pn_std, DimPart.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    items = [
        {
            "part_id": part_id,
            "pn": pn,
            "description": _description(description),
            "pending_qty": int(pending),
            "shop_qty": int(shop),
            "scrap_qty": int(scrap),
        }
        for part_id, pn, description, pending, shop, scrap in rows
    ]
    return _page(page, page_size, total, items)


def intent_status(db: Session, record_id: UUID) -> dict:
    record = db.get(MaintenanceRepairRecord, record_id)
    intent = _intent_for(db, record_id)
    if intent is None:
        return {"record_id": str(record_id), "status": "not_queued"}
    payload = intent_payload(
        intent, record, with_record_id=True
    )
    assert payload is not None
    return payload

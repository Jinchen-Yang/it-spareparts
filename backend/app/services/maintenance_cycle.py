"""坏件循环池服务（D-0056）。

所有工位转移经本模块；不写氚云库存 / 前置库 / 采销单。
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.dimensions import DimPart
from app.models.maintenance_cycle import (
    MaintenanceCycleEvent,
    MaintenanceCycleSellIntent,
    MaintenanceCycleUnit,
)
from app.models.maintenance_doc_import import MaintenanceRkdReturnLine


class CycleError(RuntimeError):
    pass


class CycleConflict(CycleError):
    pass


class CycleForbidden(CycleError):
    pass


WORKER_HIDE = frozenset(
    {"vendor_name", "source_receipt_id", "serial_number", "note"}
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _lock_unit(db: Session, unit_id: int) -> MaintenanceCycleUnit:
    row = db.scalar(
        select(MaintenanceCycleUnit)
        .where(MaintenanceCycleUnit.id == unit_id)
        .with_for_update()
    )
    if row is None:
        raise CycleError("循环单位不存在")
    return row


def _require_version(row: MaintenanceCycleUnit, expected: int) -> None:
    if row.version != expected:
        raise CycleConflict(
            f"版本冲突：期望 {expected}，当前 {row.version}"
        )


def _append_event(
    db: Session,
    *,
    unit: MaintenanceCycleUnit,
    event_type: str,
    actor: str,
    payload: dict | None = None,
) -> None:
    db.add(
        MaintenanceCycleEvent(
            unit_id=unit.id,
            event_type=event_type,
            payload=payload or {},
            actor=actor,
        )
    )


def _bump(row: MaintenanceCycleUnit, actor: str) -> None:
    row.version += 1
    row.updated_by = actor
    row.updated_at = _now()


def serialize_unit(row: MaintenanceCycleUnit, *, manage: bool) -> dict:
    data = {
        "id": row.id,
        "unit_uid": str(row.unit_uid),
        "part_id": row.part_id,
        "pn": row.pn,
        "station": row.station,
        "outcome": row.outcome,
        "condition_bucket": row.condition_bucket,
        "intake_condition": row.intake_condition,
        "repair_round": row.repair_round,
        "source_kind": row.source_kind,
        "assignee_username": row.assignee_username,
        "version": row.version,
        "created_by": row.created_by,
        "updated_by": row.updated_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "closed_at": row.closed_at.isoformat() if row.closed_at else None,
    }
    if manage:
        data["vendor_name"] = row.vendor_name
        data["source_receipt_id"] = row.source_receipt_id
        data["serial_number"] = row.serial_number
        data["note"] = row.note
    return data


def _resolve_part(db: Session, *, part_id: int | None, pn: str | None) -> DimPart:
    part: DimPart | None = None
    if part_id is not None:
        part = db.get(DimPart, part_id)
    elif pn is not None and pn.strip():
        part = db.scalar(
            select(DimPart).where(
                DimPart.pn_std == pn.strip(),
                DimPart.status == "active",
            )
        )
    if part is None or part.status != "active":
        raise CycleError("必须绑定有效 part_id / 可解析 PN")
    return part


def handoff_from_receipt(
    db: Session,
    *,
    receipt_id: str,
    qty: int,
    expected_receipt_version: int,
    actor: str,
    serial_numbers: list[str] | None = None,
) -> list[MaintenanceCycleUnit]:
    if not isinstance(qty, int) or isinstance(qty, bool) or qty < 1 or qty > 1000:
        raise CycleError("入池数量必须是 1–1000 的整数")
    receipt = db.scalar(
        select(MaintenanceRkdReturnLine)
        .where(MaintenanceRkdReturnLine.rkd_line_id == receipt_id)
        .with_for_update()
    )
    if receipt is None:
        raise CycleError("返还收货行不存在")
    if receipt.line_status != "active":
        raise CycleError("收货行已作废，不能入池")
    if receipt.version != expected_receipt_version:
        raise CycleConflict(
            f"收货行版本冲突：期望 {expected_receipt_version}，当前 {receipt.version}"
        )
    if receipt.part_id is None:
        raise CycleError("收货行尚未绑定 part_id，不能入池")
    part = db.get(DimPart, receipt.part_id)
    if part is None or part.status != "active":
        raise CycleError("收货行 part_id 无效")

    already = db.scalar(
        select(func.count())
        .select_from(MaintenanceCycleUnit)
        .where(
            MaintenanceCycleUnit.source_receipt_id == receipt_id,
            MaintenanceCycleUnit.station != "closed",
        )
    ) or 0
    # 允许同一收货行分批入池，但不自动按 qty 校验剩余（产品可后续加强）
    _ = already

    serials = list(serial_numbers or [])
    if serials and len(serials) != qty:
        raise CycleError("提供 SN 时数量必须等于 SN 个数")
    if not serials and receipt.serial_numbers and len(receipt.serial_numbers) == int(receipt.qty):
        # 可选：从收货行带 SN 时按件拆；本轮仅当调用方未传 SN 且收货 qty==sn 数时取前 qty 个
        source_sns = [str(s) for s in receipt.serial_numbers]
        if len(source_sns) >= qty:
            serials = source_sns[:qty]

    units: list[MaintenanceCycleUnit] = []
    for index in range(qty):
        unit = MaintenanceCycleUnit(
            unit_uid=str(uuid4()),
            part_id=part.id,
            pn=receipt.pn,
            station="pool",
            intake_condition=receipt.test_result,
            source_kind="receipt",
            source_receipt_id=receipt.rkd_line_id,
            serial_number=serials[index] if index < len(serials) else None,
            created_by=actor,
            updated_by=actor,
        )
        db.add(unit)
        db.flush()
        _append_event(
            db,
            unit=unit,
            event_type="handoff",
            actor=actor,
            payload={
                "receipt_id": receipt.rkd_line_id,
                "intake_condition": receipt.test_result,
            },
        )
        units.append(unit)
    return units


def intake_external(
    db: Session,
    *,
    actor: str,
    qty: int,
    part_id: int | None = None,
    pn: str | None = None,
    note: str | None = None,
    intake_condition: str | None = None,
) -> list[MaintenanceCycleUnit]:
    if not isinstance(qty, int) or isinstance(qty, bool) or qty < 1 or qty > 1000:
        raise CycleError("入池数量必须是 1–1000 的整数")
    part = _resolve_part(db, part_id=part_id, pn=pn)
    units: list[MaintenanceCycleUnit] = []
    for _ in range(qty):
        unit = MaintenanceCycleUnit(
            unit_uid=str(uuid4()),
            part_id=part.id,
            pn=part.pn_std,
            station="pool",
            intake_condition=intake_condition,
            source_kind="external",
            note=(note.strip() or None) if note else None,
            created_by=actor,
            updated_by=actor,
        )
        db.add(unit)
        db.flush()
        _append_event(
            db,
            unit=unit,
            event_type="intake_external",
            actor=actor,
            payload={"note": unit.note},
        )
        units.append(unit)
    return units


def dispatch(
    db: Session,
    *,
    unit_id: int,
    expected_version: int,
    actor: str,
    to: str,
    assignee_username: str | None = None,
    vendor_name: str | None = None,
) -> MaintenanceCycleUnit:
    row = _lock_unit(db, unit_id)
    _require_version(row, expected_version)
    if row.station not in ("pool", "ready"):
        raise CycleError("仅回收池或待终态单位可派工")
    if to not in ("testing", "repairing", "outsource"):
        raise CycleError("派工目标仅支持 testing / repairing / outsource")
    if to == "outsource":
        name = (vendor_name or "").strip()
        if not name:
            raise CycleError("委外必须填写供应商名称")
        row.vendor_name = name
        row.assignee_username = None
    else:
        row.assignee_username = (assignee_username or "").strip() or None
    row.station = to
    if to == "repairing":
        row.repair_round += 1
    _bump(row, actor)
    _append_event(
        db,
        unit=row,
        event_type="dispatch",
        actor=actor,
        payload={
            "to": to,
            "assignee_username": row.assignee_username,
            "has_vendor": bool(row.vendor_name),
        },
    )
    return row


def self_pass(
    db: Session,
    *,
    unit_id: int,
    expected_version: int,
    actor: str,
) -> MaintenanceCycleUnit:
    row = _lock_unit(db, unit_id)
    _require_version(row, expected_version)
    if row.station != "pool":
        raise CycleError("仅回收池可自测通过")
    row.station = "ready"
    row.condition_bucket = "good"
    row.assignee_username = None
    _bump(row, actor)
    _append_event(db, unit=row, event_type="self_pass", actor=actor, payload={})
    return row


def complete_test(
    db: Session,
    *,
    unit_id: int,
    expected_version: int,
    actor: str,
    result: str,
    next_step: str,
) -> MaintenanceCycleUnit:
    row = _lock_unit(db, unit_id)
    _require_version(row, expected_version)
    if row.station != "testing":
        raise CycleError("单位不在检测工位")
    if result == "good":
        if next_step != "ready":
            raise CycleError("测好后只能进入待终态")
        row.station = "ready"
        row.condition_bucket = "good"
        row.assignee_username = None
    elif result == "bad":
        if next_step == "repairing":
            row.station = "repairing"
            row.repair_round += 1
            row.condition_bucket = "bad"
        elif next_step == "scrap":
            return _close_locked(
                db, row=row, actor=actor, outcome="scrap", condition_bucket="bad"
            )
        else:
            raise CycleError("测坏后只能送修或报废")
    else:
        raise CycleError("检测结果仅支持 good / bad")
    _bump(row, actor)
    _append_event(
        db,
        unit=row,
        event_type="complete_test",
        actor=actor,
        payload={"result": result, "next": next_step},
    )
    return row


def complete_repair(
    db: Session,
    *,
    unit_id: int,
    expected_version: int,
    actor: str,
    result: str,
    next_step: str,
) -> MaintenanceCycleUnit:
    row = _lock_unit(db, unit_id)
    _require_version(row, expected_version)
    if row.station != "repairing":
        raise CycleError("单位不在维修工位")
    if result == "fixed":
        if next_step == "testing":
            row.station = "testing"
            row.condition_bucket = None
            row.assignee_username = None
        elif next_step == "ready":
            row.station = "ready"
            row.condition_bucket = "good"
            row.assignee_username = None
        else:
            raise CycleError("修好后只能再测或进入待终态")
    elif result == "unfixed":
        if next_step != "scrap":
            raise CycleError("修不了只能报废")
        return _close_locked(
            db, row=row, actor=actor, outcome="scrap", condition_bucket="bad"
        )
    else:
        raise CycleError("维修结果仅支持 fixed / unfixed")
    _bump(row, actor)
    _append_event(
        db,
        unit=row,
        event_type="complete_repair",
        actor=actor,
        payload={"result": result, "next": next_step},
    )
    return row


def outsource_return(
    db: Session,
    *,
    unit_id: int,
    expected_version: int,
    actor: str,
    next_step: str,
    condition_bucket: str | None = None,
) -> MaintenanceCycleUnit:
    row = _lock_unit(db, unit_id)
    _require_version(row, expected_version)
    if row.station != "outsource":
        raise CycleError("单位不在委外工位")
    if next_step == "testing":
        row.station = "testing"
        row.assignee_username = None
        _bump(row, actor)
    elif next_step == "repairing":
        row.station = "repairing"
        row.repair_round += 1
        row.assignee_username = None
        _bump(row, actor)
    elif next_step in ("to_shop", "to_company", "scrap"):
        bucket = condition_bucket
        if next_step == "to_shop":
            if bucket not in ("good", "bad"):
                raise CycleError("直接去电商必须指定 condition_bucket")
        return _close_locked(
            db, row=row, actor=actor, outcome=next_step, condition_bucket=bucket
        )
    else:
        raise CycleError("委外回来去向不合法")
    _append_event(
        db,
        unit=row,
        event_type="outsource_return",
        actor=actor,
        payload={"next": next_step},
    )
    return row


def close(
    db: Session,
    *,
    unit_id: int,
    expected_version: int,
    actor: str,
    outcome: str,
    condition_bucket: str | None = None,
    note: str | None = None,
) -> MaintenanceCycleUnit:
    row = _lock_unit(db, unit_id)
    _require_version(row, expected_version)
    if row.station not in ("pool", "ready"):
        raise CycleError("仅回收池或待终态可关闭")
    if outcome not in ("to_shop", "to_company", "scrap"):
        raise CycleError("终态仅支持 to_shop / to_company / scrap")
    if note:
        row.note = note.strip() or row.note
    bucket = condition_bucket or row.condition_bucket
    if outcome == "to_shop" and bucket not in ("good", "bad"):
        raise CycleError("去电商必须指定 condition_bucket")
    return _close_locked(
        db, row=row, actor=actor, outcome=outcome, condition_bucket=bucket
    )


def _close_locked(
    db: Session,
    *,
    row: MaintenanceCycleUnit,
    actor: str,
    outcome: str,
    condition_bucket: str | None,
) -> MaintenanceCycleUnit:
    row.station = "closed"
    row.outcome = outcome
    row.condition_bucket = condition_bucket
    row.assignee_username = None
    row.closed_at = _now()
    _bump(row, actor)
    _append_event(
        db,
        unit=row,
        event_type="close",
        actor=actor,
        payload={"outcome": outcome, "condition_bucket": condition_bucket},
    )
    if outcome == "to_shop":
        assert condition_bucket in ("good", "bad")
        existing = db.scalar(
            select(MaintenanceCycleSellIntent).where(
                MaintenanceCycleSellIntent.unit_id == row.id
            )
        )
        if existing is None:
            db.add(
                MaintenanceCycleSellIntent(
                    delivery_id=str(uuid4()),
                    unit_id=row.id,
                    part_id=row.part_id,
                    qty=Decimal(1),
                    condition_bucket=condition_bucket,
                    state="pending",
                )
            )
    return row


def mark_sold(
    db: Session,
    *,
    unit_id: int,
    expected_version: int,
    actor: str,
) -> MaintenanceCycleUnit:
    row = _lock_unit(db, unit_id)
    _require_version(row, expected_version)
    if row.station != "closed" or row.outcome != "to_shop":
        raise CycleError("仅已去电商的单位可标售出")
    row.outcome = "sold"
    _bump(row, actor)
    _append_event(db, unit=row, event_type="mark_sold", actor=actor, payload={})
    return row


def list_units(
    db: Session,
    *,
    stations: list[str] | None = None,
    assignee_username: str | None = None,
    part_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[MaintenanceCycleUnit], int]:
    stmt = select(MaintenanceCycleUnit)
    count_stmt = select(func.count()).select_from(MaintenanceCycleUnit)
    if stations:
        stmt = stmt.where(MaintenanceCycleUnit.station.in_(stations))
        count_stmt = count_stmt.where(MaintenanceCycleUnit.station.in_(stations))
    if assignee_username:
        stmt = stmt.where(MaintenanceCycleUnit.assignee_username == assignee_username)
        count_stmt = count_stmt.where(
            MaintenanceCycleUnit.assignee_username == assignee_username
        )
    if part_id is not None:
        stmt = stmt.where(MaintenanceCycleUnit.part_id == part_id)
        count_stmt = count_stmt.where(MaintenanceCycleUnit.part_id == part_id)
    total = int(db.scalar(count_stmt) or 0)
    rows = list(
        db.scalars(
            stmt.order_by(MaintenanceCycleUnit.id.desc()).offset(offset).limit(limit)
        )
    )
    return rows, total


def wip_by_parts(db: Session, part_ids: list[int]) -> list[dict]:
    ids = list(dict.fromkeys(part_ids))
    if not ids:
        return []
    rows = db.execute(
        select(
            MaintenanceCycleUnit.part_id,
            MaintenanceCycleUnit.station,
            func.count(),
        )
        .where(
            MaintenanceCycleUnit.part_id.in_(ids),
            MaintenanceCycleUnit.station.in_(
                ("testing", "repairing", "outsource", "ready")
            ),
        )
        .group_by(MaintenanceCycleUnit.part_id, MaintenanceCycleUnit.station)
    ).all()
    buckets = {
        pid: {"part_id": pid, "testing": 0, "repairing": 0, "outsource": 0, "ready": 0}
        for pid in ids
    }
    for part_id, station, count in rows:
        if part_id in buckets and station in buckets[part_id]:
            buckets[part_id][station] = int(count)
    return [buckets[pid] for pid in ids]


def list_events(
    db: Session, *, unit_id: int, manage: bool
) -> list[dict]:
    rows = list(
        db.scalars(
            select(MaintenanceCycleEvent)
            .where(MaintenanceCycleEvent.unit_id == unit_id)
            .order_by(MaintenanceCycleEvent.id.asc())
        )
    )
    out = []
    for row in rows:
        payload = dict(row.payload or {})
        if not manage:
            payload.pop("vendor_name", None)
            payload.pop("has_vendor", None)
        out.append(
            {
                "id": row.id,
                "event_type": row.event_type,
                "payload": payload,
                "actor": row.actor,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
        )
    return out

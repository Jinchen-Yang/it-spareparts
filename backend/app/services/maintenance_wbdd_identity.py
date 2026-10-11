"""Reject source-identity rewrites before a WBDD import changes any facts.

The pipeline owns the data-change advisory lock and transaction. Raw IDs are
identities; source line numbers are editable display fields, never identities.
"""
from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.etl.transform import TransformResult
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder

_CHUNK_SIZE = 1000


def _chunks(values: Iterable[str]):
    values = sorted(set(values))
    for offset in range(0, len(values), _CHUNK_SIZE):
        yield values[offset:offset + _CHUNK_SIZE]


def validate_source_identities(
    db: Session, result: TransformResult, source_snapshot: dict,
) -> None:
    """Check only parsed headers/lines that the loader is about to write.

    A blocked source row remains membership evidence, but cannot invent a
    prospective line update here. All checks precede loader mutations; callers
    must roll back the entire import on ImportIntegrityError.
    """
    from app.etl.loader import ImportIntegrityError

    source_orders = {
        row["raw_order_id"]: row for row in source_snapshot.get("orders", [])
    }

    def location(raw_order_id: str) -> str:
        order_no = str(result.orders.get(raw_order_id, {}).get("order_no") or "未编号")
        # Limit output to the document label and data-row positions. Contacts,
        # source row contents, raw IDs, and descriptive fields are never logged.
        order_no = "".join(ch for ch in order_no if ch.isprintable())[:64]
        row_numbers = source_orders.get(raw_order_id, {}).get("row_numbers", [])
        rows = "、".join(str(number) for number in row_numbers[:10]) or "未知"
        if len(row_numbers) > 10:
            rows += "等"
        return f"维保需求单 {order_no}（数据行 {rows}）"

    identity_conflicts = {
        "order_number_conflict", "duplicate_order_number", "line_parent_conflict",
    }
    for raw_order_id, source_order in source_orders.items():
        if identity_conflicts.intersection(source_order.get("blocked_reasons", ())):
            raise ImportIntegrityError(
                f"{location(raw_order_id)}：原件单头或明细身份冲突，请核对原件；本次未导入")

    incoming_numbers: dict[str, str] = {}
    for raw_order_id, header in result.orders.items():
        order_no = header.get("order_no")
        if not order_no:
            continue
        previous = incoming_numbers.setdefault(order_no, raw_order_id)
        if previous != raw_order_id:
            raise ImportIntegrityError(
                f"{location(raw_order_id)}：同一单号对应多个原始单头身份，请核对原件；本次未导入")

    incoming_lines: dict[str, str] = {}
    for line in result.lines:
        line_id = line["raw_line_id"]
        raw_order_id = line["_order_raw_id"]
        previous = incoming_lines.setdefault(line_id, raw_order_id)
        if previous != raw_order_id:
            raise ImportIntegrityError(
                f"{location(raw_order_id)}：同一明细身份出现在不同父单，请核对原件；本次未导入")

    for numbers in _chunks(incoming_numbers):
        existing = db.execute(select(
            FMaintenanceOrder.order_no, FMaintenanceOrder.raw_order_id,
        ).where(FMaintenanceOrder.order_no.in_(numbers)))
        for order_no, existing_raw_id in existing:
            incoming_raw_id = incoming_numbers[order_no]
            if existing_raw_id != incoming_raw_id:
                raise ImportIntegrityError(
                    f"{location(incoming_raw_id)}：单号已存在，但原始单头身份不同，请核对原件；本次未导入")

    for line_ids in _chunks(incoming_lines):
        existing = db.execute(select(
            FMaintenanceLine.raw_line_id, FMaintenanceOrder.raw_order_id,
        ).join(FMaintenanceOrder, FMaintenanceOrder.id == FMaintenanceLine.order_id)
          .where(FMaintenanceLine.raw_line_id.in_(line_ids)))
        for line_id, existing_parent_id in existing:
            incoming_parent_id = incoming_lines[line_id]
            if existing_parent_id != incoming_parent_id:
                raise ImportIntegrityError(
                    f"{location(incoming_parent_id)}：明细原始身份已属于另一张单，请核对原件；本次未导入")

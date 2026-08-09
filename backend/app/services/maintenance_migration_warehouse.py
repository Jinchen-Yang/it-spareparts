"""Integration seam for canonical warehouse facts.

The warehouse adapter slice replaces this fail-closed implementation during the
combined integration.  Keeping the default explicit prevents an isolated cutover
branch from silently treating an empty warehouse feed as complete.
"""

from datetime import date
from typing import Any, Mapping, Sequence

from sqlalchemy.orm import Session


class MaintenanceMigrationWarehouseError(ValueError):
    """Canonical warehouse facts cannot satisfy the migration cutover contract."""


def validate_cutover_inventory_movements(
    movements: Sequence[Mapping[str, Any]], *, cutover_date: date
) -> tuple[Mapping[str, Any], ...]:
    """Fail closed when an adapter emits undated or pre-cutover movements."""

    validated: list[Mapping[str, Any]] = []
    for row in movements:
        movement_id = str(row.get("movement_id") or "").strip()
        if not movement_id:
            raise MaintenanceMigrationWarehouseError("仓库流水缺少稳定编号")
        raw_date = row.get("document_date")
        try:
            document_date = (
                raw_date
                if isinstance(raw_date, date)
                else date.fromisoformat(str(raw_date))
            )
        except (TypeError, ValueError) as exc:
            raise MaintenanceMigrationWarehouseError(
                f"仓库流水 {movement_id} 缺少有效单据日期"
            ) from exc
        if document_date < cutover_date:
            raise MaintenanceMigrationWarehouseError(
                f"仓库流水 {movement_id} 的单据日期早于切换日"
            )
        validated.append(row)
    return tuple(validated)


def load_project_inventory_movements(
    _db: Session, _project_id: str, _cutover_date: date
) -> tuple[Sequence[Mapping[str, Any]], bool]:
    return (), False

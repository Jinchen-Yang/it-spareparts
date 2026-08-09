"""Integration seam for canonical warehouse facts.

The warehouse adapter slice replaces this fail-closed implementation during the
combined integration.  Keeping the default explicit prevents an isolated cutover
branch from silently treating an empty warehouse feed as complete.
"""

from datetime import date
from typing import Any, Mapping, Sequence

from sqlalchemy.orm import Session


def load_project_inventory_movements(
    _db: Session, _project_id: str, _cutover_date: date
) -> tuple[Sequence[Mapping[str, Any]], bool]:
    return (), False

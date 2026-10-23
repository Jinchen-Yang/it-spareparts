"""Append project operation audit entries in the caller's transaction.

Does not flush, commit, acquire locks, or bump workbook revisions. Callers own
those operations. The legacy fallback reason is retained for compatibility.
"""
from sqlalchemy.orm import Session

from app.models.maintenance_project_operations import MaintenanceProjectOperationAudit


def write_project_audit(db: Session, *, project_id: str, entity_type: str,
                 entity_id: str, action: str, operated_by: str,
                 reason: str, before: dict | None = None,
                 after: dict | None = None) -> None:
    """行级审计（行级即可，不做字段级全量 diff）。"""
    db.add(MaintenanceProjectOperationAudit(
        project_id=project_id, entity_type=entity_type, entity_id=str(entity_id),
        action=action, before_json=before, after_json=after,
        reason=reason or "工作簿编辑", operated_by=operated_by,
    ))

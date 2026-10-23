"""Completed workbook receipts and exact-request identity.

Read helpers use the caller session and never commit. HTTP owns current
identity, project, upload and conditional amount authorization.
"""
from __future__ import annotations

import hashlib
from copy import deepcopy
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models.maintenance_project_operations import MaintenanceProjectOperationAudit, MaintenanceProjectWorkbookOperation
from app.services.maintenance_workbook_protocol import V2_PROTOCOL_ID, V2_TEMPLATE_VERSION
from app.services.maintenance_workbook_plans import MasterV2Plan, WorkbookError

def _v2_apply_operation_key(plan: MasterV2Plan, operated_by: str) -> str | None:
    if not plan.export_id or not plan.file_sha256:
        return None
    identity = "|".join([
        plan.project_id, plan.export_id, plan.file_sha256, operated_by,
    ])
    return "master-v2:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()

def _v2_apply_result(
    plan: MasterV2Plan,
    *,
    operated_by: str,
    import_batch_id: str,
    replayed: bool,
    revision_drift: bool = False,
) -> dict:
    return {
        "applied_by": operated_by,
        "import_batch_id": import_batch_id,
        "protocol_id": V2_PROTOCOL_ID,
        "template_version": V2_TEMPLATE_VERSION,
        "project_id": plan.project_id,
        "sheets": list(plan.sheets),
        **plan.summary,
        "summary": plan.summary,
        "site_issue_merge_count": len(plan.site_issue_merges),
        "site_issue_merges": [dict(item) for item in plan.site_issue_merges],
        "will_void_rows": [dict(row) for row in plan.will_void_rows],
        "changes": [dict(item) for item in plan.field_changes],
        "conflicts": [dict(item) for item in plan.conflicts],
        "overridden": [dict(item) for item in plan.overridden],
        "force_takeover": plan.force_takeover,
        "revision_drift": revision_drift,
        "warnings": list(plan.warnings),
        "voided_rows": [dict(item) for item in plan.voided_rows],
        "replayed": replayed,
    }

def _v2_stored_apply_result(
    db: Session, receipt: MaintenanceProjectWorkbookOperation,
) -> tuple[dict, bool] | None:
    # The command evidence is append-only. Pin its transaction timestamp as
    # well as its identity to use the existing project/time audit index.
    audits = list(db.scalars(select(MaintenanceProjectOperationAudit).where(
        MaintenanceProjectOperationAudit.project_id == receipt.project_id,
        MaintenanceProjectOperationAudit.operated_at == receipt.created_at,
        MaintenanceProjectOperationAudit.entity_type == "master_workbook_operation",
        MaintenanceProjectOperationAudit.entity_id == str(receipt.id),
        MaintenanceProjectOperationAudit.action == "apply_completed",
    )))
    if not audits:
        # Historical operations did not save the response. Do not manufacture
        # a supposedly original receipt from today's facts or backfill history.
        return None
    envelope = audits[0].after_json
    if not isinstance(envelope, dict):
        raise WorkbookError("idempotency_conflict", "工作簿原回执格式无效")
    response = envelope.get("response")
    requires_amount = envelope.get("requires_contract_amount_manage")
    if (
        len(audits) != 1
        or audits[0].operated_by != receipt.operated_by
        or envelope.get("schema") != "master_v2_apply_receipt_v1"
        or envelope.get("operation_key") != receipt.operation_key
        or not isinstance(requires_amount, bool)
        or not isinstance(response, dict)
        or response.get("project_id") != receipt.project_id
        or response.get("applied_by") != receipt.operated_by
        or response.get("replayed") is not False
    ):
        raise WorkbookError("idempotency_conflict", "工作簿原回执与应用身份不一致")
    return {**deepcopy(response), "replayed": True}, requires_amount

def get_project_master_v2_replay(
    db: Session, *, project_id: str, file_sha256: str, operated_by: str,
) -> tuple[dict, bool] | None:
    """Read a completed upload before merging it against current facts.

    The HTTP caller must first enforce its current identity/project/upload
    permissions, and recheck amount permission when the returned flag is true.
    Exact bytes are bound to the original successful validation by SHA-256;
    an in-flight first apply remains invisible and follows the normal locks.
    This read neither locks nor commits and never exposes another operator's
    receipt. Operations predating stored responses retain their old path.
    """
    receipts = list(db.scalars(select(MaintenanceProjectWorkbookOperation).where(
        MaintenanceProjectWorkbookOperation.project_id == project_id,
        MaintenanceProjectWorkbookOperation.file_sha256 == file_sha256,
        MaintenanceProjectWorkbookOperation.operated_by == operated_by,
        MaintenanceProjectWorkbookOperation.operation_type == "file_apply",
    )))
    if not receipts:
        return None
    if len(receipts) != 1 or receipts[0].payload_hash != file_sha256:
        raise WorkbookError("idempotency_conflict", "工作簿应用回执与当前请求不一致")
    return _v2_stored_apply_result(db, receipts[0])

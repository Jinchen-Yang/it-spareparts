"""Host-owned identity changes: business facts, catalog audit and durable derived work.

The reusable catalog does not know purchase/sales/maintenance tables. This adapter
owns their coordination; callers own commit and authorization is never inferred
from an actor display name. Lock order: account policy, receipt, DATA_CHANGE,
identity topology, sorted PN parents, business facts, then queue publication.
"""
import hashlib
import json
from typing import Annotated, Literal

from pydantic import Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app import config
from app.models.dimensions import DimPart, PartAlias
from app.models.inquiry import FPartInquiry
from app.models.inventory import Inventory
from app.models.maintenance import FMaintenanceLine
from app.models.master_data import ProductMergeLog
from app.models.purchase import FPurchaseLine
from app.models.sales import FSalesLine
from app.pn_catalog import api, jobs, media
from app.pn_catalog.dto import (
    AuthorizationDecision,
    Authorize,
    CommandContext,
    StrictDTO,
)
from app.pn_catalog.errors import CatalogError
from app.pn_catalog.job_dto import JobLease, JobSpec
from app.services import inventory, maintenance_cost, master_data, merge, profit

PositiveID = Annotated[int, Field(gt=0)]
PartKey = Annotated[str, Field(pattern=r"^[1-9][0-9]{0,18}$")]


class ExpectedPart(StrictDTO):
    identity_revision: int = Field(ge=0)
    profile_revision: int = Field(ge=0)


class MergeIdentity(StrictDTO):
    operation: Literal["merge"] = "merge"
    source_part_id: PositiveID
    target_part_id: PositiveID
    expected: dict[PartKey, ExpectedPart] = Field(min_length=2, max_length=2000)


class UnmergeIdentity(StrictDTO):
    operation: Literal["unmerge"] = "unmerge"
    merge_log_id: PositiveID
    expected: dict[PartKey, ExpectedPart] = Field(min_length=2, max_length=2000)


class ReassignAlias(StrictDTO):
    operation: Literal["alias_reassign"] = "alias_reassign"
    alias_id: PositiveID
    target_part_id: PositiveID
    expected: dict[PartKey, ExpectedPart] = Field(min_length=1, max_length=2000)


class IdentityPreview(StrictDTO):
    operation: Literal["merge", "unmerge", "alias_reassign"]
    source_part_id: PositiveID | None = None
    target_part_id: PositiveID | None = None
    merge_log_id: PositiveID | None = None
    alias_id: PositiveID | None = None


IdentityCommand = MergeIdentity | UnmergeIdentity | ReassignAlias
_FACTS = (("f_purchase_line_ids", FPurchaseLine), ("f_sales_line_ids", FSalesLine),
          ("inventory_ids", Inventory), ("f_part_inquiry_ids", FPartInquiry),
          ("f_maintenance_line_ids", FMaintenanceLine))


def _data_lock(db):
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": config.DATA_CHANGE_ADVISORY_LOCK_KEY})


def _scope(db, command) -> tuple[set[int], int]:
    if command.operation == "merge":
        source, target = command.source_part_id, command.target_part_id
        if source is None or target is None or source == target:
            raise CatalogError("identity.invalid_request", "合并需要两个不同的 PN", status_code=422)
        children = set(db.scalars(select(DimPart.id).where(DimPart.merged_into_id == source)))
        return {source, target} | children, target
    if command.operation == "unmerge":
        log = db.get(ProductMergeLog, command.merge_log_id, populate_existing=True)
        if log is None:
            raise CatalogError("identity.merge_not_found", "合并记录不存在", status_code=404)
        return ({log.source_part_id, log.target_part_id} |
                set((log.affected_records or {}).get("path_compressed_part_ids") or [])), log.target_part_id
    alias = db.get(PartAlias, command.alias_id, populate_existing=True)
    if alias is None or command.target_part_id is None:
        raise CatalogError("identity.alias_not_found", "别名或目标 PN 不存在", status_code=404)
    ids = {command.target_part_id}
    if alias.part_id:
        ids.add(alias.part_id)
    # Historical facts may carry a third PN: they also require governance authority.
    for model in (FPurchaseLine, FSalesLine):
        ids.update(db.scalars(select(model.part_id).where(model.pn_raw == alias.pn_raw,
                                                          model.part_id.is_not(None)).distinct()))
    return ids, command.target_part_id


def _authorize_scope(db, ids, context, authorize, operation=None):
    decisions = {}
    for pid in sorted(int(value) for value in ids):
        identity = authorize(db, context, "manage_identity", pid)
        facts = authorize(db, context, "govern_facts", pid)
        if not isinstance(identity, AuthorizationDecision) or not isinstance(facts, AuthorizationDecision):
            raise CatalogError("auth.forbidden", "无权调整 PN 身份和业务归属", status_code=403)
        decisions[str(pid)] = {"identity": identity.metadata, "facts": facts.metadata}
        if operation is not None:
            action = authorize(db, context, f"identity.{operation}", pid)
            if not isinstance(action, AuthorizationDecision):
                raise CatalogError("auth.unavailable", "身份操作授权不可用", status_code=503)
            decisions[str(pid)]["operation"] = action.metadata
    return decisions


def preview(db: Session, command: IdentityPreview, *, context: CommandContext,
            authorize: Authorize) -> dict:
    """A consistent baseline; no business mutation. Caller releases shared locks at end."""
    initial, target = _scope(db, command)
    _authorize_scope(db, initial, context, authorize, command.operation)
    api.acquire_identity_lock(db, shared=True)
    current, target = _scope(db, command)
    if current != initial:
        raise CatalogError("identity.scope_changed", "关联 PN 已变化，请重新预览", retryable=True)
    expected, parts = {}, []
    states = api.read_states(db, current)
    if set(states) != current:
        raise CatalogError("catalog.part_not_found", "型号不存在", status_code=404)
    for pid, value in sorted(states.items()):
        expected[str(pid)] = {"identity_revision": value["identity_revision"],
                              "profile_revision": value["profile_revision"]}
        parts.append({"part_id": pid, "pn_std": value["identity"]["pn_std"], "status": value["identity"]["status"],
                      "merged_into_id": value["identity"]["merged_into_id"]})
    return {"operation": command.operation, "target_part_id": target,
            "expected": expected, "parts": parts}


def _check_unmerge_ownership(db, merge_log_id):
    log = db.get(ProductMergeLog, merge_log_id, populate_existing=True)
    for key, model in _FACTS + (("part_alias_ids", PartAlias),):
        row_ids = list((log.affected_records or {}).get(key) or [])
        if key == "part_alias_ids" and (log.affected_records or {}).get("identity_alias_inserted_id"):
            row_ids.append(log.affected_records["identity_alias_inserted_id"])
        if row_ids and db.scalar(select(model.id).where(model.id.in_(row_ids),
            model.part_id.is_distinct_from(log.target_part_id)).limit(1)) is not None:
            raise CatalogError("identity.undo_conflict", "合并后的事实归属已变化，不能覆盖后续治理")


def _projection(db, receipt, *, context, authorize, replayed):
    ids = receipt.result["part_ids"]
    _authorize_scope(db, ids, context, authorize, receipt.operation.removeprefix("identity."))
    api.acquire_identity_lock(db, shared=True)
    parts = []
    for pid in sorted(ids):
        if not isinstance(authorize(db, context, "read", pid), AuthorizationDecision):
            raise CatalogError("auth.unavailable", "主数据读取授权不可用", status_code=503)
    states = api.read_states(db, ids)
    for pid, versions in sorted(states.items()):
        parts.append({"part_id": pid, "pn_std": versions["identity"]["pn_std"], "status": versions["identity"]["status"],
            "merged_into_id": versions["identity"]["merged_into_id"], "identity_revision": versions["identity_revision"],
            "profile_revision": versions["profile_revision"]})
    target = next(item for item in parts if item["part_id"] == receipt.part_id)
    return {"data": {"parts": parts, "outcome": receipt.result["outcome"],
                     "recalculation_job": jobs.get_job(db, receipt.result["job_id"]).model_dump(mode="json")},
            "meta": api.receipt_meta(receipt, data_revision=target["identity_revision"],
                identity_revision=target["identity_revision"], replayed=replayed)}


def execute(db: Session, command: IdentityCommand, *, context: CommandContext,
            authorize: Authorize) -> dict:
    if context.actor.provenance.kind == "ai" and not context.confirmed_by:
        raise CatalogError("auth.ai_adoption_required", "身份变更需要人工确认", status_code=403)
    policy = _authorize_scope(db, command.expected, context, authorize, command.operation)
    # Target can be obtained from the immutable merge log before taking locks.
    _, target_id = _scope(db, command)
    handle = api.begin_command(db, context=context, authorize=authorize,
        operation=f"identity.{command.operation}", part_id=target_id,
        resource_type="identity", resource_id=target_id, payload=command)
    if handle.receipt is not None:
        return _projection(db, handle.receipt, context=context, authorize=authorize, replayed=True)
    _data_lock(db)
    api.acquire_identity_lock(db)
    scope, target_id = _scope(db, command)
    if scope != {int(key) for key in command.expected}:
        raise CatalogError("identity.scope_changed", "关联 PN 已变化，请重新预览")
    before = api.capture_states(db, scope)
    if set(before) != scope:
        raise CatalogError("catalog.part_not_found", "型号不存在", status_code=404)
    for key, expected in command.expected.items():
        pid = int(key)
        state = before[pid]
        if (state["identity_revision"] != expected.identity_revision or
                state["profile_revision"] != expected.profile_revision):
            raise CatalogError("identity.version_conflict", "PN 资料或身份已变化，请重新核对",
                details={"part_id": pid, "identity_revision": state["identity_revision"],
                         "profile_revision": state["profile_revision"]})
    by_id = {p.id: p for p in db.scalars(select(DimPart).where(DimPart.id.in_(scope)))}
    gallery_change = None
    if command.operation in {"merge", "unmerge"}:
        media.ensure_identity_gallery_members(db, scope)
    display = (context.actor.display_name or context.actor.principal_id)[:64]
    try:
        if command.operation == "merge":
            outcome = merge.merge_parts_in_transaction(db, by_id[command.source_part_id].pn_std,
                by_id[target_id].pn_std, context.reason, display, catalog_context=context)
            gallery_change = media.merge_gallery_groups(db, command.source_part_id, target_id)
        elif command.operation == "unmerge":
            _check_unmerge_ownership(db, command.merge_log_id)
            merge_log = db.get(ProductMergeLog, command.merge_log_id)
            source_id = merge_log.source_part_id
            source_members = {source_id} | set((merge_log.affected_records or {}).get("path_compressed_part_ids") or [])
            outcome = merge.unmerge_in_transaction(db, command.merge_log_id, display, catalog_context=context)
            gallery_change = media.unmerge_gallery_groups(db, source_id, target_id, source_members)
        else:
            outcome = master_data.review_alias_in_transaction(db, command.alias_id, "reassign",
                by_id[target_id].pn_std, display, catalog_context=context)
    except (merge.MergeError, master_data.AliasError) as exc:
        raise CatalogError("identity.invalid_state", str(exc)) from exc
    if gallery_change is not None:
        outcome = {**outcome, "gallery": gallery_change}
    after = api.capture_states(db, scope)
    versions = {str(pid): after[pid]["identity_revision"] for pid in sorted(scope)}
    revision = after[target_id]["identity_revision"]
    intent = json.dumps([context.actor.principal_id, context.actor.client_id, str(context.request_id)])
    job = jobs.enqueue(db, JobSpec(kind="identity.recalculate", queue="maintenance",
        resource_type="identity", resource_id=str(target_id), part_id=target_id,
        resource_generation=revision, input_revision=revision,
        payload={"identity_revisions": versions, "operation": command.operation},
        deduplication_key="identity:" + hashlib.sha256(intent.encode()).hexdigest()),
        context=context)
    audit = api.record_audit(db, target_id, context=context, action=f"identity.{command.operation}.coordinated",
        changes={"outcome": outcome, "job_id": str(job.job_id), "part_ids": sorted(scope)},
        before_revision=before[target_id]["identity_revision"], after_revision=revision,
        authorization={"parts": policy}, payload_hash=handle.payload_hash,
        revisions={"axis": "identity", "before": {str(pid): before[pid]["identity_revision"] for pid in sorted(scope)},
                   "after": versions})
    receipt = api.finish_command(db, handle, part_id=target_id, applied_revision=revision,
        audit_id=audit.id, result={"resource": "identity", "identity_revision": revision,
        "job_id": str(job.job_id), "part_ids": sorted(scope), "outcome": outcome})
    return _projection(db, receipt, context=context, authorize=authorize, replayed=False)


def project_receipt(db, receipt, *, context, authorize, replayed=True):
    return _projection(db, receipt, context=context, authorize=authorize, replayed=replayed)


def recalculation_handler(authorize: Authorize) -> jobs.JobHandler:
    def verify(db, lease: JobLease):
        original = CommandContext.model_validate_json(json.dumps(lease.context))
        ids = sorted(int(pid) for pid in lease.payload["identity_revisions"])
        _authorize_scope(db, ids, original, authorize, lease.payload.get("operation"))
        _data_lock(db)
        api.acquire_identity_lock(db, shared=True)
        current = api.capture_states(db, ids)
        if any(current[pid]["identity_revision"] != lease.payload["identity_revisions"][str(pid)] for pid in ids):
            return None
        revision = current[lease.part_id]["identity_revision"]
        return revision, revision

    def publish(db, _lease, _prepared):
        return {"profit": profit.recompute(db, commit=False),
                "inventory": inventory.backfill_costs(db, commit=False),
                "maintenance": maintenance_cost.recompute(db, commit=False)}
    return jobs.JobHandler(prepare=lambda _lease: None, verify_resource=verify, publish=publish)

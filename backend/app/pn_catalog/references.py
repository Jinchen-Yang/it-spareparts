"""Bind exact source IDs atomically and resolve them under current authority."""
import hashlib
from uuid import UUID

from sqlalchemy import select, text

from app.config import get_settings
from app.models.dimensions import DimPart
from .dto import CommandContext
from .errors import CatalogError
from .models import PartState
from .persistence import acquire_identity_lock, append_audit, lock_part
from .reference_models import ExternalReference


def _namespace(context, namespace, *, writing=False):
    if context.actor.client_id != "itsp":
        settings = get_settings()
        if context.actor.client_id != settings.pn_shop_client_id or namespace != settings.pn_shop_source_namespace:
            raise CatalogError("auth.forbidden", "客户端不能使用此来源命名空间", status_code=403)
    elif writing and namespace != "itsp":
        raise CatalogError("auth.forbidden", "历史外部编号须经迁移绑定入口处理", status_code=403)


def _lock_reference(db, namespace, source_id):
    raw = hashlib.sha256(f"pn-reference:{namespace}:{source_id}".encode()).digest()[:8]
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": int.from_bytes(raw, "big", signed=True)})


def lock_reference_batch(db, pairs):
    """Migration batch lock order: references, topology, sorted PN, dependent rows."""
    if len(pairs) > 1000:
        raise CatalogError("request.validation_failed", "映射批次超出限制", status_code=422)
    for namespace, source_id in sorted(set(pairs)):
        _lock_reference(db, namespace, source_id)


def effective_reference(db, namespace: str, source_id: UUID) -> ExternalReference | None:
    """Internal migration/read port. The original archived mapping is not rewritten."""
    return db.get(ExternalReference, (namespace, source_id), populate_existing=True)


def prepare_new_reference(db, reference, *, context):
    _namespace(context, reference.namespace, writing=True)
    _lock_reference(db, reference.namespace, reference.source_id)
    if effective_reference(db, reference.namespace, reference.source_id) is not None:
        raise CatalogError("catalog.reference_already_bound", "来源编号已绑定，请按精确编号恢复原创建结果")


def _bind(db, *, namespace, source_id, part_id, identity_revision, decision_ref, context, authorization):
    event = append_audit(db, part_id, context=context, action="reference.bind",
        changes={"external_reference": {"namespace": namespace, "source_id": str(source_id),
                                        "part_id": part_id, "decision_ref": decision_ref}},
        before_revision=0, after_revision=1, authorization=authorization,
        revisions={"axis": "external_reference", "before": {"binding": 0}, "after": {"binding": 1}})
    db.flush()
    binding = ExternalReference(namespace=namespace, source_id=source_id, part_id=part_id,
        identity_revision=identity_revision, version=1, decision_ref=decision_ref, audit_id=event.id)
    db.add(binding)
    db.flush()
    return binding


def bind_created_reference(db, reference, *, part_id, identity_revision, context, authorization):
    return _bind(db, namespace=reference.namespace, source_id=reference.source_id,
        part_id=part_id, identity_revision=identity_revision, decision_ref=f"create:{context.request_id}",
        context=context, authorization=authorization)


def render_receipt(db, receipt, *, context, authorize, replayed):
    from . import api
    value = receipt.result
    api._authorize(db, context, authorize, "read", receipt.part_id)
    result = resolve_references(db, namespace=value["namespace"], source_ids=[UUID(value["source_id"])],
                                context=context, authorize=authorize)
    current = result["data"][0]
    if current["state"] != "bound":
        raise CatalogError("identity.invalid_state", "已提交的来源绑定缺失")
    return {"data": current, "meta": api.receipt_meta(receipt, data_revision=current["binding_version"],
        identity_revision=current["identity_revision"], replayed=replayed)}


def bind_existing_reference(db, part_id, command, *, context, authorize):
    """Create a new immutable source identity for an explicitly selected active PN.

    This permits a Shop zero-stock shell for a PN originally created by ITSP.
    It cannot reassign a source UUID or move any existing business facts.
    """
    from . import api
    _namespace(context, command.namespace, writing=True)
    handle = api.begin_command(db, context=context, authorize=authorize, operation="create",
        part_id=part_id, resource_type="external_reference", resource_id=command.source_id, payload=command)
    api._authorize(db, context, authorize, "read", part_id)
    if handle.receipt is not None:
        return render_receipt(db, handle.receipt, context=context, authorize=authorize, replayed=True)
    if context.if_match_revision is not None:
        raise CatalogError("request.validation_failed", "来源绑定须使用身份版本基线", status_code=422)
    prepare_new_reference(db, command, context=context)
    acquire_identity_lock(db, shared=True)
    part = lock_part(db, part_id, shared=True)
    state = db.get(PartState, part_id, populate_existing=True)
    revision = state.identity_revision if state else 0
    if part.status != "active" or command.expected_identity_revision != revision:
        raise CatalogError("catalog.identity_changed", "型号身份已变化，请重新选择目标")
    binding = _bind(db, namespace=command.namespace, source_id=command.source_id, part_id=part_id,
        identity_revision=revision, decision_ref=f"selected:{context.request_id}", context=context,
        authorization=handle.decision.metadata)
    receipt = api.finish_command(db, handle, part_id=part_id, applied_revision=binding.version,
        audit_id=binding.audit_id, result={"resource": "external_reference", "part_id": part_id,
        "namespace": binding.namespace, "source_id": str(binding.source_id), "identity_revision": revision})
    return render_receipt(db, receipt, context=context, authorize=authorize, replayed=False)


def bind_migration_reference(db, *, namespace, source_id, part_id, expected_identity_revision,
                             decision_ref, context, authorize):
    """Local audited resolution of an unmapped UUID, never implicit reassignment.

    The migration coordinator must acquire sorted reference locks before sorted
    PN locks when binding a batch; one call acquires reference then topology/PN.
    """
    from .migration import _full_archive_access
    from .reference_dto import ExternalReferenceInput
    _full_archive_access(db, context, authorize, "materialize_migration", writing=True)
    if context.actor.entrypoint != "migration" or context.actor.provenance.kind != "migration":
        raise CatalogError("auth.forbidden", "需要本机可信迁移入口", status_code=403)
    ref = ExternalReferenceInput(namespace=namespace, source_id=source_id)
    if not isinstance(decision_ref, str) or not 1 <= len(decision_ref.strip()) <= 256:
        raise CatalogError("request.validation_failed", "需要已核对的映射决定编号", status_code=422)
    _lock_reference(db, ref.namespace, ref.source_id)
    existing = effective_reference(db, ref.namespace, ref.source_id)
    if existing is not None:
        if existing.part_id != part_id:
            raise CatalogError("migration.reference_conflict", "来源编号已绑定其他身份，须独立核对关联事实")
        return existing
    acquire_identity_lock(db, shared=True)
    lock_part(db, part_id, shared=True)
    state = db.get(PartState, part_id, populate_existing=True)
    revision = state.identity_revision if state else 0
    if type(expected_identity_revision) is not int or expected_identity_revision != revision:
        raise CatalogError("identity.version_conflict", "目标 PN 身份已变化，需重新确认映射")
    return _bind(db, namespace=ref.namespace, source_id=ref.source_id, part_id=part_id,
                 identity_revision=revision, decision_ref=decision_ref.strip(), context=context,
                 authorization={"migration": True, "expected_identity_revision": revision})


def resolve_references(db, *, namespace, source_ids, context: CommandContext, authorize):
    _namespace(context, namespace)
    if not 1 <= len(source_ids) <= 100 or any(type(item) is not UUID for item in source_ids):
        raise CatalogError("request.validation_failed", "来源编号列表须为 1–100 个 UUID", status_code=422)
    decision = authorize(db, context, "list", None)
    if decision.metadata.get("scope") != "all":
        raise CatalogError("auth.forbidden", "当前资料范围不支持批量解析", status_code=403)
    acquire_identity_lock(db, shared=True)
    found = {row.source_id: row for row in db.scalars(select(ExternalReference).where(
        ExternalReference.namespace == namespace, ExternalReference.source_id.in_(source_ids)).execution_options(populate_existing=True))}
    # Populate graph in bounded batches; _project then only reads from the identity
    # map. This keeps database work independent of the number of input UUIDs.
    targets = {row.part_id for row in found.values()}
    parts = {}
    for _ in range(64):
        pending = targets - parts.keys()
        if not pending:
            break
        rows = list(db.scalars(select(DimPart).where(DimPart.id.in_(pending)).execution_options(populate_existing=True)))
        parts.update({part.id: part for part in rows})
        targets.update(part.merged_into_id for part in rows if part.status == "merged" and part.merged_into_id)
        if pending - parts.keys():
            raise CatalogError("identity.invalid_state", "关联身份缺失")
    states = {row.part_id: row for row in db.scalars(select(PartState).where(PartState.part_id.in_(parts)).execution_options(populate_existing=True))} if parts else {}
    values = []
    for source_id in source_ids:
        binding = found.get(source_id)
        if binding is None:
            values.append({"namespace": namespace, "source_id": str(source_id), "state": "unresolved"})
            continue
        seen, current = set(), binding.part_id
        while current in parts and parts[current].status == "merged" and current not in seen:
            seen.add(current)
            current = parts[current].merged_into_id
        if current not in parts or current in seen:
            raise CatalogError("identity.invalid_state", "型号关联无效，需核对")
        values.append({"namespace": namespace, "source_id": str(source_id), "state": "bound",
            "part_id": binding.part_id, "canonical_part_id": current, "binding_version": binding.version,
            "binding_identity_revision": binding.identity_revision,
            "identity_revision": states[current].identity_revision if current in states else 0})
    return {"data": values, "meta": {"request_id": str(context.request_id), "replayed": False, "next_cursor": None}}

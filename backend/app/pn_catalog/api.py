"""Public transaction facade for the shared PN catalog.

Every mutation requires a host authorization callback and leaves commit/rollback to
its caller. HTTP, Excel and maintenance jobs therefore share the same DB boundary.
"""
import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.dimensions import DimPart
from .dto import (Authorize, AuthorizationDecision, CATEGORY_FIELDS, CommandContext,
                  CreatePart, PatchProfile, PROFILE_FIELDS, SetAvailability)
from .errors import CatalogError
from .models import AuditEvent, CommandReceipt, PartState
from .persistence import (acquire_creation_locks, acquire_identity_lock, append_audit, blocked_spec_keys, capture_state, capture_states,
                          legacy_context, lock_part, read_versions, record_legacy_change,
                          record_legacy_changes, revision_vector, snapshot, state_for_write)

__all__ = ["get_part", "list_parts", "create_part", "patch_profile", "set_availability",
           "get_receipt", "list_audit_events", "capture_state", "capture_states",
           "record_legacy_change", "record_legacy_changes", "legacy_context",
           "blocked_spec_keys", "read_versions", "acquire_identity_lock", "acquire_creation_locks"]


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _digest(value) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _authorize(db: Session, context: CommandContext, authorize: Authorize,
               action: str, part_id: int | None) -> AuthorizationDecision:
    decision = authorize(db, context, action, part_id)
    if not isinstance(decision, AuthorizationDecision):
        raise CatalogError("auth.unavailable", "权限服务不可用", status_code=503, retryable=True)
    return decision


def _allowed(fields: frozenset[str], path: str) -> bool:
    return "*" in fields or path in fields or (path.startswith("specs.") and "specs.*" in fields)


def _check_fields(decision: AuthorizationDecision, paths) -> None:
    if any(not _allowed(decision.writable_fields, path) for path in paths):
        raise CatalogError("auth.forbidden", "没有修改所选字段的权限", status_code=403)


def _project(value: dict, decision: AuthorizationDecision) -> dict:
    fields = decision.readable_fields
    public = {k: value[k] for k in ("part_id", "pn_std", "identity", "identity_revision",
                                   "profile_revision", "availability", "availability_revision")}
    public["profile"] = {k: v for k, v in value["profile"].items() if _allowed(fields, k)}
    public["specs"] = {k: {**v, "version": value["field_versions"].get(f"specs.{k}", 0), "deleted": False}
                       for k, v in value["specs"].items() if _allowed(fields, f"specs.{k}")}
    for key in value["deleted_specs"]:
        if _allowed(fields, f"specs.{key}"):
            public["specs"][key] = {"value": None, "unit": None, "numeric_value": None,
                                    "source": "manual", "deleted": True,
                                    "version": value["field_versions"].get(f"specs.{key}", 0)}
    public["field_versions"] = {k: v for k, v in value["field_versions"].items() if _allowed(fields, k)}
    public["field_provenance"] = {k: v for k, v in value["field_provenance"].items() if _allowed(fields, k)}
    public["locked_fields"] = [key for key in value["locked_fields"] if _allowed(fields, key)]
    return public


def get_part(db: Session, part_id: int, *, context: CommandContext, authorize: Authorize) -> dict:
    decision = _authorize(db, context, authorize, "read", part_id)
    # SHARE guarantees profile/specs/version come from one logical state under READ COMMITTED.
    part = lock_part(db, part_id, shared=True)
    value = snapshot(db, part)
    return {"data": _project(value, decision), "meta": {
        "request_id": str(context.request_id), "resource_revision": value["profile_revision"],
        "data_revision": value["profile_revision"], "identity_revision": value["identity_revision"],
        "replayed": False}}


def _payload_hash(operation: str, part_id: int | None, command, context: CommandContext) -> str:
    return _digest({"operation": operation, "part_id": part_id,
        "command": command.model_dump(mode="json"), "if_match_revision": context.if_match_revision,
        "reason": context.reason, "provenance": context.actor.provenance.model_dump(mode="json"),
        "confirmed_by": context.confirmed_by, "adopted_from_candidate_id": context.adopted_from_candidate_id})


def _receipt_lock(db: Session, context: CommandContext) -> None:
    key = _json(["pn-catalog-receipt", context.actor.principal_id, context.actor.client_id, str(context.request_id)])
    lock = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big", signed=True)
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock})


def _lookup_receipt(db: Session, context: CommandContext) -> CommandReceipt | None:
    return db.get(CommandReceipt, (context.actor.principal_id, context.actor.client_id, context.request_id),
                  populate_existing=True)


def _response(db: Session, receipt: CommandReceipt, *, context: CommandContext,
              authorize: Authorize, replayed: bool) -> dict:
    decision = _authorize(db, context, authorize, "read", receipt.part_id)
    part = lock_part(db, receipt.part_id, shared=True)
    value = snapshot(db, part)
    resource = receipt.result["resource"]
    data_revision = value["availability_revision"] if resource == "availability" else value["profile_revision"]
    return {"data": _project(value, decision), "meta": {
        "request_id": str(receipt.request_id), "resource_revision": receipt.applied_revision,
        "applied_revision": receipt.applied_revision, "data_revision": data_revision,
        "identity_revision": value["identity_revision"],
        "applied_identity_revision": receipt.result["identity_revision"],
        "audit_id": receipt.audit_id, "replayed": replayed}}


def _replay(db: Session, context: CommandContext, digest: str, authorize: Authorize) -> dict | None:
    _receipt_lock(db, context)
    receipt = _lookup_receipt(db, context)
    if receipt is None:
        return None
    if not hmac.compare_digest(receipt.payload_hash, digest):
        raise CatalogError("request.idempotency_mismatch", "同一请求编号的内容已改变")
    # A create replay now has a real resource, and must recheck its current scope.
    _authorize(db, context, authorize, receipt.operation, receipt.part_id)
    return _response(db, receipt, context=context, authorize=authorize, replayed=True)


def _save_receipt(db: Session, part_id: int, *, context: CommandContext, operation: str,
                  digest: str, audit_id: int, value: dict, resource: str = "profile") -> CommandReceipt:
    revision = value["availability_revision"] if resource == "availability" else value["profile_revision"]
    receipt = CommandReceipt(principal_id=context.actor.principal_id, client_id=context.actor.client_id,
        request_id=context.request_id, operation=operation, part_id=part_id, payload_hash=digest,
        applied_revision=revision, audit_id=audit_id,
        result={"part_id": part_id, "resource": resource, "identity_revision": value["identity_revision"]})
    db.add(receipt)
    db.flush()
    return receipt


def _baseline(context: CommandContext, value: dict, expected_identity: int,
              expected_profile: int | None, field_versions: dict | None, paths) -> None:
    if context.if_match_revision is not None and context.if_match_revision != value["profile_revision"]:
        raise CatalogError("request.precondition_failed", "资料版本已变化", status_code=412)
    if value["identity_revision"] != expected_identity or value["identity"]["status"] != "active":
        raise CatalogError("catalog.identity_changed", "型号身份已变化，请重新确认目标")
    if expected_profile is not None and expected_profile != value["profile_revision"]:
        raise CatalogError("catalog.field_conflict", "资料版本已变化，请核对草稿", details={
            "fields": [], "resolution": "reload_and_confirm"})
    if field_versions is None:
        if expected_profile is None:
            raise CatalogError("request.precondition_required", "需要编辑基线", status_code=428)
        return
    missing = set(paths) - set(field_versions)
    if missing:
        raise CatalogError("request.precondition_required", "缺少所选字段的版本", status_code=428,
                           details={"fields": sorted(missing)})
    conflicts = [{"path": path, "base_revision": field_versions[path],
                  "current_revision": value["field_versions"].get(path, 0)} for path in sorted(paths)
                 if field_versions[path] != value["field_versions"].get(path, 0)]
    if conflicts:
        raise CatalogError("catalog.field_conflict", "所选字段已变化，请核对草稿",
                           details={"fields": conflicts, "resolution": "reload_and_confirm"})


def _manual_specs(command) -> dict:
    from app.services.spec_extract import validate_manual_specs
    try:
        return validate_manual_specs({k: v.model_dump() if v is not None else None
                                      for k, v in command.spec_updates.items()})
    except ValueError as exc:
        raise CatalogError("request.validation_failed", str(exc), status_code=422) from exc


def _apply_fields(db: Session, part: DimPart, state: PartState, changes: dict, specs: dict) -> None:
    from app.services import master_data, spec_extract
    old_description = part.description
    normal = {k: v for k, v in changes.items() if k != "manufacturer"}
    for key, value in normal.items():
        setattr(part, key, value)
    if "manufacturer" in changes:
        state.manufacturer = changes["manufacturer"]
    locked = set(part.locked_fields or []) | set(changes)
    if CATEGORY_FIELDS & set(changes):
        locked.update(CATEGORY_FIELDS)
        part.category_source = "MANUAL"
    part.locked_fields = sorted(locked)
    part.reviewed_at = datetime.now(timezone.utc)
    master_data.bind_part_dictionaries(db, part, normal)
    db.flush()
    if "description" in changes and old_description != part.description:
        spec_extract.refresh_part_specs(db, part.id, part.description, catalog_record=False)
    spec_extract.apply_manual_specs(db, part.id, specs, catalog_record=False)


def _derived_spec_paths(before: dict | None, changes: dict, manual_specs: dict) -> set[str]:
    """Compute the effective extraction delta without changing persistence."""
    if "description" not in changes or (before and before["profile"]["description"] == changes["description"]):
        return set()
    from app.services.spec_extract import extract
    previous = before["specs"] if before else {}
    blocked = set(before["deleted_specs"]) if before else set()
    effective = {key: value for key, value in previous.items() if value["source"] != "auto"}
    for spec in extract(changes["description"]):
        key = spec["spec_key"]
        if key in effective or key in blocked:
            continue
        number = spec.get("numeric_value")
        effective[key] = {"value": spec["spec_value"], "unit": spec["spec_unit"],
                          "numeric_value": format(number.normalize(), "f") if number is not None else None,
                          "source": "auto"}
    for key, spec in manual_specs.items():
        if spec is None:
            effective.pop(key, None)
        else:
            number = spec["numeric_value"]
            effective[key] = {"value": spec["spec_value"], "unit": spec["spec_unit"],
                              "numeric_value": format(number.normalize(), "f") if number is not None else None,
                              "source": "manual"}
    return {f"specs.{key}" for key in previous.keys() | effective.keys() if previous.get(key) != effective.get(key)}


def _check_derived_access(before: dict | None, changes: dict, specs: dict,
                          write_decision: AuthorizationDecision, read_decision: AuthorizationDecision) -> None:
    paths = _derived_spec_paths(before, changes, specs)
    _check_fields(write_decision, paths)
    if any(not _allowed(read_decision.readable_fields, path) for path in paths):
        raise CatalogError("auth.forbidden", "描述变更需要对应派生规格的读取权限", status_code=403)


def _reject_unadopted_ai(context: CommandContext) -> None:
    if context.actor.provenance.kind == "ai":
        raise CatalogError("catalog.candidate_required", "AI 结果必须经候选采用流程生效", status_code=422)


def patch_profile(db: Session, part_id: int, command: PatchProfile, *,
                  context: CommandContext, authorize: Authorize) -> dict:
    decision = _authorize(db, context, authorize, "update_profile", part_id)
    _reject_unadopted_ai(context)
    read_decision = _authorize(db, context, authorize, "read", part_id)
    paths = set(command.changes) | {f"specs.{key}" for key in command.spec_updates}
    if CATEGORY_FIELDS & paths:
        paths.update(CATEGORY_FIELDS)
    _check_fields(decision, paths)
    if any(not _allowed(read_decision.readable_fields, path) for path in paths):
        raise CatalogError("auth.forbidden", "需要读取所选字段的权限", status_code=403)
    digest = _payload_hash("update_profile", part_id, command, context)
    replay = _replay(db, context, digest, authorize)
    if replay is not None:
        return replay
    part = lock_part(db, part_id)
    before = snapshot(db, part)
    _baseline(context, before, command.expected_identity_revision,
              command.expected_profile_revision, command.field_versions, paths)
    specs = _manual_specs(command)
    _check_derived_access(before, command.changes, specs, decision, read_decision)
    state = state_for_write(db, part_id)
    _apply_fields(db, part, state, command.changes, specs)
    result = record_legacy_change(db, part_id, before, context=context, action="update_profile",
        deleted_spec_keys=[k for k, v in specs.items() if v is None],
        restored_spec_keys=[k for k, v in specs.items() if v is not None], force_fields=paths,
        authorization=decision.metadata, payload_hash=digest)
    receipt = _save_receipt(db, part_id, context=context, operation="update_profile", digest=digest,
                           audit_id=result["audit_id"], value=result["snapshot"])
    return _response(db, receipt, context=context, authorize=authorize, replayed=False)


def _candidates(db: Session, pn: str) -> list[dict]:
    from app.services import part_resolver
    from app.services.master_edit import _compact
    compact = _compact(pn)
    query = select(DimPart).where(DimPart.pn_compact == compact, DimPart.status == "active")
    candidates = {p.id: p for p in db.scalars(query).all()} if compact else {}
    # No catch-all fallback: an unavailable duplicate checker cannot imply distinctness.
    if not candidates:
        try:
            matches = part_resolver.resolve(db, pn, limit=5, log_miss=False)
        except Exception as exc:
            raise CatalogError("catalog.unavailable", "型号查重暂不可用", status_code=503, retryable=True) from exc
        names = [m["pn_std"] for m in matches.get("items", [])
                 if m.get("pn_std") and (m.get("score") or 0) >= 0.6]
        if names:
            candidates = {p.id: p for p in db.scalars(select(DimPart).where(DimPart.pn_std.in_(names))).all()}
    return [{"part_id": p.id, "pn_std": p.pn_std,
             "identity_revision": read_versions(db, p.id)["identity_revision"]}
            for p in sorted(candidates.values(), key=lambda p: p.id)][:5]


def _confirmation_basis(command: CreatePart, context: CommandContext, candidates: list) -> dict:
    return {"principal_id": context.actor.principal_id, "client_id": context.actor.client_id,
            "payload": _digest(command.model_dump(mode="json", exclude={"confirmation_token", "confirmation_reason"})),
            "candidates_hash": _digest(candidates)}


def _encode_confirmation(basis: dict, secret: bytes) -> str:
    payload = base64.urlsafe_b64encode(_json({**basis, "expires": int(time.time()) + 600}).encode()).decode().rstrip("=")
    sig = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def _verify_confirmation(token: str, basis: dict, secret: bytes) -> bool:
    try:
        payload, signature = token.split(".")
        expected = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return False
        value = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        expiry = value.pop("expires")
        return type(expiry) is int and expiry > time.time() and value == basis
    except (ValueError, TypeError, KeyError):
        return False


def create_part(db: Session, command: CreatePart, *, context: CommandContext,
                authorize: Authorize, confirmation_secret: bytes | None = None) -> dict:
    decision = _authorize(db, context, authorize, "create", None)
    _reject_unadopted_ai(context)
    paths = set(command.changes) | {f"specs.{key}" for key in command.spec_updates}
    _check_fields(decision, paths)
    digest = _payload_hash("create", None, command, context)
    replay = _replay(db, context, digest, authorize)
    if replay is not None:
        return replay
    if context.if_match_revision is not None:
        raise CatalogError("request.validation_failed", "新建不能指定已有资料的 If-Match", status_code=422)
    acquire_identity_lock(db, shared=True)
    acquire_creation_locks(db, [command.pn_std])
    if db.scalar(select(DimPart.id).where(DimPart.pn_std == command.pn_std)) is not None:
        raise CatalogError("catalog.pn_exists", "型号已存在，请选用已有型号")
    specs = _manual_specs(command)
    _check_derived_access(None, command.changes, specs, decision, decision)
    candidates = _candidates(db, command.pn_std)
    if candidates:
        basis = _confirmation_basis(command, context, candidates)
        valid = (confirmation_secret and command.confirmation_token and command.confirmation_reason
                 and _verify_confirmation(command.confirmation_token, basis, confirmation_secret))
        if not valid:
            # Never disclose descriptions or IDs outside the current read scope.
            visible = []
            for candidate in candidates:
                try:
                    _authorize(db, context, authorize, "read", candidate["part_id"])
                    visible.append(candidate)
                except CatalogError as exc:
                    if exc.status_code not in (403, 404):
                        raise
            details = {"candidates": visible}
            if confirmation_secret:
                details["confirmation_token"] = _encode_confirmation(basis, confirmation_secret)
            raise CatalogError("catalog.possible_duplicate", "存在近似型号，请核对后确认", details=details)
        _authorize(db, context, authorize, "confirm_distinct", None)
    part = DimPart(pn_std=command.pn_std, master_source="manual", locked_fields=[])
    try:
        with db.begin_nested():
            db.add(part)
            db.flush()
    except IntegrityError as exc:
        if getattr(getattr(exc.orig, "diag", None), "constraint_name", None) == "dim_part_pn_std_key":
            raise CatalogError("catalog.pn_exists", "型号已存在，请选用已有型号") from exc
        raise
    state = state_for_write(db, part.id)
    changes = {"machine_or_part": "备件", **command.changes}
    _apply_fields(db, part, state, changes, specs)
    audit_context = context
    if command.confirmation_reason:
        audit_context = context.model_copy(update={"reason": command.confirmation_reason})
    result = record_legacy_change(db, part.id, None, context=audit_context, action="create",
        deleted_spec_keys=[k for k, v in specs.items() if v is None],
        restored_spec_keys=[k for k, v in specs.items() if v is not None], force_fields=changes,
        authorization=decision.metadata, payload_hash=digest)
    receipt = _save_receipt(db, part.id, context=context, operation="create", digest=digest,
                           audit_id=result["audit_id"], value=result["snapshot"])
    return _response(db, receipt, context=context, authorize=authorize, replayed=False)


def set_availability(db: Session, part_id: int, command: SetAvailability, *,
                     context: CommandContext, authorize: Authorize) -> dict:
    decision = _authorize(db, context, authorize, "set_availability", part_id)
    _authorize(db, context, authorize, "read", part_id)
    digest = _payload_hash("set_availability", part_id, command, context)
    replay = _replay(db, context, digest, authorize)
    if replay is not None:
        return replay
    part = lock_part(db, part_id)
    state = state_for_write(db, part_id)
    if state.identity_revision != command.expected_identity_revision or part.status != "active":
        raise CatalogError("catalog.identity_changed", "型号身份已变化")
    if context.if_match_revision is not None and context.if_match_revision != state.availability_revision:
        raise CatalogError("request.precondition_failed", "停用状态已变化", status_code=412)
    if state.availability_revision != command.expected_revision:
        raise CatalogError("catalog.field_conflict", "停用状态已变化")
    before = snapshot(db, part, state)
    old, revision = state.availability, state.availability_revision
    state.availability = command.availability
    state.availability_revision += 1
    event = append_audit(db, part_id, context=context, action="set_availability",
        changes={"availability": {"before": old, "after": state.availability}, "reason": command.reason},
        before_revision=revision, after_revision=state.availability_revision,
        authorization=decision.metadata, payload_hash=digest,
        revisions=revision_vector(before, state, axis="availability"))
    value = snapshot(db, part, state)
    receipt = _save_receipt(db, part_id, context=context, operation="set_availability", digest=digest,
                           audit_id=event.id, value=value, resource="availability")
    return _response(db, receipt, context=context, authorize=authorize, replayed=False)


def get_receipt(db: Session, request_id: UUID, *, context: CommandContext, authorize: Authorize) -> dict:
    receipt = db.get(CommandReceipt, (context.actor.principal_id, context.actor.client_id, request_id))
    if receipt is None:
        raise CatalogError("request.not_found", "请求回执不存在", status_code=404)
    _authorize(db, context, authorize, receipt.operation, receipt.part_id)
    return _response(db, receipt, context=context, authorize=authorize, replayed=True)


def _decode_cursor(cursor: str | None, fingerprint: str) -> int:
    if cursor is None:
        return 0
    try:
        if len(cursor) > 512:
            raise ValueError()
        value = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if set(value) != {"id", "q", "v"} or value["q"] != fingerprint or value["v"] != 1:
            raise ValueError()
        if type(value["id"]) is not int or value["id"] < 0:
            raise ValueError()
        return value["id"]
    except (ValueError, TypeError):
        raise CatalogError("request.validation_failed", "分页游标无效", status_code=422) from None


def _encode_cursor(row_id: int, fingerprint: str) -> str:
    return base64.urlsafe_b64encode(_json({"v": 1, "q": fingerprint, "id": row_id}).encode()).decode().rstrip("=")


def list_parts(db: Session, *, context: CommandContext, authorize: Authorize,
               query: str = "", cursor: str | None = None, limit: int = 50,
               include_inactive: bool = False) -> dict:
    decision = _authorize(db, context, authorize, "list", None)
    # A registered SQL scope adapter is required before narrower row grants can list.
    if decision.metadata.get("scope") != "all":
        raise CatalogError("auth.forbidden", "当前资料范围不支持此查询", status_code=403)
    if include_inactive:
        _authorize(db, context, authorize, "read_inactive", None)
    if not 1 <= limit <= 200 or len(query) > 256:
        raise CatalogError("request.validation_failed", "查询参数超出范围", status_code=422)
    fingerprint = _digest({"query": query, "principal": context.actor.principal_id,
                            "client": context.actor.client_id, "include_inactive": include_inactive})
    after_id = _decode_cursor(cursor, fingerprint)
    columns = [DimPart.id.label("part_id"), DimPart.pn_std,
               func.coalesce(PartState.identity_revision, 0).label("identity_revision"),
               func.coalesce(PartState.profile_revision, 0).label("profile_revision"),
               func.coalesce(PartState.availability, "active").label("availability")]
    if _allowed(decision.readable_fields, "description"):
        columns.append(func.substr(DimPart.description, 1, 240).label("description_preview"))
    for key in ("brand", "category_major", "category_minor"):
        if _allowed(decision.readable_fields, key):
            columns.append(getattr(DimPart, key))
    if _allowed(decision.readable_fields, "manufacturer"):
        columns.append(PartState.manufacturer)
    stmt = select(*columns).select_from(DimPart).outerjoin(PartState, PartState.part_id == DimPart.id).where(DimPart.id > after_id)
    if not include_inactive:
        stmt = stmt.where(DimPart.status == "active", or_(PartState.part_id.is_(None), PartState.availability == "active"))
    if query:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        columns = [DimPart.pn_std, DimPart.pn_compact]
        columns.extend(getattr(DimPart, key) for key in PROFILE_FIELDS - {"manufacturer"}
                       if _allowed(decision.readable_fields, key))
        if _allowed(decision.readable_fields, "manufacturer"):
            columns.append(PartState.manufacturer)
        stmt = stmt.where(or_(*(column.ilike(f"%{escaped}%", escape="\\") for column in columns)))
    # One SQL snapshot, summary-only projection: no full descriptions, specs, aliases or baselines.
    parts = db.execute(stmt.order_by(DimPart.id).limit(limit + 1)).mappings().all()
    visible = parts[:limit]
    values = [dict(row) for row in visible]
    next_cursor = _encode_cursor(visible[-1]["part_id"], fingerprint) if len(parts) > limit else None
    return {"data": values, "meta": {"request_id": str(context.request_id),
            "replayed": False, "next_cursor": next_cursor}}


def list_audit_events(db: Session, part_id: int, *, context: CommandContext, authorize: Authorize,
                      cursor: str | None = None, limit: int = 20) -> dict:
    _authorize(db, context, authorize, "read_audit", part_id)
    decision = _authorize(db, context, authorize, "read", part_id)
    if not 1 <= limit <= 200:
        raise CatalogError("request.validation_failed", "查询参数超出范围", status_code=422)
    fingerprint = _digest({"part_id": part_id, "principal": context.actor.principal_id,
                           "client": context.actor.client_id, "resource": "audit"})
    before_id = _decode_cursor(cursor, fingerprint)
    query = select(AuditEvent).where(AuditEvent.part_id == part_id)
    if before_id:
        query = query.where(AuditEvent.id < before_id)
    rows = db.scalars(query.order_by(AuditEvent.id.desc()).limit(limit + 1)).all()
    items = []
    remaining = 512 * 1024 - 2048  # Reserve envelope/cursor space; byte budget is UTF-8.
    for row in rows[:limit]:
        item = {"audit_id": row.id, "action": row.action, "actor": row.actor,
                "context": row.context, "authorization": row.authorization,
                "request_id": str(row.request_id), "occurred_at": row.occurred_at.isoformat(),
                "before_revision": row.before_revision, "after_revision": row.after_revision,
                "revisions": row.revisions,
                "changes": {key: value for key, value in row.changes.items()
                            if _allowed(decision.readable_fields, key)}}
        size = len(_json(item).encode()) + 1
        if size > remaining:
            if not items:
                raise CatalogError("request.payload_too_large", "单条审计记录超出响应上限", status_code=413,
                                   details={"audit_id": row.id, "max_bytes": 512 * 1024})
            break
        remaining -= size
        items.append(item)
    more = len(rows) > len(items)
    return {"data": items, "meta": {"request_id": str(context.request_id), "replayed": False,
            "next_cursor": _encode_cursor(items[-1]["audit_id"], fingerprint) if more else None}}

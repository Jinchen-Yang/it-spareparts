"""Real PostgreSQL tests for bounded JSON types, key CAS, receipts and current grants."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.db import engine
from app.models.dimensions import DimPart
from app.pn_catalog import api
from app.pn_catalog.attribute_dto import PatchAttributes
from app.pn_catalog.attribute_models import AttributeState, AttributeValue
from app.pn_catalog.dto import ActorContext, AuthorizationDecision, CommandContext, CreatePart, PatchProfile, Provenance
from app.pn_catalog.errors import CatalogError
from app.pn_catalog.models import AuditEvent, CommandReceipt, PartState


def context(**kwargs):
    return CommandContext(actor=ActorContext(principal_id="account:42", client_id="itsp",
        executor_id="itsp-api", entrypoint="itsp_ui", **kwargs), request_id=uuid4())


def allow(*args):
    return AuthorizationDecision(frozenset({"*"}), frozenset({"*"}), {"scope": "all"})


def seed(db):
    part = DimPart(pn_std=f"ATTRIBUTE-{uuid4()}")
    db.add(part)
    db.commit()
    return part.id


def patch(db, pid, changes=None, versions=None, *, ctx=None, authorize=allow, identity=0):
    changes = changes or {"型号参数": {"operation": "set", "value": {"nested": [None, True, 42, 1.25, "文本"]}}}
    command = PatchAttributes(expected_identity_revision=identity,
        field_versions=versions or {key: 0 for key in changes}, changes=changes)
    return api.patch_attributes(db, pid, command, context=ctx or context(), authorize=authorize)


def test_types_null_and_tombstone_preserve_aba_and_provenance(db):
    pid = seed(db)
    first = patch(db, pid)
    assert first["data"]["items"]["型号参数"]["value"] == {"nested": [None, True, 42, 1.25, "文本"]}
    result = patch(db, pid, {"null": {"operation": "set", "value": None}, "deleted": {"operation": "delete"}})
    assert result["data"]["items"]["null"]["value"] is None
    assert not result["data"]["items"]["null"]["deleted"]
    assert result["data"]["items"]["deleted"]["deleted"]
    assert result["data"]["items"]["deleted"]["version"] == 1
    patch(db, pid, {"deleted": {"operation": "set", "value": None}}, {"deleted": 1})
    patch(db, pid, {"deleted": {"operation": "delete"}}, {"deleted": 2})
    with pytest.raises(CatalogError) as exc:
        patch(db, pid, {"deleted": {"operation": "set", "value": "stale"}}, {"deleted": 1})
    assert exc.value.code == "catalog.field_conflict"
    db.commit()
    stored = db.get(AttributeValue, (pid, "deleted"))
    assert stored.version == 3 and stored.deleted and stored.value is None
    assert stored.provenance["confirmed_by"] == "account:42"
    assert db.get(PartState, pid).profile_revision == 0
    assert db.get(PartState, pid).fill_status == "confirmed"


def test_read_only_get_does_not_initialize_tables(db):
    pid = seed(db)
    db.execute(text("SET TRANSACTION READ ONLY"))
    result = api.get_attributes(db, pid, context=context(), authorize=allow)
    assert result["data"]["items"] == {} and result["data"]["revision"] == 0
    assert db.get(AttributeState, pid) is None


def test_shared_receipt_is_current_projection_and_rolls_back_together(db):
    pid = seed(db)
    ctx = context()
    first = patch(db, pid, ctx=ctx)
    replay = patch(db, pid, ctx=ctx)
    assert replay["meta"]["replayed"]
    assert first["meta"]["audit_id"] == replay["meta"]["audit_id"]
    assert api.get_receipt(db, ctx.request_id, context=ctx, authorize=allow)["data"] == first["data"]
    with pytest.raises(CatalogError) as exc:
        patch(db, pid, {"new": {"operation": "set", "value": 1}}, ctx=ctx)
    assert exc.value.code == "request.idempotency_mismatch"
    db.rollback()
    assert db.get(AttributeState, pid) is None
    assert db.get(PartState, pid) is None
    assert db.scalar(select(func.count()).select_from(CommandReceipt)) == 0
    assert db.scalar(select(func.count()).select_from(AuditEvent)) == 0


def test_revoked_group_permission_blocks_write_replay_and_receipt_read(db):
    pid = seed(db)
    ctx = context()
    patch(db, pid, ctx=ctx)
    db.commit()
    def denied(*args):
        return AuthorizationDecision(frozenset({"description"}), frozenset({"description"}))
    for action in (lambda: patch(db, pid, ctx=ctx, authorize=denied),
                   lambda: api.get_receipt(db, ctx.request_id, context=ctx, authorize=denied)):
        with pytest.raises(CatalogError) as exc:
            action()
        assert exc.value.code == "auth.forbidden"
    assert db.get(AttributeState, pid).revision == 1


def test_identity_revalidation_and_nonhuman_write_guard(db):
    pid = seed(db)
    db.add(PartState(part_id=pid, identity_revision=2))
    db.commit()
    with pytest.raises(CatalogError) as exc:
        patch(db, pid)
    assert exc.value.code == "catalog.identity_changed"
    with pytest.raises(CatalogError) as exc:
        patch(db, pid, identity=2, ctx=context(actor_kind="system", provenance=Provenance(kind="ai")))
    assert exc.value.code == "catalog.human_confirmation_required"
    assert db.get(AttributeState, pid) is None


@pytest.mark.parametrize("same_key", [True, False])
def test_concurrent_same_key_conflicts_disjoint_keys_merge(db, same_key):
    pid = seed(db)
    barrier = Barrier(2)
    def writer(i):
        with Session(engine) as session:
            barrier.wait(timeout=5)
            try:
                patch(session, pid, {"same" if same_key else str(i): {"operation": "set", "value": i}})
                session.commit()
                return "ok"
            except CatalogError as exc:
                session.rollback()
                return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(writer, [0, 1]))
    assert outcomes == (["catalog.field_conflict", "ok"] if same_key else ["ok", "ok"])
    assert len(api.get_attributes(db, pid, context=context(), authorize=allow)["data"]["items"]) == (1 if same_key else 2)


def test_independent_pn_does_not_block_and_budgets_apply_to_aggregate(db):
    one, two = seed(db), seed(db)
    with Session(engine) as held:
        patch(held, one)
        with Session(engine) as other:
            other.execute(text("SET LOCAL lock_timeout='600ms'"))
            patch(other, two)
            other.commit()
        held.rollback()
    changes = {str(i): {"operation": "delete"} for i in range(256)}
    patch(db, one, changes)
    with pytest.raises(CatalogError) as exc:
        patch(db, one, {"overflow": {"operation": "set", "value": 1}})
    assert exc.value.code == "catalog.attribute_limit"
    assert db.get(AttributeValue, (one, "overflow")) is None


@pytest.mark.parametrize("value", [float("nan"), float("inf"), {1: "wrong"}, (1,), "x" * 50000,
    [[[[[[[[[[1]]]]]]]]]], [0] * 4097])
def test_json_boundary_rejects_nonjson_or_excessive_values(value):
    with pytest.raises((ValidationError, ValueError)):
        PatchAttributes(expected_identity_revision=0, field_versions={"key": 0},
            changes={"key": {"operation": "set", "value": value}})


def test_baseline_keys_strict_and_set_requires_value():
    for body in ({"field_versions": {"other": 0}, "changes": {"k": {"operation": "delete"}}},
                 {"field_versions": {"k": True}, "changes": {"k": {"operation": "delete"}}},
                 {"field_versions": {"k": 0}, "changes": {"k": {"operation": "set"}}},
                 {"field_versions": {"k": 0}, "changes": {"k": {"operation": "delete", "value": None}}}):
        with pytest.raises(ValidationError):
            PatchAttributes(expected_identity_revision=0, **body)


def test_profile_fill_status_preserves_unknown_then_confirms_only_human_effective_change(db):
    pid = seed(db)
    assert api.get_part(db, pid, context=context(), authorize=allow)["data"]["fill_status"] is None
    db.add(PartState(part_id=pid, fill_status="ai_filled", fill_status_revision=1))
    db.commit()
    before = api.get_part(db, pid, context=context(), authorize=allow)["data"]
    api.patch_profile(db, pid, PatchProfile(expected_identity_revision=0, expected_profile_revision=0,
        changes={"brand": "人工品牌"}), context=context(), authorize=allow)
    current = api.get_part(db, pid, context=context(), authorize=allow)["data"]
    assert before["fill_status"] == "ai_filled" and current["fill_status"] == "confirmed"
    assert current["fill_status_revision"] == 2
    event = db.scalar(select(AuditEvent).where(AuditEvent.part_id == pid))
    assert event.changes["fill_status"] == {"before": "ai_filled", "after": "confirmed"}
    assert event.revisions["after"]["fill_status"] == 2


def test_empty_create_is_pending_and_b1_command_hash_stays_unchanged(db):
    from app.pn_catalog.api import _command_payload
    cmd = CreatePart(pn_std="ATTR-NEW-UNIQUE-X78426")
    assert "fill_status" not in _command_payload(cmd) and "attributes" not in _command_payload(cmd)
    result = api.create_part(db, cmd, context=context(), authorize=allow)
    assert result["data"]["fill_status"] == "pending"


def test_generated_json_contract_is_recursive_in_both_directions():
    import json
    from scripts.export_pn_catalog_openapi import export
    schemas = json.loads(export())["components"]["schemas"]
    json_schemas = {name:value for name,value in schemas.items() if "JsonValue" in name}
    assert json_schemas
    for name,value in json_schemas.items():
        assert {item.get("type") for item in value["anyOf"]} == {
            "object","array","string","integer","number","boolean","null"}
        reference = {"$ref":f"#/components/schemas/{name}"}
        assert next(item for item in value["anyOf"] if item["type"]=="object")["additionalProperties"] == reference
        assert next(item for item in value["anyOf"] if item["type"]=="array")["items"] == reference

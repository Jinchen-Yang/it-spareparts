"""Exact UUID identity survives retries and forbids accidental cross-PN rebinding."""
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError

from app.db import SessionLocal
from app.models.dimensions import DimPart
from app.pn_catalog import api
from app.pn_catalog.dto import AuthorizationDecision, CreatePart, ExternalReferenceInput, Provenance
from app.pn_catalog.errors import CatalogError
from app.pn_catalog.reference_models import ExternalReference
from app.pn_catalog.references import bind_migration_reference, resolve_references
from tests.test_pn_catalog_media import context


def allow(*_args):
    return AuthorizationDecision(readable_fields=frozenset({"*"}), writable_fields=frozenset({"*"}),
                                 metadata={"scope": "all"})


def command(source_id=None, pn="REFERENCE-INITIAL"):
    return CreatePart(pn_std=pn, external_reference=ExternalReferenceInput(namespace="shop", source_id=source_id or uuid4()))


def test_create_binding_is_atomic_replayed_and_namespace_scoped(db):
    ctx, cmd = context(), command()
    result = api.create_part(db, cmd, context=ctx, authorize=allow)
    db.commit()
    pid = result["data"]["part_id"]
    replay = api.create_part(db, cmd, context=ctx, authorize=allow)
    assert replay["meta"]["replayed"] is True
    assert replay["data"]["part_id"] == pid
    assert db.scalar(select(func.count()).select_from(ExternalReference)) == 1
    with pytest.raises(CatalogError) as exc:
        api.create_part(db, cmd, context=context(), authorize=allow)
    assert exc.value.code == "catalog.reference_already_bound"
    db.rollback()
    bad = cmd.model_copy(update={"external_reference": ExternalReferenceInput(namespace="another-shop", source_id=uuid4())})
    with pytest.raises(CatalogError) as exc:
        api.create_part(db, bad, context=context(), authorize=allow)
    assert exc.value.code == "auth.forbidden"
    assert db.scalar(select(func.count()).select_from(DimPart)) == 1
    assert "external_reference" not in api._command_payload(CreatePart(pn_std="OLD-RETRY"))


def test_reference_rollback_leaves_no_orphan(db):
    api.create_part(db, command(), context=context(), authorize=allow)
    db.rollback()
    assert db.scalar(select(func.count()).select_from(ExternalReference)) == 0
    assert db.scalar(select(func.count()).select_from(DimPart)) == 0


def test_two_creation_requests_for_same_uuid_cannot_create_two_pns(db):
    source_id = uuid4()
    def run(pn):
        with SessionLocal() as session:
            try:
                result = api.create_part(session, command(source_id, pn), context=context(), authorize=allow)
                session.commit()
                return result["data"]["part_id"]
            except CatalogError as exc:
                session.rollback()
                return exc.code
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(run, ["DISK-REFERENCE-A", "MOTHERBOARD-REFERENCE-B"]))
    assert len([x for x in results if isinstance(x, int)]) == 1
    assert "catalog.reference_already_bound" in results
    assert db.scalar(select(func.count()).select_from(DimPart)) == 1


def test_exact_lookup_batch_is_bounded_and_keeps_original_identity_after_merge(db):
    cmd = command()
    pid = api.create_part(db, cmd, context=context(), authorize=allow)["data"]["part_id"]
    target = DimPart(pn_std="CANONICAL-TARGET")
    db.add(target)
    db.flush()
    original = db.get(DimPart, pid)
    original.status, original.merged_into_id = "merged", target.id
    db.commit()
    target_id = target.id
    source_ids = [cmd.external_reference.source_id, *[uuid4() for _ in range(99)]]
    statements = []
    def capture(_conn, _cursor, statement, *_rest):
        statements.append(statement)
    event.listen(db.get_bind(), "before_cursor_execute", capture)
    try:
        result = resolve_references(db, namespace="shop", source_ids=source_ids, context=context(), authorize=allow)
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", capture)
    assert len(statements) == 5  # topology lock + bindings + 2 graph levels + revisions
    assert result["data"][0]["part_id"] == pid
    assert result["data"][0]["canonical_part_id"] == target_id
    assert all(row["state"] == "unresolved" for row in result["data"][1:])
    with pytest.raises(CatalogError):
        resolve_references(db, namespace="shop", source_ids=[*source_ids, uuid4()], context=context(), authorize=allow)


def test_migration_requires_current_identity_and_cannot_rebind_existing_uuid(db):
    part = DimPart(pn_std="MIGRATION-REF")
    other = DimPart(pn_std="OTHER-REF")
    db.add_all([part, other])
    db.commit()
    actor = context().actor.model_copy(update={"client_id": "itsp", "entrypoint": "migration", "provenance": Provenance(kind="migration")})
    ctx = context().model_copy(update={"actor": actor})
    source_id = uuid4()
    kwargs = dict(namespace="shop", source_id=source_id, part_id=part.id,
                  expected_identity_revision=1, decision_ref="review:exact-id", context=ctx, authorize=allow)
    with pytest.raises(CatalogError) as exc:
        bind_migration_reference(db, **kwargs)
    assert exc.value.code == "identity.version_conflict"
    binding = bind_migration_reference(db, **{**kwargs, "expected_identity_revision": 0})
    db.commit()
    assert binding.part_id == part.id
    with pytest.raises(CatalogError) as exc:
        bind_migration_reference(db, **{**kwargs, "part_id": other.id, "expected_identity_revision": 0})
    assert exc.value.code == "migration.reference_conflict"
    with pytest.raises(DBAPIError):
        with db.begin_nested():
            db.execute(text("DELETE FROM pn_catalog_external_reference"))

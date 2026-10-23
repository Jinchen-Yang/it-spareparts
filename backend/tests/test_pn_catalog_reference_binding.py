"""Existing exact PN selection creates only an immutable external identity."""
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.db import SessionLocal
from app.models.dimensions import DimPart
from app.pn_catalog import api
from app.pn_catalog.dto import CreatePart
from app.pn_catalog.errors import CatalogError
from app.pn_catalog.models import AuditEvent, PartState
from app.pn_catalog.reference_dto import BindReference
from app.pn_catalog.reference_models import ExternalReference
from app.pn_catalog.references import bind_existing_reference
from tests.test_pn_catalog_media import context
from tests.test_pn_catalog_references import allow


def seed(db, pn="ITSP-FIRST"):
    value = api.create_part(db, CreatePart(pn_std=pn), context=context(), authorize=allow)
    db.commit()
    return value["data"]["part_id"], value["data"]["identity_revision"]


def command(revision):
    return BindReference(namespace="shop", source_id=uuid4(), expected_identity_revision=revision)


def test_existing_binding_replay_is_current_and_actor_audited(db):
    pid, revision = seed(db)
    cmd, ctx = command(revision), context()
    result = bind_existing_reference(db, pid, cmd, context=ctx, authorize=allow)
    db.commit()
    assert result["data"]["part_id"] == pid
    assert result["meta"]["applied_revision"] == 1
    assert db.scalar(select(func.count()).select_from(DimPart)) == 1
    audit = db.get(AuditEvent, result["meta"]["audit_id"])
    assert audit.action == "reference.bind"
    assert audit.actor["principal_id"] == ctx.actor.principal_id
    replay = api.get_receipt(db, ctx.request_id, context=context(), authorize=allow)
    assert replay["data"]["source_id"] == str(cmd.source_id)
    assert replay["meta"]["replayed"]
    with pytest.raises(CatalogError) as exc:
        bind_existing_reference(db, pid, cmd.model_copy(update={"source_id": uuid4()}), context=ctx, authorize=allow)
    assert exc.value.code == "request.idempotency_mismatch"
    def denied(*_args):
        raise CatalogError("auth.forbidden", "revoked", status_code=403)
    with pytest.raises(CatalogError):
        bind_existing_reference(db, pid, cmd, context=ctx, authorize=denied)


def test_binding_identity_baseline_namespace_rollback_and_no_reassignment(db):
    pid, revision = seed(db)
    cmd = command(revision)
    with pytest.raises(CatalogError) as exc:
        bind_existing_reference(db, pid, cmd.model_copy(update={"expected_identity_revision": revision + 1}), context=context(), authorize=allow)
    assert exc.value.code == "catalog.identity_changed"
    db.rollback()
    with pytest.raises(CatalogError) as exc:
        bind_existing_reference(db, pid, cmd.model_copy(update={"namespace": "foreign"}), context=context(), authorize=allow)
    assert exc.value.code == "auth.forbidden"
    db.rollback()
    bind_existing_reference(db, pid, cmd, context=context(), authorize=allow)
    db.rollback()
    assert db.get(ExternalReference, (cmd.namespace, cmd.source_id)) is None
    bind_existing_reference(db, pid, cmd, context=context(), authorize=allow)
    db.commit()
    with pytest.raises(CatalogError) as exc:
        bind_existing_reference(db, pid, cmd, context=context(), authorize=allow)
    assert exc.value.code == "catalog.reference_already_bound"


def test_same_external_uuid_race_between_existing_pns_has_one_winner(db):
    first, revision = seed(db)
    second, _ = seed(db, "OTHER-DISTINCT-TARGET")
    cmd = command(revision)
    def run(pid):
        with SessionLocal() as session:
            try:
                result = bind_existing_reference(session, pid, cmd, context=context(), authorize=allow)
                session.commit()
                return result["data"]["part_id"]
            except CatalogError as exc:
                session.rollback()
                return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(run, [first, second]))
    assert sum(type(v) is int for v in values) == 1
    assert "catalog.reference_already_bound" in values
    assert db.scalar(select(func.count()).select_from(ExternalReference)) == 1


def test_merged_origin_rejected_but_existing_binding_receipt_tracks_canonical(db):
    pid, revision = seed(db)
    target, _ = seed(db, "EXPLICIT-CANONICAL")
    cmd, ctx = command(revision), context()
    bind_existing_reference(db, pid, cmd, context=ctx, authorize=allow)
    db.commit()
    part = db.get(DimPart, pid)
    part.status, part.merged_into_id = "merged", target
    db.get(PartState, pid).identity_revision += 1
    db.commit()
    replay = bind_existing_reference(db, pid, cmd, context=ctx, authorize=allow)
    assert replay["data"]["part_id"] == pid
    assert replay["data"]["canonical_part_id"] == target
    with pytest.raises(CatalogError) as exc:
        bind_existing_reference(db, pid, command(revision), context=context(), authorize=allow)
    assert exc.value.code == "catalog.identity_changed"

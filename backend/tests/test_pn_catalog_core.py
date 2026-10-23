"""The shared facade's behavioral contract, exercised against real PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import engine
from app.models.dimensions import DimPart
from app.pn_catalog import api
from app.pn_catalog.dto import ActorContext, AuthorizationDecision, CommandContext, CreatePart, PatchProfile
from app.pn_catalog.errors import CatalogError
from app.pn_catalog.models import AuditEvent, CommandReceipt


def context(**kwargs):
    return CommandContext(actor=ActorContext(principal_id="account:42", client_id="itsp",
                          executor_id="itsp-api", entrypoint="itsp_ui"),
                          request_id=uuid4(), **kwargs)


def allow(_db, _context, _action, _part_id):
    return AuthorizationDecision(frozenset({"*"}), frozenset({"*"}), {"policy_version": "test-1"})


def seed(db, pn="PN-A", description="A"):
    p = DimPart(pn_std=pn, description=description)
    db.add(p)
    db.commit()
    return p.id


def patch(db, pid, value, *, version=0, ctx=None):
    return api.patch_profile(db, pid, PatchProfile(expected_identity_revision=0,
            field_versions={"description": version}, changes={"description": value}),
            context=ctx or context(), authorize=allow)


def test_aba_is_detected_and_other_fields_remain_editable(db):
    pid = seed(db)
    patch(db, pid, "B")
    db.commit()
    patch(db, pid, "A", version=1)
    db.commit()
    with pytest.raises(CatalogError) as caught:
        patch(db, pid, "C", version=0)
    assert caught.value.code == "catalog.field_conflict"
    db.rollback()
    result = api.patch_profile(db, pid, PatchProfile(expected_identity_revision=0,
            field_versions={"brand": 0}, changes={"brand": "Seagate"}),
            context=context(), authorize=allow)
    assert result["data"]["profile"]["description"] == "A"
    assert result["data"]["field_versions"]["description"] == 2


def test_replay_precedes_stale_precondition_and_permission_is_rechecked(db):
    pid = seed(db)
    ctx = context()
    first = patch(db, pid, "B", ctx=ctx)
    db.commit()
    replay = patch(db, pid, "B", ctx=ctx)
    assert replay["meta"]["replayed"] is True
    assert replay["meta"]["audit_id"] == first["meta"]["audit_id"]
    assert db.scalar(select(func.count()).select_from(AuditEvent)) == 1
    with pytest.raises(CatalogError) as mismatch:
        patch(db, pid, "different", ctx=ctx)
    assert mismatch.value.code == "request.idempotency_mismatch"
    db.rollback()
    def revoked(*args):
        raise CatalogError("auth.forbidden", "revoked", status_code=403)
    with pytest.raises(CatalogError) as denied:
        api.patch_profile(db, pid, PatchProfile(expected_identity_revision=0,
             field_versions={"description": 0}, changes={"description": "B"}),
             context=ctx, authorize=revoked)
    assert denied.value.code == "auth.forbidden"


def test_caller_rollback_removes_change_receipt_and_audit(db):
    pid = seed(db)
    ctx = context()
    patch(db, pid, "never committed", ctx=ctx)
    db.rollback()
    assert db.get(DimPart, pid).description == "A"
    assert db.scalar(select(func.count()).select_from(AuditEvent)) == 0
    assert db.scalar(select(func.count()).select_from(CommandReceipt)) == 0


def test_concurrent_same_field_has_one_winner(db):
    pid = seed(db)
    barrier = Barrier(2)
    def worker(value):
        with Session(engine) as s:
            barrier.wait(timeout=5)
            try:
                result = patch(s, pid, value)
                s.commit()
                return result["data"]["profile"]["description"]
            except CatalogError as exc:
                s.rollback()
                return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(worker, ["B", "C"]))
    assert values.count("catalog.field_conflict") == 1
    assert db.scalar(select(func.count()).select_from(AuditEvent)) == 1


def test_concurrent_same_command_has_one_receipt(db):
    pid = seed(db)
    barrier = Barrier(2)
    ctx = context()
    def worker(_):
        with Session(engine) as s:
            barrier.wait(timeout=5)
            result = patch(s, pid, "B", ctx=ctx)
            s.commit()
            return result["meta"]["replayed"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(worker, [1, 2])) == [False, True]
    assert db.scalar(select(func.count()).select_from(CommandReceipt)) == 1
    assert db.scalar(select(func.count()).select_from(AuditEvent)) == 1


def test_disjoint_parts_do_not_share_a_global_write_lock(db):
    from sqlalchemy import text
    first, second = seed(db, "PN-ONE"), seed(db, "PN-TWO")
    with Session(engine) as held:
        patch(held, first, "held")
        with ThreadPoolExecutor(max_workers=1) as pool:
            def other_part():
                with Session(engine) as worker:
                    worker.execute(text("SET LOCAL lock_timeout = '1s'"))
                    result = patch(worker, second, "independent")
                    worker.commit()
                    return result
            result = pool.submit(other_part).result(timeout=4)
            assert result["data"]["profile"]["description"] == "independent"
        held.rollback()
    db.expire_all()
    assert db.get(DimPart, first).description == "A"


def test_category_is_one_baseline_and_manufacturer_is_explicit(db):
    pid = seed(db)
    with pytest.raises(CatalogError) as missing:
        api.patch_profile(db, pid, PatchProfile(expected_identity_revision=0,
            field_versions={"category_major": 0}, changes={"category_major": "内存"}),
            context=context(), authorize=allow)
    assert missing.value.code == "request.precondition_required"
    db.rollback()
    result = api.patch_profile(db, pid, PatchProfile(expected_identity_revision=0,
        field_versions={"category_major": 0, "category_minor": 0, "manufacturer": 0},
        changes={"category_major": "内存", "manufacturer": "Samsung"}), context=context(), authorize=allow)
    assert result["data"]["field_versions"]["category_minor"] == 1
    assert result["data"]["profile"]["manufacturer"] == "Samsung"
    assert result["data"]["profile"]["brand"] is None


def test_spec_delete_persists_tombstone_and_manual_restore_keeps_version(db):
    pid = seed(db, description="SATA SSD 1TB")
    deleted = api.patch_profile(db, pid, PatchProfile(expected_identity_revision=0,
        field_versions={"specs.capacity": 0}, spec_updates={"capacity": None}),
        context=context(), authorize=allow)
    db.commit()
    assert deleted["data"]["specs"]["capacity"]["deleted"] is True
    patch(db, pid, "SATA SSD 2TB")
    db.commit()
    current = api.get_part(db, pid, context=context(), authorize=allow)["data"]
    assert current["specs"]["capacity"]["deleted"] is True
    assert current["field_versions"]["specs.capacity"] == 1
    restored = api.patch_profile(db, pid, PatchProfile(expected_identity_revision=0,
        field_versions={"specs.capacity": 1}, spec_updates={"capacity": {
            "value": "3TB", "unit": "GB", "numeric_value": "3000"}}),
        context=context(), authorize=allow)
    assert restored["data"]["specs"]["capacity"]["deleted"] is False
    assert restored["data"]["field_versions"]["specs.capacity"] == 2


def test_availability_does_not_change_identity_or_governance(db):
    from app.pn_catalog.dto import SetAvailability
    pid = seed(db)
    part = db.get(DimPart, pid)
    part.is_excluded = True
    db.commit()
    result = api.set_availability(db, pid, SetAvailability(expected_identity_revision=0,
        expected_revision=0, availability="inactive", reason="暂停售用"), context=context(), authorize=allow)
    assert result["data"]["availability"] == "inactive"
    assert result["data"]["profile_revision"] == 0
    assert part.is_excluded is True and part.status == "active"
    def allow_all(*args):
        return AuthorizationDecision(frozenset({"*"}), frozenset({"*"}), {"scope": "all"})
    assert api.list_parts(db, context=context(), authorize=allow_all)["data"] == []
    assert len(api.list_parts(db, context=context(), authorize=allow_all, include_inactive=True)["data"]) == 1


def test_replay_current_projection_and_applied_revision_are_distinct(db):
    pid = seed(db)
    ctx = context()
    patch(db, pid, "secret original", ctx=ctx)
    db.commit()
    patch(db, pid, "newest", version=1)
    db.commit()
    def without_description(*args):
        return AuthorizationDecision(frozenset({"brand"}), frozenset({"*"}))
    result = api.get_receipt(db, ctx.request_id, context=ctx, authorize=without_description)
    assert "description" not in result["data"]["profile"]
    assert "description" not in result["data"]["field_versions"]
    assert result["meta"]["applied_revision"] == 1
    assert result["meta"]["data_revision"] == 2


def test_same_request_uuid_in_different_client_is_separate_command(db):
    pid = seed(db)
    first = context()
    second = first.model_copy(update={"actor": first.actor.model_copy(update={"client_id": "shop"})})
    patch(db, pid, "B", ctx=first)
    api.patch_profile(db, pid, PatchProfile(expected_identity_revision=0,
        field_versions={"brand": 0}, changes={"brand": "vendor"}), context=second, authorize=allow)
    assert db.scalar(select(func.count()).select_from(CommandReceipt)) == 2


def test_if_match_is_bound_to_idempotency_and_checked_after_replay(db):
    pid = seed(db)
    ctx = context(if_match_revision=0)
    patch(db, pid, "B", ctx=ctx)
    db.commit()
    assert patch(db, pid, "B", ctx=ctx)["meta"]["replayed"] is True
    with pytest.raises(CatalogError) as stale:
        patch(db, pid, "C", version=1, ctx=context(if_match_revision=0))
    assert stale.value.code == "request.precondition_failed"


def test_list_filters_hidden_fields_before_search_and_binds_cursor(db):
    first = seed(db, "PN-FIRST", "unreadable-needle")
    seed(db, "PN-SECOND", "public")
    def limited(*args):
        return AuthorizationDecision(frozenset({"brand"}), frozenset(), {"scope": "all"})
    assert api.list_parts(db, context=context(), authorize=limited, query="unreadable-needle")["data"] == []
    page = api.list_parts(db, context=context(), authorize=limited, query="PN", limit=1)
    assert page["data"][0]["part_id"] == first
    assert page["meta"]["next_cursor"]
    next_page = api.list_parts(db, context=context(), authorize=limited, query="PN", limit=1,
                               cursor=page["meta"]["next_cursor"])
    assert len(next_page["data"]) == 1
    with pytest.raises(CatalogError) as invalid:
        api.list_parts(db, context=context(), authorize=limited, query="changed",
                       cursor=page["meta"]["next_cursor"])
    assert invalid.value.code == "request.validation_failed"


def test_narrow_scope_cannot_silently_list_all_parts(db):
    seed(db)
    with pytest.raises(CatalogError) as denied:
        api.list_parts(db, context=context(), authorize=allow)
    assert denied.value.code == "auth.forbidden"


def test_cannot_write_a_field_that_is_currently_unreadable(db):
    pid = seed(db)
    def blind_writer(*args):
        return AuthorizationDecision(frozenset({"brand"}), frozenset({"description"}))
    with pytest.raises(CatalogError) as denied:
        api.patch_profile(db, pid, PatchProfile(expected_identity_revision=0,
            field_versions={"description": 0}, changes={"description": "x"}),
            context=context(), authorize=blind_writer)
    assert denied.value.code == "auth.forbidden"
    assert db.scalar(select(func.count()).select_from(AuditEvent)) == 0


def test_create_and_near_duplicate_confirmation_are_bound_to_actor_and_payload(db):
    secret = b"isolated-test-signing-secret"
    first = api.create_part(db, CreatePart(pn_std="ST8000NM000A", changes={"description": "test"}),
                            context=context(), authorize=allow, confirmation_secret=secret)
    db.commit()
    ctx = context()
    command = CreatePart(pn_std="ST-8000-NM000A", changes={"description": "distinct"})
    with pytest.raises(CatalogError) as duplicate:
        api.create_part(db, command, context=ctx, authorize=allow, confirmation_secret=secret)
    assert duplicate.value.code == "catalog.possible_duplicate"
    token = duplicate.value.details["confirmation_token"]
    db.rollback()
    confirmed = command.model_copy(update={"confirmation_token": token, "confirmation_reason": "实物型号已核对"})
    wrong = ctx.model_copy(update={"actor": ctx.actor.model_copy(update={"principal_id": "account:other"})})
    with pytest.raises(CatalogError) as mismatch:
        api.create_part(db, confirmed, context=wrong, authorize=allow, confirmation_secret=secret)
    assert mismatch.value.code == "catalog.possible_duplicate"
    db.rollback()
    result = api.create_part(db, confirmed, context=ctx, authorize=allow, confirmation_secret=secret)
    assert result["data"]["part_id"] != first["data"]["part_id"]
    assert result["data"]["identity_revision"] == 1
    assert db.get(DimPart, first["data"]["part_id"]).description == "test"


def test_audit_is_append_only_and_audit_pagination_redacts_fields(db):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    pid = seed(db)
    patch(db, pid, "first")
    db.commit()
    patch(db, pid, "second", version=1)
    db.commit()
    def limited(*args):
        return AuthorizationDecision(frozenset({"brand"}), frozenset())
    page = api.list_audit_events(db, pid, context=context(), authorize=limited, limit=1)
    assert page["data"][0]["changes"] == {}
    assert page["meta"]["next_cursor"]
    assert api.list_audit_events(db, pid, context=context(), authorize=limited,
          cursor=page["meta"]["next_cursor"])["data"][0]["changes"] == {}
    with pytest.raises(DBAPIError):
        db.execute(text("UPDATE pn_catalog_audit SET action = 'forged'"))
    db.rollback()
    assert db.scalar(select(func.count()).select_from(AuditEvent)) == 2


def test_batch_protocol_keeps_query_count_bounded_and_tracks_aliases(db):
    from sqlalchemy import event
    from app.models.dimensions import PartAlias
    parts = [DimPart(pn_std=f"BULK-{i}", description="old") for i in range(80)]
    db.add_all(parts)
    db.commit()
    ids = [part.id for part in parts]
    statements = []
    def count(_conn, _cursor, statement, *args):
        statements.append(statement)
    event.listen(engine, "before_cursor_execute", count)
    try:
        before = api.capture_states(db, ids)
        for part in parts:
            part.description = "new"
        db.add(PartAlias(part_id=parts[0].id, pn_raw="BULK-ALIAS", pn_std=parts[0].pn_std,
                         status="active", source="manual", needs_review=False))
        result = api.record_legacy_changes(db, before, context=context(), action="batch-update")
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert len(statements) <= 20, statements
    assert len(result) == 80
    assert result[parts[0].id]["snapshot"]["identity_revision"] == 1
    assert result[parts[-1].id]["snapshot"]["identity_revision"] == 0
    assert db.scalar(select(func.count()).select_from(AuditEvent)) == 80


def test_migration_preserves_legacy_identity_and_refuses_lossy_downgrade(db):
    from alembic import command as alembic_command
    from tests.conftest import _alembic_cfg
    from app.pn_catalog.models import PartState
    pid = seed(db)
    part = db.get(DimPart, pid)
    part.is_excluded = True
    db.commit()
    alembic_command.downgrade(_alembic_cfg(), "d8f6a2c4b9e7")
    alembic_command.upgrade(_alembic_cfg(), "head")
    db.expire_all()
    state = db.get(PartState, pid)
    assert state.profile_revision == state.identity_revision == 0
    assert db.get(DimPart, pid).is_excluded is True
    assert db.get(DimPart, pid).status == "active"
    assert db.scalar(select(func.count()).select_from(AuditEvent)) == 0
    patch(db, pid, "adopted")
    db.commit()
    with pytest.raises(RuntimeError, match="contains adopted data"):
        alembic_command.downgrade(_alembic_cfg(), "d8f6a2c4b9e7")
    db.expire_all()
    assert db.get(DimPart, pid).description == "adopted"
    assert db.scalar(select(func.count()).select_from(CommandReceipt)) == 1


def test_list_is_bounded_summary_in_a_single_query(db):
    from sqlalchemy import event
    seed(db, description="文" * 20000)
    statements = []
    def count(_conn, _cursor, statement, *args):
        statements.append(statement)
    def all_scope(*args):
        return AuthorizationDecision(frozenset({"*"}), frozenset(), {"scope": "all"})
    event.listen(engine, "before_cursor_execute", count)
    try:
        result = api.list_parts(db, context=context(), authorize=all_scope)
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert len(statements) == 1
    assert len(result["data"][0]["description_preview"]) == 240
    assert "specs" not in result["data"][0] and "profile" not in result["data"][0]


def test_identity_only_audit_has_independent_revision_vector(db):
    pid = seed(db)
    before = api.capture_state(db, pid)
    api.record_legacy_change(db, pid, before, context=context(), action="identity.membership",
                             force_identity=True)
    event = db.scalar(select(AuditEvent))
    assert event.before_revision == 0 and event.after_revision == 1
    assert event.revisions == {"axis": "identity", "before": {"identity": 0, "profile": 0, "availability": 0},
                              "after": {"identity": 1, "profile": 0, "availability": 0}}


def test_public_ai_write_requires_candidate_adoption(db):
    from app.pn_catalog.dto import Provenance
    pid = seed(db)
    ctx = context()
    ctx = ctx.model_copy(update={"actor": ctx.actor.model_copy(update={"provenance": Provenance(kind="ai")})})
    with pytest.raises(CatalogError) as rejected:
        patch(db, pid, "unreviewed AI", ctx=ctx)
    assert rejected.value.code == "catalog.candidate_required"
    assert db.scalar(select(func.count()).select_from(AuditEvent)) == 0


def test_audit_byte_budget_paginates_without_skipping_large_events(db):
    import json
    pid = seed(db)
    for version in range(16):
        patch(db, pid, str(version) + "文" * 19000, version=version)
    db.commit()
    cursor, seen = None, []
    while True:
        page = api.list_audit_events(db, pid, context=context(), authorize=allow, cursor=cursor, limit=200)
        assert len(json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode()) <= 512 * 1024
        seen.extend(item["audit_id"] for item in page["data"])
        cursor = page["meta"]["next_cursor"]
        if cursor is None:
            break
    expected = list(db.scalars(select(AuditEvent.id).order_by(AuditEvent.id.desc())).all())
    assert seen == expected
    assert len(seen) == 16


def test_description_update_cannot_change_ungranted_derived_specs(db):
    from app.models.master_data import ProductSpec
    pid = seed(db, description="SSD SATA 1TB")
    db.add(ProductSpec(part_id=pid, spec_key="capacity", spec_value="1TB", spec_unit="GB",
                       numeric_value=1000, source="auto"))
    db.commit()
    def description_only(*args):
        return AuthorizationDecision(frozenset({"description"}), frozenset({"description"}))
    with pytest.raises(CatalogError) as forbidden:
        api.patch_profile(db, pid, PatchProfile(expected_identity_revision=0,
            field_versions={"description": 0}, changes={"description": "SSD SATA 2TB"}),
            context=context(), authorize=description_only)
    assert forbidden.value.code == "auth.forbidden"
    db.rollback()
    assert db.get(DimPart, pid).description == "SSD SATA 1TB"
    assert db.scalar(select(ProductSpec.spec_value).where(ProductSpec.part_id == pid)) == "1TB"
    assert db.scalar(select(func.count()).select_from(AuditEvent)) == 0


def test_create_cannot_publish_ungranted_auto_specs(db):
    def description_only(*args):
        return AuthorizationDecision(frozenset({"description"}), frozenset({"description"}))
    with pytest.raises(CatalogError) as forbidden:
        api.create_part(db, CreatePart(pn_std="NEW-SSD", changes={"description": "SSD SATA 1TB"}),
                        context=context(), authorize=description_only)
    assert forbidden.value.code == "auth.forbidden"
    assert db.scalar(select(func.count()).select_from(DimPart)) == 0


def test_shared_and_legacy_creation_use_the_same_duplicate_gate(db, monkeypatch):
    from threading import Event
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    from app.services import master_edit
    checked, release = Event(), Event()
    candidates = api._candidates
    def hold_after_check(session, pn):
        result = candidates(session, pn)
        checked.set()
        assert release.wait(10)
        return result
    monkeypatch.setattr(api, "_candidates", hold_after_check)
    def shared_create():
        with Session(engine) as worker:
            result = api.create_part(worker, CreatePart(pn_std="RACE-987-Z"),
                                     context=context(), authorize=allow)
            worker.commit()
            return result
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(shared_create)
        assert checked.wait(5)
        try:
            db.execute(text("SET LOCAL lock_timeout = '250ms'"))
            with pytest.raises(DBAPIError) as waiting:
                master_edit.create_part_in_transaction(db, pn_std="RACE987Z")
            assert getattr(waiting.value.orig, "sqlstate", None) == "55P03"
            db.rollback()
        finally:
            release.set()
        assert future.result(timeout=5)["data"]["pn_std"] == "RACE-987-Z"
    legacy = master_edit.create_part_in_transaction(db, pn_std="RACE987Z")
    assert legacy["created"] is False
    assert legacy["near_duplicates"]
    assert db.scalar(select(func.count()).select_from(DimPart)) == 1


def test_write_snapshot_refreshes_specs_already_loaded_in_the_caller_session(db):
    from app.models.master_data import ProductSpec
    pid = seed(db)
    spec = ProductSpec(part_id=pid, spec_key="capacity", spec_value="1TB", spec_unit="GB",
                       numeric_value=1000, source="auto")
    db.add(spec)
    db.commit()
    assert spec.spec_value == "1TB"  # Keep a populated ORM instance alive across core bulk SQL.
    result = api.patch_profile(db, pid, PatchProfile(expected_identity_revision=0,
        field_versions={"specs.capacity": 0}, spec_updates={"capacity": {
            "value": "2TB", "unit": "GB", "numeric_value": "2000"}}), context=context(), authorize=allow)
    assert result["data"]["specs"]["capacity"]["value"] == "2TB"
    assert result["data"]["specs"]["capacity"]["source"] == "manual"
    event = db.scalar(select(AuditEvent))
    assert event.changes["specs.capacity"]["before"]["value"] == "1TB"
    assert event.changes["specs.capacity"]["after"]["value"] == "2TB"

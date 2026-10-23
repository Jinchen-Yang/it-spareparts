"""Real PostgreSQL content/candidate transactions, concurrency and source provenance."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.db import engine
from app.models.dimensions import DimPart
from app.pn_catalog import api
from app.pn_catalog.content_dto import (AdoptCandidate, CandidateProvenance, CreateContentCandidate,
    CreateProfileCandidate, LegacyEffectiveContent, PatchContent, RejectCandidate)
from app.pn_catalog.content_models import ConditionContent, ContentCandidate
from app.pn_catalog.dto import ActorContext, AuthorizationDecision, CommandContext, Provenance
from app.pn_catalog.errors import CatalogError
from app.pn_catalog.models import AuditEvent, CommandReceipt, PartState


def context(**kwargs):
    return CommandContext(actor=ActorContext(principal_id="account:42", client_id="itsp",
        executor_id="itsp-api", entrypoint="itsp_ui"), request_id=uuid4(), **kwargs)


def allow(*args):
    return AuthorizationDecision(frozenset({"*"}), frozenset({"*"}), {"policy_version": "test"})


def seed(db, pn="CONTENT-1"):
    part = DimPart(pn_std=pn)
    db.add(part)
    db.commit()
    return part.id


def patch(db, pid, grade="good", changes=None, version=0, ctx=None, authorize=allow):
    return api.patch_content(db, pid, grade, PatchContent(expected_identity_revision=0,
        expected_revision=version, changes=changes or {"description": "通用说明"}),
        context=ctx or context(), authorize=authorize)


def proposal(db, pid, *, resource="content", grade="good", changes=None, provenance=None, base=0):
    body = {"resource": resource, "part_id": pid, "expected_identity_revision": 0,
            "base_revision": base, "changes": changes or {"title": "AI 标题", "description": "AI 通用说明"},
            "provenance": provenance or CandidateProvenance(kind="ai", provider="local", model="model1",
                                                            run_id="run1", template_version="v1")}
    command = (CreateContentCandidate(grade=grade, **body) if resource == "content" else CreateProfileCandidate(**body))
    result = api.create_candidate(db, command, context=context(), authorize=allow)
    db.commit()
    return UUID(result["data"]["id"])


def adoption(db, cid, *, fields=None, candidate_revision=1, target_revision=0, ctx=None, authorize=allow):
    return api.adopt_candidate(db, cid, AdoptCandidate(expected_candidate_revision=candidate_revision,
        expected_identity_revision=0, expected_target_revision=target_revision,
        fields=fields or ["description"]), context=ctx or context(), authorize=authorize)


def test_manual_content_is_per_grade_and_does_not_touch_profile(db):
    pid = seed(db)
    good = patch(db, pid)
    bad = patch(db, pid, "bad", {"description": "坏件通用说明"})
    assert good["data"]["revision"] == bad["data"]["revision"] == 1
    assert good["data"]["status"] == "confirmed"
    assert bad["data"]["description"] == "坏件通用说明"
    assert api.read_versions(db, pid)["profile_revision"] == 0
    assert good["data"]["field_provenance"]["description"]["confirmed_by"] == "account:42"


def test_content_replay_cross_command_key_and_caller_rollback(db):
    pid = seed(db)
    ctx = context()
    patch(db, pid, ctx=ctx)
    replay = patch(db, pid, ctx=ctx)
    assert replay["meta"]["replayed"]
    assert db.scalar(select(func.count()).select_from(AuditEvent)) == 1
    with pytest.raises(CatalogError, match="同一请求"):
        patch(db, pid, "bad", ctx=ctx)
    db.rollback()
    assert db.get(ConditionContent, (pid, "good")) is None
    assert db.scalar(select(func.count()).select_from(CommandReceipt)) == 0
    assert db.scalar(select(func.count()).select_from(AuditEvent)) == 0


def test_good_and_bad_writes_do_not_block_each_other(db):
    pid = seed(db)
    with Session(engine) as held:
        patch(held, pid, "good")
        def independent():
            with Session(engine) as other:
                other.execute(text("SET LOCAL lock_timeout = '600ms'"))
                result = patch(other, pid, "bad")
                other.commit()
                return result
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(independent).result(timeout=5)["data"]["revision"] == 1
        held.rollback()


def test_same_grade_concurrency_has_one_winner(db):
    pid = seed(db)
    barrier = Barrier(2)
    def writer(value):
        with Session(engine) as session:
            barrier.wait(timeout=5)
            try:
                patch(session, pid, changes={"description": value})
                session.commit()
                return "ok"
            except CatalogError as exc:
                session.rollback()
                return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(writer, ["first", "second"])) == ["catalog.field_conflict", "ok"]


def test_partial_adoption_then_rejection_keeps_effective_content_and_origin(db):
    pid = seed(db)
    cid = proposal(db, pid)
    response = adoption(db, cid)
    assert response["data"]["status"] == "partially_adopted"
    assert response["data"]["adopted_fields"] == ["description"]
    content = api.get_content(db, pid, "good", context=context(), authorize=allow)["data"]
    assert content["description"] == "AI 通用说明"
    assert content["title"] is None
    origin = content["field_provenance"]["description"]
    assert origin["kind"] == "ai" and origin["model"] == "model1"
    assert origin["confirmed_by"] == "account:42"
    assert origin["adopted_from_candidate_id"] == str(cid)
    rejected = api.reject_candidate(db, cid, RejectCandidate(expected_candidate_revision=2, reason="标题不合适"),
                                    context=context(), authorize=allow)
    assert rejected["data"]["status"] == "rejected"
    assert db.get(ConditionContent, (pid, "good")).description == "AI 通用说明"
    assert len(rejected["data"]["decisions"]) == 2


def test_double_adoption_replay_and_new_key_conflict(db):
    pid = seed(db)
    cid = proposal(db, pid, changes={"description": "candidate"})
    ctx = context()
    first = adoption(db, cid, ctx=ctx)
    db.commit()
    assert adoption(db, cid, ctx=ctx)["meta"]["audit_id"] == first["meta"]["audit_id"]
    assert api.get_receipt(db, ctx.request_id, context=ctx, authorize=allow)["data"]["status"] == "adopted"
    with pytest.raises(CatalogError) as failure:
        adoption(db, cid)
    assert failure.value.code == "catalog.candidate_conflict"
    assert db.get(ConditionContent, (pid, "good")).revision == 1


def test_concurrent_adopt_and_reject_only_one_decision(db):
    pid = seed(db)
    cid = proposal(db, pid, changes={"description": "candidate"})
    barrier = Barrier(2)
    def decide(kind):
        with Session(engine) as session:
            barrier.wait(timeout=5)
            try:
                if kind == "adopt":
                    adoption(session, cid)
                else:
                    api.reject_candidate(session, cid, RejectCandidate(expected_candidate_revision=1, reason="reject"),
                                         context=context(), authorize=allow)
                session.commit()
                return "ok"
            except CatalogError as exc:
                session.rollback()
                return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(decide, ["adopt", "reject"])) == ["catalog.candidate_conflict", "ok"]
    assert len(db.get(ContentCandidate, cid).decisions) == 1


def test_candidate_cannot_cross_identity_change_or_stale_effective_content(db):
    pid = seed(db)
    cid = proposal(db, pid, changes={"description": "candidate"})
    patch(db, pid, changes={"description": "human protected"})
    db.commit()
    with pytest.raises(CatalogError) as stale:
        adoption(db, cid)
    assert stale.value.code == "catalog.field_conflict"
    db.rollback()
    db.add(PartState(part_id=pid, identity_revision=1))
    db.commit()
    with pytest.raises(CatalogError) as identity:
        adoption(db, cid, target_revision=1)
    assert identity.value.code == "catalog.identity_changed"
    assert db.get(ConditionContent, (pid, "good")).description == "human protected"


def test_current_authorization_required_for_adoption_replay_and_receipt(db):
    pid = seed(db)
    cid = proposal(db, pid, changes={"description": "candidate"})
    ctx = context()
    adoption(db, cid, ctx=ctx)
    db.commit()
    def revoked(_db, _ctx, action, _pid):
        if action == "adopt_candidate":
            raise CatalogError("auth.forbidden", "revoked", status_code=403)
        return allow()
    with pytest.raises(CatalogError, match="revoked"):
        adoption(db, cid, ctx=ctx, authorize=revoked)
    with pytest.raises(CatalogError, match="revoked"):
        api.get_receipt(db, ctx.request_id, context=ctx, authorize=revoked)
    def title_only(*args):
        return AuthorizationDecision(frozenset({"content.good.title"}), frozenset({"content.good.title"}))
    receipt = api.get_receipt(db, ctx.request_id, context=ctx, authorize=title_only)
    assert receipt["data"]["proposal"]["changes"] == {}
    assert receipt["data"]["adopted_fields"] == []


def test_adoption_requires_actual_human_and_effective_field_grants(db):
    pid = seed(db)
    cid = proposal(db, pid, changes={"description": "candidate"})
    ctx = context()
    robot = ctx.model_copy(update={"actor": ctx.actor.model_copy(update={"actor_kind": "system"})})
    with pytest.raises(CatalogError) as failure:
        adoption(db, cid, ctx=robot)
    assert failure.value.code == "catalog.human_confirmation_required"
    def candidate_only(_db, _ctx, action, _pid):
        return AuthorizationDecision(frozenset({"*"}), frozenset() if action == "update_content" else frozenset({"*"}))
    with pytest.raises(CatalogError) as denied:
        adoption(db, cid, authorize=candidate_only)
    assert denied.value.code == "auth.forbidden"
    assert db.get(ConditionContent, (pid, "good")) is None


def test_profile_candidate_adoption_records_source_without_public_ai_bypass(db):
    pid = seed(db)
    cid = proposal(db, pid, resource="profile", changes={"manufacturer": "Vendor from sheet"},
        provenance=CandidateProvenance(kind="excel", file_sha256="a" * 64, sheet="sheet1", row=2))
    adoption(db, cid, fields=["manufacturer"])
    data = api.get_part(db, pid, context=context(), authorize=allow)["data"]
    assert data["profile"]["manufacturer"] == "Vendor from sheet"
    assert data["field_provenance"]["manufacturer"]["kind"] == "excel"
    assert data["field_provenance"]["manufacturer"]["confirmed_by"] == "account:42"
    assert db.scalar(select(func.count()).select_from(CommandReceipt)) == 2


def test_adoption_rollback_clears_effective_candidate_decision_and_receipt(db):
    pid = seed(db)
    cid = proposal(db, pid, changes={"description": "candidate"})
    adoption(db, cid)
    db.rollback()
    assert db.get(ConditionContent, (pid, "good")) is None
    assert db.get(ContentCandidate, cid).status == "pending"
    assert db.scalar(select(func.count()).select_from(CommandReceipt)) == 1
    assert db.scalar(select(func.count()).select_from(AuditEvent)) == 1


def test_historical_ai_import_preserves_origin_and_title_edit_is_not_confirmation(db):
    pid = seed(db)
    ctx = context()
    command = LegacyEffectiveContent(grade="good", expected_identity_revision=0,
        source_namespace="shop", source_record_id="legacy-good-1", source_revision="5",
        title="old title", description="old AI description", status="ai_filled",
        provenance=Provenance(kind="ai", model="legacy-model"))
    original = api.migrate_effective_content(db, pid, command, context=ctx, authorize=allow)
    assert original["data"]["status"] == "ai_filled"
    historical = original["data"]["field_provenance"]["description"]
    assert historical["confirmed_by"] is None and historical["principal_id"] is None
    assert original["data"]["protected_fields"] == []
    db.commit()
    edited = patch(db, pid, changes={"title": "human title"}, version=1)
    assert edited["data"]["status"] == "ai_filled"
    assert edited["data"]["field_provenance"]["description"] == historical
    assert api.migrate_effective_content(db, pid, command, context=ctx, authorize=allow)["meta"]["replayed"]
    with pytest.raises(CatalogError) as duplicated:
        api.migrate_effective_content(db, pid, command, context=context(), authorize=allow)
    assert duplicated.value.code == "migration.source_already_imported"


def test_candidate_query_cursor_is_scoped_and_complete(db):
    pid = seed(db)
    ids = {proposal(db, pid, changes={"title": f"title{i}"}) for i in range(3)}
    ctx, found, cursor = context(), set(), None
    while True:
        result = api.list_candidates(db, pid, context=ctx, authorize=allow, cursor=cursor, limit=1)
        found.update(UUID(item["id"]) for item in result["data"])
        cursor = result["meta"]["next_cursor"]
        if cursor is None:
            break
    assert found == ids
    first = api.list_candidates(db, pid, context=ctx, authorize=allow, limit=1)
    with pytest.raises(CatalogError):
        api.list_candidates(db, pid, context=ctx, authorize=allow, cursor=first["meta"]["next_cursor"], status="rejected")


def test_content_receipt_projects_current_revision_with_original_applied_revision(db):
    pid = seed(db)
    ctx = context()
    first = patch(db, pid, ctx=ctx)
    db.commit()
    patch(db, pid, version=1, changes={"description": "new current"})
    db.commit()
    current = api.get_receipt(db, ctx.request_id, context=ctx, authorize=allow)
    assert current["data"]["description"] == "new current"
    assert current["meta"]["applied_revision"] == first["meta"]["applied_revision"] == 1
    assert current["meta"]["data_revision"] == 2
    def title_only(*args):
        return AuthorizationDecision(frozenset({"content.good.title"}), frozenset({"*"}))
    hidden = api.get_receipt(db, ctx.request_id, context=ctx, authorize=title_only)["data"]
    assert "description" not in hidden and "status" not in hidden
    assert hidden["field_provenance"] == {}


def test_profile_ai_adoption_enforces_actual_derived_spec_permissions(db):
    pid = seed(db)
    cid = proposal(db, pid, resource="profile", changes={"description": "64GB DDR4 memory"})
    def description_only(*args):
        return AuthorizationDecision(frozenset({"description"}), frozenset({"description"}))
    with pytest.raises(CatalogError) as denied:
        adoption(db, cid, authorize=description_only)
    assert denied.value.code == "auth.forbidden"
    db.rollback()
    assert db.get(ContentCandidate, cid).status == "pending"
    assert db.get(DimPart, pid).description is None
    adoption(db, cid)
    result = api.get_part(db, pid, context=context(), authorize=allow)["data"]
    assert result["field_provenance"]["description"]["kind"] == "ai"
    assert result["field_provenance"]["description"]["adopted_from_candidate_id"] == str(cid)
    assert result["specs"]["capacity"]["value"] == "64GB"


def test_content_import_requires_empty_target_and_preserves_unknown_confirming_actor(db):
    pid = seed(db)
    command = LegacyEffectiveContent(grade="bad", expected_identity_revision=0,
        source_namespace="shop", source_record_id="legacy-bad", source_revision="1",
        title=None, description="old human description", status="confirmed",
        provenance=Provenance(kind="unknown"), protected_fields=["description"])
    result = api.migrate_effective_content(db, pid, command, context=context(), authorize=allow)
    origin = result["data"]["field_provenance"]["description"]
    assert result["data"]["status"] == "confirmed" and origin["confirmed_by"] is None
    assert result["data"]["protected_fields"] == ["description"]
    db.commit()
    other = command.model_copy(update={"source_record_id": "another-source"})
    with pytest.raises(CatalogError) as blocked:
        api.migrate_effective_content(db, pid, other, context=context(), authorize=allow)
    assert blocked.value.code == "catalog.field_conflict"
    db.rollback()
    assert db.get(ConditionContent, (pid, "bad")).legacy_source_record_id == "legacy-bad"


def test_concurrent_duplicate_adoption_same_key_has_one_effective_write(db):
    pid = seed(db)
    cid = proposal(db, pid, changes={"description": "AI candidate"})
    ctx, barrier = context(), Barrier(2)
    def adopt(_):
        with Session(engine) as session:
            barrier.wait(timeout=5)
            result = adoption(session, cid, ctx=ctx)
            session.commit()
            return result["meta"]["replayed"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(adopt, [1, 2])) == [False, True]
    assert db.get(ConditionContent, (pid, "good")).revision == 1
    assert db.scalar(select(func.count()).select_from(CommandReceipt)) == 2
    assert db.scalar(select(func.count()).select_from(AuditEvent)) == 3


def test_candidate_if_match_fences_candidate_not_target_revision(db):
    pid = seed(db)
    cid = proposal(db, pid, changes={"description": "candidate"})
    ctx = context(if_match_revision=1)
    applied = adoption(db, cid, ctx=ctx)
    assert applied["data"]["revision"] == 2  # Target started at 0, candidate at 1.
    db.commit()
    assert adoption(db, cid, ctx=ctx)["meta"]["replayed"]
    other = proposal(db, pid, changes={"title": "another"}, base=1)
    with pytest.raises(CatalogError) as failed:
        api.reject_candidate(db, other, RejectCandidate(expected_candidate_revision=1, reason="reject"),
                             context=context(if_match_revision=0), authorize=allow)
    assert failed.value.status_code == 412


def test_candidate_evidence_cannot_be_rewritten_after_confirmation(db):
    from sqlalchemy.exc import DBAPIError
    pid = seed(db)
    cid = proposal(db, pid, changes={"description": "original source evidence"})
    adoption(db, cid)
    db.commit()
    candidate = db.get(ContentCandidate, cid)
    candidate.proposal = {"changes": {"description": "rewritten history"}}
    with pytest.raises(DBAPIError, match="source evidence is immutable"):
        db.flush()
    db.rollback()
    assert db.get(ContentCandidate, cid).proposal["changes"]["description"] == "original source evidence"

"""Host identity coordination: authorization, stale previews and durable derived work."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db import engine
from app.etl import loader
from app.models.dimensions import DimPart, PartAlias
from app.models.master_data import ProductMergeLog
from app.models.purchase import FPurchaseLine
from app.models.system import SysImportBatch
from app.pn_catalog import api, jobs
from app.pn_catalog.dto import ActorContext, AuthorizationDecision, CommandContext, PatchProfile
from app.pn_catalog.errors import CatalogError
from app.pn_catalog.job_models import CatalogJob
from app.pn_catalog.models import AuditEvent, CommandReceipt
from app.pn_catalog.media_models import GalleryGroup, GalleryMember
from app.services import pn_catalog_identity as identity
from tests import factories as f


def context():
    return CommandContext(actor=ActorContext(principal_id="account:42", client_id="itsp",
        executor_id="itsp-api", entrypoint="itsp_ui", display_name="tester"), request_id=uuid4())


def allow(*_args):
    return AuthorizationDecision(frozenset({"*"}), frozenset({"*"}), {"policy_version": "test"})


@pytest.fixture
def parts(db):
    batch = SysImportBatch(filename="identity.xlsx", file_type="purchase", file_hash="test-hash")
    db.add(batch)
    db.flush()
    loader.load(db, f.purchase_result({"O1": f.purchase_head("O1")},
        [f.purchase_line("O1", f"L{n}", pn, qty="2", price="50")
         for n, pn in enumerate(("PN-A", "PN-B", "PN-C"))]), batch.id, date(2026, 6, 1))
    db.commit()
    return {p.pn_std: p.id for p in db.scalars(select(DimPart))}


def command(db, a, b):
    baseline = identity.preview(db, identity.IdentityPreview(operation="merge", source_part_id=a,
        target_part_id=b), context=context(), authorize=allow)
    db.commit()
    return identity.MergeIdentity(source_part_id=a, target_part_id=b,
        expected={key: identity.ExpectedPart(**value) for key, value in baseline["expected"].items()})


def test_merge_receipt_audit_job_and_facts_commit_together(db, parts):
    ctx = context()
    cmd = command(db, parts["PN-A"], parts["PN-B"])
    result = identity.execute(db, cmd, context=ctx, authorize=allow)
    db.commit()
    assert result["data"]["recalculation_job"]["status"] == "pending"
    assert db.get(DimPart, parts["PN-A"]).merged_into_id == parts["PN-B"]
    assert db.scalar(select(FPurchaseLine.part_id).where(FPurchaseLine.pn_raw == "PN-A")) == parts["PN-B"]
    receipt = db.scalar(select(CommandReceipt))
    audit = db.get(AuditEvent, receipt.audit_id)
    assert audit.principal_id == "account:42" and audit.authorization["parts"]
    assert audit.changes["job_id"] == result["data"]["recalculation_job"]["job_id"]
    assert audit.changes["outcome"]["gallery"]["moved_part_ids"] == [parts["PN-A"]]
    assert db.get(GalleryMember, parts["PN-A"]).group_id == db.get(GalleryMember, parts["PN-B"]).group_id
    replay = identity.execute(db, cmd, context=ctx, authorize=allow)
    assert replay["meta"]["replayed"] is True
    assert db.scalar(select(func.count()).select_from(CatalogJob)) == 1
    assert db.scalar(select(func.count()).select_from(ProductMergeLog)) == 1


def test_unmerge_rejects_old_cycle_after_same_pair_is_merged_again(db, parts):
    a, b = parts["PN-A"], parts["PN-B"]
    first = identity.execute(db, command(db, a, b), context=context(), authorize=allow)
    db.commit()
    first_id = first["data"]["outcome"]["merge_log_id"]
    preview = identity.preview(db, identity.IdentityPreview(operation="unmerge", merge_log_id=first_id),
        context=context(), authorize=allow)
    first_undo = identity.UnmergeIdentity(merge_log_id=first_id, expected=preview["expected"])
    first_undo_context = context()
    identity.execute(db, first_undo, context=first_undo_context, authorize=allow)
    db.commit()
    state = api.read_versions(db, a)
    api.patch_profile(db, a, PatchProfile(expected_identity_revision=state["identity_revision"],
        field_versions={"description": state["field_versions"].get("description", 0)},
        changes={"description": "cycle two source"}), context=context(), authorize=allow)
    db.commit()
    second = identity.execute(db, command(db, a, b), context=context(), authorize=allow)
    db.commit()
    second_id = second["data"]["outcome"]["merge_log_id"]
    assert second_id != first_id
    latest = identity.preview(db, identity.IdentityPreview(operation="unmerge", merge_log_id=second_id),
        context=context(), authorize=allow)
    db.commit()
    with pytest.raises(CatalogError) as old_preview:
        identity.preview(db, identity.IdentityPreview(operation="unmerge", merge_log_id=first_id),
            context=context(), authorize=allow)
    assert old_preview.value.code == "identity.undo_conflict"
    db.rollback()
    counts = [db.scalar(select(func.count()).select_from(model)) for model in (CatalogJob, AuditEvent)]
    with pytest.raises(CatalogError) as old_command:
        identity.execute(db, identity.UnmergeIdentity(merge_log_id=first_id, expected=latest["expected"]),
            context=context(), authorize=allow)
    assert old_command.value.code == "identity.undo_conflict"
    db.rollback()
    assert [db.scalar(select(func.count()).select_from(model)) for model in (CatalogJob, AuditEvent)] == counts
    assert db.get(DimPart, a).status == "merged"
    assert db.scalar(select(FPurchaseLine.part_id).where(FPurchaseLine.pn_raw == "PN-A")) == b
    with pytest.raises(identity.merge.MergeError, match="后续合并替代"):
        identity.merge.unmerge_in_transaction(db, first_id, "tester")
    db.rollback()
    replay = identity.execute(db, first_undo, context=first_undo_context, authorize=allow)
    assert replay["meta"]["replayed"] is True
    identity.execute(db, identity.UnmergeIdentity(merge_log_id=second_id, expected=latest["expected"]),
        context=context(), authorize=allow)
    db.commit()
    assert db.get(DimPart, a).description == "cycle two source"
    assert db.get(DimPart, a).status == "active"


def test_rollback_and_enqueue_failure_leave_no_partial_merge(db, parts, monkeypatch):
    a, b = parts["PN-A"], parts["PN-B"]
    cmd = command(db, a, b)
    count = db.scalar(select(func.count()).select_from(AuditEvent))
    identity.execute(db, cmd, context=context(), authorize=allow)
    db.rollback()
    assert db.get(DimPart, a).status == "active"
    assert db.scalar(select(func.count()).select_from(AuditEvent)) == count
    assert db.scalar(select(func.count()).select_from(CatalogJob)) == 0
    def failed(*args, **kwargs):
        raise RuntimeError("queue insert unavailable")
    monkeypatch.setattr(jobs, "enqueue", failed)
    with pytest.raises(RuntimeError):
        identity.execute(db, cmd, context=context(), authorize=allow)
    db.rollback()
    assert db.get(DimPart, a).status == "active"
    assert db.scalar(select(func.count()).select_from(ProductMergeLog)) == 0
    assert db.scalar(select(func.count()).select_from(CommandReceipt)) == 0
    assert db.scalar(select(func.count()).select_from(GalleryGroup)) == 0


def test_stale_profile_even_restored_value_rejects_identity_change(db, parts):
    a, b = parts["PN-A"], parts["PN-B"]
    cmd = command(db, a, b)
    version = api.read_versions(db, b)["field_versions"].get("description", 0)
    for offset, value in enumerate(("temporary", None)):
        api.patch_profile(db, b, PatchProfile(expected_identity_revision=cmd.expected[str(b)].identity_revision,
            field_versions={"description": version + offset}, changes={"description": value}),
            context=context(), authorize=allow)
        db.commit()
    with pytest.raises(CatalogError) as caught:
        identity.execute(db, cmd, context=context(), authorize=allow)
    assert caught.value.code == "identity.version_conflict"
    db.rollback()
    assert db.get(DimPart, a).status == "active"


def test_permissions_intersection_and_replay_revocation(db, parts):
    cmd = command(db, parts["PN-A"], parts["PN-B"])
    ctx = context()
    def no_facts(db, context, action, pid):
        if action == "govern_facts" and pid == parts["PN-A"]:
            raise CatalogError("auth.forbidden", "denied", status_code=403)
        return allow()
    with pytest.raises(CatalogError):
        identity.execute(db, cmd, context=ctx, authorize=no_facts)
    db.rollback()
    identity.execute(db, cmd, context=ctx, authorize=allow)
    db.commit()
    with pytest.raises(CatalogError):
        identity.execute(db, cmd, context=ctx, authorize=no_facts)
    db.rollback()
    assert db.scalar(select(func.count()).select_from(CatalogJob)) == 1


def test_merge_action_on_source_is_required_even_when_target_and_fact_grants_allow(db, parts):
    cmd = command(db, parts["PN-A"], parts["PN-B"])
    def deny_source(db, context, action, pid):
        if action == "identity.merge" and pid == parts["PN-A"]:
            raise CatalogError("auth.forbidden", "source merge denied", status_code=403)
        return allow()
    with pytest.raises(CatalogError) as denied:
        identity.execute(db, cmd, context=context(), authorize=deny_source)
    assert denied.value.code == "auth.forbidden"
    db.rollback()
    assert db.get(DimPart, parts["PN-A"]).status == "active"
    assert db.scalar(select(func.count()).select_from(CatalogJob)) == 0


def test_concurrent_same_source_merge_has_one_winner(db, parts):
    commands = [command(db, parts["PN-A"], parts[target]) for target in ("PN-B", "PN-C")]
    barrier = Barrier(2)
    def worker(cmd):
        with Session(engine) as session:
            barrier.wait(timeout=5)
            try:
                identity.execute(session, cmd, context=context(), authorize=allow)
                session.commit()
                return "ok"
            except CatalogError as exc:
                session.rollback()
                return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(worker, commands))
    assert outcomes.count("ok") == 1
    assert outcomes.count("identity.version_conflict") == 1
    assert db.scalar(select(func.count()).select_from(CatalogJob)) == 1


def test_merge_scope_includes_compressed_children(db, parts):
    a, b, c = (parts[pn] for pn in ("PN-A", "PN-B", "PN-C"))
    identity.execute(db, command(db, a, b), context=context(), authorize=allow)
    db.commit()
    complete = command(db, b, c)
    assert set(complete.expected) == {str(a), str(b), str(c)}
    incomplete = complete.model_copy(update={"expected": {k: v for k, v in complete.expected.items() if k != str(a)}})
    with pytest.raises(CatalogError) as caught:
        identity.execute(db, incomplete, context=context(), authorize=allow)
    assert caught.value.code == "identity.scope_changed"


def test_unmerge_refuses_to_overwrite_later_fact_reassignment(db, parts):
    a, b, c = (parts[pn] for pn in ("PN-A", "PN-B", "PN-C"))
    result = identity.execute(db, command(db, a, b), context=context(), authorize=allow)
    db.commit()
    log_id = result["data"]["outcome"]["merge_log_id"]
    line = db.scalar(select(FPurchaseLine).where(FPurchaseLine.pn_raw == "PN-A"))
    line.part_id = c
    db.commit()
    baseline = identity.preview(db, identity.IdentityPreview(operation="unmerge", merge_log_id=log_id),
        context=context(), authorize=allow)
    db.commit()
    cmd = identity.UnmergeIdentity(merge_log_id=log_id,
        expected={k: identity.ExpectedPart(**v) for k, v in baseline["expected"].items()})
    with pytest.raises(CatalogError) as caught:
        identity.execute(db, cmd, context=context(), authorize=allow)
    assert caught.value.code == "identity.undo_conflict"
    db.rollback()
    assert db.get(FPurchaseLine, line.id).part_id == c


def test_alias_reassignment_and_unmerge_queue_recalculation(db, parts):
    a, b = parts["PN-A"], parts["PN-B"]
    alias = PartAlias(pn_raw="raw alias", pn_std="PN-A", part_id=a, source="auto", status="pending")
    db.add(alias)
    db.commit()
    baseline = identity.preview(db, identity.IdentityPreview(operation="alias_reassign", alias_id=alias.id,
        target_part_id=b), context=context(), authorize=allow)
    db.commit()
    result = identity.execute(db, identity.ReassignAlias(alias_id=alias.id, target_part_id=b,
        expected={k: identity.ExpectedPart(**v) for k, v in baseline["expected"].items()}),
        context=context(), authorize=allow)
    db.commit()
    assert result["data"]["outcome"]["action"] == "reassign"
    assert db.get(PartAlias, alias.id).part_id == b
    merged = identity.execute(db, command(db, a, b), context=context(), authorize=allow)
    db.commit()
    log = merged["data"]["outcome"]["merge_log_id"]
    baseline = identity.preview(db, identity.IdentityPreview(operation="unmerge", merge_log_id=log),
        context=context(), authorize=allow)
    db.commit()
    identity.execute(db, identity.UnmergeIdentity(merge_log_id=log,
        expected={k: identity.ExpectedPart(**v) for k, v in baseline["expected"].items()}),
        context=context(), authorize=allow)
    db.commit()
    assert db.get(DimPart, a).status == "active"
    assert db.scalar(select(func.count()).select_from(CatalogJob)) == 3
    assert db.get(GalleryMember, a).group_id != db.get(GalleryMember, b).group_id


def test_derived_recalculation_executes_and_stale_job_never_publishes(db, parts, monkeypatch):
    a, b, c = (parts[pn] for pn in ("PN-A", "PN-B", "PN-C"))
    identity.execute(db, command(db, a, b), context=context(), authorize=allow)
    db.commit()
    result = jobs.run_once(sessionmaker(engine), worker_id="identity:1", queues=["maintenance"],
        handlers={"identity.recalculate": identity.recalculation_handler(allow)}, lease_seconds=600)
    assert result.status == "succeeded"
    assert set(result.result) == {"profit", "inventory", "maintenance"}
    identity.execute(db, command(db, b, c), context=context(), authorize=allow)
    db.commit()
    lease = jobs.claim(db, worker_id="identity:old", queues=["maintenance"], lease_seconds=600)
    db.commit()
    # A newer unmerge changes the generation; old work must not publish.
    log = db.scalar(select(ProductMergeLog).where(ProductMergeLog.source_part_id == b))
    baseline = identity.preview(db, identity.IdentityPreview(operation="unmerge", merge_log_id=log.id),
        context=context(), authorize=allow)
    db.commit()
    identity.execute(db, identity.UnmergeIdentity(merge_log_id=log.id,
        expected={k: identity.ExpectedPart(**v) for k, v in baseline["expected"].items()}),
        context=context(), authorize=allow)
    db.commit()
    handler = identity.recalculation_handler(allow)
    result = jobs.complete(db, lease, verify_resource=handler.verify_resource,
        publish=lambda *_: pytest.fail("stale identity worker published"))
    assert result.status == "stale"


def test_unmerge_never_deletes_inserted_alias_after_later_reassignment(db, parts):
    from sqlalchemy import delete
    a, b, c = (parts[pn] for pn in ("PN-A", "PN-B", "PN-C"))
    db.execute(delete(PartAlias).where(PartAlias.part_id == a))
    db.commit()
    result = identity.execute(db, command(db, a, b), context=context(), authorize=allow)
    db.commit()
    log_id = result["data"]["outcome"]["merge_log_id"]
    alias = db.scalar(select(PartAlias).where(PartAlias.pn_raw == "PN-A"))
    alias.part_id, alias.pn_std = c, "PN-C"
    db.commit()
    baseline = identity.preview(db, identity.IdentityPreview(operation="unmerge", merge_log_id=log_id),
        context=context(), authorize=allow)
    db.commit()
    with pytest.raises(CatalogError) as caught:
        identity.execute(db, identity.UnmergeIdentity(merge_log_id=log_id,
            expected={k: identity.ExpectedPart(**v) for k, v in baseline["expected"].items()}),
            context=context(), authorize=allow)
    assert caught.value.code == "identity.undo_conflict"
    db.rollback()
    assert db.get(PartAlias, alias.id).part_id == c

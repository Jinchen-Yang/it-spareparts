"""Manual workflows use local providers and a real isolated PostgreSQL database."""
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.db import engine
from app.pn_catalog import api, jobs
from app.pn_catalog.content_models import ContentCandidate
from app.pn_catalog.dto import PatchProfile
from app.pn_catalog.enrichment_dto import AnalysisResult, Evidence, FieldProposal, RecognizeInbox, ResearchPN, SearchResult, RecognitionResult, RecognitionHint, CreateBundle
from app.pn_catalog.errors import CatalogError
from app.pn_catalog.job_models import CatalogJob
from app.pn_catalog.media_models import InboxItem, GalleryEntry
from app.services import pn_catalog_enrichment as commands
from app.services.pn_enrichment_execution import ManualExecution, enrichment_handlers
from tests.test_pn_catalog_media import context, allow, seed, create_ticket, add


def research_command(**kw):
    return ResearchPN(expected_identity_revision=0, expected_profile_revision=0,
        expected_content_revisions={"good": 0, "bad": 0}, capabilities=["text"], **kw)


def enqueue(db, part_id, **kw):
    response = commands.research(db, part_id, research_command(), context=context(), authorize=allow)
    db.commit()
    lease = jobs.claim(db, worker_id="enrichment-test", queues=["enrichment"])
    db.commit()
    assert str(lease.job_id) == response["data"]["job_id"]
    return lease


def test_manual_command_receipt_audit_and_enqueue_are_one_transaction(db):
    part_id = seed(db)
    ctx = context()
    result = commands.research(db, part_id, research_command(), context=ctx, authorize=allow)
    replay = commands.research(db, part_id, research_command(), context=ctx, authorize=allow)
    assert replay["meta"]["replayed"] and result["data"]["job_id"] == replay["data"]["job_id"]
    assert "payload" not in result["data"]
    db.rollback()
    assert db.scalar(select(func.count()).select_from(CatalogJob)) == 0


def test_active_research_capability_blocks_same_actor_or_part(db):
    first, second = seed(db), seed(db, "PN-B")
    commands.research(db, first, research_command(), context=context(), authorize=allow)
    db.commit()
    for part_id, actor in [(second, "account:42"), (first, "account:99")]:
        with pytest.raises(CatalogError) as caught:
            commands.research(db, part_id, research_command(), context=context(actor), authorize=allow)
        assert caught.value.code == "enrichment.busy"
        db.rollback()


def test_research_rejects_stale_baseline_and_system_trigger(db):
    part_id = seed(db)
    with pytest.raises(CatalogError) as caught:
        commands.research(db, part_id, research_command().model_copy(update={"expected_profile_revision": 8}),
                          context=context(), authorize=allow)
    assert caught.value.code == "enrichment.version_conflict"
    db.rollback()
    ctx = context()
    ctx = ctx.model_copy(update={"actor": ctx.actor.model_copy(update={"actor_kind": "system"})})
    with pytest.raises(CatalogError) as caught:
        commands.research(db, part_id, research_command(), context=ctx, authorize=allow)
    assert caught.value.code == "enrichment.manual_required"


def test_budget_persists_unknown_usage_and_blocks_retry_bypass(db):
    lease = enqueue(db, seed(db))
    jobs.reserve_execution(db, lease, tokens=23000, searches=1, external_calls=1)
    db.commit()
    jobs.fail(db, lease, code="test.ambiguous", retryable=True, delay_seconds=0)
    db.commit()
    new = jobs.claim(db, worker_id="enrichment-retry", queues=["enrichment"])
    db.commit()
    with pytest.raises(CatalogError) as caught:
        jobs.reserve_execution(db, new, tokens=2000, searches=1, external_calls=1)
    assert caught.value.code == "enrichment.budget_exceeded"
    db.rollback()
    assert db.get(CatalogJob, lease.job_id).execution_usage["reserved_tokens"] == 23000


def test_budget_settlement_and_cancel_fence(db):
    lease = enqueue(db, seed(db))
    reservation = jobs.reserve_execution(db, lease, tokens=22000, searches=1, external_calls=1)
    jobs.settle_execution(db, lease, reservation, tokens=300)
    jobs.settle_execution(db, lease, reservation, tokens=300)
    db.commit()
    jobs.cancel(db, lease.job_id, context=context())
    db.commit()
    with pytest.raises(CatalogError) as caught:
        jobs.settle_execution(db, lease, reservation, tokens=300)
    assert caught.value.code == "job.lease_lost"


def test_each_outbound_step_checks_resource_again(db):
    part_id = seed(db)
    lease = enqueue(db, part_id)
    execution = ManualExecution(sessionmaker(engine), lease, allow)
    execution.check(external=True)
    api.patch_profile(db, part_id, PatchProfile(expected_identity_revision=0, expected_profile_revision=0,
                      changes={"brand": "Changed"}), context=context(), authorize=allow)
    db.commit()
    with pytest.raises(CatalogError) as caught:
        execution.check(external=True)
    assert caught.value.code == "job.resource_changed"


class Provider:
    def __init__(self, execution, mutate=None):
        self.execution, self.mutate = execution, mutate
    def with_context(self, snapshot):
        return self
    async def search(self, pn):
        self.execution.check(reserve_tokens=100, searches=1, external=True)
        assert engine.pool.checkedout() == 0
        self.execution.settle(100, 10)
        return SearchResult(provider="test", model="search", evidence=[Evidence(
            title=pn, url="https://example.org/product/PN-A", snippet=pn)])
    async def analyze(self, pn, evidence):
        self.execution.check(reserve_tokens=100, external=True)
        assert engine.pool.checkedout() == 0
        self.execution.settle(100, 10)
        if self.mutate:
            self.mutate()
        return AnalysisResult(provider="test", model="text", fields=[
            FieldProposal(field="manufacturer", value="Verified maker", confidence=0.9,
                          source_urls=["https://example.org/product/PN-A"]),
            FieldProposal(field="good_description", value="型号资料；状态以实物为准", confidence=0.9,
                          source_urls=["https://example.org/product/PN-A"])])
    async def recognize(self, url):
        self.execution.check("recognition", reserve_tokens=100, external=True)
        assert engine.pool.checkedout() == 0
        self.execution.settle(100, 10)
        return RecognitionResult(provider="test", model="vision", candidates=[RecognitionHint(pn="PN-A", confidence=0.9)])


class Cloud:
    def recognition(self, key, content_type):
        assert engine.pool.checkedout() == 0
        return SimpleNamespace(key="vision/a.jpg")
    def bundle(self, identifier, number, files, **kwargs):
        assert engine.pool.checkedout() == 0
        return SimpleNamespace(key=f"bundle/{identifier}/part-{number}.zip", size_bytes=100,
                               sha256="a" * 64, filename="images.zip")


class Storage:
    def signed_get(self, key, **kwargs):
        return "https://storage.invalid/read"


def handlers(mutate=None):
    return enrichment_handlers(sessionmaker(engine), authorize=allow, storage=Storage(), cloud=Cloud(),
        provider_factory=lambda execution: Provider(execution, mutate))


def test_research_publishes_candidates_without_changing_effective_fields(db):
    part_id = seed(db)
    commands.research(db, part_id, research_command(), context=context(), authorize=allow)
    db.commit()
    result = jobs.run_once(sessionmaker(engine), worker_id="test", queues=["enrichment"], handlers=handlers())
    assert result.status == "succeeded", result
    assert len(result.result["candidate_ids"]) == 2
    candidates = list(db.scalars(select(ContentCandidate)))
    assert all(candidate.status == "pending" for candidate in candidates)
    assert all(candidate.provenance["run_id"] == str(result.job_id) for candidate in candidates)
    assert api.get_part(db, part_id, context=context(), authorize=allow)["data"]["profile"]["manufacturer"] is None


def test_changed_profile_during_network_prevents_all_publication(db):
    part_id = seed(db)
    commands.research(db, part_id, research_command(), context=context(), authorize=allow)
    db.commit()
    def mutate():
        with sessionmaker(engine)() as other:
            api.patch_profile(other, part_id, PatchProfile(expected_identity_revision=0, expected_profile_revision=0,
                changes={"brand": "User choice"}), context=context(), authorize=allow)
            other.commit()
    result = jobs.run_once(sessionmaker(engine), worker_id="test", queues=["enrichment"], handlers=handlers(mutate))
    assert result.status == "stale"
    assert db.scalar(select(func.count()).select_from(ContentCandidate)) == 0


def test_recognition_returns_hints_and_never_assigns_inbox(db):
    ticket = create_ticket(db)
    item = db.scalar(select(InboxItem).where(InboxItem.upload_id == ticket))
    item_id, revision = item.id, item.revision
    commands.recognize(db, item_id, RecognizeInbox(expected_revision=revision), context=context(), authorize=allow)
    db.commit()
    result = jobs.run_once(sessionmaker(engine), worker_id="test", queues=["enrichment"], handlers=handlers())
    assert result.status == "succeeded", result
    assert result.result["candidates"] == [{"pn": "PN-A", "confidence": 0.9}]
    db.expire_all()
    assert db.get(InboxItem, item_id).part_id is None


def test_bundle_is_manual_and_download_rechecks_deleted_source(db):
    part_id = seed(db)
    ticket = create_ticket(db, part_id)
    added = add(db, part_id, ticket)
    db.commit()
    entry_id = UUID(added["data"]["entry_id"])
    from app.pn_catalog import media
    snapshot = media.bundle_snapshot(db, part_id, grade="good", context=context(), authorize=allow)
    commands.bundle(db, part_id, CreateBundle(expected_identity_revision=0,
        expected_gallery_revision=snapshot["gallery_revision"], grade="good"), context=context(), authorize=allow)
    db.commit()
    result = jobs.run_once(sessionmaker(engine), worker_id="test", queues=["enrichment"], handlers=handlers())
    assert result.status == "succeeded", result
    assert "key" not in result.result["archives"][0]
    value = commands.bundle_downloads(db, result.job_id, context=context(), authorize=allow, storage=Storage())
    assert value["data"]["expires_in"] <= 900
    db.commit()
    entry = db.get(GalleryEntry, entry_id)
    entry.status, entry.generation = "deleted", entry.generation + 1
    db.commit()
    with pytest.raises(CatalogError):
        commands.bundle_downloads(db, result.job_id, context=context(), authorize=allow, storage=Storage())


def test_concurrent_budget_reservations_cannot_double_spend(db):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    lease = enqueue(db, seed(db))
    barrier = Barrier(2)
    def spend(_):
        with sessionmaker(engine)() as other:
            barrier.wait(timeout=5)
            try:
                jobs.reserve_execution(other, lease, tokens=14000, external_calls=1)
                other.commit()
                return "reserved"
            except CatalogError as error:
                other.rollback()
                return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(spend, range(2)))
    assert sorted(result) == ["enrichment.budget_exceeded", "reserved"]
    assert db.get(CatalogJob, lease.job_id).execution_usage["reserved_tokens"] == 14000


def test_candidate_publication_rolls_back_all_candidates_on_late_failure(db, monkeypatch):
    part_id = seed(db)
    commands.research(db, part_id, research_command(), context=context(), authorize=allow)
    db.commit()
    original = api.create_candidate
    calls = []
    def failing(*args, **kwargs):
        calls.append(True)
        if len(calls) == 2:
            raise CatalogError("test.late_failure", "late failure")
        return original(*args, **kwargs)
    monkeypatch.setattr(api, "create_candidate", failing)
    result = jobs.run_once(sessionmaker(engine), worker_id="test", queues=["enrichment"], handlers=handlers())
    assert result.status == "failed" and len(calls) == 2
    assert db.scalar(select(func.count()).select_from(ContentCandidate)) == 0


def test_image_research_publishes_only_candidate_gallery_entries(db):
    from app.pn_catalog.enrichment_dto import ImageProposal
    part_id = seed(db)
    commands.research(db, part_id, ResearchPN(expected_identity_revision=0, expected_profile_revision=0,
        capabilities=["images"], expected_gallery_revision=0), context=context(), authorize=allow)
    db.commit()
    class ImageCloud(Cloud):
        def discover_remote_images(self, source):
            assert engine.pool.checkedout() == 0
            return [SimpleNamespace(url="https://example.org/images/a.jpg", source_url=source)]
        def import_remote_image(self, url, key):
            assert engine.pool.checkedout() == 0
            return SimpleNamespace(key=key, content_type="image/jpeg")
        def inspect(self, key, content_type):
            assert engine.pool.checkedout() == 0
            return SimpleNamespace(sha256="b" * 64, width=20, height=20, bytes=100,
                                   content_type="image/jpeg", source_etag="etag")
        def copy(self, *args, **kwargs):
            assert engine.pool.checkedout() == 0
    class ImageProvider(Provider):
        async def analyze(self, pn, evidence):
            await super().analyze(pn, evidence)
            return AnalysisResult(provider="test", model="images", images=[ImageProposal(
                url="https://example.org/images/a.jpg", source_url="https://example.org/product/PN-A", confidence=0.9)])
    handler_map = enrichment_handlers(sessionmaker(engine), authorize=allow, storage=Storage(), cloud=ImageCloud(),
                                      provider_factory=ImageProvider)
    result = jobs.run_once(sessionmaker(engine), worker_id="test", queues=["enrichment"], handlers=handler_map)
    assert result.status == "succeeded", result
    assert len(result.result["entry_ids"]) == 1 and result.result["candidate_ids"] == []
    entry = db.get(GalleryEntry, UUID(result.result["entry_ids"][0]))
    assert entry.status == "candidate" and entry.source["source_job_id"] == str(result.job_id)
